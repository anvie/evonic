"""
Panel Plugin — Flask Route Handlers

Provides:
- GET  /plugin/panel/<agent_id>/render       → HTML panel UI with action buttons
- GET  /plugin/panel/<agent_id>/actions      → JSON list of enabled actions
- POST /plugin/panel/<agent_id>/execute/<int:action_id> → Execute action (script/prompt)
- GET  /plugin/panel/<agent_id>/execution/<execution_id>/status → Poll execution status
- GET  /plugin/panel/<agent_id>/log          → Execution log entries

Security:
- Routes use /plugin/panel/ prefix (CSRF-exempt in app.py)
- Requesting user must be authenticated (session-based)
- Scripts run as the agent's run_as_user (via sudo), or as the system
  default user (the server's own process user) when run_as_user is unset
- Per-agent mutex prevents concurrent script execution
"""

import json
import os
import re
import shlex
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, session

from plugins.panel.db import panel_db_factory, validate_slash_command

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# In-memory execution state
# ---------------------------------------------------------------------------

# Per-agent locks to prevent concurrent script execution
_agent_locks: dict = {}
_locks_lock = threading.Lock()

# Execution output buffers: execution_id → {"status", "output_lines": [...], "exit_code", "started_at"}
_execution_buffers: dict = {}
_buffers_lock = threading.Lock()


def _get_agent_lock(agent_id: str) -> threading.Lock:
    """Return a per-agent threading.Lock, creating it if needed."""
    with _locks_lock:
        if agent_id not in _agent_locks:
            _agent_locks[agent_id] = threading.Lock()
        return _agent_locks[agent_id]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).isoformat()


def _get_plugin_config():
    """Return the panel plugin config dict."""
    try:
        from backend.plugin_manager import plugin_manager
        return plugin_manager.get_plugin_config("panel")
    except Exception:
        return {}


def _get_max_timeout():
    """Return MAX_SCRIPT_TIMEOUT from plugin config, default 60."""
    cfg = _get_plugin_config()
    try:
        return int(cfg.get("MAX_SCRIPT_TIMEOUT", 60))
    except (TypeError, ValueError):
        return 60


def _check_agent_access(agent_id: str):
    """
    Validate the requesting user can access this agent.

    Returns (agent_dict, error_tuple).
    error_tuple is (json_body, status_code) or None if OK.
    """
    if not session.get("authenticated"):
        return None, ({"error": "Authentication required"}, 401)

    try:
        from models.db import db
        agent = db.get_agent(agent_id)
    except Exception as e:
        return None, ({"error": f"Database error: {str(e)}"}, 500)

    if not agent:
        return None, ({"error": "Agent not found"}, 404)

    return agent, None


def _emit_panel_updated(agent_id: str) -> None:
    """Best-effort realtime nudge so every open Panel tab re-renders.

    Mirrors the panel skill tools (panel_add_action / update / remove): the
    Panel tab in agent_detail.html listens for 'panel_updated' and calls
    refreshPluginTab('panel'). Failures here must never break the response.
    """
    try:
        from backend.event_stream import event_stream
        event_stream.emit("panel_updated", {"agent_id": agent_id})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Blueprint factory
# ---------------------------------------------------------------------------

def create_blueprint():
    bp = Blueprint("panel", __name__, url_prefix="/plugin/panel")

    # ── GET /plugin/panel/<agent_id>/render ──────────────────────────────

    @bp.route("/<agent_id>/render")
    def panel_render(agent_id: str):
        """Return the interactive panel HTML with action buttons grid."""
        agent, error = _check_agent_access(agent_id)
        if error:
            return jsonify(error[0]), error[1]

        db = panel_db_factory(agent_id)
        actions = db.list_actions()

        # Build ETag from the latest updated_at timestamp
        max_updated = max(
            (a.get("updated_at", "") for a in actions), default=""
        )
        etag = f'"{hash(max_updated or agent_id)}"'

        # Check If-None-Match for 304
        if_none_match = request.headers.get("If-None-Match", "")
        if if_none_match and if_none_match == etag:
            return "", 304

        # Build the HTML
        html = _render_panel_html(agent_id, agent, actions)

        from flask import make_response as flask_make_response
        response = flask_make_response(html, 200)
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "no-cache"
        return response

    # ── GET /plugin/panel/<agent_id>/actions ─────────────────────────────

    @bp.route("/<agent_id>/actions")
    def panel_actions(agent_id: str):
        """Return JSON list of enabled actions."""
        agent, error = _check_agent_access(agent_id)
        if error:
            return jsonify(error[0]), error[1]

        db = panel_db_factory(agent_id)
        actions = db.list_actions()

        return jsonify({"actions": actions})

    # ── PATCH|PUT /plugin/panel/<agent_id>/actions/<int:action_id> ───────────
    # Used by the Panel tab's edit mode to rename a button / set its slash command.

    @bp.route("/<agent_id>/actions/<int:action_id>", methods=["PATCH", "PUT"])
    def panel_update_action(agent_id: str, action_id: int):
        """Edit a panel action button (label and slash command only)."""
        agent, error = _check_agent_access(agent_id)
        if error:
            return jsonify(error[0]), error[1]

        db = panel_db_factory(agent_id)
        if not db.get_action(action_id):
            return jsonify({"error": "Action not found"}), 404

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "A JSON object body is required"}), 400

        fields = {}
        if "label" in payload:
            raw_label = payload.get("label")
            label = raw_label.strip() if isinstance(raw_label, str) else ""
            if not label:
                return jsonify({"error": "Label cannot be empty"}), 400
            if len(label) > 120:
                return jsonify({"error": "Label is too long (max 120 characters)"}), 400
            fields["label"] = label

        if "slash_command" in payload:
            raw_cmd = payload.get("slash_command")
            if raw_cmd is not None and not isinstance(raw_cmd, str):
                return jsonify({"error": "slash_command must be a string"}), 400
            # Empty string clears the assignment.
            cmd, cmd_error = validate_slash_command(agent_id, raw_cmd or "", exclude_action_id=action_id)
            if cmd_error:
                return jsonify({"error": cmd_error}), 400
            fields["slash_command"] = cmd

        if not fields:
            return jsonify({"error": "No editable fields provided"}), 400

        try:
            updated = db.update_action(action_id, **fields)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        if not updated:
            return jsonify({"error": "Action not found"}), 404

        _emit_panel_updated(agent_id)
        return jsonify({"status": "updated", "action": updated})

    # ── DELETE /plugin/panel/<agent_id>/actions/<int:action_id> ──────────────
    # Used by the Panel tab's edit mode to remove a button.

    @bp.route("/<agent_id>/actions/<int:action_id>", methods=["DELETE"])
    def panel_delete_action(agent_id: str, action_id: int):
        """Delete a panel action button, scoped to this agent."""
        agent, error = _check_agent_access(agent_id)
        if error:
            return jsonify(error[0]), error[1]

        db = panel_db_factory(agent_id)
        if not db.delete_action(action_id):
            return jsonify({"error": "Action not found"}), 404

        _emit_panel_updated(agent_id)
        return jsonify({"status": "deleted", "action_id": action_id})

    # ── POST /plugin/panel/<agent_id>/execute/<int:action_id> ────────────

    @bp.route("/<agent_id>/execute/<int:action_id>", methods=["POST"])
    def panel_execute(agent_id: str, action_id: int):
        """Execute a panel action (script or prompt)."""
        agent, error = _check_agent_access(agent_id)
        if error:
            return jsonify(error[0]), error[1]

        db = panel_db_factory(agent_id)
        action = db.get_action(action_id)

        if not action:
            return jsonify({"error": "Action not found"}), 404

        if not action.get("enabled"):
            return jsonify({"error": "Action is disabled"}), 400

        action_type = action["action_type"]

        # ── Prompt action ────────────────────────────────────────────

        if action_type == "prompt":
            try:
                _send_prompt_to_agent(agent_id, action)
            except Exception as e:
                return jsonify({"error": f"Failed to send prompt: {str(e)}"}), 500

            db.log_execution(
                action_id=action_id,
                action_label=action["label"],
                action_type=action_type,
                params_used=None,
                result="Prompt sent to agent session",
                status="success",
            )

            return jsonify({"status": "prompt_sent"})

        # ── Script action ────────────────────────────────────────────

        if action_type == "script":
            # Check params
            params_def = _parse_params(action.get("params"))
            if params_def:
                provided_params = request.get_json(silent=True) or {}
                if "params" in provided_params:
                    provided_params = provided_params["params"]

                missing = [p for p in params_def if p["name"] not in provided_params]
                if missing:
                    return jsonify({
                        "needs_params": True,
                        "params": params_def,
                    }), 200

                user_params = provided_params
            else:
                user_params = {}

            execution_id, err = start_script_execution(agent_id, agent, action, user_params)
            if err == "busy":
                return jsonify({
                    "error": "A script is already running for this agent",
                    "status": "busy",
                }), 409

            return jsonify({
                "status": "queued",
                "execution_id": execution_id,
            }), 202

        return jsonify({"error": f"Unknown action type: {action_type}"}), 400

    # ── GET /plugin/panel/<agent_id>/execution/<execution_id>/status ────

    @bp.route("/<agent_id>/execution/<execution_id>/status")
    def panel_execution_status(agent_id: str, execution_id: str):
        """Poll execution status and output."""
        agent, error = _check_agent_access(agent_id)
        if error:
            return jsonify(error[0]), error[1]

        with _buffers_lock:
            buf = _execution_buffers.get(execution_id)

        if not buf:
            return jsonify({"error": "Execution not found"}), 404

        return jsonify({
            "status": buf["status"],
            "output_lines": list(buf["output_lines"]),
            "exit_code": buf["exit_code"],
        })

    # ── GET /plugin/panel/<agent_id>/log ─────────────────────────────────

    @bp.route("/<agent_id>/log")
    def panel_log(agent_id: str):
        """Return execution log entries from SQLite."""
        agent, error = _check_agent_access(agent_id)
        if error:
            return jsonify(error[0]), error[1]

        db = panel_db_factory(agent_id)
        try:
            log_entries = db.get_log()
        except Exception:
            log_entries = []

        return jsonify({"log": log_entries})

    return bp


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

# Inline icons for the per-button edit controls (stroke inherits currentColor).
_PENCIL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>'
)
_TRASH_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<polyline points="3 6 5 6 21 6"/>'
    '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
    '<line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>'
)


def _render_panel_html(agent_id: str, agent: dict, actions: list) -> str:
    """Build the panel UI as an HTML body fragment.

    Returned fragment is injected into the Panel tab of agent_detail.html via
    innerHTML (scripts are re-created by setPluginTabHTML so they execute).
    Must NOT be a full document and must NOT load external scripts.
    """

    # This JSON is embedded verbatim inside a <script> block, so escape the
    # characters that could close it (a label like "</script>" would otherwise
    # break out). \u003c etc. parse back to the same characters in JS.
    actions_json = (
        json.dumps(actions)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    agent_name = agent.get("name", agent_id)

    panel_css = """<style>
  .pnl-wrap { display:grid; grid-template-columns:minmax(240px,320px) 1fr; gap:1rem; padding:1rem; align-items:start; }
  .pnl-left { display:flex; flex-direction:column; gap:.75rem; min-width:0; }
  .pnl-title { font-size:1.05rem; font-weight:700; margin:0; }
  .pnl-title span { color:#818cf8; }
  .pnl-search-wrap { position:relative; }
  .pnl-search-wrap > svg { position:absolute; left:.625rem; top:50%; transform:translateY(-50%); width:1rem; height:1rem; color:#9ca3af; pointer-events:none; }
  .pnl-search { width:100%; box-sizing:border-box; padding:.5rem .75rem .5rem 2rem; border-radius:.5rem; border:1px solid #d1d5db; background:#fff; color:#111827; font-size:.8125rem; }
  .pnl-search:focus { outline:none; border-color:#6366f1; box-shadow:0 0 0 3px rgba(99,102,241,.15); }
  .pnl-btn-grid { display:grid; grid-template-columns:1fr; gap:.5rem; }
  .panel-btn { display:flex; align-items:center; gap:.5rem; color:#fff; font-weight:500; font-size:.8125rem; padding:.625rem .75rem; border-radius:.5rem; border:none; cursor:pointer; text-align:left; width:100%; min-width:0; box-sizing:border-box; transition:background-color .15s, transform .05s; }
  .panel-btn:active { transform:translateY(1px); }
  .panel-btn .pnl-dot { width:.5rem; height:.5rem; border-radius:9999px; flex:0 0 auto; background:rgba(255,255,255,.75); }
  .panel-btn .pnl-hour { display:none; flex:0 0 auto; font-size:.8125rem; line-height:1; }
  .panel-btn.btn-running { opacity:.65; cursor:wait; }
  .panel-btn.btn-running .pnl-dot { display:none; }
  .panel-btn.btn-running .pnl-hour { display:inline-block; animation:pnl-hourspin 1.5s ease-in-out infinite; }
  @keyframes pnl-hourspin { 0%,20% { transform:rotate(0deg); } 50%,70% { transform:rotate(180deg); } 100% { transform:rotate(360deg); } }
  .panel-btn .pnl-label { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .btn-script { background-color:#4f46e5; }
  .btn-script:hover { background-color:#4338ca; }
  .btn-prompt { background-color:#059669; }
  .btn-prompt:hover { background-color:#047857; }
  .btn-disabled { opacity:.5; cursor:not-allowed; }
  .pnl-hidden { display:none !important; }
  .pnl-empty { color:#6b7280; font-size:.8125rem; padding:1rem; text-align:center; border:1px dashed #d1d5db; border-radius:.5rem; }
  .panel-btn .pnl-cmd { margin-left:auto; flex:0 0 auto; font-size:.6875rem; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; padding:.0625rem .375rem; border-radius:.25rem; background:rgba(255,255,255,.18); }
  .pnl-btn-grid .pnl-empty { grid-column:1 / -1; }
  .pnl-term { display:flex; flex-direction:column; background:#0f172a; border:1px solid #1e293b; border-radius:.75rem; overflow:hidden; height:72vh; min-height:360px; }
  .pnl-term-head { display:flex; align-items:center; justify-content:space-between; gap:.5rem; padding:.5rem .75rem; background:#1e293b; border-bottom:1px solid #334155; }
  .pnl-term-head-left { display:flex; align-items:center; gap:.5rem; }
  .pnl-term-dots { display:flex; gap:.375rem; }
  .pnl-term-dots i { width:.6rem; height:.6rem; border-radius:9999px; display:block; }
  .pnl-term-title { color:#94a3b8; font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.06em; }
  .pnl-badge { font-size:.68rem; padding:.125rem .5rem; border-radius:9999px; font-weight:600; color:#fff; white-space:nowrap; }
  .pnl-badge.idle { background:#475569; }
  .pnl-badge.running { background:#ca8a04; }
  .pnl-badge.done { background:#059669; }
  .pnl-badge.error { background:#dc2626; }
  .pnl-clear { font-size:.68rem; color:#94a3b8; background:transparent; border:1px solid #334155; border-radius:.375rem; padding:.2rem .5rem; cursor:pointer; white-space:nowrap; }
  .pnl-clear:hover { color:#e2e8f0; border-color:#475569; }
  .pnl-run { position:relative; }
  .pnl-run + .pnl-run { border-top:1px solid #1e293b; margin-top:.35rem; padding-top:.35rem; }
  .pnl-run-fwd { position:absolute; top:0; right:0; opacity:0; transition:opacity .12s; font-size:.66rem; color:#cbd5e1; background:#1e293b; border:1px solid #334155; border-radius:.375rem; padding:.1rem .45rem; cursor:pointer; z-index:1; white-space:nowrap; }
  .pnl-run:hover .pnl-run-fwd { opacity:1; }
  .pnl-run-fwd:hover { color:#fff; border-color:#475569; }
  .pnl-term-body { flex:1; overflow-y:auto; padding:.75rem 1rem; margin:0; font-family:'Fira Code','Cascadia Code','Consolas',Menlo,Monaco,monospace; font-size:12.5px; line-height:1.5; color:#e2e8f0; white-space:pre-wrap; word-break:break-word; }
  .pnl-cmd { color:#818cf8; font-weight:600; }
  .pnl-cmd-src { color:#a5b4fc; opacity:.85; }
  .pnl-muted { color:#64748b; }
  .pnl-ok { color:#34d399; }
  .pnl-err { color:#f87171; }
  .pnl-modal { position:fixed; inset:0; background:rgba(0,0,0,.6); display:flex; align-items:center; justify-content:center; z-index:50; padding:1rem; }
  .pnl-modal-card { background:#1e293b; border:1px solid #334155; border-radius:.75rem; padding:1.25rem; width:100%; max-width:28rem; box-sizing:border-box; }
  .pnl-modal-title { color:#f1f5f9; font-size:1rem; font-weight:600; margin:0 0 1rem; }
  .pnl-field { margin-bottom:.75rem; }
  .pnl-field label { display:block; color:#94a3b8; font-size:.8125rem; margin-bottom:.25rem; }
  .pnl-field input { width:100%; box-sizing:border-box; background:#0f172a; border:1px solid #334155; border-radius:.5rem; padding:.5rem .75rem; color:#f1f5f9; font-size:.8125rem; }
  .pnl-field input:focus { outline:none; border-color:#6366f1; }
  .pnl-modal-actions { display:flex; justify-content:flex-end; gap:.5rem; margin-top:.5rem; }
  .pnl-btn-sec, .pnl-btn-pri { padding:.5rem 1rem; border-radius:.5rem; font-size:.8125rem; font-weight:500; cursor:pointer; border:none; color:#fff; }
  .pnl-btn-sec { background:#475569; }
  .pnl-btn-sec:hover { background:#334155; }
  .pnl-btn-pri { background:#4f46e5; }
  .pnl-btn-pri:hover { background:#4338ca; }
  /* ── Edit mode ─────────────────────────────────────────────────────────── */
  .pnl-head { display:flex; align-items:center; justify-content:space-between; gap:.5rem; }
  .pnl-edit-toggle { display:inline-flex; align-items:center; gap:.3rem; flex:0 0 auto; font-size:.72rem; font-weight:600; color:#4338ca; background:#eef2ff; border:1px solid #c7d2fe; border-radius:.5rem; padding:.3rem .55rem; cursor:pointer; white-space:nowrap; }
  .pnl-edit-toggle:hover { background:#e0e7ff; }
  .pnl-edit-toggle:focus-visible { outline:2px solid #6366f1; outline-offset:1px; }
  .pnl-edit-toggle svg { width:.85rem; height:.85rem; }
  .pnl-edit-toggle.is-active { background:#4f46e5; border-color:#4f46e5; color:#fff; }
  .pnl-edit-toggle.is-active:hover { background:#4338ca; }
  .pnl-edit-hint { margin:0; font-size:.72rem; line-height:1.4; color:#6b7280; }
  .dark .pnl-edit-toggle { color:#c7d2fe; background:#312e81; border-color:#4338ca; }
  .dark .pnl-edit-toggle:hover { background:#3730a3; }
  .dark .pnl-edit-toggle.is-active { background:#4f46e5; border-color:#6366f1; color:#fff; }
  .dark .pnl-edit-hint { color:#9ca3af; }
  .pnl-item { position:relative; min-width:0; }
  .pnl-item-tools { display:none; position:absolute; top:50%; right:.3rem; transform:translateY(-50%); align-items:center; gap:.15rem; padding:.12rem .2rem; border-radius:.5rem; background:rgba(255,255,255,.95); box-shadow:0 1px 4px rgba(15,23,42,.3); }
  .pnl-edit-mode .pnl-item-tools { display:flex; }
  /* In edit mode the action button itself is inert: no pointer events, no
     focus, and marked aria-disabled so assistive tech announces it. */
  .pnl-edit-mode .panel-btn { pointer-events:none; padding-right:4.5rem; }
  .pnl-tool { display:inline-flex; align-items:center; justify-content:center; width:1.5rem; height:1.5rem; padding:0; border:none; border-radius:.375rem; background:transparent; color:#4b5563; cursor:pointer; }
  .pnl-tool:hover { background:#e5e7eb; }
  .pnl-tool:focus-visible { outline:2px solid #6366f1; outline-offset:1px; }
  .pnl-tool svg { width:.9rem; height:.9rem; }
  .pnl-tool-edit:hover { color:#4338ca; }
  .pnl-tool-del:hover { color:#b91c1c; background:#fee2e2; }
  .pnl-toast { position:fixed; left:50%; bottom:1.25rem; transform:translateX(-50%); background:#1e293b; color:#f8fafc; font-size:.8rem; padding:.5rem .9rem; border-radius:.5rem; border:1px solid #334155; box-shadow:0 8px 24px rgba(15,23,42,.45); z-index:60; }
  .pnl-toast.pnl-toast-err { background:#7f1d1d; border-color:#b91c1c; }
  .dark .pnl-search { background:#374151; border-color:#4b5563; color:#f3f4f6; }
  .dark .pnl-empty { border-color:#4b5563; color:#9ca3af; }
  @media (max-width:1023px) {
    .pnl-wrap { grid-template-columns:1fr; }
    .pnl-term { height:60vh; }
  }
  @media (max-width:640px) {
    .pnl-btn-grid { grid-template-columns:1fr; }
  }
</style>
"""

    html = panel_css + f"""<div class="pnl-wrap">
  <div class="pnl-left">
    <div class="pnl-head">
      <h2 class="pnl-title">Panel — <span>{_escape_html(agent_name)}</span></h2>
      <button id="pnl-edit-toggle" class="pnl-edit-toggle" type="button" aria-pressed="false" title="Edit panel buttons">{_PENCIL_SVG}<span id="pnl-edit-toggle-label">Edit</span></button>
    </div>
    <p id="pnl-edit-hint" class="pnl-edit-hint pnl-hidden">Edit mode: buttons are paused. Use the pencil to edit or the trash to remove a button, then click Done.</p>
    <div class="pnl-search-wrap">
      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="m21 21-4.3-4.3m1.8-4.7a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0Z"/></svg>
      <input id="pnl-search" class="pnl-search" type="search" placeholder="Filter buttons..." autocomplete="off">
    </div>
    <div id="pnl-btn-grid" class="pnl-btn-grid">
"""

    for action in actions:
        label = _escape_html(action["label"])
        label_lower = _escape_html((action["label"] or "").lower())
        aid = action["id"]
        atype = action["action_type"]
        enabled = action.get("enabled", True)

        color_class = "btn-script" if atype == "script" else "btn-prompt"

        disabled_attr = ""
        disabled_class = ""
        if not enabled:
            disabled_attr = "disabled"
            disabled_class = " btn-disabled"

        slash_command = (action.get("slash_command") or "").strip()
        cmd_badge = ""
        if slash_command:
            cmd = _escape_html(slash_command)
            label_lower = f"{label_lower} /{_escape_html(slash_command.lower())}"
            cmd_badge = f'<span class="pnl-cmd" title="Run from chat with /{cmd}">/{cmd}</span>'

        html += f"""      <div class="pnl-item" data-action-id="{aid}">
        <button class="panel-btn {color_class}{disabled_class}" data-action-id="{aid}" data-action-type="{atype}" data-label="{label}" data-search="{label_lower}" {disabled_attr}><span class="pnl-dot"></span><span class="pnl-hour">&#8987;</span><span class="pnl-label">{label}</span>{cmd_badge}</button>
        <div class="pnl-item-tools">
          <button class="pnl-tool pnl-tool-edit" type="button" data-action-id="{aid}" title="Edit button" aria-label="Edit {label}">{_PENCIL_SVG}</button>
          <button class="pnl-tool pnl-tool-del" type="button" data-action-id="{aid}" title="Delete button" aria-label="Delete {label}">{_TRASH_SVG}</button>
        </div>
      </div>
"""

    if not actions:
        html += '      <div class="pnl-empty">No actions yet. This agent has not added any panel buttons.</div>\n'

    html += """    </div>
    <div id="pnl-nomatch" class="pnl-empty pnl-hidden">No buttons match your search.</div>
  </div>

  <div class="pnl-term">
    <div class="pnl-term-head">
      <div class="pnl-term-head-left">
        <span class="pnl-term-dots"><i style="background:#ef4444"></i><i style="background:#eab308"></i><i style="background:#22c55e"></i></span>
        <span class="pnl-term-title">Output</span>
        <span id="pnl-badge" class="pnl-badge idle">idle</span>
      </div>
      <button id="pnl-clear" class="pnl-clear" type="button">Clear</button>
    </div>
    <div id="pnl-term-body" class="pnl-term-body"><span class="pnl-muted"># Ready — click a button to run it.</span></div>
  </div>
</div>

<div id="pnl-modal" class="pnl-modal pnl-hidden">
  <div class="pnl-modal-card">
    <h3 id="pnl-modal-title" class="pnl-modal-title">Enter Parameters</h3>
    <div id="pnl-modal-body"></div>
    <div class="pnl-modal-actions">
      <button id="pnl-cancel" class="pnl-btn-sec" type="button">Cancel</button>
      <button id="pnl-submit" class="pnl-btn-pri" type="button">Execute</button>
    </div>
  </div>
</div>

<div id="pnl-rename-modal" class="pnl-modal pnl-hidden">
  <div class="pnl-modal-card" role="dialog" aria-modal="true" aria-labelledby="pnl-rename-title">
    <h3 id="pnl-rename-title" class="pnl-modal-title">Edit button</h3>
    <div class="pnl-field">
      <label for="pnl-rename-input">Button label</label>
      <input id="pnl-rename-input" type="text" maxlength="120" autocomplete="off">
    </div>
    <div class="pnl-field">
      <label for="pnl-cmd-input">Slash command (optional)</label>
      <input id="pnl-cmd-input" type="text" maxlength="33" autocomplete="off" placeholder="e.g. deploy — leave empty for none">
    </div>
    <div class="pnl-modal-actions">
      <button id="pnl-rename-cancel" class="pnl-btn-sec" type="button">Cancel</button>
      <button id="pnl-rename-save" class="pnl-btn-pri" type="button">Save</button>
    </div>
  </div>
</div>
"""

    panel_script = """<script>
(function() {
  'use strict';

  const AGENT_ID = '__AGENT_ID__';
  const actions = __ACTIONS_JSON__;

  let currentExecutionId = null;
  let pollTimer = null;
  let pendingAction = null;
  let termFresh = true;
  let currentRunBody = null;
  let editMode = false;

  const termBody = document.getElementById('pnl-term-body');
  const badge = document.getElementById('pnl-badge');

  function termShow() {
    if (termFresh) { termBody.innerHTML = ''; termFresh = false; }
  }

  function termAppend(text, cls) {
    let target = currentRunBody;
    if (!target) { termShow(); target = termBody; }
    const span = document.createElement('span');
    if (cls) span.className = cls;
    span.textContent = text;
    target.appendChild(span);
    termBody.scrollTop = termBody.scrollHeight;
  }

  // Begin a new output session (one per action run): a hoverable block with its
  // own "→ Chat" forward button and a command-echo header. Subsequent output is
  // appended into this block via currentRunBody.
  function startRun(label) {
    termShow();
    const run = document.createElement('div');
    run.className = 'pnl-run';
    const fwd = document.createElement('button');
    fwd.className = 'pnl-run-fwd';
    fwd.type = 'button';
    fwd.title = 'Quote this output into the Chat tab to ask the agent about it';
    fwd.innerHTML = '&#8594; Chat';
    const body = document.createElement('div');
    body.className = 'pnl-run-body';
    fwd.addEventListener('click', function(e) {
      e.stopPropagation();
      forwardToChat(body.textContent || '');
    });
    run.appendChild(fwd);
    run.appendChild(body);
    termBody.appendChild(run);
    currentRunBody = body;
    const cmd = document.createElement('span');
    cmd.className = 'pnl-cmd';
    cmd.textContent = '$ ' + label + '\\n';
    body.appendChild(cmd);
    termBody.scrollTop = termBody.scrollHeight;
  }

  // Quote the given output into the chat composer and switch to the Chat tab.
  function forwardToChat(rawText) {
    const text = (rawText || '').trim();
    if (!text) return;
    const quoted = text.split('\\n').map(function(l) { return '> ' + l; }).join('\\n');
    const message = quoted + '\\n\\n';
    if (typeof window.showTab === 'function') window.showTab('chat');
    const input = document.getElementById('chat-input');
    if (input) {
      input.value = message;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.focus();
      try { input.setSelectionRange(input.value.length, input.value.length); } catch (e) {}
      input.scrollTop = input.scrollHeight;
    }
  }

  // ── ANSI (SGR) color rendering ────────────────────────────────────
  const ANSI_16 = [
    '#1e293b','#f87171','#34d399','#fbbf24','#60a5fa','#c084fc','#22d3ee','#e5e7eb',
    '#94a3b8','#fca5a5','#86efac','#fde047','#93c5fd','#d8b4fe','#67e8f9','#ffffff'
  ];
  let ansi = null;
  function ansiReset() { ansi = { fg:null, bg:null, bold:false, dim:false, italic:false, underline:false, inverse:false }; }
  ansiReset();

  function ansiStyle() {
    let fg = ansi.fg, bg = ansi.bg;
    if (ansi.inverse) { const t = fg || '#e2e8f0'; fg = bg || '#0f172a'; bg = t; }
    let s = '';
    if (fg) s += 'color:' + fg + ';';
    if (bg) s += 'background:' + bg + ';';
    if (ansi.bold) s += 'font-weight:700;';
    if (ansi.dim) s += 'opacity:.7;';
    if (ansi.italic) s += 'font-style:italic;';
    if (ansi.underline) s += 'text-decoration:underline;';
    return s;
  }

  function xterm256(n) {
    n = n | 0;
    if (n < 16) return ANSI_16[n];
    if (n >= 232) { const v = 8 + (n - 232) * 10; return 'rgb(' + v + ',' + v + ',' + v + ')'; }
    n -= 16;
    const conv = function(x) { return x === 0 ? 0 : 55 + x * 40; };
    return 'rgb(' + conv(Math.floor(n / 36)) + ',' + conv(Math.floor((n % 36) / 6)) + ',' + conv(n % 6) + ')';
  }

  function ansiApplySgr(codes) {
    for (let i = 0; i < codes.length; i++) {
      const c = codes[i];
      if (c === 0) ansiReset();
      else if (c === 1) ansi.bold = true;
      else if (c === 2) ansi.dim = true;
      else if (c === 3) ansi.italic = true;
      else if (c === 4) ansi.underline = true;
      else if (c === 7) ansi.inverse = true;
      else if (c === 22) { ansi.bold = false; ansi.dim = false; }
      else if (c === 23) ansi.italic = false;
      else if (c === 24) ansi.underline = false;
      else if (c === 27) ansi.inverse = false;
      else if (c === 39) ansi.fg = null;
      else if (c === 49) ansi.bg = null;
      else if (c >= 30 && c <= 37) ansi.fg = ANSI_16[c - 30];
      else if (c >= 90 && c <= 97) ansi.fg = ANSI_16[c - 90 + 8];
      else if (c >= 40 && c <= 47) ansi.bg = ANSI_16[c - 40];
      else if (c >= 100 && c <= 107) ansi.bg = ANSI_16[c - 100 + 8];
      else if (c === 38 || c === 48) {
        const isFg = c === 38, mode = codes[i + 1];
        if (mode === 5) { const col = xterm256(codes[i + 2]); if (isFg) ansi.fg = col; else ansi.bg = col; i += 2; }
        else if (mode === 2) { const col = 'rgb(' + (codes[i+2]||0) + ',' + (codes[i+3]||0) + ',' + (codes[i+4]||0) + ')'; if (isFg) ansi.fg = col; else ansi.bg = col; i += 4; }
      }
    }
  }

  const ANSI_RE = /\\x1b\\[([0-9;]*)([A-Za-z])/g;
  function ansiAppend(text) {
    let target = currentRunBody;
    if (!target) { termShow(); target = termBody; }
    ANSI_RE.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0, m;
    const emit = function(str) {
      if (!str) return;
      const span = document.createElement('span');
      const st = ansiStyle();
      if (st) span.setAttribute('style', st);
      span.textContent = str;
      frag.appendChild(span);
    };
    while ((m = ANSI_RE.exec(text)) !== null) {
      emit(text.slice(last, m.index));
      last = ANSI_RE.lastIndex;
      if (m[2] === 'm') {
        const codes = m[1] === '' ? [0] : m[1].split(';').map(function(x) { return parseInt(x, 10) || 0; });
        ansiApplySgr(codes);
      }
    }
    emit(text.slice(last));
    target.appendChild(frag);
    termBody.scrollTop = termBody.scrollHeight;
  }

  function setBadge(state, text) {
    badge.className = 'pnl-badge ' + state;
    badge.textContent = text;
  }

  let loadingBtn = null;

  function setBtnLoading(actionId) {
    loadingBtn = document.querySelector('.panel-btn[data-action-id="' + actionId + '"]');
    if (loadingBtn) loadingBtn.classList.add('btn-running');
  }

  function setButtonsDisabled(state) {
    document.querySelectorAll('.panel-btn').forEach(function(b) {
      // While edit mode is on the buttons must stay inert even after a run.
      if (!b.classList.contains('btn-disabled')) b.disabled = state || editMode;
    });
    if (!state && loadingBtn) {
      loadingBtn.classList.remove('btn-running');
      loadingBtn = null;
    }
  }

  // ── Search filter ─────────────────────────────────────────────────
  const searchInput = document.getElementById('pnl-search');
  const btnGrid = document.getElementById('pnl-btn-grid');
  const noMatch = document.getElementById('pnl-nomatch');
  if (searchInput) {
    searchInput.addEventListener('input', function() {
      const q = searchInput.value.trim().toLowerCase();
      const items = btnGrid.querySelectorAll('.pnl-item');
      let visible = 0;
      items.forEach(function(item) {
        const btn = item.querySelector('.panel-btn');
        const match = !q || ((btn && btn.dataset.search) || '').indexOf(q) !== -1;
        item.classList.toggle('pnl-hidden', !match);
        if (match) visible++;
      });
      noMatch.classList.toggle('pnl-hidden', !(items.length > 0 && visible === 0));
    });
  }

  // ── Clear terminal ────────────────────────────────────────────────
  const clearBtn = document.getElementById('pnl-clear');
  if (clearBtn) {
    clearBtn.addEventListener('click', function() {
      termBody.innerHTML = '<span class="pnl-muted"># Ready — click a button to run it.</span>';
      termFresh = true;
      currentRunBody = null;
      ansiReset();
      setBadge('idle', 'idle');
    });
  }

  // ── Edit mode ────────────────────────────────────────────────────────────
  // Entering edit mode turns every action button into an inert element: it is
  // disabled, taken out of the pointer/tab path and marked aria-disabled, so
  // clicking it can no longer run the action. In exchange, each button gains a
  // pencil (rename) and a trash (delete) control. The mode is remembered in
  // sessionStorage so an automatic panel re-render (the 'panel_updated'
  // realtime event) does not drop the user back out of it.
  const EDIT_STORE_KEY = 'pnl-edit-mode:' + AGENT_ID;
  const editToggle = document.getElementById('pnl-edit-toggle');
  const editToggleLabel = document.getElementById('pnl-edit-toggle-label');
  const editHint = document.getElementById('pnl-edit-hint');

  function setEditMode(on) {
    editMode = !!on;
    btnGrid.classList.toggle('pnl-edit-mode', editMode);
    if (editToggle) {
      editToggle.classList.toggle('is-active', editMode);
      editToggle.setAttribute('aria-pressed', editMode ? 'true' : 'false');
    }
    if (editToggleLabel) editToggleLabel.textContent = editMode ? 'Done' : 'Edit';
    if (editHint) editHint.classList.toggle('pnl-hidden', !editMode);
    document.querySelectorAll('.panel-btn').forEach(function(btn) {
      if (editMode) {
        if (btn.dataset.pnlWasDisabled === undefined) {
          btn.dataset.pnlWasDisabled = btn.disabled ? '1' : '0';
        }
        btn.disabled = true;
        btn.setAttribute('aria-disabled', 'true');
      } else {
        const wasDisabled = btn.dataset.pnlWasDisabled === '1';
        btn.disabled = wasDisabled;
        if (!wasDisabled) btn.removeAttribute('aria-disabled');
        delete btn.dataset.pnlWasDisabled;
      }
    });
    try { sessionStorage.setItem(EDIT_STORE_KEY, editMode ? '1' : '0'); } catch (e) {}
  }

  if (editToggle) {
    editToggle.addEventListener('click', function() { setEditMode(!editMode); });
  }

  // Small transient message used by the rename/delete flows.
  function editToast(message, isError) {
    const el = document.createElement('div');
    el.className = 'pnl-toast' + (isError ? ' pnl-toast-err' : '');
    el.setAttribute('role', 'status');
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(function() { el.remove(); }, 2600);
  }

  function refreshGridEmptyState() {
    if (btnGrid.querySelectorAll('.panel-btn').length > 0) return;
    noMatch.classList.add('pnl-hidden');
    if (!btnGrid.querySelector('.pnl-empty')) {
      const empty = document.createElement('div');
      empty.className = 'pnl-empty';
      empty.textContent = 'No actions yet. This agent has not added any panel buttons.';
      btnGrid.appendChild(empty);
    }
  }

  const renameModal = document.getElementById('pnl-rename-modal');
  const renameInput = document.getElementById('pnl-rename-input');
  const cmdInput = document.getElementById('pnl-cmd-input');
  let renameTargetId = null;

  function openRenameModal(actionId) {
    const action = actions.find(function(a) { return a.id === actionId; });
    if (!action || !renameModal) return;
    renameTargetId = actionId;
    renameInput.value = action.label || '';
    cmdInput.value = action.slash_command || '';
    renameModal.classList.remove('pnl-hidden');
    renameInput.focus();
    renameInput.select();
  }

  function closeRenameModal() {
    renameTargetId = null;
    if (renameModal) renameModal.classList.add('pnl-hidden');
  }

  // Keep the freshly renamed button (label, filter index, tool tips) in sync
  // without waiting for the realtime re-render.
  function syncButtonLabel(actionId, label, slashCommand) {
    const btn = btnGrid.querySelector('.panel-btn[data-action-id="' + actionId + '"]');
    if (!btn) return;
    btn.dataset.label = label;
    const action = actions.find(function(a) { return a.id === actionId; });
    const cmd = slashCommand ? String(slashCommand).trim().toLowerCase() : '';
    btn.dataset.search = cmd ? (label.toLowerCase() + ' /' + cmd) : label.toLowerCase();
    const labelEl = btn.querySelector('.pnl-label');
    if (labelEl) labelEl.textContent = label;
    let badge = btn.querySelector('.pnl-cmd');
    if (cmd && !badge) {
      badge = document.createElement('span');
      badge.className = 'pnl-cmd';
      btn.appendChild(badge);
    }
    if (badge) {
      if (cmd) { badge.textContent = '/' + cmd; badge.title = 'Run from chat with /' + cmd; }
      else badge.remove();
    }
    const item = btn.closest('.pnl-item');
    if (item) {
      item.querySelectorAll('.pnl-tool').forEach(function(tool) {
        const verb = tool.classList.contains('pnl-tool-edit') ? 'Edit ' : 'Delete ';
        tool.setAttribute('aria-label', verb + label);
      });
    }
  }

  async function saveRename() {
    if (renameTargetId === null) return;
    const actionId = renameTargetId;
    const label = (renameInput.value || '').trim();
    const slashCommand = (cmdInput.value || '').trim().replace(/^\\/+/, '').toLowerCase();
    const action = actions.find(function(a) { return a.id === actionId; });

    if (!label) {
      editToast('Button label cannot be empty.', true);
      renameInput.focus();
      return;
    }
    if (action && label === action.label && slashCommand === (action.slash_command || '')) { closeRenameModal(); return; }

    const saveBtn = document.getElementById('pnl-rename-save');
    if (saveBtn) saveBtn.disabled = true;
    try {
      const resp = await fetch('/plugin/panel/' + AGENT_ID + '/actions/' + actionId, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: label, slash_command: slashCommand })
      });
      const data = await resp.json().catch(function() { return {}; });
      if (!resp.ok) {
        editToast(data.error || 'Could not update this button.', true);
        return;
      }
      if (action) {
        action.label = label;
        action.slash_command = slashCommand;
      }
      syncButtonLabel(actionId, label, slashCommand);
      closeRenameModal();
      editToast('Button updated.');
    } catch (err) {
      editToast('Network error: ' + err.message, true);
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  async function removeAction(actionId) {
    const action = actions.find(function(a) { return a.id === actionId; });
    const label = action ? action.label : ('action ' + actionId);
    let ok = false;
    if (window.ui && window.ui.confirm) {
      ok = await window.ui.confirm({
        title: 'Delete button',
        message: 'Remove "' + label + '" from this panel? This cannot be undone.',
        confirmText: 'Delete',
        danger: true
      });
    } else {
      ok = window.confirm('Remove "' + label + '" from this panel?');
    }
    if (!ok) return;

    try {
      const resp = await fetch('/plugin/panel/' + AGENT_ID + '/actions/' + actionId, { method: 'DELETE' });
      const data = await resp.json().catch(function() { return {}; });
      if (!resp.ok) {
        editToast(data.error || 'Could not delete this button.', true);
        return;
      }
      const item = btnGrid.querySelector('.pnl-item[data-action-id="' + actionId + '"]');
      if (item) item.remove();
      for (let i = actions.length - 1; i >= 0; i--) {
        if (actions[i].id === actionId) actions.splice(i, 1);
      }
      refreshGridEmptyState();
      editToast('Button removed.');
    } catch (err) {
      editToast('Network error: ' + err.message, true);
    }
  }

  // The pencil/trash controls are delegated from the grid so they keep working
  // for buttons added by a later re-render.
  if (btnGrid) {
    btnGrid.addEventListener('click', function(e) {
      if (!editMode || !e.target.closest) return;
      const editTool = e.target.closest('.pnl-tool-edit');
      if (editTool) {
        e.preventDefault();
        e.stopPropagation();
        openRenameModal(parseInt(editTool.dataset.actionId, 10));
        return;
      }
      const delTool = e.target.closest('.pnl-tool-del');
      if (delTool) {
        e.preventDefault();
        e.stopPropagation();
        removeAction(parseInt(delTool.dataset.actionId, 10));
      }
    });
  }

  if (renameModal) {
    document.getElementById('pnl-rename-cancel').addEventListener('click', closeRenameModal);
    document.getElementById('pnl-rename-save').addEventListener('click', saveRename);
    renameModal.addEventListener('click', function(e) { if (e.target === renameModal) closeRenameModal(); });
    [renameInput, cmdInput].forEach(function(input) {
      input.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { e.preventDefault(); saveRename(); }
        else if (e.key === 'Escape') { e.preventDefault(); closeRenameModal(); }
      });
    });
  }

  // ── Button click handler ──────────────────────────────────────────
  document.querySelectorAll('.panel-btn:not([disabled])').forEach(function(btn) {
    btn.addEventListener('click', async function() {
      if (editMode) return; // edit mode: buttons are inert, only the tools act
      const actionId = parseInt(btn.dataset.actionId);
      const actionType = btn.dataset.actionType;
      const action = actions.find(function(a) { return a.id === actionId; });
      if (!action) return;

      if (actionType === 'prompt') {
        executeAction(actionId, null);
        return;
      }

      // Confirm dialog for destructive operations
      if (action.confirm_dialog === 1 && window.ui && window.ui.confirm) {
        const ok = await window.ui.confirm({
          title: action.label || 'Confirm',
          message: 'Are you sure you want to run this action?',
          confirmText: 'Run',
          danger: true
        });
        if (!ok) return;
      }

      let paramsDef = [];
      if (action.params) {
        try {
          paramsDef = typeof action.params === 'string' ? JSON.parse(action.params) : action.params;
        } catch (e) { paramsDef = []; }
      }
      if (paramsDef && paramsDef.length > 0) {
        showParamsModal(actionId, action.label, paramsDef);
        return;
      }
      executeAction(actionId, {});
    });
  });

  // ── Params modal ──────────────────────────────────────────────────
  function showParamsModal(actionId, label, paramsDef) {
    pendingAction = { actionId: actionId, paramsDef: paramsDef };
    document.getElementById('pnl-modal-title').textContent = label;
    const body = document.getElementById('pnl-modal-body');
    body.innerHTML = '';
    paramsDef.forEach(function(p) {
      const field = document.createElement('div');
      field.className = 'pnl-field';
      const lab = document.createElement('label');
      lab.textContent = p.label || p.name;
      const inp = document.createElement('input');
      inp.type = p.type === 'password' ? 'password' : (p.type === 'number' ? 'number' : 'text');
      inp.name = 'param_' + p.name;
      if (p.default) inp.value = p.default;
      field.appendChild(lab);
      field.appendChild(inp);
      body.appendChild(field);
    });
    document.getElementById('pnl-modal').classList.remove('pnl-hidden');
  }

  document.getElementById('pnl-cancel').addEventListener('click', function() {
    document.getElementById('pnl-modal').classList.add('pnl-hidden');
    pendingAction = null;
  });

  document.getElementById('pnl-submit').addEventListener('click', function() {
    if (!pendingAction) return;
    const params = {};
    pendingAction.paramsDef.forEach(function(p) {
      const input = document.querySelector('input[name="param_' + p.name + '"]');
      if (input) params[p.name] = input.value;
    });
    document.getElementById('pnl-modal').classList.add('pnl-hidden');
    executeAction(pendingAction.actionId, params);
    pendingAction = null;
  });

  // ── Execute action ────────────────────────────────────────────────
  async function executeAction(actionId, params) {
    const action = actions.find(function(a) { return a.id === actionId; });
    const label = action ? action.label : ('action ' + actionId);

    setButtonsDisabled(true);
    setBtnLoading(actionId);
    ansiReset();
    startRun(label);
    if (action && action.action_type === 'script' && action.content) {
      let cmdText = action.content;
      if (params) {
        let paramsDef = [];
        try {
          paramsDef = typeof action.params === 'string' ? JSON.parse(action.params) : (action.params || []);
        } catch (e) { paramsDef = []; }
        Object.keys(params).forEach(function(k) {
          const def = paramsDef.find(function(p) { return p.name === k; });
          const shown = def && def.type === 'password' ? '********' : String(params[k]);
          cmdText = cmdText.split('{{' + k + '}}').join(shown);
        });
      }
      termAppend(cmdText.replace(/\\s+$/, '') + '\\n', 'pnl-cmd-src');
    }
    setBadge('running', 'running');

    try {
      const body = params !== null ? JSON.stringify({ params: params }) : JSON.stringify({});
      const resp = await fetch('/plugin/panel/' + AGENT_ID + '/execute/' + actionId, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body
      });
      const data = await resp.json();

      if (data.needs_params) {
        termAppend('Error: parameters required but not provided.\\n', 'pnl-err');
        setBadge('error', 'error');
        setButtonsDisabled(false);
        return;
      }
      if (resp.status === 409) {
        termAppend((data.error || 'A script is already running for this agent.') + '\\n', 'pnl-err');
        setBadge('error', 'busy');
        setButtonsDisabled(false);
        return;
      }
      if (data.status === 'prompt_sent') {
        termAppend('✓ Prompt sent to agent session.\\n', 'pnl-ok');
        setBadge('done', 'sent');
        setButtonsDisabled(false);
        return;
      }
      if (data.status === 'queued' && data.execution_id) {
        currentExecutionId = data.execution_id;
        pollExecution(data.execution_id);
        return;
      }
      termAppend(JSON.stringify(data, null, 2) + '\\n', 'pnl-muted');
      setBadge('done', 'done');
      setButtonsDisabled(false);
    } catch (err) {
      termAppend('Network error: ' + err.message + '\\n', 'pnl-err');
      setBadge('error', 'error');
      setButtonsDisabled(false);
    }
  }

  // ── Poll execution status ─────────────────────────────────────────
  async function pollExecution(executionId) {
    if (pollTimer) clearInterval(pollTimer);
    let seenLines = 0;

    pollTimer = setInterval(async function() {
      try {
        const resp = await fetch('/plugin/panel/' + AGENT_ID + '/execution/' + executionId + '/status');
        if (!resp.ok) {
          clearInterval(pollTimer);
          termAppend('Error polling execution status.\\n', 'pnl-err');
          setBadge('error', 'error');
          setButtonsDisabled(false);
          return;
        }
        const data = await resp.json();

        if (data.output_lines && data.output_lines.length > seenLines) {
          ansiAppend(data.output_lines.slice(seenLines).join(''));
          seenLines = data.output_lines.length;
        }

        if (data.status === 'completed') {
          clearInterval(pollTimer);
          termAppend('\\n[completed · exit ' + data.exit_code + ']\\n', 'pnl-muted');
          setBadge('done', 'completed (' + data.exit_code + ')');
          setButtonsDisabled(false);
        } else if (data.status === 'error') {
          clearInterval(pollTimer);
          termAppend('\\n[error · exit ' + data.exit_code + ']\\n', 'pnl-err');
          setBadge('error', 'error (' + data.exit_code + ')');
          setButtonsDisabled(false);
        }
      } catch (err) {
        clearInterval(pollTimer);
        termAppend('\\n[polling error: ' + err.message + ']\\n', 'pnl-err');
        setBadge('error', 'error');
        setButtonsDisabled(false);
      }
    }, 500);
  }

  // Re-enter edit mode if the user had it on before an automatic re-render.
  try {
    if (sessionStorage.getItem(EDIT_STORE_KEY) === '1') setEditMode(true);
  } catch (e) {}
})();
</script>"""

    html += panel_script.replace('__AGENT_ID__', agent_id).replace('__ACTIONS_JSON__', actions_json)

    return html


def _escape_html(text: str) -> str:
    """Escape text for safe HTML embedding."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
    )


# ---------------------------------------------------------------------------
# Script execution (daemon thread)
# ---------------------------------------------------------------------------

def start_script_execution(agent_id: str, agent: dict, action: dict,
                           user_params: dict, session_id: str = None,
                           notify_on_complete: threading.Event = None):
    """Start a script action in a background daemon thread.

    Returns (execution_id, err). err is None on success, or:
    "busy" — a script is already running for this agent.

    The script runs in the agent's environment via the shared execution
    backend: its workplace (local host, remote SSH, or tunnel) when one is
    assigned, otherwise the local host — using run_as_user when set, or the
    system default user (the server's own process user) when unset.
    """
    # Mutual exclusion: per-agent lock, non-blocking
    lock = _get_agent_lock(agent_id)
    if not lock.acquire(blocking=False):
        return None, "busy"

    execution_id = str(uuid.uuid4())
    with _buffers_lock:
        _execution_buffers[execution_id] = {
            "status": "running",
            "output_lines": [],
            "exit_code": None,
            "started_at": _now(),
        }

    thread = threading.Thread(
        target=_run_script,
        args=(agent_id, agent, action, user_params, execution_id, lock,
              session_id, notify_on_complete),
        daemon=True,
    )
    thread.start()

    return execution_id, None


def _notify_script_completion(agent_id: str, action: dict, exit_code,
                              output: str, session_id: str):
    """Best-effort push of a finished script result to the originating chat.

    Only used when the slash handler already replied "still running in the
    background" (i.e. the synchronous wait timed out). Failures here must
    never break the execution flow, so everything is swallowed.
    """
    try:
        from backend.agent_runtime.notifier import notify_agent
        notify_agent(
            agent_id=agent_id,
            tag="Panel",
            message=(f"**{action['label']}** finished (exit {exit_code}):\n"
                     f"```\n{output or '(no output)'}\n```"),
            session_id=session_id,
            dedup=False,
            trigger_llm=True,
        )
    except Exception:
        pass  # Best-effort; never break the execution flow


def _substitute_params(content: str, user_params: dict) -> str:
    """Replace {{param}} placeholders with shell-quoted values in one pass.

    A single pass keeps a value that itself looks like ``{{other}}`` from being
    expanded again, which would let a later parameter break out of its quotes.
    """
    if not user_params:
        return content
    pattern = re.compile(r"\{\{(" + "|".join(re.escape(str(k)) for k in user_params) + r")\}\}")
    quoted = {str(k): shlex.quote(str(v)) for k, v in user_params.items()}
    return pattern.sub(lambda m: quoted[m.group(1)], content)


def _run_script(agent_id: str, agent: dict, action: dict, user_params: dict,
                execution_id: str, lock: threading.Lock,
                session_id: str = None,
                notify_on_complete: threading.Event = None):
    """
    Execute a script action in a background daemon thread.

    Runs the script through the shared execution backend so it lands in the
    agent's environment — its workplace (local host, remote SSH, or tunnel)
    when assigned, otherwise the local host (honoring run_as_user). The backend
    returns buffered output, which is stored in the in-memory buffer for polling
    and logged to SQLite upon completion.
    """
    try:
        content = action.get("content", "")

        content = _substitute_params(content, user_params)

        max_timeout = _get_max_timeout()

        # Resolve the execution backend from the agent's environment. When a
        # workplace is assigned this dispatches to local / remote-SSH / tunnel
        # automatically; otherwise it runs on the local host, honoring
        # run_as_user. Panel is an ops surface, so we never route to an
        # ephemeral Docker sandbox (sandbox_enabled=0).
        from backend.tools.lib.exec_backend import registry
        exec_ctx = {
            "id": agent_id,
            "agent_id": agent_id,
            "workplace_id": agent.get("workplace_id"),
            "workspace": agent.get("workspace"),
            "run_as_user": (agent.get("run_as_user") or "").strip() or None,
            "sandbox_enabled": 0,
        }
        backend = registry.get_backend(f"panel-{agent_id}", exec_ctx)

        # Stream output live into the buffer as it arrives. Backends that can't
        # stream (Docker / tunnel) never invoke this — handled below.
        streamed = {"v": False}

        def _on_output(chunk):
            streamed["v"] = True
            with _buffers_lock:
                b = _execution_buffers.get(execution_id)
                if b:
                    b["output_lines"].append(chunk)

        result = backend.run_bash(content, timeout=max_timeout, env={},
                                  on_output=_on_output)

        exit_code = result.get("exit_code")
        status = "completed" if exit_code == 0 else "error"
        err_msg = result.get("error")

        with _buffers_lock:
            buf = _execution_buffers.get(execution_id)
            if buf:
                # Non-streaming backends: append their buffered output now.
                if not streamed["v"]:
                    if result.get("stdout"):
                        buf["output_lines"].append(result["stdout"])
                    if result.get("stderr"):
                        buf["output_lines"].append(result["stderr"])
                # Backend error (timeout / connection lost) is never streamed.
                if err_msg:
                    buf["output_lines"].append(f"\n[{err_msg}]\n")
                buf["status"] = status
                buf["exit_code"] = exit_code

        # Full output for the SQLite log (always present in the result dict).
        log_parts = []
        if result.get("stdout"):
            log_parts.append(result["stdout"])
        if result.get("stderr"):
            log_parts.append(result["stderr"])
        if err_msg:
            log_parts.append(f"\n[{err_msg}]\n")
        log_text = "".join(log_parts)

        # Log to SQLite
        try:
            db = panel_db_factory(agent_id)
            db.log_execution(
                action_id=action["id"],
                action_label=action["label"],
                action_type="script",
                params_used=json.dumps(user_params) if user_params else None,
                result=log_text[-20000:],
                status=status,
            )
        except Exception:
            pass  # Best-effort logging

        # If the slash handler already replied "still running in the
        # background", push the final result to the originating chat session.
        if notify_on_complete is not None and notify_on_complete.is_set():
            _notify_script_completion(agent_id, action, exit_code,
                                      log_text[-2000:], session_id)

    except Exception as e:
        with _buffers_lock:
            buf = _execution_buffers.get(execution_id)
            if buf:
                buf["status"] = "error"
                buf["output_lines"].append(f"\n[Execution error: {str(e)}]\n")
                buf["exit_code"] = -1

        if notify_on_complete is not None and notify_on_complete.is_set():
            _notify_script_completion(
                agent_id, action, -1,
                f"[Execution error: {str(e)}]", session_id)

    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Prompt sending
# ---------------------------------------------------------------------------

def _send_prompt_to_agent(agent_id: str, action: dict, session_id: str = None,
                          params: dict = None):
    """Deliver a prompt action to the agent and trigger a turn (async)."""
    content = action.get("content", "")
    for key, value in (params or {}).items():
        content = content.replace(f"{{{{{key}}}}}", shlex.quote(str(value)))
    if not content:
        return

    def _deliver():
        from backend.agent_runtime.notifier import notify_agent
        kwargs = dict(agent_id=agent_id, tag="Panel", message=content,
                      dedup=False, trigger_llm=True)
        if session_id:
            kwargs["session_id"] = session_id
        else:
            kwargs["external_user_id"] = "panel"
            kwargs["channel_id"] = None
        notify_agent(**kwargs)

    # notify_agent(trigger_llm=True) blocks until the LLM turn completes —
    # deliver from a daemon thread so callers (HTTP route, slash handler)
    # return immediately.
    threading.Thread(target=_deliver, daemon=True).start()


def _parse_params(params):
    """Parse params field from action. Returns list of param defs or None."""
    if not params:
        return None
    if isinstance(params, str):
        try:
            parsed = json.loads(params)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, list) else None
    if isinstance(params, list):
        return params
    return None


# ---------------------------------------------------------------------------
# Slash command: /panel
# ---------------------------------------------------------------------------
# Registered here (not in a handler.py) so the slash handler shares this
# module's in-memory execution state (_execution_buffers, per-agent locks)
# with the Flask routes — plugin_lifecycle loads this file under a synthetic
# package name, so importing plugins.panel.routes elsewhere would create a
# second module instance with separate state.

def _slug(s: str) -> str:
    return re.sub(r"\s+", "-", (s or "").strip().lower())


def _usage_line(command_name: str, params_def: list) -> str:
    """Render `/cmd <required> [optional]` from a param definition list."""
    parts = [f"/{command_name}"]
    for p in params_def:
        name = p.get("name", "arg")
        parts.append(f"<{name}>" if p.get("required") else f"[{name}]")
    return " ".join(parts)


def _map_positional_args(action: dict, command_name: str, args: str):
    """Map whitespace-separated args onto the action's param defs.

    Returns (params_dict, error_message). Values are substituted through the
    existing shlex.quote path, so no new injection surface is added.
    """
    params_def = _parse_params(action.get("params"))
    if not params_def:
        return {}, None

    try:
        tokens = shlex.split(args or "")
    except ValueError as e:
        return None, f"Could not parse arguments: {e}"

    values = {}
    for i, p in enumerate(params_def):
        name = p.get("name")
        if not name:
            continue
        if i < len(tokens):
            values[name] = tokens[i]
        elif p.get("default") not in (None, ""):
            values[name] = p["default"]
        elif p.get("required"):
            return None, (f"**{action['label']}** needs more arguments.\n"
                          f"Usage: `{_usage_line(command_name, params_def)}`")
    return values, None


def _run_panel_action(agent_id: str, action: dict, session_id=None, params: dict = None) -> str:
    """Execute a panel action from chat and return the reply text."""
    db = panel_db_factory(agent_id)
    params = params or {}

    if action["action_type"] == "prompt":
        _send_prompt_to_agent(agent_id, action, session_id=session_id, params=params)
        db.log_execution(action_id=action["id"], action_label=action["label"],
                         action_type="prompt", params_used=params or None,
                         result="Prompt sent via slash command", status="success")
        return f"Panel action **{action['label']}** sent to agent."

    from models.db import db as models_db
    agent = models_db.get_agent(agent_id)
    if not agent:
        return f"Agent '{agent_id}' not found."

    # Arm a completion notification only if the bounded wait below times out:
    # short scripts already return their output inline, and arming for those
    # would produce a duplicate follow-up message.
    notify_on_complete = threading.Event()

    execution_id, err = start_script_execution(
        agent_id, agent, action, params,
        session_id=session_id, notify_on_complete=notify_on_complete)
    if err == "busy":
        return (f"Cannot run **{action['label']}**: a script is already "
                "running for this agent.")

    # Bounded wait so short scripts return their output right in chat;
    # longer scripts keep running in the background thread.
    deadline = time.time() + min(_get_max_timeout(), 30)
    while time.time() < deadline:
        with _buffers_lock:
            buf = dict(_execution_buffers.get(execution_id) or {})
        if buf.get("status") in ("completed", "error"):
            output = "".join(buf.get("output_lines", []))[-2000:]
            return (f"**{action['label']}** finished (exit {buf.get('exit_code')}):\n"
                    f"```\n{output or '(no output)'}\n```")
        time.sleep(0.5)

    # Final check right before arming: closes the race where the script
    # finishes between the last loop iteration and the event being set.
    with _buffers_lock:
        buf = dict(_execution_buffers.get(execution_id) or {})
    if buf.get("status") in ("completed", "error"):
        output = "".join(buf.get("output_lines", []))[-2000:]
        return (f"**{action['label']}** finished (exit {buf.get('exit_code')}):\n"
                f"```\n{output or '(no output)'}\n```")

    notify_on_complete.set()
    return (f"**{action['label']}** is still running in the background — "
            "the result will be sent here when it finishes.")


def _panel_slash_handler(session_id, agent_id, external_user_id, channel_id, args):
    """Handle /panel (list actions) and /panel:<action> (execute) from chat."""
    db = panel_db_factory(agent_id)
    actions = [a for a in db.list_actions() if a.get("enabled")]

    sub = (args or "").strip()
    if not sub:
        if not actions:
            return "No panel actions configured for this agent."
        lines = ["**Panel actions:**"]
        for a in actions:
            assigned = (a.get("slash_command") or "").strip()
            alias = f" — also `/{assigned}`" if assigned else ""
            lines.append(f"- `/panel:{_slug(a['label'])}` — {a['label']} ({a['action_type']}){alias}")
        return "\n".join(lines)

    target = _slug(sub.split()[0])
    action = next((a for a in actions if _slug(a["label"]) == target), None)
    if not action:
        return f"No panel action matching '{target}'. Use `/panel` to list actions."

    # Params can't be collected through /panel:<action> — those need the Panel
    # tab, or a dedicated slash command that takes positional arguments.
    if action["action_type"] == "script" and _parse_params(action.get("params")):
        assigned = (action.get("slash_command") or "").strip()
        if assigned:
            params_def = _parse_params(action.get("params"))
            return (f"Action **{action['label']}** requires parameters — run it as "
                    f"`{_usage_line(assigned, params_def)}` or from the Panel tab.")
        return (f"Action **{action['label']}** requires parameters — "
                f"run it from the Panel tab instead.")

    return _run_panel_action(agent_id, action, session_id=session_id)


def _panel_command_provider(agent_id: str) -> list:
    """Expose each panel action with an assigned slash command to the registry."""
    from backend.slash_commands import SlashCommand

    commands = []
    for action in panel_db_factory(agent_id).list_actions():
        name = (action.get("slash_command") or "").strip().lower()
        if not name or not action.get("enabled"):
            continue

        params_def = _parse_params(action.get("params")) or []
        parameters = [{
            "name": p.get("name", "arg"),
            "required": bool(p.get("required")),
            "description": p.get("label") or p.get("name", ""),
        } for p in params_def]

        def _handler(session_id, agent_id, external_user_id, channel_id, args,
                     _action_id=action["id"], _name=name):
            # Re-read the action so an edit between listing and execution is honored.
            live = panel_db_factory(agent_id).get_action(_action_id)
            if not live or not live.get("enabled"):
                return f"Panel action for `/{_name}` no longer exists."
            params, err = _map_positional_args(live, _name, args)
            if err:
                return err
            return _run_panel_action(agent_id, live, session_id=session_id, params=params)

        commands.append(SlashCommand(
            name,
            _handler,
            f"{action['label']} (panel {action['action_type']})",
            parameters,
        ))
    return commands


try:
    from backend.slash_commands import command_registry
    command_registry.register(
        "panel",
        _panel_slash_handler,
        "Execute panel actions — /panel to list, /panel:<action> to run",
        [{"name": "action", "required": False, "description": "Panel action ID; omit to list actions"}],
    )
    command_registry.register_provider("panel", _panel_command_provider)
except Exception as _e:
    from backend.logging_config import get_logger
    get_logger(__name__).error("Failed to register /panel slash command: %s", _e)
