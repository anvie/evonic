r"""Reusable, parameterized agent templates — the template layer.

A *template* is a JSON blueprint that captures the FULL agent spec: basic and
advanced settings, tools, skills, variables, knowledge-base files and a
parameterized system prompt.  Users pick a template in the UI (or a plugin
calls this module directly) to instantiate a real agent.

This module is deliberately free of Flask dependencies.  The dependency
direction is strictly one-way::

    backend.agent_templates  ->  backend.agent_factory  ->  db / filesystem

The template layer never re-implements spec normalisation: every default,
override and instantiation payload is validated and coerced by
:func:`backend.agent_factory.normalize_spec` / ``create_agent``, which is the
single source of truth for the mutable agent field surface.

Storage
-------

Templates are read from two roots, relative to ``base_dir`` (``config.BASE_DIR``
by default):

``agent_templates/<id>.json``
    The canonical, writable location.  ``create_template``,
    ``update_template`` and ``delete_template`` only ever touch this root.
``skillsets/<id>.json``
    The legacy, **READ-ONLY** location (see :mod:`backend.skillsets`).  Legacy
    files are adapted on read (``model`` becomes ``defaults.model_id``,
    ``kb_files`` is kept as-is) and are never modified, moved or deleted.
    Updating or deleting a legacy-only template raises a clear error telling
    the caller to create a canonical copy first.

``agent_templates/`` wins when the same id exists in both roots.  Shadowing is
never silent: :func:`list_collisions` reports it, entries in
:func:`list_templates` carry ``shadowed``/``shadows`` flags, and
``create_template`` refuses to shadow a legacy skillset unless the caller
explicitly passes ``allow_shadow=True``.

Path safety
-----------

Every template id is validated against :data:`SLUG_RE`, then the target path is
canonicalized with ``os.path.realpath`` and asserted to stay directly inside
the template root (this also rejects ``<id>.json`` symlinks that escape the
root).  Knowledge-base keys are validated by reusing
:func:`backend.agent_portability._safe_kb_path` — never re-implemented here.

Rendering rules (system prompt)
-------------------------------

* Placeholders are ``{{ name }}`` where *name* matches :data:`PARAM_NAME_RE`
  (ASCII only).  Whitespace around the name is allowed.
* The renderer is a **single pass**: the substituted output is never scanned
  again, so a parameter value that itself contains ``{{other}}`` stays
  literal (no recursive/self-injection).
* Unknown placeholders are a HARD ERROR listing the offending names — both at
  save time (:func:`validate_template`) and at render time
  (:func:`render_system_prompt`).
* A malformed ``{{`` (anything that does not match the placeholder pattern) is
  also a hard error; a literal ``{{`` must be escaped as ``\{{``.
* Resolution order for a parameter is *caller value > declared default >* ``''``.
  ``required: true`` means the **rendered value must be non-empty**.
* Parameter values are length-capped (:data:`MAX_PARAM_VALUE_LENGTH`) and the
  rendered prompt is length-capped (:data:`agent_factory.MAX_SYSTEM_PROMPT_LENGTH`,
  enforced AFTER rendering).

Secrets
-------

Templates store variable **declarations only** (``key``, ``is_secret``,
``required``, ``description``); a secret declaration may not carry a ``default``
or ``value``.  Actual secret values are supplied at instantiate time through
``overrides['variables']`` and flow straight into the agent's variable store
via ``agent_factory``.  Values are never written back to the template file and
never logged — error messages reference keys/names only.

Public API
----------

``list_templates(*, base_dir=None, include_legacy=True) -> list[dict]``
``has_template(template_id, *, base_dir=None) -> bool``
``get_template(template_id, *, base_dir=None) -> dict``
``list_collisions(*, base_dir=None) -> list[dict]``
``create_template(data, *, base_dir=None, allow_shadow=False) -> dict``
``update_template(template_id, data, *, base_dir=None) -> dict``
``delete_template(template_id, *, base_dir=None) -> bool``
``validate_template(payload) -> dict``
``resolve_parameters(template, params=None, *, strict=True) -> dict``
``render_system_prompt(template, params=None, *, strict=True) -> str``
``preview_template(template_id, params=None, *, base_dir=None, strict=False) -> dict``
``resolve_template(template_id, *, base_dir=None) -> dict``
``create_agent_from_template(template_id, params=None, overrides=None, *, ...) -> str``

Legacy skillset compatibility views (consumed by ``routes/skills.py`` so that the
``/api/skillsets*`` surface keeps its pre-template response shape while reading
through this module):

``legacy_skillsets(*, base_dir=None) -> list[dict]``
``get_legacy_skillset(template_id, *, base_dir=None) -> dict | None``
``resolve_legacy_skillset(template_id, *, base_dir=None) -> dict | None``
``build_legacy_skillset_spec(template_id, agent_data, *, base_dir=None) -> dict``

``get_template`` returns the canonical template with a read-only ``_meta``
block (``source``, ``legacy``, ``writable``, ``file``).  ``_meta`` is stripped
on write, so a fetch → edit → ``update_template`` round-trip works unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Tuple

from backend import agent_factory
from backend.agent_factory import (
    MAX_KB_PATH_LENGTH,
    MAX_SYSTEM_PROMPT_LENGTH,
    SPEC_FIELD_ALLOWLIST,
    AgentFactoryError,
    SpecValidationError,
)
from backend.agent_portability import (
    CONFIG_KEYS,
    MAX_KB_FILE_LENGTH,
    VARIABLE_KEY_RE,
    AgentPortabilityError,
    _safe_kb_path,
)
from models.boolean import normalize_bool

__all__ = [
    # Constants
    "TEMPLATE_SCHEMA_VERSION",
    "SLUG_RE",
    "PARAM_NAME_RE",
    "PARAM_TYPES",
    "MAX_PARAM_VALUE_LENGTH",
    "MAX_PARAM_COUNT",
    "MAX_TEMPLATE_ITEMS",
    "MAX_TEMPLATE_BYTES",
    "OVERRIDE_KEYS",
    # Errors
    "TemplateError",
    "TemplateValidationError",
    "TemplateRenderError",
    "TemplateNotFoundError",
    "TemplateExistsError",
    "TemplateResolveError",
    # Storage helpers
    "templates_dir",
    "legacy_templates_dir",
    # Read API
    "list_templates",
    "has_template",
    "get_template",
    "list_collisions",
    # Write API
    "create_template",
    "update_template",
    "delete_template",
    # Validation / rendering
    "validate_template",
    "resolve_parameters",
    "render_system_prompt",
    "preview_template",
    # Resolution / instantiation
    "resolve_template",
    "create_agent_from_template",
    # Legacy skillset compatibility views
    "legacy_skillsets",
    "get_legacy_skillset",
    "resolve_legacy_skillset",
    "build_legacy_skillset_spec",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Schema version of the canonical template format.
TEMPLATE_SCHEMA_VERSION = 1

#: Template ids are lowercase slugs; they are also the file stem, so they must
#: never contain a path separator, a leading dot or a traversal segment.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Parameter names MUST match the placeholder charset (ASCII only).
PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: One-pass placeholder matcher.  ``\w`` is deliberately avoided: it would
#: accept non-ASCII word characters that :data:`PARAM_NAME_RE` rejects.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

#: Escape sequence for a literal ``{{``.
ESCAPE_SEQUENCE = "\\{{"

PARAM_TYPES = frozenset({"text", "number", "boolean", "select"})

MAX_PARAM_VALUE_LENGTH = 4000
MAX_PARAM_COUNT = 64
MAX_PARAMETER_OPTIONS = 200
MAX_PARAM_NAME_LENGTH = 64
MAX_LABEL_LENGTH = 200
MAX_PLACEHOLDER_LENGTH = 200
MAX_TEMPLATE_DESCRIPTION_LENGTH = 2000
MAX_CATEGORY_LENGTH = 64
MAX_ICON_LENGTH = 64
MAX_TOOL_ID_LENGTH = 200

#: Upper bound for the number of entries in tools/skills/variables/kb_files.
MAX_TEMPLATE_ITEMS = 256

#: Upper bound for the serialized template file.
MAX_TEMPLATE_BYTES = 4 * 1024 * 1024

#: Keys accepted at the top level of a canonical template.
_TEMPLATE_KEYS = frozenset({
    "id",
    "name",
    "description",
    "category",
    "icon",
    "schema_version",
    "parameters",
    "system_prompt",
    "defaults",
    "tools",
    "skills",
    "variables",
    "kb_files",
})

#: Keys accepted inside a parameter definition.
_PARAM_KEYS = frozenset({
    "name",
    "label",
    "type",
    "required",
    "default",
    "options",
    "description",
    "placeholder",
    "min",
    "max",
    "integer",
})

#: Keys accepted inside a variable declaration.
_VARIABLE_DECLARATION_KEYS = frozenset({
    "key",
    "is_secret",
    "required",
    "description",
    "default",
})

#: Metadata keys added by the loader and stripped on write.
_META_KEY = "_meta"

#: Structural keys a caller may override at instantiate time, on top of the
#: allowlisted agent columns (:data:`agent_factory.SPEC_FIELD_ALLOWLIST`).
_STRUCTURAL_OVERRIDE_KEYS = frozenset({
    "id",
    "tools",
    "skills",
    "variables",
    "knowledge_base",
})

#: Every key accepted in ``create_agent_from_template(overrides=...)``.
OVERRIDE_KEYS = frozenset(SPEC_FIELD_ALLOWLIST | _STRUCTURAL_OVERRIDE_KEYS)

#: Keys a legacy ``skillsets/*.json`` may contain (``model`` is mapped to
#: ``defaults.model_id``).
_LEGACY_KEYS = frozenset({
    "id", "name", "description", "system_prompt", "model",
    "tools", "skills", "kb_files",
})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class TemplateError(ValueError):
    """Base class for every template-layer failure."""


class TemplateValidationError(TemplateError):
    """A template, parameter or override payload is invalid."""


class TemplateRenderError(TemplateValidationError):
    """The system prompt cannot be rendered (unknown/malformed placeholders)."""


class TemplateNotFoundError(TemplateError):
    """No template with the requested id exists in any storage root."""


class TemplateExistsError(TemplateError):
    """A template with that id already exists (or would shadow a legacy one)."""


class TemplateResolveError(TemplateError):
    """A template declares a tool/skill that cannot be resolved right now.

    The full report produced by :func:`resolve_template` is attached as
    :attr:`report` so an editor can show precisely what is missing.
    """

    def __init__(self, message: str, report: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.report: Dict[str, Any] = report or {}


# ---------------------------------------------------------------------------
# Lazy global accessors (keep Flask out of the import graph)
# ---------------------------------------------------------------------------

def _default_base_dir() -> str:
    from config import BASE_DIR
    return BASE_DIR


def templates_dir(base_dir: Optional[str] = None) -> str:
    """Return the canonical (writable) template root."""
    root = base_dir if base_dir is not None else _default_base_dir()
    return os.path.join(root, "agent_templates")


def legacy_templates_dir(base_dir: Optional[str] = None) -> str:
    """Return the legacy, read-only skillset root."""
    root = base_dir if base_dir is not None else _default_base_dir()
    return os.path.join(root, "skillsets")


def _roots(base_dir: Optional[str] = None) -> Tuple[str, str]:
    return templates_dir(base_dir), legacy_templates_dir(base_dir)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TemplateValidationError(message)


def _escape_hint() -> str:
    """Human-readable reminder of the literal-brace escape sequence."""
    return "escaped as '\\{{'"


def _snippet(source: str, offset: int, length: int = 40) -> str:
    """Return a short, single-line excerpt of *source* for error messages."""
    raw = source[offset:offset + length]
    return raw.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def _format_value(value: Any) -> str:
    """Render one resolved parameter value for prompt substitution."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _as_list(value: Any, field: str) -> List[Any]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TemplateValidationError(
            "Template field '%s' must be a list." % field
        )
    if len(value) > MAX_TEMPLATE_ITEMS:
        raise TemplateValidationError(
            "Template field '%s' has too many entries (max %d)."
            % (field, MAX_TEMPLATE_ITEMS)
        )
    return list(value)


def _string_list(value: Any, field: str, *, max_length: int = MAX_TOOL_ID_LENGTH) -> List[str]:
    """Validate a list of unique, non-empty id strings (order preserved)."""
    items = _as_list(value, field)
    out: List[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise TemplateValidationError(
                "Template field '%s' must contain non-empty strings." % field
            )
        cleaned = item.strip()
        if len(cleaned) > max_length:
            raise TemplateValidationError(
                "Template field '%s' contains an entry that is too long (max %d characters)."
                % (field, max_length)
            )
        if cleaned not in out:
            out.append(cleaned)
    return out


def _optional_string(
    value: Any,
    field: str,
    *,
    default: str = "",
    max_length: int,
) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise TemplateValidationError("Template field '%s' must be a string." % field)
    if len(value) > max_length:
        raise TemplateValidationError(
            "Template field '%s' is too long (max %d characters)." % (field, max_length)
        )
    return value


def _validate_template_id(template_id: Any) -> str:
    """Validate a template id used to build a filesystem path."""
    if not isinstance(template_id, str) or not template_id.strip():
        raise TemplateValidationError("Template id must be a non-empty string.")
    cleaned = template_id.strip()
    if not SLUG_RE.fullmatch(cleaned):
        raise TemplateValidationError(
            "Template id '%s' is invalid: use lowercase letters, digits, '_' or '-' "
            "(1-64 characters, starting with a letter or digit)." % cleaned[:80]
        )
    # Defence in depth: the regex already rejects these, but the id decides a
    # filesystem path, so never trust a single layer.
    if cleaned in (".", "..") or "/" in cleaned or "\\" in cleaned or os.sep in cleaned:
        raise TemplateValidationError("Template id must not contain a path separator.")
    return cleaned


def _template_path(root: str, template_id: str) -> str:
    """Return the canonical file path for *template_id*, or raise.

    The resolved parent directory must be exactly the template root, which
    rejects ``..`` traversal and any ``<id>.json`` symlink that escapes it.
    """
    cleaned = _validate_template_id(template_id)
    root_real = os.path.realpath(root)
    path = os.path.join(root_real, cleaned + ".json")
    if os.path.islink(path):
        raise TemplateValidationError(
            "Template file '%s.json' must not be a symbolic link." % cleaned
        )
    resolved = os.path.realpath(path)
    if os.path.dirname(resolved) != root_real:
        raise TemplateValidationError(
            "Template id '%s' resolves outside the template directory." % cleaned
        )
    return path


def _atomic_write_template(path: str, payload: Mapping[str, Any]) -> None:
    """Write JSON atomically: temp file in the same dir, fsync, then replace."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    try:
        serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise TemplateValidationError(
            "Template payload is not JSON serializable: %s." % exc.__class__.__name__
        ) from exc
    if len(serialized.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        raise TemplateValidationError(
            "Template file is too large (max %d bytes)." % MAX_TEMPLATE_BYTES
        )

    handle = None
    temp_path = None
    try:
        descriptor, temp_path = tempfile.mkstemp(
            prefix=".tmp-%s." % os.path.basename(path),
            suffix=".tmp",
            dir=directory,
        )
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover - defensive
                pass
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:  # pragma: no cover - defensive
                pass


def _read_json_object(path: str) -> Dict[str, Any]:
    """Read a template file as a JSON object (size- and symlink-guarded)."""
    if os.path.islink(path):
        raise TemplateValidationError("Template files must not be symbolic links.")
    try:
        if os.path.getsize(path) > MAX_TEMPLATE_BYTES:
            raise TemplateValidationError(
                "Template file is too large (max %d bytes)." % MAX_TEMPLATE_BYTES
            )
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise TemplateNotFoundError(
            "Template file '%s' does not exist." % os.path.basename(path)
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TemplateValidationError(
            "Template file '%s' is not valid UTF-8 JSON." % os.path.basename(path)
        ) from exc
    except OSError as exc:
        raise TemplateValidationError(
            "Template file '%s' could not be read (%s)."
            % (os.path.basename(path), exc.__class__.__name__)
        ) from exc
    if not isinstance(payload, Mapping):
        raise TemplateValidationError(
            "Template file '%s' must contain a JSON object." % os.path.basename(path)
        )
    return dict(payload)


def _strip_meta(payload: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in payload.items() if key != _META_KEY}


# ---------------------------------------------------------------------------
# Parameter validation / coercion
# ---------------------------------------------------------------------------

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _coerce_text(param_name: str, value: Any, *, cap: int = MAX_PARAM_VALUE_LENGTH) -> str:
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        text = _format_value(value)
    elif isinstance(value, str):
        text = value
    elif value is None:
        text = ""
    else:
        raise TemplateValidationError(
            "Parameter '%s' must be a text value." % param_name
        )
    if len(text) > cap:
        raise TemplateValidationError(
            "Parameter '%s' exceeds the maximum length of %d characters."
            % (param_name, cap)
        )
    return text


def _coerce_boolean(param_name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off"):
            return False
        raise TemplateValidationError(
            "Parameter '%s' must be a boolean (true/false)." % param_name
        )
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise TemplateValidationError(
        "Parameter '%s' must be a boolean (true/false)." % param_name
    )


def _coerce_number(param: Mapping[str, Any], value: Any) -> Any:
    name = param.get("name")
    if isinstance(value, bool) or value is None:
        raise TemplateValidationError(
            "Parameter '%s' must be a number." % name
        )
    if isinstance(value, (int, float)):
        number: Any = value
    elif isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"[+-]?\d+", stripped):
            number = int(stripped)
        else:
            try:
                number = float(stripped)
            except ValueError as exc:
                raise TemplateValidationError(
                    "Parameter '%s' must be a number." % name
                ) from exc
    else:
        raise TemplateValidationError("Parameter '%s' must be a number." % name)
    if isinstance(number, float) and (number != number or number in (float("inf"), float("-inf"))):
        raise TemplateValidationError("Parameter '%s' must be a finite number." % name)
    if param.get("integer"):
        if isinstance(number, float):
            if not number.is_integer():
                raise TemplateValidationError(
                    "Parameter '%s' must be a whole number." % name
                )
            number = int(number)
        elif not isinstance(number, int):
            raise TemplateValidationError(
                "Parameter '%s' must be a whole number." % name
            )
    minimum = param.get("min")
    maximum = param.get("max")
    if minimum is not None and number < minimum:
        raise TemplateValidationError(
            "Parameter '%s' must be greater than or equal to %s." % (name, minimum)
        )
    if maximum is not None and number > maximum:
        raise TemplateValidationError(
            "Parameter '%s' must be less than or equal to %s." % (name, maximum)
        )
    return number


def _coerce_param_value(param: Mapping[str, Any], value: Any) -> Any:
    """Type-check and coerce one parameter value.

    Error messages reference the parameter *name* and the constraint only —
    never the offending value (it may be sensitive).
    """
    name = param.get("name")
    param_type = param.get("type") or "text"
    if param_type == "text":
        return _coerce_text(name, value)
    if param_type == "select":
        options = param.get("options") or []
        if not isinstance(value, str):
            raise TemplateValidationError(
                "Parameter '%s' must be one of the declared options." % name
            )
        stripped = value.strip()
        if stripped not in options:
            raise TemplateValidationError(
                "Parameter '%s' must be one of: %s." % (name, ", ".join(options))
            )
        if len(stripped) > MAX_PARAM_VALUE_LENGTH:
            raise TemplateValidationError(
                "Parameter '%s' exceeds the maximum length of %d characters."
                % (name, MAX_PARAM_VALUE_LENGTH)
            )
        return stripped
    if param_type == "boolean":
        return _coerce_boolean(name, value)
    if param_type == "number":
        return _coerce_number(param, value)
    raise TemplateValidationError(  # pragma: no cover - guarded at save time
        "Parameter '%s' has an unsupported type." % name
    )


def _normalize_parameters(raw: Any) -> List[Dict[str, Any]]:
    items = _as_list(raw, "parameters")
    if len(items) > MAX_PARAM_COUNT:
        raise TemplateValidationError(
            "Template declares too many parameters (max %d)." % MAX_PARAM_COUNT
        )
    parameters: List[Dict[str, Any]] = []
    seen: set = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise TemplateValidationError("Each template parameter must be an object.")
        unknown = sorted(set(item) - _PARAM_KEYS)
        if unknown:
            raise TemplateValidationError(
                "Template parameter contains unsupported key(s): %s." % ", ".join(unknown)
            )
        name = item.get("name")
        if not isinstance(name, str) or not PARAM_NAME_RE.fullmatch(name):
            raise TemplateValidationError(
                "Parameter names must start with a letter or underscore and contain only "
                "letters, digits and underscores."
            )
        if len(name) > MAX_PARAM_NAME_LENGTH:
            raise TemplateValidationError(
                "Parameter name '%s' is too long (max %d characters)."
                % (name[:MAX_PARAM_NAME_LENGTH], MAX_PARAM_NAME_LENGTH)
            )
        if name in seen:
            raise TemplateValidationError("Duplicate parameter name '%s'." % name)
        seen.add(name)

        param_type = item.get("type", "text")
        if not isinstance(param_type, str) or param_type.strip().lower() not in PARAM_TYPES:
            raise TemplateValidationError(
                "Parameter '%s' has an unsupported type; expected one of: %s."
                % (name, ", ".join(sorted(PARAM_TYPES)))
            )
        param_type = param_type.strip().lower()

        param: Dict[str, Any] = {
            "name": name,
            "label": _optional_string(
                item.get("label"), "parameters.label", default=name, max_length=MAX_LABEL_LENGTH
            ) or name,
            "type": param_type,
            "required": normalize_bool(item.get("required"), default=False),
            "default": None,
            "options": [],
            "description": _optional_string(
                item.get("description"), "parameters.description",
                default="", max_length=MAX_TEMPLATE_DESCRIPTION_LENGTH,
            ),
            "placeholder": _optional_string(
                item.get("placeholder"), "parameters.placeholder",
                default="", max_length=MAX_PLACEHOLDER_LENGTH,
            ),
            "min": None,
            "max": None,
            "integer": False,
        }

        if param_type == "select":
            options = _as_list(item.get("options"), "parameters.options")
            if not options:
                raise TemplateValidationError(
                    "Select parameter '%s' must declare a non-empty 'options' list." % name
                )
            if len(options) > MAX_PARAMETER_OPTIONS:
                raise TemplateValidationError(
                    "Select parameter '%s' has too many options (max %d)."
                    % (name, MAX_PARAMETER_OPTIONS)
                )
            cleaned_options: List[str] = []
            for option in options:
                if not isinstance(option, str) or not option.strip():
                    raise TemplateValidationError(
                        "Select parameter '%s' options must be non-empty strings." % name
                    )
                if option not in cleaned_options:
                    cleaned_options.append(option)
            param["options"] = cleaned_options
        elif item.get("options") not in (None, [], (), ""):
            raise TemplateValidationError(
                "Parameter '%s': 'options' is only valid for select parameters." % name
            )

        if param_type == "number":
            minimum = item.get("min")
            maximum = item.get("max")
            for label, bound in (("min", minimum), ("max", maximum)):
                if bound is not None and not _is_number(bound):
                    raise TemplateValidationError(
                        "Parameter '%s': '%s' must be a number." % (name, label)
                    )
            if minimum is not None and maximum is not None and minimum > maximum:
                raise TemplateValidationError(
                    "Parameter '%s': 'min' must not be greater than 'max'." % name
                )
            param["min"] = minimum
            param["max"] = maximum
            param["integer"] = normalize_bool(item.get("integer"), default=False)
        elif (
            item.get("min") is not None
            or item.get("max") is not None
            or normalize_bool(item.get("integer"), default=False)
        ):
            # A canonical template always carries these keys (as ``None`` /
            # ``False``), so only a *set* bound or flag is an error.  Otherwise
            # the file written by ``create_template`` could not be read back.
            raise TemplateValidationError(
                "Parameter '%s': 'min', 'max' and 'integer' are only valid for number "
                "parameters." % name
            )

        if "default" in item and item.get("default") is not None:
            # An invalid default makes the template broken: reject it here.
            try:
                param["default"] = _coerce_param_value(param, item["default"])
            except TemplateValidationError as exc:
                raise TemplateValidationError(
                    "Parameter '%s' has an invalid default: %s" % (name, exc)
                ) from exc

        parameters.append(param)
    return parameters


# ---------------------------------------------------------------------------
# Variables (declarations only) / knowledge base / defaults
# ---------------------------------------------------------------------------

def _normalize_variable_declarations(raw: Any) -> List[Dict[str, Any]]:
    items = _as_list(raw, "variables")
    declarations: List[Dict[str, Any]] = []
    seen: set = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise TemplateValidationError(
                "Each template variable must be an object with a 'key'."
            )
        unknown = sorted(set(item) - _VARIABLE_DECLARATION_KEYS)
        if unknown:
            raise TemplateValidationError(
                "Template variable contains unsupported key(s): %s." % ", ".join(unknown)
            )
        key = item.get("key")
        if not isinstance(key, str) or not VARIABLE_KEY_RE.fullmatch(key):
            raise TemplateValidationError(
                "Variable keys must start with a letter or underscore and contain only "
                "letters, digits and underscores."
            )
        if key in seen:
            raise TemplateValidationError("Duplicate variable declaration '%s'." % key)
        seen.add(key)
        is_secret = normalize_bool(item.get("is_secret"), default=False)
        default_value = item.get("default")
        if default_value is not None and not isinstance(default_value, str):
            raise TemplateValidationError(
                "Variable '%s' must declare its default as a string." % key
            )
        if is_secret and default_value is not None:
            raise TemplateValidationError(
                "Template variable '%s' is secret: a template stores declarations only, "
                "so it must not embed a default value." % key
            )
        declarations.append({
            "key": key,
            "is_secret": is_secret,
            "required": normalize_bool(item.get("required"), default=False),
            "description": _optional_string(
                item.get("description"), "variables.description",
                default="", max_length=MAX_TEMPLATE_DESCRIPTION_LENGTH,
            ),
            "default": default_value,
        })
    return declarations


def _normalize_kb_files(raw: Any) -> Dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise TemplateValidationError(
            "Template field 'kb_files' must be an object mapping relative paths to "
            "file contents."
        )
    if len(raw) > MAX_TEMPLATE_ITEMS:
        raise TemplateValidationError(
            "Template declares too many knowledge-base files (max %d)." % MAX_TEMPLATE_ITEMS
        )
    files: Dict[str, str] = {}
    for path, content in raw.items():
        try:
            safe_path = _safe_kb_path(path)
        except AgentPortabilityError as exc:
            raise TemplateValidationError(
                "Knowledge-base path in template 'kb_files' is invalid: %s" % exc
            ) from exc
        if len(safe_path) > MAX_KB_PATH_LENGTH:
            raise TemplateValidationError(
                "Knowledge-base path is too long (max %d characters)." % MAX_KB_PATH_LENGTH
            )
        if safe_path in files:
            raise TemplateValidationError(
                "Duplicate knowledge-base path '%s' in template 'kb_files'." % safe_path
            )
        if not isinstance(content, str):
            raise TemplateValidationError(
                "Knowledge-base file '%s' must have string content." % safe_path
            )
        if len(content) > MAX_KB_FILE_LENGTH:
            raise TemplateValidationError(
                "Knowledge-base file '%s' is too large (max %d characters)."
                % (safe_path, MAX_KB_FILE_LENGTH)
            )
        files[safe_path] = content
    return files


def _normalize_defaults(raw: Any) -> Dict[str, Any]:
    """Validate ``defaults`` against the agent-factory field allowlist.

    Config keys are coerced by :func:`agent_factory.normalize_spec` so that a
    template default is written exactly like a UI-created agent setting
    (booleans become 0/1, enum aliases are canonicalized, ...).
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise TemplateValidationError("Template field 'defaults' must be an object.")

    unknown = sorted(set(raw) - set(SPEC_FIELD_ALLOWLIST))
    if unknown:
        raise TemplateValidationError(
            "Template 'defaults' contains unsupported key(s): %s." % ", ".join(unknown)
        )

    config_values = {key: value for key, value in raw.items() if key in CONFIG_KEYS}
    identity_values = {
        key: value for key, value in raw.items() if key not in CONFIG_KEYS
    }
    spec: Dict[str, Any] = {"id": "_template_defaults"}
    if config_values:
        spec["configuration"] = config_values
    spec.update(identity_values)
    try:
        normalized = agent_factory.normalize_spec(spec)
    except SpecValidationError as exc:
        raise TemplateValidationError("Template 'defaults' is invalid: %s" % exc) from exc
    return dict(normalized["values"])


# ---------------------------------------------------------------------------
# Placeholder tokenizer / renderer
# ---------------------------------------------------------------------------

def _tokenize_prompt(source: str) -> List[Tuple[str, Any]]:
    """Split *source* into ``('lit', text)`` and ``('ph', name)`` tokens.

    One pass, escape-aware, and never re-scans substituted output.  A ``{{``
    that is neither a valid placeholder nor escaped raises
    :class:`TemplateRenderError`.
    """
    tokens: List[Tuple[str, Any]] = []
    literal: List[str] = []
    index = 0
    length = len(source)
    while index < length:
        if source.startswith(ESCAPE_SEQUENCE, index):
            literal.append("{{")
            index += len(ESCAPE_SEQUENCE)
            continue
        if source.startswith("{{", index):
            match = PLACEHOLDER_RE.match(source, index)
            if match is None:
                raise TemplateRenderError(
                    "Malformed placeholder at offset %d ('%s'): a placeholder must look "
                    "like '{{ name }}' and a literal '{{' must be %s."
                    % (index, _snippet(source, index), _escape_hint())
                )
            if literal:
                tokens.append(("lit", "".join(literal)))
                literal = []
            tokens.append(("ph", match.group(1)))
            index = match.end()
            continue
        literal.append(source[index])
        index += 1
    if literal:
        tokens.append(("lit", "".join(literal)))
    return tokens


def placeholder_names(source: str) -> List[str]:
    """Return every placeholder name referenced by *source*, in order."""
    names: List[str] = []
    for kind, payload in _tokenize_prompt(source):
        if kind == "ph" and payload not in names:
            names.append(payload)
    return names


def _render_source(source: str, declared: Mapping[str, Any], resolved: Mapping[str, Any]) -> str:
    """Render *source* with already-resolved values (single pass)."""
    tokens = _tokenize_prompt(source)
    unknown = sorted({
        payload for kind, payload in tokens if kind == "ph" and payload not in declared
    })
    if unknown:
        raise TemplateRenderError(
            "Unknown placeholder(s): %s. Declared parameters: %s."
            % (
                ", ".join(unknown),
                ", ".join(declared.keys()) or "(none)",
            )
        )
    parts: List[str] = []
    for kind, payload in tokens:
        if kind == "ph":
            parts.append(_format_value(resolved.get(payload, "")))
        else:
            parts.append(payload)
    return "".join(parts)


def _declared_parameters(template: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw = template.get("parameters")
    if raw is None:
        return {}
    if not isinstance(raw, (list, tuple)):
        raise TemplateValidationError("Template field 'parameters' must be a list.")
    declared: Dict[str, Dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise TemplateValidationError("Each template parameter must be an object.")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise TemplateValidationError("Each template parameter must declare a 'name'.")
        declared[name] = dict(item)
    return declared


def resolve_parameters(
    template: Mapping[str, Any],
    params: Optional[Mapping[str, Any]] = None,
    *,
    strict: bool = True,
) -> Dict[str, Any]:
    """Resolve every declared parameter to a validated value.

    Resolution order is *caller value > declared default >* ``''``.  Caller
    parameters that the template does not declare are rejected.  With
    ``strict=True`` (the default) a ``required`` parameter that resolves to an
    empty rendered value is an error.
    """
    if not isinstance(template, Mapping):
        raise TemplateValidationError("Template must be a mapping.")
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise TemplateValidationError("Template parameters must be provided as an object.")

    declared = _declared_parameters(template)
    unknown = sorted(key for key in params if key not in declared)
    if unknown:
        raise TemplateValidationError(
            "Undeclared parameter(s) for template '%s': %s. Only declared parameters may "
            "be supplied." % (template.get("id") or "?", ", ".join(unknown))
        )

    resolved: Dict[str, Any] = {}
    empty_required: List[str] = []
    for name, param in declared.items():
        if name in params and params[name] is not None:
            value = _coerce_param_value(param, params[name])
        elif param.get("default") is not None:
            value = _coerce_param_value(param, param["default"])
        else:
            value = ""
        if param.get("required") and not _format_value(value).strip():
            empty_required.append(name)
        resolved[name] = value

    if strict and empty_required:
        raise TemplateValidationError(
            "Required parameter(s) resolved to an empty value: %s."
            % ", ".join(empty_required)
        )
    return resolved


def render_system_prompt(
    template: Mapping[str, Any],
    params: Optional[Mapping[str, Any]] = None,
    *,
    strict: bool = True,
) -> str:
    """Render a template's system prompt with validated parameters.

    Unknown or malformed placeholders raise :class:`TemplateRenderError`; the
    rendered prompt is length-capped AFTER rendering.
    """
    if not isinstance(template, Mapping):
        raise TemplateValidationError("Template must be a mapping.")
    prompt = template.get("system_prompt")
    if not isinstance(prompt, str):
        raise TemplateValidationError("Template 'system_prompt' must be a string.")
    declared = _declared_parameters(template)
    resolved = resolve_parameters(template, params, strict=strict)
    rendered = _render_source(prompt, declared, resolved)
    if len(rendered) > MAX_SYSTEM_PROMPT_LENGTH:
        raise TemplateRenderError(
            "Rendered system prompt is too long (%d characters; max %d)."
            % (len(rendered), MAX_SYSTEM_PROMPT_LENGTH)
        )
    return rendered


# ---------------------------------------------------------------------------
# Template validation
# ---------------------------------------------------------------------------

def validate_template(payload: Any) -> Dict[str, Any]:
    """Validate a raw template object and return its canonical form.

    ``_meta`` (added by the loader) is ignored so that a fetch → edit → save
    round-trip needs no massaging.  Raises :class:`TemplateValidationError` /
    :class:`TemplateRenderError` for anything malformed.
    """
    if not isinstance(payload, Mapping):
        raise TemplateValidationError("Template must be a JSON object.")
    data = _strip_meta(payload)

    unknown = sorted(set(data) - _TEMPLATE_KEYS)
    if unknown:
        raise TemplateValidationError(
            "Template contains unsupported key(s): %s." % ", ".join(unknown)
        )

    template_id = _validate_template_id(data.get("id"))

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise TemplateValidationError("Template field 'name' is required.")
    if len(name) > 200:
        raise TemplateValidationError("Template name is too long (max 200 characters).")

    description = _optional_string(
        data.get("description"), "description",
        default="", max_length=MAX_TEMPLATE_DESCRIPTION_LENGTH,
    )
    category = _optional_string(
        data.get("category"), "category", default="general", max_length=MAX_CATEGORY_LENGTH
    ) or "general"
    icon = _optional_string(
        data.get("icon"), "icon", default="", max_length=MAX_ICON_LENGTH
    )

    schema_version = data.get("schema_version", TEMPLATE_SCHEMA_VERSION)
    if isinstance(schema_version, str) and re.fullmatch(r"\d+", schema_version.strip()):
        schema_version = int(schema_version.strip())
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise TemplateValidationError("Template field 'schema_version' must be an integer.")
    if schema_version != TEMPLATE_SCHEMA_VERSION:
        raise TemplateValidationError(
            "Unsupported template schema_version %d (expected %d)."
            % (schema_version, TEMPLATE_SCHEMA_VERSION)
        )

    parameters = _normalize_parameters(data.get("parameters"))

    system_prompt = data.get("system_prompt")
    if not isinstance(system_prompt, str):
        raise TemplateValidationError(
            "Template field 'system_prompt' is required and must be a string."
        )
    if len(system_prompt) > MAX_SYSTEM_PROMPT_LENGTH:
        raise TemplateValidationError(
            "Template system prompt is too long (max %d characters)." % MAX_SYSTEM_PROMPT_LENGTH
        )

    tools = _string_list(data.get("tools"), "tools")
    skills = _string_list(data.get("skills"), "skills")
    variables = _normalize_variable_declarations(data.get("variables"))
    kb_files = _normalize_kb_files(data.get("kb_files"))
    defaults = _normalize_defaults(data.get("defaults"))

    canonical: Dict[str, Any] = {
        "id": template_id,
        "name": name,
        "description": description,
        "category": category,
        "icon": icon,
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "parameters": parameters,
        "system_prompt": system_prompt,
        "defaults": defaults,
        "tools": tools,
        "skills": skills,
        "variables": variables,
        "kb_files": kb_files,
    }

    # Placeholder audit: every placeholder must be declared, at save time too.
    declared = {param["name"]: param for param in parameters}
    for label, source in (("system_prompt", system_prompt),):
        unknown_placeholders = sorted(
            name for name in placeholder_names(source) if name not in declared
        )
        if unknown_placeholders:
            raise TemplateRenderError(
                "Template '%s' uses undeclared placeholder(s) in %s: %s. Declared "
                "parameters: %s."
                % (
                    template_id,
                    label,
                    ", ".join(unknown_placeholders),
                    ", ".join(declared) or "(none)",
                )
            )
    for key in ("name", "description"):
        value = defaults.get(key)
        if isinstance(value, str) and "{{" in value:
            unknown_placeholders = sorted(
                placeholder for placeholder in placeholder_names(value)
                if placeholder not in declared
            )
            if unknown_placeholders:
                raise TemplateRenderError(
                    "Template '%s' uses undeclared placeholder(s) in defaults.%s: %s."
                    % (template_id, key, ", ".join(unknown_placeholders))
                )
    return canonical


# ---------------------------------------------------------------------------
# Legacy (skillsets/) adaptation + scanning
# ---------------------------------------------------------------------------

def _adapt_legacy(raw: Mapping[str, Any], template_id: str) -> Dict[str, Any]:
    """Convert a legacy ``skillsets/*.json`` payload to the canonical shape."""
    defaults: Dict[str, Any] = {}
    model = raw.get("model")
    if isinstance(model, str) and model.strip():
        defaults["model_id"] = model.strip()
    return {
        "id": template_id,
        "name": raw.get("name") or template_id,
        "description": raw.get("description") or "",
        "category": "legacy",
        "icon": "",
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "parameters": [],
        "system_prompt": raw.get("system_prompt") or "",
        "defaults": defaults,
        "tools": raw.get("tools") or [],
        "skills": raw.get("skills") or [],
        "variables": [],
        "kb_files": raw.get("kb_files") or {},
    }


def _scan_dir(root: str) -> List[str]:
    """Return the sorted ``*.json`` file names directly inside *root*."""
    if not os.path.isdir(root):
        return []
    names: List[str] = []
    for name in sorted(os.listdir(root)):
        if name.startswith(".") or not name.endswith(".json"):
            continue
        path = os.path.join(root, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        names.append(name)
    return names


def _scan_canonical(root: str) -> Dict[str, Dict[str, Any]]:
    """Map id -> entry for every canonical template file."""
    entries: Dict[str, Dict[str, Any]] = {}
    for file_name in _scan_dir(root):
        stem = file_name[: -len(".json")]
        entry: Dict[str, Any] = {
            "id": stem,
            "source": "agent_templates",
            "file": file_name,
            "valid": False,
            "error": None,
            "template": None,
            "raw": None,
        }
        try:
            raw = _read_json_object(os.path.join(root, file_name))
        except TemplateError as exc:
            entry["error"] = str(exc)
            entry["raw"] = None
            entries[stem] = entry
            continue
        entry["raw"] = raw
        declared_id = raw.get("id")
        if declared_id is not None and declared_id != stem:
            entry["error"] = (
                "Template file '%s' declares id '%s' which does not match its file name."
                % (file_name, declared_id)
            )
            entries[stem] = entry
            continue
        try:
            entry["template"] = validate_template({**raw, "id": stem})
            entry["valid"] = True
        except (TemplateValidationError, AgentFactoryError) as exc:
            entry["error"] = str(exc)
        entries[stem] = entry
    return entries


def _scan_legacy(root: str) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """Map id -> entry for legacy skillset files, plus duplicate reports."""
    entries: Dict[str, Dict[str, Any]] = {}
    duplicates: List[Dict[str, Any]] = []
    for file_name in _scan_dir(root):
        stem = file_name[: -len(".json")]
        entry: Dict[str, Any] = {
            "id": stem,
            "source": "skillsets",
            "file": file_name,
            "valid": False,
            "error": None,
            "template": None,
            "raw": None,
        }
        try:
            raw = _read_json_object(os.path.join(root, file_name))
        except TemplateError as exc:
            entry["error"] = str(exc)
            entries.setdefault(stem, entry)
            continue
        entry["raw"] = raw
        declared_id = raw.get("id")
        template_id = declared_id.strip() if isinstance(declared_id, str) and declared_id.strip() else stem
        entry["id"] = template_id
        if template_id in entries:
            duplicates.append({
                "id": template_id,
                "kind": "legacy_duplicate",
                "chosen": "skillsets",
                "shadowed": "skillsets",
                "canonical_file": None,
                "legacy_file": entries[template_id]["file"],
                "duplicate_file": file_name,
                "message": (
                    "Skillset id '%s' is declared by both '%s' and '%s' in skillsets/; the "
                    "first file wins and the second is shadowed (neither is modified)."
                    % (template_id, entries[template_id]["file"], file_name)
                ),
            })
            continue
        try:
            entry["template"] = validate_template(_adapt_legacy(raw, template_id))
            entry["valid"] = True
        except (TemplateValidationError, AgentFactoryError) as exc:
            entry["error"] = str(exc)
        entries[template_id] = entry
    return entries, duplicates


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------

def _entry_summary(entry: Mapping[str, Any], *, shadowed: bool, shadows: bool) -> Dict[str, Any]:
    template = entry.get("template")
    legacy = entry.get("source") == "skillsets"
    if template is not None:
        summary = {
            "id": template["id"],
            "name": template["name"],
            "description": template["description"],
            "category": template["category"],
            "icon": template["icon"],
            "schema_version": template["schema_version"],
            "parameter_count": len(template["parameters"]),
            "tool_count": len(template["tools"]),
            "skill_count": len(template["skills"]),
            "variable_count": len(template["variables"]),
            "kb_file_count": len(template["kb_files"]),
        }
    else:
        raw = entry.get("raw") or {}
        summary = {
            "id": entry.get("id"),
            "name": raw.get("name") or entry.get("id"),
            "description": raw.get("description") or "",
            "category": "legacy" if legacy else "general",
            "icon": "",
            "schema_version": raw.get("schema_version") or TEMPLATE_SCHEMA_VERSION,
            "parameter_count": len(raw.get("parameters") or []),
            "tool_count": len(raw.get("tools") or []),
            "skill_count": len(raw.get("skills") or []),
            "variable_count": len(raw.get("variables") or []),
            "kb_file_count": len(raw.get("kb_files") or {}),
        }
    summary.update({
        "source": entry.get("source"),
        "file": entry.get("file"),
        "legacy": legacy,
        "writable": not legacy,
        "shadowed": bool(shadowed),
        "shadows": bool(shadows),
        "collision": bool(shadowed and not legacy),
        "valid": bool(entry.get("valid")),
        "error": entry.get("error"),
    })
    return summary


def list_templates(
    *,
    base_dir: Optional[str] = None,
    include_legacy: bool = True,
) -> List[Dict[str, Any]]:
    """List template summaries from both storage roots.

    The canonical entry wins; the legacy entry (if any) is still listed with
    ``shadowed=True`` so the UI can surface the collision explicitly.
    """
    canonical_root, legacy_root = _roots(base_dir)
    canonical = _scan_canonical(canonical_root)
    legacy, _duplicates = _scan_legacy(legacy_root) if include_legacy else ({}, [])

    summaries: List[Dict[str, Any]] = []
    for template_id in sorted(set(canonical) | set(legacy)):
        in_canonical = template_id in canonical
        in_legacy = template_id in legacy
        if in_canonical:
            summaries.append(_entry_summary(
                canonical[template_id], shadowed=False, shadows=in_legacy
            ))
        if in_legacy and include_legacy:
            summaries.append(_entry_summary(
                legacy[template_id], shadowed=in_canonical, shadows=False
            ))
    return summaries


def has_template(template_id: str, *, base_dir: Optional[str] = None) -> bool:
    """Return whether *template_id* exists in any storage root."""
    canonical_root, legacy_root = _roots(base_dir)
    cleaned = _validate_template_id(template_id)
    canonical_map = _scan_canonical(canonical_root)
    if cleaned in canonical_map:
        return True
    legacy_map, _duplicates = _scan_legacy(legacy_root)
    return cleaned in legacy_map


def list_collisions(*, base_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Report every id that exists in more than one place.

    Collisions are never resolved silently: ``agent_templates/`` always wins.
    """
    canonical_root, legacy_root = _roots(base_dir)
    canonical = _scan_canonical(canonical_root)
    legacy, duplicates = _scan_legacy(legacy_root)
    collisions: List[Dict[str, Any]] = []
    for template_id in sorted(set(canonical) & set(legacy)):
        collisions.append({
            "id": template_id,
            "kind": "canonical_legacy",
            "chosen": "agent_templates",
            "shadowed": "skillsets",
            "canonical_file": canonical[template_id]["file"],
            "legacy_file": legacy[template_id]["file"],
            "duplicate_file": None,
            "message": (
                "Template id '%s' exists in both agent_templates/ ('%s') and skillsets/ "
                "('%s'); the canonical template wins and the legacy skillset is shadowed "
                "(it is never modified or deleted)."
                % (template_id, canonical[template_id]["file"], legacy[template_id]["file"])
            ),
        })
    collisions.extend(duplicates)
    return collisions


def get_template(template_id: str, *, base_dir: Optional[str] = None) -> Dict[str, Any]:
    """Load a template (canonical first, then legacy) as a canonical mapping.

    The returned mapping carries a read-only ``_meta`` block describing where
    the template came from and whether it is writable.
    """
    canonical_root, legacy_root = _roots(base_dir)
    cleaned = _validate_template_id(template_id)

    canonical_path = _template_path(canonical_root, cleaned)
    if os.path.isfile(canonical_path):
        raw = _read_json_object(canonical_path)
        declared_id = raw.get("id")
        if declared_id is not None and declared_id != cleaned:
            raise TemplateValidationError(
                "Template file '%s.json' declares id '%s' which does not match its file name."
                % (cleaned, declared_id)
            )
        template = validate_template({**raw, "id": cleaned})
        template[_META_KEY] = {
            "source": "agent_templates",
            "legacy": False,
            "writable": True,
            "file": os.path.basename(canonical_path),
        }
        return template

    legacy_entries, _duplicates = _scan_legacy(legacy_root)
    entry = legacy_entries.get(cleaned)
    if entry is None:
        raise TemplateNotFoundError("Template '%s' was not found." % cleaned)
    if not entry["valid"]:
        raise TemplateValidationError(
            "Legacy skillset '%s' is not a valid template: %s" % (cleaned, entry["error"])
        )
    template = dict(entry["template"])
    template[_META_KEY] = {
        "source": "skillsets",
        "legacy": True,
        "writable": False,
        "file": entry["file"],
    }
    return template


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------

def create_template(
    data: Any,
    *,
    base_dir: Optional[str] = None,
    allow_shadow: bool = False,
) -> Dict[str, Any]:
    """Validate, then atomically write a new canonical template.

    Refuses to overwrite an existing canonical file, and refuses to shadow a
    legacy skillset unless ``allow_shadow=True`` is passed explicitly.
    """
    if not isinstance(data, Mapping):
        raise TemplateValidationError("Template must be a JSON object.")
    cleaned = _validate_template_id(_strip_meta(data).get("id"))
    template = validate_template({**_strip_meta(data), "id": cleaned})

    canonical_root, legacy_root = _roots(base_dir)
    path = _template_path(canonical_root, cleaned)
    if os.path.exists(path):
        raise TemplateExistsError(
            "A template with id '%s' already exists in agent_templates/." % cleaned
        )
    legacy_entries, _duplicates = _scan_legacy(legacy_root)
    shadowed = legacy_entries.get(cleaned)
    if shadowed is not None and not allow_shadow:
        raise TemplateExistsError(
            "Template id '%s' would shadow the legacy skillset '%s' in skillsets/. Pass "
            "allow_shadow=True to shadow it explicitly." % (cleaned, shadowed["file"])
        )

    _atomic_write_template(path, template)
    logger.info("agent_templates: created template '%s'", cleaned)
    return get_template(cleaned, base_dir=base_dir)


def update_template(
    template_id: str,
    data: Any,
    *,
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Update a canonical template (top-level shallow merge) and rewrite it.

    Legacy-only templates are read-only: create a canonical copy first.
    """
    cleaned = _validate_template_id(template_id)
    if not isinstance(data, Mapping):
        raise TemplateValidationError("Template must be a JSON object.")
    payload = _strip_meta(data)

    canonical_root, legacy_root = _roots(base_dir)
    path = _template_path(canonical_root, cleaned)
    if not os.path.isfile(path):
        legacy_entries, _duplicates = _scan_legacy(legacy_root)
        if cleaned in legacy_entries:
            raise TemplateError(
                "Template '%s' is a read-only legacy skillset in skillsets/. Create a "
                "canonical copy in agent_templates/ (for example with create_template) "
                "before editing it." % cleaned
            )
        raise TemplateNotFoundError("Template '%s' was not found." % cleaned)

    declared_id = payload.get("id")
    if declared_id is not None and declared_id != cleaned:
        raise TemplateValidationError(
            "The id of an existing template cannot be changed ('%s' -> '%s')."
            % (cleaned, declared_id)
        )

    existing = _strip_meta(_read_json_object(path))
    merged = {**existing, **payload}
    merged["id"] = cleaned
    template = validate_template(merged)
    _atomic_write_template(path, template)
    logger.info("agent_templates: updated template '%s'", cleaned)
    return get_template(cleaned, base_dir=base_dir)


def delete_template(template_id: str, *, base_dir: Optional[str] = None) -> bool:
    """Delete a canonical template file.  Legacy files are never touched."""
    cleaned = _validate_template_id(template_id)
    canonical_root, legacy_root = _roots(base_dir)
    path = _template_path(canonical_root, cleaned)
    if os.path.isfile(path):
        os.unlink(path)
        logger.info("agent_templates: deleted template '%s'", cleaned)
        return True
    legacy_entries, _duplicates = _scan_legacy(legacy_root)
    if cleaned in legacy_entries:
        raise TemplateError(
            "Template '%s' is a read-only legacy skillset in skillsets/ and cannot be "
            "deleted through the template engine." % cleaned
        )
    raise TemplateNotFoundError("Template '%s' was not found." % cleaned)


# ---------------------------------------------------------------------------
# Dependency resolution
# ---------------------------------------------------------------------------

def _available_tool_ids() -> Optional[set]:
    """Return every assignable tool id, or ``None`` when it cannot be checked.

    Returns ``None`` (never a false "missing" report) when the tool registry is
    unavailable in this process.
    """
    try:
        from backend.tools import tool_registry
        from backend.tools.registry import BUILTIN_TOOL_IDS

        names = set(BUILTIN_TOOL_IDS)
        for definition in tool_registry.get_all_tool_defs():
            function = (definition or {}).get("function") or {}
            name = function.get("name")
            if name:
                names.add(name)
        for definition in tool_registry.get_builtin_tool_defs():
            name = (definition or {}).get("name")
            if name:
                names.add(name)
        return names
    except Exception:  # noqa: BLE001 - availability check must never be fatal
        logger.warning(
            "agent_templates: tool registry unavailable; skipping tool dependency checks",
            exc_info=True,
        )
        return None


def _available_skill_ids(base_dir: str) -> Optional[set]:
    """Return installed skill ids (manifest scan), or ``None`` if unknowable."""
    skills_root = os.path.join(base_dir, "skills")
    if not os.path.isdir(skills_root):
        return None
    return {
        name
        for name in os.listdir(skills_root)
        if os.path.isfile(os.path.join(skills_root, name, "skill.json"))
    }


def _resolution_report(template: Mapping[str, Any], *, base_dir: str) -> Dict[str, Any]:
    """Build the editor-facing resolution report for a canonical template."""
    declared_tools = list(template.get("tools") or [])
    declared_skills = list(template.get("skills") or [])
    parameters = _declared_parameters(template)

    available_tools = _available_tool_ids()
    tools_checked = available_tools is not None
    missing_tools = (
        [name for name in declared_tools if name not in available_tools]
        if tools_checked else []
    )

    available_skills = _available_skill_ids(base_dir)
    skills_checked = available_skills is not None
    missing_skills = (
        [name for name in declared_skills if name not in available_skills]
        if skills_checked else []
    )

    referenced: List[str] = list(placeholder_names(template.get("system_prompt") or ""))
    for key in ("name", "description"):
        value = (template.get("defaults") or {}).get(key)
        if isinstance(value, str):
            referenced.extend(placeholder_names(value))

    declared_variables = list(template.get("variables") or [])
    required_variables = [
        declaration["key"] for declaration in declared_variables if declaration.get("required")
    ]
    unresolved_variables = [
        declaration["key"]
        for declaration in declared_variables
        if declaration.get("required") and not declaration.get("default")
    ]
    required_parameters = [name for name, param in parameters.items() if param.get("required")]
    parameters_without_value = [
        name for name in required_parameters if parameters[name].get("default") in (None, "")
    ]

    warnings: List[str] = []
    errors: List[str] = []
    if not tools_checked:
        warnings.append("Tool availability could not be checked (registry unavailable).")
    if not skills_checked:
        warnings.append(
            "Skill availability could not be checked (no skills/ directory under base_dir)."
        )
    if missing_tools:
        errors.append("Missing tool(s): %s." % ", ".join(missing_tools))
    if missing_skills:
        errors.append("Missing skill(s): %s." % ", ".join(missing_skills))
    unused = [name for name in parameters if name not in referenced]
    if unused:
        warnings.append("Declared parameter(s) not referenced by the prompt: %s." % ", ".join(unused))
    if unresolved_variables:
        warnings.append(
            "Required variable(s) need a value at instantiate time: %s."
            % ", ".join(unresolved_variables)
        )
    if parameters_without_value:
        warnings.append(
            "Required parameter(s) without a default need a value at instantiate time: %s."
            % ", ".join(parameters_without_value)
        )

    return {
        "id": template.get("id"),
        "name": template.get("name"),
        "source": (template.get(_META_KEY) or {}).get("source"),
        "legacy": bool((template.get(_META_KEY) or {}).get("legacy")),
        "writable": bool((template.get(_META_KEY) or {}).get("writable")),
        "ok": not errors,
        "deps_checked": {"tools": tools_checked, "skills": skills_checked},
        "tools": {
            "declared": declared_tools,
            "resolved": [name for name in declared_tools if name not in missing_tools],
            "missing": missing_tools,
        },
        "skills": {
            "declared": declared_skills,
            "resolved": [name for name in declared_skills if name not in missing_skills],
            "missing": missing_skills,
        },
        "parameters": {
            "declared": list(parameters),
            "required": required_parameters,
            "required_without_value": parameters_without_value,
            "unused": unused,
        },
        "variables": {
            "declared": [declaration["key"] for declaration in declared_variables],
            "required": required_variables,
            "unresolved": unresolved_variables,
        },
        "warnings": warnings,
        "errors": errors,
    }


def resolve_template(template_id: str, *, base_dir: Optional[str] = None) -> Dict[str, Any]:
    """Return the full editor report for a template (never raises for deps).

    Tools and skills are resolved at *inspect* time only — nothing is cached
    back into the template file.
    """
    template = get_template(template_id, base_dir=base_dir)
    root = base_dir if base_dir is not None else _default_base_dir()
    report = _resolution_report(template, base_dir=root)
    report["template"] = template
    return report


# ---------------------------------------------------------------------------
# Preview + instantiation
# ---------------------------------------------------------------------------

def preview_template(
    template_id: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    base_dir: Optional[str] = None,
    strict: bool = False,
) -> Dict[str, Any]:
    """Render a template without creating anything (editor preview).

    ``strict=False`` (default) tolerates required parameters that still need a
    value, and reports them as warnings.
    """
    template = get_template(template_id, base_dir=base_dir)
    resolved = resolve_parameters(template, params, strict=False)
    declared = _declared_parameters(template)
    prompt = _render_source(
        template.get("system_prompt") or "", declared, resolved
    )
    if len(prompt) > MAX_SYSTEM_PROMPT_LENGTH:
        raise TemplateRenderError(
            "Rendered system prompt is too long (%d characters; max %d)."
            % (len(prompt), MAX_SYSTEM_PROMPT_LENGTH)
        )

    defaults = dict(template.get("defaults") or {})
    if not defaults.get("name"):
        defaults["name"] = template.get("name") or template.get("id")
    if "description" not in defaults:
        defaults["description"] = template.get("description") or ""
    rendered_identity = {
        key: _render_source(value, declared, resolved) if isinstance(value, str) else value
        for key, value in defaults.items()
    }

    warnings: List[str] = []
    if not strict:
        empty_required = [
            name
            for name, param in declared.items()
            if param.get("required") and _format_value(resolved.get(name, "")) == ""
        ]
        if empty_required:
            warnings.append(
                "Required parameter(s) without a value yet: %s." % ", ".join(empty_required)
            )

    return {
        "id": template["id"],
        "name": template["name"],
        "description": template["description"],
        "source": (template.get(_META_KEY) or {}).get("source"),
        "writable": bool((template.get(_META_KEY) or {}).get("writable")),
        "system_prompt": prompt,
        "system_prompt_length": len(prompt),
        "values": resolved,
        "spec_preview": rendered_identity,
        "warnings": warnings,
    }


def _normalize_overrides(overrides: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping):
        raise TemplateValidationError("Template overrides must be provided as an object.")
    unknown = sorted(key for key in overrides if key not in OVERRIDE_KEYS)
    if unknown:
        raise TemplateValidationError(
            "Template override(s) not allowed: %s." % ", ".join(unknown)
        )
    return dict(overrides)


def _coerce_variable_value(key: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return _format_value(value)
    if isinstance(value, str):
        return value
    raise TemplateValidationError(
        "Variable '%s' must have a string value." % key
    )


def _merge_variables(
    template: Mapping[str, Any],
    provided: Any,
    *,
    allow_unresolved: bool,
) -> List[Dict[str, Any]]:
    """Merge template variable declarations with values supplied at instantiate.

    Declarations own secrecy; values are never logged or written to the
    template file.
    """
    declarations = list(template.get("variables") or [])
    declared = {declaration["key"]: declaration for declaration in declarations}

    values: Dict[str, Any] = {}
    if provided is None:
        pass
    elif isinstance(provided, Mapping):
        values = dict(provided)
    elif isinstance(provided, (list, tuple)):
        for item in provided:
            if not isinstance(item, Mapping) or not isinstance(item.get("key"), str):
                raise TemplateValidationError(
                    "Each variable override must be an object with a 'key'."
                )
            values[item["key"]] = item.get("value", "")
    else:
        raise TemplateValidationError(
            "Variable overrides must be an object or a list of objects."
        )

    unknown = sorted(key for key in values if key not in declared)
    if unknown:
        raise TemplateValidationError(
            "Undeclared variable(s): %s. The template declares: %s."
            % (", ".join(unknown), ", ".join(declared) or "(none)")
        )

    missing: List[str] = []
    merged: List[Dict[str, Any]] = []
    for key, declaration in declared.items():
        if key in values and values[key] is not None:
            value = _coerce_variable_value(key, values[key])
        elif declaration.get("default") is not None:
            value = declaration["default"]
        else:
            value = ""
        if declaration.get("required") and not value:
            missing.append(key)
        merged.append({
            "key": key,
            "value": value,
            "is_secret": 1 if declaration.get("is_secret") else 0,
        })

    if missing and not allow_unresolved:
        raise TemplateValidationError(
            "Missing value(s) for required variable(s): %s." % ", ".join(missing)
        )
    return merged


def create_agent_from_template(
    template_id: str,
    params: Optional[Mapping[str, Any]] = None,
    overrides: Optional[Mapping[str, Any]] = None,
    *,
    db: Any = None,
    base_dir: Optional[str] = None,
    if_exists: Optional[str] = None,
    allow_missing_deps: bool = False,
    allow_unresolved_variables: bool = False,
) -> str:
    """Instantiate a real agent from a template and return its agent id.

    Sequence: load + schema-validate → validate params and overrides →
    resolve tool/skill dependencies (unless ``allow_missing_deps``) → render
    the prompt and merge defaults (``overrides`` > ``defaults``) → hand the
    spec to :func:`backend.agent_factory.create_agent`, which performs the
    atomic DB/filesystem creation.

    ``base_dir`` is the repository root used both for template lookup and for
    the new agent's files.
    """
    template = get_template(template_id, base_dir=base_dir)
    resolved_params = resolve_parameters(template, params, strict=True)
    override_values = _normalize_overrides(overrides)

    root = base_dir if base_dir is not None else _default_base_dir()
    report = _resolution_report(template, base_dir=root)
    if report["errors"] and not allow_missing_deps:
        raise TemplateResolveError(
            "Template '%s' has unresolved dependencies: %s"
            % (template["id"], " ".join(report["errors"])),
            report,
        )

    variables = _merge_variables(
        template,
        override_values.get("variables"),
        allow_unresolved=allow_unresolved_variables,
    )

    declared = _declared_parameters(template)
    prompt = _render_source(
        template.get("system_prompt") or "", declared, resolved_params
    )
    if len(prompt) > MAX_SYSTEM_PROMPT_LENGTH:
        raise TemplateRenderError(
            "Rendered system prompt is too long (%d characters; max %d)."
            % (len(prompt), MAX_SYSTEM_PROMPT_LENGTH)
        )

    spec: Dict[str, Any] = {}
    for key, value in (template.get("defaults") or {}).items():
        spec[key] = value
    for key, value in override_values.items():
        if key in SPEC_FIELD_ALLOWLIST:
            spec[key] = value

    if not spec.get("name"):
        spec["name"] = template.get("name") or template["id"]
    if "description" not in spec:
        spec["description"] = template.get("description") or ""
    for key in ("name", "description"):
        if isinstance(spec.get(key), str) and "{{" in spec[key]:
            spec[key] = _render_source(spec[key], declared, resolved_params)

    spec["system_prompt"] = prompt
    spec["tools"] = (
        _string_list(override_values["tools"], "tools")
        if "tools" in override_values else list(template.get("tools") or [])
    )
    spec["skills"] = (
        _string_list(override_values["skills"], "skills")
        if "skills" in override_values else list(template.get("skills") or [])
    )
    spec["knowledge_base"] = (
        override_values["knowledge_base"]
        if "knowledge_base" in override_values
        else [
            {"path": path, "content": content}
            for path, content in (template.get("kb_files") or {}).items()
        ]
    )
    spec["variables"] = variables
    if "id" in override_values:
        spec["id"] = override_values["id"]

    agent_id = agent_factory.create_agent(
        spec, db=db, base_dir=base_dir, if_exists=if_exists
    )
    logger.info(
        "agent_templates: created agent '%s' from template '%s'", agent_id, template["id"]
    )
    return agent_id


# ---------------------------------------------------------------------------
# Legacy skillset compatibility views
#
# ``routes/skills.py`` serves the pre-template ``/api/skillsets*`` surface.  It
# reads through the functions below (this module owns the legacy root) and then
# adapts the payloads back to the exact legacy response shape, so the old UI
# (``templates/skills.html``, ``templates/agents.html``,
# ``templates/edit_skillset.html``) keeps working unchanged.
#
# These views deliberately reproduce ``backend.skillsets`` semantics for the
# legacy root only: they never look at ``agent_templates/`` and they never
# write, move or validate the legacy files.
# ---------------------------------------------------------------------------

def legacy_skillsets(*, base_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return the raw payload of every legacy ``skillsets/*.json`` file.

    Files are returned in legacy file order (``sorted(os.listdir())``) so the
    adapter in ``routes/skills.py`` reproduces the legacy listing byte for byte.
    A file that cannot be read as a JSON *object* (unparseable, not UTF-8, not
    an object, or oversized) is skipped, exactly like the legacy loader skipped
    the files it could not parse.  Legacy files are never modified.
    """
    _canonical_root, legacy_root = _roots(base_dir)
    payloads: List[Dict[str, Any]] = []
    for file_name in _scan_dir(legacy_root):
        try:
            payloads.append(_read_json_object(os.path.join(legacy_root, file_name)))
        except TemplateError:
            continue
    return payloads


def get_legacy_skillset(
    template_id: str,
    *,
    base_dir: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return one raw legacy skillset payload, or ``None`` when absent.

    Lookup matches the *declared* ``id`` field, which is what the legacy
    ``backend.skillsets.get_skillset`` did (a file whose name and declared id
    disagree is matched by its declared id, not by its file name).
    """
    if not isinstance(template_id, str) or not template_id:
        return None
    for payload in legacy_skillsets(base_dir=base_dir):
        if payload.get("id") == template_id:
            return payload
    return None


def resolve_legacy_skillset(
    template_id: str,
    *,
    base_dir: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return a raw legacy payload plus ``resolved_tools``/``unresolved_tools``.

    Mirrors ``backend.skillsets.resolve_skillset``: declared tool names the
    registry knows are reported as *resolved*, unknown ones as *unresolved*, and
    when dependency checking is impossible (registry unavailable) every declared
    name is reported as resolved rather than wrongly reported as missing.
    """
    payload = get_legacy_skillset(template_id, base_dir=base_dir)
    if payload is None:
        return None

    declared = payload.get("tools") or []
    if not isinstance(declared, (list, tuple)):
        declared = []
    names = [name for name in declared if isinstance(name, str) and name]

    available = _available_tool_ids()
    resolved = (
        list(names) if available is None
        else [name for name in names if name in available]
    )
    result = dict(payload)
    result["resolved_tools"] = resolved
    result["unresolved_tools"] = [name for name in names if name not in resolved]
    return result


def build_legacy_skillset_spec(
    template_id: str,
    agent_data: Optional[Mapping[str, Any]],
    *,
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Merge a legacy skillset with ``agent_data`` into an agent-factory spec.

    This is the legacy ``backend.skillsets.apply_skillset`` merge — ``agent_data``
    wins over the skillset, the identity fields always come from ``agent_data`` —
    expressed as a spec that :func:`backend.agent_factory.create_agent` accepts:

    * the skillset's ``model`` (and a caller-supplied ``model``/``model_id``)
      becomes the spec's ``model_id``, adapting the legacy key exactly like
      :func:`_adapt_legacy` does for the canonical template path;
    * ``kb_files`` (a ``{path: content}`` object) becomes ``knowledge_base`` and
      goes through the same path validation as a template's KB files;
    * keys the factory does not model are ignored, which is what the legacy
      route did when it handed the merge result to ``Database.create_agent``.

    Raises :class:`TemplateNotFoundError` when the skillset does not exist and
    :class:`TemplateValidationError` when the merge produces an unusable spec.
    """
    payload = get_legacy_skillset(template_id, base_dir=base_dir)
    if payload is None:
        raise TemplateNotFoundError("Skillset '%s' was not found." % template_id)
    if agent_data is None:
        agent_data = {}
    if not isinstance(agent_data, Mapping):
        raise TemplateValidationError("Agent data must be a JSON object.")

    def merged(key: str, default: Any) -> Any:
        return agent_data[key] if key in agent_data else default

    spec: Dict[str, Any] = {
        "id": agent_data.get("id") or "",
        "name": merged("name", payload.get("name") or "") or "",
        "description": merged("description", payload.get("description") or "") or "",
        "system_prompt": merged("system_prompt", payload.get("system_prompt") or "") or "",
        "tools": merged("tools", payload.get("tools") or []),
        "skills": merged("skills", payload.get("skills") or []),
    }

    model = agent_data.get(
        "model", agent_data.get("model_id", payload.get("model") or "")
    )
    if isinstance(model, str) and model.strip():
        spec["model_id"] = model.strip()

    kb_files = _normalize_kb_files(merged("kb_files", payload.get("kb_files") or {}))
    spec["knowledge_base"] = [
        {"path": path, "content": content} for path, content in kb_files.items()
    ]
    return spec
