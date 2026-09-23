"""Agent lifecycle factory — the single source of truth for creating and
configuring Evonic agents.

This module is deliberately free of Flask dependencies: it only needs the
SQLite-backed ``Database`` object from :mod:`models.db`, so it can be called
from routes, plugins, skills, CLI helpers, or plain scripts without an
application context.

Public API
----------

``create_agent(spec, *, db=None, base_dir=None, if_exists=None) -> agent_id``
    Validate a spec, insert the agent row, apply the full set of agent
    settings, write the filesystem artifacts (``agents/<id>/SYSTEM.md``,
    ``agents/<id>/kb/``, ``shared/agents/<id>/``, ``shared/agents/<id>/artifacts/``)
    and copy the default knowledge-base files.  Creation is atomic: any
    failure is compensated in reverse order and the original exception is
    re-raised unchanged.

``apply_spec(agent_id, spec, *, db=None, base_dir=None) -> agent_id``
    Update an existing agent from a (partial) spec.  Only the fields present
    in the spec are touched.

Spec shape
----------

A spec is a mapping whose keys must all belong to :data:`SPEC_ALLOWED_KEYS`.
Agent settings may be given either flat at the top level or nested inside a
``configuration`` mapping (never both for the same key)::

    {
        "id": "research_bot",            # optional; derived from name otherwise
        "name": "Research Bot",
        "description": "Finds things",
        "avatar_path": "avatars/bot.png",
        "system_prompt": "You are ...",
        "configuration": {               # any CONFIG_KEYS subset, or flat keys
            "enabled": True,
            "vision_enabled": False,
            "summarize_threshold": 8,
        },
        "tools": ["web_search"],         # manual tools (managed ones added below)
        "skills": ["github"],
        "variables": [{"key": "TOKEN", "value": "s3cret", "is_secret": True}],
        "knowledge_base": [{"path": "guide/start.md", "content": "Hello"}],
    }

Unknown keys (and unknown configuration keys) raise
:class:`SpecValidationError` instead of being silently ignored.

Allowlisting
------------

The mutable field surface is ``CONFIG_KEYS`` (from
:mod:`backend.agent_portability`) plus the identity keys ``name``,
``description`` and ``avatar_path``, minus server-controlled columns
(``id``, ``is_super``, ``created_at``, ``updated_at``, ``last_active_at``,
``session_count``, ``owner``).  A spec is *never* passed to
``Database.update_agent()`` verbatim — only allowlisted keys are picked out,
so a server-controlled column such as ``is_super`` can never be smuggled in
through a template.

Defaults and write order
------------------------

``Database.create_agent()`` inserts a subset of the columns and hardcodes
``vision_enabled``, ``inject_agent_id``, ``inject_datetime``,
``send_intermediate_responses`` and ``enable_agent_state`` to ``1``.  The
factory therefore always performs ``create_agent()`` *followed by*
``update_agent()`` with the complete merged field set from :data:`DEFAULTS`,
which guarantees that a field the caller set to a non-default value is not
silently overridden and that fields omitted from the INSERT still land in the
row.

Managed tools
-------------

``ARTIFACT_TOOLS`` are (un)assigned with ``artifacts_enabled`` and
``VISION_TOOLS`` with ``vision_enabled``, mirroring the implicit augmentation
performed by ``routes/agents.py:api_create_agent`` so that programmatic
instantiation produces exactly the same agent as creating one in the UI.

Notes
-----

* ``system_prompt`` lives in ``agents/<id>/SYSTEM.md`` at runtime; the
  database column is populated on create, and on update the file is rewritten
  (matching ``routes/agents.py:api_update_agent``).
* Secret variable values must never reach the error or log path.  Error
  messages reference variable *keys* only, and failures are logged with the
  exception class name instead of the exception message.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Tuple

from backend.agent_portability import (
    CONFIG_KEYS,
    ID_RE,
    MAX_KB_FILE_LENGTH,
    SUBAGENT_ID_RE,
    VARIABLE_KEY_RE,
)
from models.boolean import normalize_bool

__all__ = [
    "AgentFactoryError",
    "SpecValidationError",
    "AgentAlreadyExistsError",
    "AgentNotFoundError",
    "ARTIFACT_TOOLS",
    "VISION_TOOLS",
    "SPEC_FIELD_ALLOWLIST",
    "SPEC_ALLOWED_KEYS",
    "DEFAULTS",
    "normalize_spec",
    "resolve_tools",
    "create_agent",
    "apply_spec",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_NAME_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 2000
MAX_SYSTEM_PROMPT_LENGTH = 102400
MAX_KB_PATH_LENGTH = 1024
_MAX_ID_LENGTH = 64
_MAX_DERIVED_ID_ATTEMPTS = 50

#: Default knowledge-base files copied into every new agent, as
#: ``(target_name, source_name_in_defaults_dir)`` pairs.  Mirrors
#: ``routes/agents.py:api_create_agent``.
DEFAULT_KB_FILES: Tuple[Tuple[str, str], ...] = (
    ("evonic.md", "super_agent_kb_evonic.md"),
    ("reminder-and-schedule-creation-rules.md", "reminder-and-schedule-creation-rules.md"),
    ("evonet.md", "evonet.md"),
)

# Tools managed exclusively by the artifacts_enabled agent setting.
# These definitions must stay in sync with routes/agents.py (drift-tested in
# unit_tests/test_agent_factory.py); they are duplicated here on purpose so
# that this module never has to import the Flask blueprint module.
ARTIFACT_TOOLS = frozenset({
    "save_artifact",
    "list_artifacts",
    "read_attachment",
    "cleanup_attachments",
    "portal_copy",
    "copy_status",
})

VISION_TOOLS = frozenset({"describe_image"})

#: Columns the factory manages on behalf of the server, never from a spec.
SERVER_CONTROLLED_KEYS = frozenset({
    "id", "is_super", "created_at", "updated_at", "last_active_at",
    "session_count", "owner",
})

IDENTITY_KEYS = frozenset({"name", "description", "avatar_path"})

#: Top-level keys that are not agent columns but spec structure.
SPEC_METADATA_KEYS = frozenset({
    "id", "system_prompt", "tools", "skills", "variables",
    "knowledge_base", "configuration",
})

#: Mutable agent columns the factory accepts from a spec.
SPEC_FIELD_ALLOWLIST = frozenset((set(CONFIG_KEYS) | IDENTITY_KEYS) - SERVER_CONTROLLED_KEYS)

#: Every key accepted at the top level of a spec.
SPEC_ALLOWED_KEYS = frozenset(SPEC_FIELD_ALLOWLIST | SPEC_METADATA_KEYS)

#: Canonical defaults for every allowlisted column.  These mirror what the UI
#: create path produces: an implicit value equal to the SQLite column default
#: for columns ``Database.create_agent()`` omits, and the hardcoded ``1`` for
#: the columns it pins.
DEFAULTS: Dict[str, Any] = {
    # Identity
    "name": "",
    "description": "",
    "avatar_path": None,
    # General
    "enabled": 1,
    "vision_enabled": 1,
    "summarize_threshold": 3,
    "summarize_tail": 5,
    "summarize_prompt": None,
    "message_buffer_seconds": 2,
    "inject_agent_id": 1,
    "inject_datetime": 1,
    "send_intermediate_responses": 1,
    "outbound_buffer_seconds": 1.5,
    "enable_agent_state": 1,
    # Capabilities
    "sandbox_enabled": 0,
    "attachments_enabled": 0,
    "attachment_max_size_mb": 20,
    "artifacts_enabled": 1,
    "safety_checker_enabled": 1,
    "disable_parallel_tool_execution": 0,
    "disable_turn_prefetch": 0,
    "agent_messaging_enabled": 1,
    "tool_compression_enabled": 1,
    "message_wrapper_enabled": 1,
    # Models
    "fallback_model_id": None,
    "model_id": None,
    "vision_model_id": None,
    # Media / runtime
    "audio_enabled": 0,
    "video_enabled": 0,
    "run_as_user": None,
    "bash_exec_enabled": 0,
    "inter_agent_clear_context": 0,
    "builtin_tools_enabled": 1,
    # Messaging ACL
    "messaging_acl": None,
    "messaging_acl_mode": "whitelist",
    # Memory / knowledge base
    "memory_engine": None,
    "kb_organizer_mode": None,
    # Feature gates
    "enable_atg": 0,
    "enable_cmp": 0,
    "always_execute": 0,
}

BOOLEAN_FIELDS = frozenset({
    "enabled", "vision_enabled", "inject_agent_id", "inject_datetime",
    "send_intermediate_responses", "enable_agent_state", "sandbox_enabled",
    "attachments_enabled", "artifacts_enabled", "safety_checker_enabled",
    "disable_parallel_tool_execution", "disable_turn_prefetch",
    "agent_messaging_enabled", "tool_compression_enabled",
    "message_wrapper_enabled", "audio_enabled", "video_enabled",
    "bash_exec_enabled", "inter_agent_clear_context", "builtin_tools_enabled",
    "enable_atg", "enable_cmp", "always_execute",
})

INTEGER_FIELDS = frozenset({"summarize_threshold", "summarize_tail", "attachment_max_size_mb"})

FLOAT_FIELDS = frozenset({"message_buffer_seconds", "outbound_buffer_seconds"})

# Accepted spellings for kb_organizer_mode, normalised to the canonical value
# understood by backend.agent_runtime.memory_manager.resolve_kb_organizer_mode.
KB_ORGANIZER_MODES: Dict[str, Optional[str]] = {
    "": None,
    "agentic": "agentic",
    "on": "agentic",
    "1": "agentic",
    "yes": "agentic",
    "true": "agentic",
    "non-agentic": "non-agentic",
    "nonagentic": "non-agentic",
    "legacy": "non-agentic",
    "sefton": "sefton",
    "off": "off",
    "no": "off",
    "0": "off",
    "false": "off",
    "none": "off",
}

MEMORY_ENGINES: Dict[str, Optional[str]] = {
    "": None,
    "evomem": "evomem",
    "fts5": "fts5",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class AgentFactoryError(Exception):
    """Base class for agent factory failures."""


class SpecValidationError(AgentFactoryError):
    """The supplied spec is malformed or contains unsupported keys."""


class AgentAlreadyExistsError(AgentFactoryError):
    """The requested agent id is already taken."""


class AgentNotFoundError(AgentFactoryError):
    """The agent referenced by ``apply_spec`` does not exist."""


# ---------------------------------------------------------------------------
# Lazy global accessors (keep Flask out of the import graph)
# ---------------------------------------------------------------------------

def _default_db():
    from models.db import db
    return db


def _default_base_dir() -> str:
    from config import BASE_DIR
    return BASE_DIR


# ---------------------------------------------------------------------------
# Spec normalisation
# ---------------------------------------------------------------------------

def _coerce_enum(key: str, value: Any, mapping: Dict[str, Optional[str]]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SpecValidationError(f"Agent spec field '{key}' must be a string or null.")
    normalized = value.strip().lower()
    if normalized not in mapping:
        allowed = sorted(name for name in mapping if name)
        raise SpecValidationError(
            f"Agent spec field '{key}' must be one of: {', '.join(allowed)}."
        )
    return mapping[normalized]


def _coerce_messaging_acl(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise SpecValidationError(
                    "Agent spec field 'messaging_acl' must contain non-empty agent ids."
                )
        return json.dumps(list(value))
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SpecValidationError(
                "Agent spec field 'messaging_acl' must be a JSON array of agent ids."
            ) from exc
        if not isinstance(decoded, list):
            raise SpecValidationError(
                "Agent spec field 'messaging_acl' must be a JSON array of agent ids."
            )
        return value
    raise SpecValidationError(
        "Agent spec field 'messaging_acl' must be a JSON array of agent ids."
    )


def _coerce_value(key: str, value: Any) -> Any:
    """Coerce one spec value to its canonical column representation."""
    if key in BOOLEAN_FIELDS:
        return 1 if normalize_bool(value, default=bool(DEFAULTS[key])) else 0
    if key in INTEGER_FIELDS:
        if isinstance(value, bool):
            raise SpecValidationError(f"Agent spec field '{key}' must be an integer.")
        if isinstance(value, int):
            return int(value)
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            return int(value.strip())
        raise SpecValidationError(f"Agent spec field '{key}' must be an integer.")
    if key in FLOAT_FIELDS:
        if isinstance(value, bool):
            raise SpecValidationError(f"Agent spec field '{key}' must be a number.")
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise SpecValidationError(f"Agent spec field '{key}' must be a number.") from exc
    if value is None:
        return None
    if key == "messaging_acl":
        return _coerce_messaging_acl(value)
    if key == "messaging_acl_mode":
        if value not in ("whitelist", "blacklist"):
            raise SpecValidationError(
                "Agent spec field 'messaging_acl_mode' must be 'whitelist' or 'blacklist'."
            )
        return value
    if key == "model_id" and isinstance(value, str) and not value.strip():
        # An empty string resets the model to the global default.
        return None
    if key == "memory_engine":
        return _coerce_enum(key, value, MEMORY_ENGINES)
    if key == "kb_organizer_mode":
        return _coerce_enum(key, value, KB_ORGANIZER_MODES)
    if not isinstance(value, str):
        raise SpecValidationError(f"Agent spec field '{key}' must be a string or null.")
    return value


def _safe_kb_relpath(path: Any) -> str:
    if not isinstance(path, str) or not path.strip():
        raise SpecValidationError("Knowledge-base paths must be non-empty strings.")
    if "\\" in path or os.path.isabs(path):
        raise SpecValidationError(
            "Knowledge-base paths must be relative and use forward slashes."
        )
    normalized = os.path.normpath(path).replace(os.sep, "/")
    if normalized in (".", "..") or normalized.startswith("../"):
        raise SpecValidationError(
            "Knowledge-base paths cannot escape the knowledge-base directory."
        )
    if len(normalized) > MAX_KB_PATH_LENGTH:
        raise SpecValidationError("Knowledge-base paths are too long.")
    return normalized


def _normalize_variables(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, (list, tuple)):
        raise SpecValidationError("Agent spec field 'variables' must be a list.")
    variables: List[Dict[str, Any]] = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise SpecValidationError(
                "Each agent variable must be a mapping with a 'key' and a 'value'."
            )
        unknown = sorted(set(item) - {"key", "value", "is_secret"})
        if unknown:
            raise SpecValidationError(
                "Agent variable contains unsupported key(s): " + ", ".join(unknown) + "."
            )
        key = item.get("key")
        if not isinstance(key, str) or not key or not VARIABLE_KEY_RE.fullmatch(key):
            raise SpecValidationError(
                "Agent variable keys must start with a letter or underscore and contain "
                "only letters, digits, and underscores."
            )
        if key in seen:
            raise SpecValidationError(f"Duplicate agent variable key: '{key}'.")
        seen.add(key)
        value = item.get("value", "")
        if value is None:
            value = ""
        elif isinstance(value, bool):
            value = "1" if value else "0"
        elif isinstance(value, (int, float)):
            value = str(value)
        if not isinstance(value, str):
            raise SpecValidationError(f"Agent variable '{key}' must have a string value.")
        variables.append({
            "key": key,
            "value": value,
            "is_secret": 1 if normalize_bool(item.get("is_secret"), default=False) else 0,
        })
    return variables


def _normalize_knowledge_base(raw: Any) -> List[Dict[str, str]]:
    if not isinstance(raw, (list, tuple)):
        raise SpecValidationError("Agent spec field 'knowledge_base' must be a list.")
    files: List[Dict[str, str]] = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise SpecValidationError(
                "Each knowledge-base entry must be a mapping with a 'path' and a 'content'."
            )
        unknown = sorted(set(item) - {"path", "content"})
        if unknown:
            raise SpecValidationError(
                "Knowledge-base entry contains unsupported key(s): " + ", ".join(unknown) + "."
            )
        path = _safe_kb_relpath(item.get("path"))
        if path in seen:
            raise SpecValidationError(f"Duplicate knowledge-base path: '{path}'.")
        seen.add(path)
        content = item.get("content", "")
        if not isinstance(content, str):
            raise SpecValidationError(f"Knowledge-base file '{path}' must have string content.")
        if len(content) > MAX_KB_FILE_LENGTH:
            raise SpecValidationError(f"Knowledge-base file '{path}' is too large.")
        files.append({"path": path, "content": content})
    return files


def _normalize_string_list(field: str, raw: Any) -> List[str]:
    if not isinstance(raw, (list, tuple)):
        raise SpecValidationError(f"Agent spec field '{field}' must be a list.")
    values: List[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise SpecValidationError(
                f"Agent spec field '{field}' must contain non-empty strings."
            )
        if item not in values:
            values.append(item)
    return values


def normalize_spec(spec: Any) -> Dict[str, Any]:
    """Validate a spec and return its canonical, write-ready representation.

    The returned mapping contains ``values`` (only the allowlisted columns the
    caller supplied), ``provided`` (the names of every key the caller supplied,
    including the metadata fields), ``id``/``explicit_id`` and
    the metadata fields (``system_prompt``, ``tools``, ``skills``,
    ``variables``, ``knowledge_base``), each ``None`` when absent from the spec.
    """
    if not isinstance(spec, dict):
        raise SpecValidationError("Agent spec must be a mapping.")

    unknown = sorted(set(spec) - SPEC_ALLOWED_KEYS)
    if unknown:
        raise SpecValidationError(
            "Agent spec contains unsupported key(s): " + ", ".join(unknown) + "."
        )

    values: Dict[str, Any] = {}
    nested = spec.get("configuration")
    if nested is not None:
        if not isinstance(nested, dict):
            raise SpecValidationError("Agent spec field 'configuration' must be a mapping.")
        unknown_config = sorted(set(nested) - set(CONFIG_KEYS))
        if unknown_config:
            raise SpecValidationError(
                "Agent spec configuration contains unsupported key(s): "
                + ", ".join(unknown_config) + "."
            )
        collision = sorted(set(nested) & (set(spec) - {"configuration"}))
        if collision:
            raise SpecValidationError(
                "Agent spec key(s) given both at the top level and in 'configuration': "
                + ", ".join(collision) + "."
            )
        for key, value in nested.items():
            values[key] = _coerce_value(key, value)

    for key in sorted(SPEC_FIELD_ALLOWLIST):
        if key in spec:
            values[key] = _coerce_value(key, spec[key])

    name = values.get("name")
    if name is not None and len(name) > MAX_NAME_LENGTH:
        raise SpecValidationError(
            f"Agent name is too long (max {MAX_NAME_LENGTH} characters)."
        )
    description = values.get("description")
    if description is not None and len(description) > MAX_DESCRIPTION_LENGTH:
        raise SpecValidationError(
            f"Agent description is too long (max {MAX_DESCRIPTION_LENGTH} characters)."
        )

    spec_id = spec.get("id")
    if spec_id is not None:
        if not isinstance(spec_id, str) or not spec_id.strip():
            raise SpecValidationError("Agent spec 'id' must be a non-empty string.")
        spec_id = spec_id.strip()
        if not ID_RE.fullmatch(spec_id):
            raise SpecValidationError(
                "Agent id must use only lowercase alphanumeric characters and underscores."
            )
        if SUBAGENT_ID_RE.search(spec_id):
            raise SpecValidationError(
                "Agent id cannot use the reserved sub-agent suffix pattern (e.g. '_sub_1')."
            )

    system_prompt = spec.get("system_prompt")
    if system_prompt is not None:
        if not isinstance(system_prompt, str):
            raise SpecValidationError("Agent spec field 'system_prompt' must be a string.")
        if len(system_prompt) > MAX_SYSTEM_PROMPT_LENGTH:
            raise SpecValidationError(
                f"Agent system prompt is too long (max {MAX_SYSTEM_PROMPT_LENGTH} characters)."
            )

    provided = set(values)
    for metadata_key in ("id", "system_prompt", "tools", "skills", "variables", "knowledge_base"):
        if metadata_key in spec:
            provided.add(metadata_key)

    return {
        "id": spec_id,
        "explicit_id": spec_id is not None,
        "values": values,
        "provided": frozenset(provided),
        "system_prompt": system_prompt,
        "tools": _normalize_string_list("tools", spec["tools"]) if "tools" in spec else None,
        "skills": _normalize_string_list("skills", spec["skills"]) if "skills" in spec else None,
        "variables": _normalize_variables(spec["variables"]) if "variables" in spec else None,
        "knowledge_base": (
            _normalize_knowledge_base(spec["knowledge_base"])
            if "knowledge_base" in spec else None
        ),
    }


def merged_fields(normalized: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
    """Merge :data:`DEFAULTS` with the spec values supplied by the caller."""
    fields = dict(DEFAULTS)
    fields.update(normalized["values"])
    if not fields.get("name"):
        fields["name"] = agent_id
    return fields


def resolve_tools(
    tools: Iterable[str],
    *,
    artifacts_enabled: bool,
    vision_enabled: bool,
) -> List[str]:
    """Apply the ``artifacts_enabled`` / ``vision_enabled`` tool lock.

    Managed tools are added when their flag is on and removed when it is off,
    which is exactly what ``routes/agents.py`` does on create and on toggle.
    """
    resolved = {tool for tool in tools or () if tool}
    if artifacts_enabled:
        resolved |= ARTIFACT_TOOLS
    else:
        resolved -= ARTIFACT_TOOLS
    if vision_enabled:
        resolved |= VISION_TOOLS
    else:
        resolved -= VISION_TOOLS
    return sorted(resolved)


def _derive_agent_id(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_") or "agent"
    base = base[:_MAX_ID_LENGTH].rstrip("_") or "agent"
    if SUBAGENT_ID_RE.search(base):
        base = (base + "_agent")[:_MAX_ID_LENGTH]
    return base


def _id_candidates(base: str) -> List[str]:
    candidates = [base]
    for suffix in range(2, _MAX_DERIVED_ID_ATTEMPTS + 1):
        tail = f"_{suffix}"
        candidates.append(base[:_MAX_ID_LENGTH - len(tail)].rstrip("_") + tail)
    return candidates


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def agent_dir_for(base_dir: str, agent_id: str) -> str:
    return os.path.join(base_dir, "agents", agent_id)


def workspace_dir_for(base_dir: str, agent_id: str) -> str:
    return os.path.join(base_dir, "shared", "agents", agent_id)


def _write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def _write_agent_files(
    base_dir: str,
    agent_id: str,
    system_prompt: str,
    knowledge_base: Iterable[Dict[str, str]],
) -> None:
    """Create ``agents/<id>/``, ``shared/agents/<id>/`` and the default KB."""
    agent_dir = agent_dir_for(base_dir, agent_id)
    kb_dir = os.path.join(agent_dir, "kb")
    workspace_dir = workspace_dir_for(base_dir, agent_id)

    os.makedirs(kb_dir, exist_ok=True)
    _write_text(os.path.join(agent_dir, "SYSTEM.md"), system_prompt)
    os.makedirs(os.path.join(workspace_dir, "artifacts"), exist_ok=True)

    defaults_dir = os.path.join(base_dir, "defaults")
    for target_name, source_name in DEFAULT_KB_FILES:
        source_path = os.path.join(defaults_dir, source_name)
        if os.path.isfile(source_path):
            shutil.copy2(source_path, os.path.join(kb_dir, target_name))

    for item in knowledge_base:
        path = os.path.join(kb_dir, item["path"])
        _write_text(path, item["content"])


def _write_kb_files(base_dir: str, agent_id: str, knowledge_base: Iterable[Dict[str, str]]) -> None:
    kb_dir = os.path.join(agent_dir_for(base_dir, agent_id), "kb")
    for item in knowledge_base:
        _write_text(os.path.join(kb_dir, item["path"]), item["content"])


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

def _log_failure(operation: str, agent_id: str, step: str, exc: BaseException) -> None:
    """Log a failure without ever touching the exception message.

    Spec validation messages are ours and contain keys/paths only; anything
    else (SQLite errors in particular) could embed a variable value, so only
    the class name is logged.
    """
    detail = str(exc) if isinstance(exc, SpecValidationError) else type(exc).__name__
    logger.error(
        "agent_factory: %s failed for agent '%s' at step '%s' (%s)",
        operation, agent_id, step, detail,
    )


def _compensate(db, agent_id: str, agent_dir: str, workspace_dir: str) -> None:
    """Reverse every artifact a failed create may have produced.

    The steps are enumerated explicitly (tool/skill/variable mappings, the
    agent row, then the two directories including the artifacts dir) and run
    best-effort: each is idempotent and a rollback failure is logged instead of
    masking the original exception.
    """
    steps = (
        ("workspace_dir", lambda: shutil.rmtree(workspace_dir, ignore_errors=True)),
        ("agent_dir", lambda: shutil.rmtree(agent_dir, ignore_errors=True)),
        ("variables", lambda: db.set_agent_variables_bulk(agent_id, [])),
        ("skills", lambda: db.set_agent_skills(agent_id, [])),
        ("tools", lambda: db.set_agent_tools(agent_id, [])),
        ("agent_row", lambda: db.delete_agent(agent_id)),
    )
    for name, action in steps:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - rollback must never mask the cause
            logger.warning(
                "agent_factory: rollback step '%s' failed for agent '%s' (%s)",
                name, agent_id, type(exc).__name__,
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_agent(
    spec: Dict[str, Any],
    *,
    db=None,
    base_dir: Optional[str] = None,
    if_exists: Optional[str] = None,
) -> str:
    """Create an agent from ``spec`` and return its id.

    ``if_exists`` controls what happens when the id is already taken:

    * ``None`` (default) — an explicitly supplied ``id`` is treated as a replay
      and the existing agent's id is returned unchanged; a derived id is
      retried with a numeric suffix.
    * ``"error"`` — raise :class:`AgentAlreadyExistsError`.
    * ``"return"`` — return the existing agent's id (requires an explicit id).

    The existence check is never performed up front: the insert is attempted
    and the database PRIMARY KEY constraint decides, so two concurrent callers
    can never both win.
    """
    if if_exists not in (None, "error", "return"):
        raise SpecValidationError("if_exists must be None, 'error', or 'return'.")
    database = db if db is not None else _default_db()
    root = base_dir if base_dir is not None else _default_base_dir()

    normalized = normalize_spec(spec)
    explicit_id = normalized["explicit_id"]
    if explicit_id:
        candidates = [normalized["id"]]
    else:
        if if_exists == "return":
            raise SpecValidationError("if_exists='return' requires an explicit 'id' in the spec.")
        candidates = _id_candidates(_derive_agent_id(normalized["values"].get("name") or ""))

    agent_id: Optional[str] = None
    for candidate in candidates:
        try:
            _insert_agent_row(database, candidate, normalized, root)
        except sqlite3.IntegrityError as exc:
            # Classify the constraint violation: PRIMARY KEY collision (the row
            # now exists) vs. anything else, without a pre-flight SELECT.
            if not database.get_agent(candidate):
                raise
            if if_exists == "return" or (if_exists is None and explicit_id):
                logger.info(
                    "agent_factory: agent '%s' already exists; returning it.", candidate
                )
                return candidate
            if explicit_id or if_exists == "error":
                raise AgentAlreadyExistsError(
                    f"Agent id '{candidate}' already exists."
                ) from exc
            # Derived id: keep the deterministic name prefix and try a suffix.
            logger.info(
                "agent_factory: derived id '%s' is taken; trying another candidate.", candidate
            )
        else:
            agent_id = candidate
            break

    if agent_id is None:
        raise AgentAlreadyExistsError(
            "Could not allocate a unique agent id derived from the supplied name."
        )

    fields = merged_fields(normalized, agent_id)
    tools = resolve_tools(
        normalized["tools"] or [],
        artifacts_enabled=bool(fields["artifacts_enabled"]),
        vision_enabled=bool(fields["vision_enabled"]),
    )
    skills = list(normalized["skills"] or [])
    variables = list(normalized["variables"] or [])
    knowledge_base = list(normalized["knowledge_base"] or [])
    system_prompt = normalized["system_prompt"] or ""

    step = "settings"
    try:
        database.update_agent(agent_id, fields)
        step = "tools"
        database.set_agent_tools(agent_id, tools)
        step = "skills"
        database.set_agent_skills(agent_id, skills)
        step = "variables"
        database.set_agent_variables_bulk(agent_id, variables)
        step = "filesystem"
        _write_agent_files(root, agent_id, system_prompt, knowledge_base)
        return agent_id
    except Exception as exc:  # noqa: BLE001 - re-raised unchanged below
        _log_failure("create_agent", agent_id, step, exc)
        _compensate(
            database, agent_id,
            agent_dir_for(root, agent_id),
            workspace_dir_for(root, agent_id),
        )
        raise


def _insert_agent_row(db, agent_id: str, normalized: Dict[str, Any], base_dir: str) -> None:
    """Insert the row via ``Database.create_agent`` (the partial INSERT)."""
    fields = merged_fields(normalized, agent_id)
    row = dict(fields)
    row["id"] = agent_id
    # Server-controlled columns are never part of a spec.
    row["is_super"] = False
    row["system_prompt"] = normalized["system_prompt"] or ""
    row["workspace"] = workspace_dir_for(base_dir, agent_id)
    db.create_agent(row)


def apply_spec(
    agent_id: str,
    spec: Dict[str, Any],
    *,
    db=None,
    base_dir: Optional[str] = None,
) -> str:
    """Apply ``spec`` to an existing agent and return its id.

    Only the fields present in the spec are written; the managed tool sets are
    re-synchronised whenever tools or the ``artifacts_enabled`` /
    ``vision_enabled`` flags are part of the spec.  Validation happens before
    the first write.
    """
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise SpecValidationError("Agent id must be a non-empty string.")
    agent_id = agent_id.strip()
    database = db if db is not None else _default_db()
    root = base_dir if base_dir is not None else _default_base_dir()

    normalized = normalize_spec(spec)
    if normalized["explicit_id"] and normalized["id"] != agent_id:
        raise SpecValidationError("The 'id' in the spec does not match the agent being updated.")

    existing = database.get_agent(agent_id)
    if not existing:
        raise AgentNotFoundError(f"Agent '{agent_id}' was not found.")

    provided = normalized["provided"]
    updates = dict(normalized["values"])
    if updates:
        database.update_agent(agent_id, updates)

    if provided & {"tools", "artifacts_enabled", "vision_enabled"}:
        row = database.get_agent(agent_id) or existing
        base_tools = (
            normalized["tools"]
            if normalized["tools"] is not None
            else database.get_agent_tools(agent_id)
        )
        database.set_agent_tools(agent_id, resolve_tools(
            base_tools,
            artifacts_enabled=bool(row.get("artifacts_enabled", True)),
            vision_enabled=bool(row.get("vision_enabled", True)),
        ))

    if normalized["skills"] is not None:
        database.set_agent_skills(agent_id, list(normalized["skills"]))
    if normalized["variables"] is not None:
        database.set_agent_variables_bulk(agent_id, list(normalized["variables"]))

    if normalized["system_prompt"] is not None:
        _write_text(
            os.path.join(agent_dir_for(root, agent_id), "SYSTEM.md"),
            normalized["system_prompt"],
        )
    if normalized["knowledge_base"]:
        _write_kb_files(root, agent_id, normalized["knowledge_base"])

    return agent_id
