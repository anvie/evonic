"""
Agent-template HTTP layer.

Exposes :mod:`backend.agent_templates` (the template engine) and
:mod:`backend.agent_runtime.simulation_runtime` (the ephemeral, throwaway
simulation runtime) over HTTP.

Endpoints
---------
Pages
    * ``GET  /templates``            — template gallery (UI owned by T8/T9)
    * ``GET  /template/<template_id>`` — template editor + simulation panel
      (UI owned by T9; an unknown id renders the "create new" editor)

API
    * ``GET    /api/templates``                      — list summaries + collisions
    * ``POST   /api/templates``                      — create (201 + Location)
    * ``GET    /api/templates/<id>``                 — dependency/resolution report
    * ``PUT    /api/templates/<id>``                 — update (top-level shallow merge)
    * ``DELETE /api/templates/<id>``                 — delete
    * ``POST   /api/templates/<id>/render``          — prompt + resolved-config preview
      (NEVER persists anything)
    * ``POST   /api/templates/<id>/simulate``        — ephemeral run through the real
      runtime (auth + per-caller/template rate limit + capped output length)
    * ``POST   /api/templates/<id>/instantiate``     — create a real agent (201 +
      Location; replaying a deterministic caller-supplied id returns 200)

There is deliberately no ``/preview`` endpoint: ``/render`` covers the
"what will this look like" question and ``/simulate`` covers "what will this do".

AuthZ model
-----------
Templates live in one **shared namespace** (``agent_templates/*.json`` plus the
read-only legacy ``skillsets/*.json``) because a template is a repo-level
artifact that plugins, skills and every agent share — there is no per-user
template tree.  *Writing* one is nevertheless **privileged**: a template
declares which tools run, which secret variables are required, and which
workplace/channel an agent is bound to.  Therefore:

* reads  — any authenticated caller;
* writes — privileged callers only (:func:`_require_privileged`, the single
  chokepoint: 401 unauthenticated, 403 authenticated-but-not-privileged).

:data:`PRIVILEGED_CALLERS_ENV` (``EVONIC_TEMPLATE_PRIVILEGED_CALLERS``) is an
optional comma-separated allowlist of caller ids; when it is unset every
authenticated caller is privileged (the single-admin install default).

``workplace_id`` / ``primary_channel_id`` are **instantiation-time inputs only**
— they are never template defaults (the engine's ``defaults`` allowlist already
excludes them, and :func:`_reject_binding_declarations` rejects them explicitly
in parameters/variables/defaults) and they are never read from ``params``.  They
must arrive at the top level of the instantiate request body, where they are
validated against the caller's privileges and the live database before anything
is created.
"""

from __future__ import annotations

import functools
import logging
import math
import os
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, make_response, render_template, request, session
from jinja2 import TemplateNotFound

from backend import agent_templates as tpl
from models.db import db

logger = logging.getLogger(__name__)

templates_bp = Blueprint("templates", __name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Sliding-window rate limit for ``/simulate`` — per (caller, template).
#: A simulation is *free LLM compute*: it runs the real runtime with the
#: template's full tool set, so it is the most expensive request this blueprint
#: exposes (cost + DoS surface).
SIMULATE_RATE_LIMIT = 10
SIMULATE_RATE_WINDOW = 60.0

#: Upper bound on the number of tracked (caller, template) windows before the
#: limiter prunes expired ones.  Keeps the in-memory map bounded.
_SIMULATE_MAX_KEYS = 2048

#: Input caps.  Exceeding these is a 400 (the caller asked for something we
#: will not run), never a silent truncation of the prompt.
MAX_SIMULATE_MESSAGE_CHARS = 8000
MAX_SIMULATE_HISTORY_TURNS = 50
MAX_SIMULATE_TURNS_HISTORY_CHARS = 40000

#: Output caps.  Simulation output is produced by the LLM and by tool calls, so
#: it is capped (and flagged as truncated) before it leaves the process.
MAX_SIMULATE_RESPONSE_CHARS = 8000
MAX_SIMULATE_TRACE_ITEMS = 50
MAX_SIMULATE_OUTBOX_ITEMS = 50

#: Agent columns that are *instantiation-time inputs*: never template defaults,
#: never template parameters, never members of ``params``.
BINDING_KEYS: Tuple[str, ...] = ("workplace_id", "primary_channel_id")

#: Optional deployment knob: comma-separated allowlist of privileged caller ids.
PRIVILEGED_CALLERS_ENV = "EVONIC_TEMPLATE_PRIVILEGED_CALLERS"

#: Conflict policy for the legacy ``skillsets/`` collision on create.  Callers
#: opt in explicitly with ``?allow_shadow=true``; a template never silently
#: shadows a legacy skillset.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class _BadRequest(Exception):
    """Request-level validation failure (mapped to a 400 JSON response)."""


def _bad_request(message: str, code: str = "invalid_request") -> "_BadRequest":
    return _BadRequest(f"{code}\u0000{message}")


def _bad_request_response(exc: _BadRequest) -> Tuple[Any, int]:
    code, _, message = str(exc).partition("\u0000")
    return jsonify({"error": code or "invalid_request", "message": message}), 400


def _template_error_response(exc: tpl.TemplateError) -> Tuple[Any, int]:
    """Map a template-engine exception onto an HTTP status + JSON body."""
    if isinstance(exc, tpl.TemplateNotFoundError):
        return jsonify({"error": "template_not_found", "message": str(exc)}), 404
    if isinstance(exc, tpl.TemplateExistsError):
        return jsonify({"error": "template_exists", "message": str(exc)}), 409
    if isinstance(exc, tpl.TemplateResolveError):
        return jsonify({
            "error": "template_unresolved",
            "message": str(exc),
            "report": getattr(exc, "report", {}) or {},
        }), 422
    if isinstance(exc, tpl.TemplateValidationError):
        # TemplateRenderError subclasses TemplateValidationError.
        return jsonify({"error": "invalid_template", "message": str(exc)}), 400
    return jsonify({"error": "template_error", "message": str(exc)}), 400


def _handles_template_errors(view):
    """Translate template-engine and request failures into JSON responses."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except _BadRequest as exc:
            return _bad_request_response(exc)
        except tpl.TemplateError as exc:
            return _template_error_response(exc)

    return wrapper


# ---------------------------------------------------------------------------
# AuthZ
# ---------------------------------------------------------------------------

def _authenticated() -> bool:
    return bool(session.get("authenticated"))


def _caller_id() -> str:
    """Stable identity of the caller for rate-limit keys and the write allowlist."""
    return str(session.get("_user_id") or "admin")


def _privileged_callers() -> Optional[frozenset]:
    """Return the optional privileged caller allowlist (``None`` = everyone).

    Unset/empty means "every authenticated caller is privileged", which is the
    single-admin install default.  Deployments that front Evonic with more than
    one authenticated identity can narrow template writes to specific callers.
    """
    raw = (os.environ.get(PRIVILEGED_CALLERS_ENV) or "").strip()
    if not raw:
        return None
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _is_privileged() -> bool:
    """Whether the current caller may create/edit/delete templates."""
    if not _authenticated():
        return False
    allowed = _privileged_callers()
    return allowed is None or _caller_id() in allowed


def _require_auth() -> Optional[Tuple[Any, int]]:
    """Return an error response when the caller is not authenticated."""
    if _authenticated():
        return None
    return jsonify({"error": "Authentication required"}), 401


def _require_privileged() -> Optional[Tuple[Any, int]]:
    """Return an error response when the caller may not write templates.

    401 for an unauthenticated caller, 403 for an authenticated caller that is
    not on the privileged allowlist.  Single chokepoint for every write path.
    """
    if not _authenticated():
        return jsonify({"error": "Authentication required"}), 401
    if not _is_privileged():
        return jsonify({
            "error": "forbidden",
            "message": (
                "Creating, editing and deleting agent templates is restricted to "
                "privileged callers (templates declare tools, secrets and "
                "workplace/channel bindings)."
            ),
        }), 403
    return None


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------

def _json_body() -> Dict[str, Any]:
    """Return the request body as a dict (an absent body is an empty dict)."""
    body = request.get_json(silent=True)
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise _bad_request("Request body must be a JSON object.", "invalid_body")
    return body


def _clean_params(body: Dict[str, Any], *, default: Optional[Dict[str, Any]] = None
                  ) -> Dict[str, Any]:
    """Extract and validate the ``params`` block.

    ``workplace_id`` / ``primary_channel_id`` are instantiation-time inputs, not
    template parameters, so they are rejected here even when a caller tries to
    smuggle them through ``params``.
    """
    params = body.get("params", {} if default is None else default)
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise _bad_request("'params' must be an object.", "invalid_params")
    leaked = [key for key in BINDING_KEYS if key in params]
    if leaked:
        raise _bad_request(
            "%s must be passed at the top level of the request body "
            "(instantiation-time input), never inside 'params'."
            % ", ".join("'%s'" % key for key in leaked),
            "binding_in_params",
        )
    return params


def _clean_overrides(body: Dict[str, Any]) -> Dict[str, Any]:
    overrides = body.get("overrides")
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise _bad_request("'overrides' must be an object.", "invalid_overrides")
    return overrides


def _reject_binding_declarations(payload: Any) -> None:
    """Reject a template that tries to bake a workplace/channel binding in.

    Delegated defaults are already excluded by the engine's ``defaults``
    allowlist; this guard makes the rule explicit (and keeps it enforced if the
    allowlist ever widens) for ``defaults``, ``parameters`` and ``variables``.
    """
    if not isinstance(payload, dict):
        return
    defaults = payload.get("defaults")
    if isinstance(defaults, dict):
        baked = [key for key in BINDING_KEYS if key in defaults]
        if baked:
            raise _bad_request(
                "Template 'defaults' cannot set %s — workplace/channel bindings "
                "are instantiation-time inputs supplied by the caller."
                % ", ".join("'%s'" % key for key in baked),
                "binding_in_defaults",
            )
    parameters = payload.get("parameters")
    if isinstance(parameters, (list, tuple)):
        declared = [
            (entry or {}).get("name") for entry in parameters
            if isinstance(entry, dict)
        ]
        clash = sorted({name for name in declared if name in BINDING_KEYS})
        if clash:
            raise _bad_request(
                "Template parameters cannot be named %s — those are "
                "instantiation-time inputs, not prompt parameters."
                % ", ".join("'%s'" % name for name in clash),
                "binding_in_parameters",
            )
    variables = payload.get("variables")
    if isinstance(variables, (list, tuple)):
        declared = [
            (entry or {}).get("key") for entry in variables
            if isinstance(entry, dict)
        ]
        clash = sorted({name for name in declared if name in BINDING_KEYS})
        if clash:
            raise _bad_request(
                "Template variables cannot be named %s — those are "
                "instantiation-time inputs, not secret variables."
                % ", ".join("'%s'" % name for name in clash),
                "binding_in_variables",
            )


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in _TRUE_VALUES


# ---------------------------------------------------------------------------
# /simulate rate limiting (in-memory sliding window, per caller + template)
# ---------------------------------------------------------------------------

_simulate_hits: Dict[str, deque] = {}
_simulate_lock = threading.Lock()


def _monotonic() -> float:
    """Indirection so tests can drive the clock."""
    return time.monotonic()


def reset_simulate_rate_limits() -> None:
    """Drop every tracked window (used by tests and by operators on demand)."""
    with _simulate_lock:
        _simulate_hits.clear()


def _prune_simulate_windows(now: float, window: float) -> None:
    if len(_simulate_hits) < _SIMULATE_MAX_KEYS:
        return
    cutoff = now - window
    for key in [k for k, hits in _simulate_hits.items()
                if not hits or hits[-1] <= cutoff]:
        _simulate_hits.pop(key, None)


def _check_simulate_rate_limit(key: str, *, limit: Optional[int] = None,
                               window: Optional[float] = None
                               ) -> Tuple[bool, int, int]:
    """Consume one slot of ``key``'s window.

    Returns ``(allowed, remaining, retry_after_seconds)``.  ``limit``/``window``
    default to the module tunables *at call time* so tests can monkeypatch them.
    """
    limit = SIMULATE_RATE_LIMIT if limit is None else limit
    window = SIMULATE_RATE_WINDOW if window is None else window
    now = _monotonic()
    with _simulate_lock:
        _prune_simulate_windows(now, window)
        hits = _simulate_hits.setdefault(key, deque())
        cutoff = now - window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= limit:
            retry_after = max(1, int(math.ceil(hits[0] + window - now)))
            return False, 0, retry_after
        hits.append(now)
        return True, limit - len(hits), 0


def _rate_limit_response(payload: Dict[str, Any], limit: int, remaining: int,
                         retry_after: int, window: float) -> Any:
    body = dict(payload)
    response = make_response(jsonify(body), 429)
    response.headers["Retry-After"] = str(retry_after)
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Reset"] = str(
        int(time.time() + (retry_after or window))
    )
    return response


# ---------------------------------------------------------------------------
# Output caps
# ---------------------------------------------------------------------------

def _cap_text(value: Any, limit: int) -> Tuple[Any, bool]:
    if not isinstance(value, str) or len(value) <= limit:
        return value, False
    return value[:limit], True


def _cap_items(value: Any, limit: int) -> Tuple[Any, bool]:
    if not isinstance(value, (list, tuple)) or len(value) <= limit:
        return value, False
    return list(value[:limit]), True


def _cap_simulation_output(result: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Cap every unbounded field of a simulation result and report what was cut."""
    capped: List[str] = []
    payload = dict(result or {})

    payload["response"], truncated = _cap_text(
        payload.get("response"), MAX_SIMULATE_RESPONSE_CHARS)
    if truncated:
        capped.append("response")
    payload["tool_trace"], truncated = _cap_items(
        payload.get("tool_trace"), MAX_SIMULATE_TRACE_ITEMS)
    if truncated:
        capped.append("tool_trace")
    payload["timeline"], truncated = _cap_items(
        payload.get("timeline"), MAX_SIMULATE_TRACE_ITEMS)
    if truncated:
        capped.append("timeline")
    payload["outbox"], truncated = _cap_items(
        payload.get("outbox"), MAX_SIMULATE_OUTBOX_ITEMS)
    if truncated:
        capped.append("outbox")
    return payload, capped


# ---------------------------------------------------------------------------
# Simulation / instantiation inputs
# ---------------------------------------------------------------------------

def _load_simulation_runtime():
    """Import the simulation runtime lazily (it is an optional dependency)."""
    try:
        from backend.agent_runtime import simulation_runtime

        return simulation_runtime
    except Exception:  # noqa: BLE001 - report, never crash the read paths
        logger.warning(
            "templates: simulation runtime unavailable", exc_info=True)
        return None


def _simulation_history(body: Dict[str, Any]) -> List[Dict[str, str]]:
    """Build the message history for a simulation run (input-cap validated)."""
    messages = body.get("messages")
    if messages is None:
        message = body.get("message")
        if message is None:
            message = ""
        if not isinstance(message, str):
            raise _bad_request("'message' must be a string.", "invalid_message")
        messages = [{"role": "user", "content": message}]
    if not isinstance(messages, (list, tuple)):
        raise _bad_request("'messages' must be a list.", "invalid_messages")
    if len(messages) > MAX_SIMULATE_HISTORY_TURNS:
        raise _bad_request(
            "'messages' is too long (%d turns; max %d)."
            % (len(messages), MAX_SIMULATE_HISTORY_TURNS),
            "message_history_too_long",
        )

    history: List[Dict[str, str]] = []
    total = 0
    for entry in messages:
        if not isinstance(entry, dict):
            raise _bad_request(
                "Each entry of 'messages' must be an object.", "invalid_messages")
        role = entry.get("role") or "user"
        content = entry.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            raise _bad_request(
                "Each entry of 'messages' needs string 'role' and 'content'.",
                "invalid_messages",
            )
        if len(content) > MAX_SIMULATE_MESSAGE_CHARS:
            raise _bad_request(
                "A simulation message is too long (%d characters; max %d)."
                % (len(content), MAX_SIMULATE_MESSAGE_CHARS),
                "message_too_long",
            )
        total += len(content)
        if total > MAX_SIMULATE_TURNS_HISTORY_CHARS:
            raise _bad_request(
                "Simulation history is too large (%d characters; max %d)."
                % (total, MAX_SIMULATE_TURNS_HISTORY_CHARS),
                "message_history_too_large",
            )
        history.append({"role": role, "content": content})
    return history


def _extract_bindings(body: Dict[str, Any]) -> Dict[str, str]:
    """Pull the instantiation-time workplace/channel bindings off the body."""
    bindings: Dict[str, str] = {}
    for key in BINDING_KEYS:
        value = body.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or not value.strip():
            raise _bad_request(
                "'%s' must be a non-empty string." % key, "invalid_binding")
        bindings[key] = value.strip()
    return bindings


def _sandbox_policy_updates(workplace_id: Optional[str]) -> Dict[str, Any]:
    """Column updates implied by a workplace binding (sandbox policy).

    Mirrors ``routes/agents.py:_apply_sandbox_workplace_policy``: docker sandbox
    is only supported on local workplaces.  Raises ``ValueError`` when the host
    cannot support the workplace's isolation.
    """
    if not workplace_id:
        return {}
    candidate: Dict[str, Any] = {}
    try:
        from routes.agents import _apply_sandbox_workplace_policy as policy
    except Exception:  # noqa: BLE001 - policy helper is optional
        logger.warning(
            "templates: sandbox workplace policy helper unavailable", exc_info=True)
        return candidate
    policy(candidate, workplace_id)
    return candidate


def _validate_bindings(bindings: Dict[str, str], *,
                       target_agent_id: Optional[str]) -> Optional[Tuple[Any, int]]:
    """AuthZ-check workplace/channel bindings *before* anything is created."""
    if not bindings:
        return None
    if not _is_privileged():
        return jsonify({
            "error": "forbidden",
            "message": (
                "Binding a workplace or a primary channel requires a privileged "
                "caller: a binding decides where an agent executes and which "
                "channel it owns."
            ),
        }), 403
    workplace_id = bindings.get("workplace_id")
    if workplace_id and not db.get_workplace(workplace_id):
        return jsonify({
            "error": "unknown_workplace",
            "message": "Workplace '%s' does not exist." % workplace_id,
        }), 400

    channel_id = bindings.get("primary_channel_id")
    if channel_id:
        if not target_agent_id:
            return jsonify({
                "error": "channel_binding_requires_agent_id",
                "message": (
                    "'primary_channel_id' can only be bound when the caller also "
                    "supplies the deterministic agent 'id', so the channel's "
                    "owner can be verified."
                ),
            }), 400
        channel = db.get_channel(channel_id)
        if not channel:
            return jsonify({
                "error": "unknown_channel",
                "message": "Channel '%s' does not exist." % channel_id,
            }), 400
        if channel.get("agent_id") != target_agent_id:
            return jsonify({
                "error": "channel_not_owned",
                "message": (
                    "Channel '%s' belongs to agent '%s', not to '%s'. A primary "
                    "channel can only be bound to the agent that owns it."
                    % (channel_id, channel.get("agent_id"), target_agent_id)
                ),
            }), 403
    return None


def _apply_bindings(agent_id: str, bindings: Dict[str, str],
                    policy_updates: Dict[str, Any]) -> Dict[str, Any]:
    """Persist the (already validated) workplace/channel bindings."""
    applied: Dict[str, Any] = {}
    workplace_id = bindings.get("workplace_id")
    if workplace_id:
        updates: Dict[str, Any] = {"workplace_id": workplace_id}
        if "sandbox_enabled" in policy_updates:
            updates["sandbox_enabled"] = policy_updates["sandbox_enabled"]
        db.update_agent(agent_id, updates)
        applied["workplace_id"] = workplace_id
        if "sandbox_enabled" in updates:
            applied["sandbox_enabled"] = updates["sandbox_enabled"]

    channel_id = bindings.get("primary_channel_id")
    if channel_id:
        db.set_primary_channel(agent_id, channel_id)
        applied["primary_channel_id"] = channel_id
    return applied


# ---------------------------------------------------------------------------
# Page routes (UI owned by T8/T9)
# ---------------------------------------------------------------------------

def _ui_page(filename: str, **context: Any) -> Any:
    """Render a UI template, degrading gracefully until T8/T9 ship it.

    The blueprint owns the *routes*; the templates are owned by the UI tasks.
    Until they land, a self-describing placeholder is served so the routes (and
    their auth behaviour) are exercisable end to end.
    """
    try:
        return render_template(filename, **context)
    except TemplateNotFound:
        logger.warning(
            "templates: UI template '%s' is not installed yet; serving placeholder",
            filename,
        )
        return _placeholder_page(filename)


def _placeholder_page(filename: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>Agent templates</title></head><body>"
        "<h1>Agent templates</h1>"
        "<p>The template API is live at <code>/api/templates</code>.</p>"
        "<p>The gallery/editor UI (<code>%s</code>) is delivered by the UI tasks; "
        "this placeholder is shown until it is installed.</p>"
        "</body></html>" % filename
    )


@templates_bp.route("/templates")
def templates_page():
    """Template gallery page (client renders from ``GET /api/templates``)."""
    return _ui_page("templates.html")


@templates_bp.route("/template/<template_id>")
def template_editor_page(template_id: str):
    """Template editor page with the simulation panel.

    An unknown id is not a 404: the editor doubles as the "create new" surface,
    so it receives ``template=None`` and lets the UI decide.
    """
    report: Optional[Dict[str, Any]] = None
    try:
        report = tpl.resolve_template(template_id)
    except tpl.TemplateNotFoundError:
        report = None
    except tpl.TemplateError as exc:
        logger.warning("templates: cannot open editor for '%s': %s", template_id, exc)
        report = None
    return _ui_page("edit_template.html", template_id=template_id, template=report)


# ---------------------------------------------------------------------------
# API — read
# ---------------------------------------------------------------------------

@templates_bp.route("/api/templates", methods=["GET"])
@_handles_template_errors
def api_list_templates():
    """List every template summary plus id collisions (never silently shadowed)."""
    denied = _require_auth()
    if denied:
        return denied

    include_legacy = not (
        request.args.get("include_legacy") or "true"
    ).strip().lower() in {"0", "false", "no", "off"}
    summaries = tpl.list_templates(include_legacy=include_legacy)
    collisions = tpl.list_collisions() if include_legacy else []
    return jsonify({
        "templates": summaries,
        "collisions": collisions,
        "count": len([entry for entry in summaries if not entry.get("shadowed")]),
    })


@templates_bp.route("/api/templates/<template_id>", methods=["GET"])
@_handles_template_errors
def api_get_template(template_id: str):
    """Full template plus its dependency/resolution report (editor payload)."""
    denied = _require_auth()
    if denied:
        return denied

    report = tpl.resolve_template(template_id)
    report["privileged"] = _is_privileged()
    return jsonify(report)


# ---------------------------------------------------------------------------
# API — writes (privileged)
# ---------------------------------------------------------------------------

@templates_bp.route("/api/templates", methods=["POST"])
@_handles_template_errors
def api_create_template():
    """Create a canonical template (201 + Location)."""
    denied = _require_privileged()
    if denied:
        return denied

    payload = _json_body()
    _reject_binding_declarations(payload)
    allow_shadow = _truthy(request.args.get("allow_shadow"))

    template = tpl.create_template(payload, allow_shadow=allow_shadow)
    response = make_response(jsonify({"template": template}), 201)
    response.headers["Location"] = "/api/templates/%s" % template["id"]
    return response


@templates_bp.route("/api/templates/<template_id>", methods=["PUT"])
@_handles_template_errors
def api_update_template(template_id: str):
    """Update a canonical template (top-level shallow merge)."""
    denied = _require_privileged()
    if denied:
        return denied

    payload = _json_body()
    _reject_binding_declarations(payload)
    template = tpl.update_template(template_id, payload)
    return jsonify({"template": template})


@templates_bp.route("/api/templates/<template_id>", methods=["DELETE"])
@_handles_template_errors
def api_delete_template(template_id: str):
    """Delete a canonical template. Legacy skillsets are never deleted."""
    denied = _require_privileged()
    if denied:
        return denied

    deleted = tpl.delete_template(template_id)
    return jsonify({"id": template_id, "deleted": bool(deleted)})


# ---------------------------------------------------------------------------
# API — render (preview only, never persists)
# ---------------------------------------------------------------------------

@templates_bp.route("/api/templates/<template_id>/render", methods=["POST"])
@_handles_template_errors
def api_render_template(template_id: str):
    """Render the system prompt + resolved configuration. Persists nothing.

    This is a pure read: no agent row, no template mutation, no file write.  The
    rendered body carries ``persisted: false`` so a client can assert that.
    """
    denied = _require_auth()
    if denied:
        return denied

    try:
        body = _json_body()
        params = _clean_params(body)
    except _BadRequest as exc:
        return _bad_request_response(exc)

    preview = tpl.preview_template(template_id, params, strict=False)
    resolution = tpl.resolve_template(template_id)
    return jsonify({
        "template_id": preview["id"],
        "name": preview["name"],
        "description": preview["description"],
        "source": preview.get("source"),
        "writable": preview.get("writable"),
        "system_prompt": preview["system_prompt"],
        "system_prompt_length": preview["system_prompt_length"],
        "values": preview["values"],
        "spec_preview": preview["spec_preview"],
        "warnings": preview["warnings"],
        "resolution": {
            "ok": resolution["ok"],
            "deps_checked": resolution["deps_checked"],
            "tools": resolution["tools"],
            "parameters": resolution["parameters"],
            "variables": resolution["variables"],
            "warnings": resolution["warnings"],
            "errors": resolution["errors"],
        },
        "persisted": False,
    })


# ---------------------------------------------------------------------------
# API — simulate (ephemeral, rate-limited, capped)
# ---------------------------------------------------------------------------

@templates_bp.route("/api/templates/<template_id>/simulate", methods=["POST"])
def api_simulate_template(template_id: str):
    """Run the template in a throwaway simulation and return the turn result.

    Auth + per-(caller, template) rate limit + output caps all live here; the
    runtime itself guarantees teardown and containment.

    Workplace/channel bindings are rejected: a simulation *always* runs in its
    own throwaway sandbox, so there is nothing a real workplace could add — and
    accepting one would defeat the containment guarantee.
    """
    denied = _require_auth()
    if denied:
        return denied

    try:
        body = _json_body()
        params = _clean_params(body)
        overrides = _clean_overrides(body)
        bindings = _extract_bindings(body)
        history = _simulation_history(body)
    except _BadRequest as exc:
        return _bad_request_response(exc)

    if bindings:
        return jsonify({
            "error": "binding_not_applicable",
            "message": (
                "Simulations run in a throwaway sandbox, so %s cannot be applied. "
                "Bind a workplace/channel when instantiating the template instead."
                % ", ".join("'%s'" % key for key in sorted(bindings))
            ),
        }), 400

    limit_key = "%s:%s" % (_caller_id(), template_id)
    allowed, remaining, retry_after = _check_simulate_rate_limit(limit_key)
    if not allowed:
        logger.warning(
            "templates: simulate rate limit hit for %s", limit_key)
        return _rate_limit_response({
            "error": "rate_limit_exceeded",
            "message": (
                "Simulation rate limit exceeded for this template "
                "(%d runs per %ds). Try again in %ds."
                % (SIMULATE_RATE_LIMIT, int(SIMULATE_RATE_WINDOW), retry_after)
            ),
            "retry_after": retry_after,
        }, SIMULATE_RATE_LIMIT, 0, retry_after, SIMULATE_RATE_WINDOW)

    runtime = _load_simulation_runtime()
    if runtime is None:
        return jsonify({
            "error": "simulation_unavailable",
            "message": "The simulation runtime is not available in this deployment.",
        }), 503

    result = runtime.simulate(
        template_id,
        params=params,
        message_history=history,
        overrides=overrides,
        external_user_id="template-sim:%s" % _caller_id(),
    )
    payload, capped_fields = _cap_simulation_output(result)
    payload["truncated"] = bool(capped_fields)
    if capped_fields:
        payload["truncated_fields"] = capped_fields
    payload["rate_limit"] = {
        "limit": SIMULATE_RATE_LIMIT,
        "remaining": remaining,
        "window": int(SIMULATE_RATE_WINDOW),
    }

    response = make_response(jsonify(payload), 200)
    response.headers["X-RateLimit-Limit"] = str(SIMULATE_RATE_LIMIT)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Reset"] = str(
        int(time.time() + SIMULATE_RATE_WINDOW))
    return response


# ---------------------------------------------------------------------------
# API — instantiate (creates a real agent; idempotent on a caller-supplied id)
# ---------------------------------------------------------------------------

@templates_bp.route("/api/templates/<template_id>/instantiate", methods=["POST"])
@_handles_template_errors
def api_instantiate_template(template_id: str):
    """Create a real agent from a template (201 + Location).

    Idempotency: when the caller supplies a deterministic agent ``id``, replaying
    the request returns ``200`` with the *existing* agent and creates nothing.
    Without a caller-supplied id the agent id is derived by the factory.

    The template is the privileged artifact (writing one is privileged), so
    instantiating an existing one only needs an authenticated caller.  A
    ``workplace_id`` / ``primary_channel_id`` binding is itself privileged and is
    authZ-checked before anything is created.
    """
    denied = _require_auth()
    if denied:
        return denied

    try:
        body = _json_body()
        params = _clean_params(body)
        overrides = _clean_overrides(body)
        bindings = _extract_bindings(body)
    except _BadRequest as exc:
        return _bad_request_response(exc)

    deterministic_id = body.get("id") or overrides.get("id")
    if deterministic_id is not None:
        if not isinstance(deterministic_id, str) or not deterministic_id.strip():
            return _bad_request_response(
                _bad_request("'id' must be a non-empty string.", "invalid_agent_id"))
        deterministic_id = deterministic_id.strip() or None
    if deterministic_id and "id" not in overrides:
        # A top-level ``id`` *is* the deterministic agent id (the idempotency
        # key): hand it to the factory so the caller-supplied id is what gets
        # created, instead of only being used for the replay comparison.
        overrides = {**overrides, "id": deterministic_id}

    # --- pre-flight: bindings are authZ-checked before anything is created ---
    try:
        policy_updates = _sandbox_policy_updates(bindings.get("workplace_id"))
    except ValueError as exc:
        return _bad_request_response(_bad_request(str(exc), "workplace_incompatible"))
    invalid = _validate_bindings(bindings, target_agent_id=deterministic_id)
    if invalid:
        return invalid

    # --- idempotent replay: the deterministic id already exists ---------------
    if deterministic_id:
        existing = db.get_agent(deterministic_id)
        if existing:
            return _instantiate_response(
                agent_id=deterministic_id, template_id=template_id,
                replayed=True, bindings={}, status=200)

    try:
        agent_id = tpl.create_agent_from_template(
            template_id,
            params,
            overrides,
            if_exists="error" if deterministic_id else None,
        )
    except tpl.TemplateError:
        raise
    except _AgentAlreadyExists as exc:
        # Only reachable with if_exists='error', i.e. when the caller supplied a
        # deterministic id: we lost a race against a concurrent identical
        # request, so replay it exactly like the pre-flight check would have.
        if deterministic_id and db.get_agent(deterministic_id):
            logger.info(
                "templates: instantiate replay for existing agent '%s'",
                deterministic_id)
            return _instantiate_response(
                agent_id=deterministic_id, template_id=template_id,
                replayed=True, bindings={}, status=200)
        logger.warning("templates: agent id allocation failed: %s", exc)
        return jsonify({
            "error": "agent_already_exists",
            "message": str(exc),
        }), 409

    applied = _apply_bindings(agent_id, bindings, policy_updates)
    logger.info(
        "templates: instantiated agent '%s' from template '%s'%s",
        agent_id, template_id, " with bindings" if applied else "")
    return _instantiate_response(
        agent_id=agent_id, template_id=template_id,
        replayed=False, bindings=applied, status=201)


def _instantiate_response(*, agent_id: str, template_id: str, replayed: bool,
                          bindings: Dict[str, Any], status: int) -> Any:
    response = make_response(jsonify({
        "agent_id": agent_id,
        "template_id": template_id,
        "replayed": replayed,
        "bindings": bindings,
        "location": "/api/agents/%s" % agent_id,
    }), status)
    response.headers["Location"] = "/api/agents/%s" % agent_id
    return response


# ``AgentAlreadyExistsError`` identifies the *only* way the instantiate path can
# lose a race: the PRIMARY KEY insert of a caller-supplied deterministic id.
# Imported defensively so an incomplete factory layer degrades to a 409 instead
# of an ImportError at request time.
try:
    from backend.agent_factory import AgentAlreadyExistsError as _AgentAlreadyExists
except Exception:  # noqa: BLE001 - replay path must stay reachable
    class _AgentAlreadyExists(Exception):  # type: ignore[no-redef]
        """Fallback so the instantiate replay path stays reachable."""
