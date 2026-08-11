"""
panel_add_action — add a new action button to an agent's panel.
"""

import json

from plugins.panel.db import PanelDB, validate_slash_command


def execute(agent: dict, args: dict) -> dict:
    """Add a new action button to an agent's panel.

    Authorization: agent can only add actions to their own panel,
    unless they are the super agent.

    Args:
        agent_id: The agent ID whose panel to add the action to.
        label: Display label for the action button.
        action_type: 'script' or 'prompt'.
        content: Script body or prompt text.
        params: Array of parameter definitions (optional, default []).
        sort_order: Display order position (optional, default 0).
        slash_command: Chat slash command that runs this action (optional).

    Returns:
        {success: true, action: {...}} on success
        {success: false, error: "..."} on failure
    """
    agent_id = args.get("agent_id", "").strip()
    label = args.get("label", "").strip()
    action_type = args.get("action_type", "").strip().lower()
    content = args.get("content", "")
    params = args.get("params")
    sort_order = args.get("sort_order")
    confirm_dialog = args.get("confirm_dialog", False)
    slash_command = args.get("slash_command", "")

    # ── authorization ──────────────────────────────────────────
    caller_id = agent.get("id", "")
    is_super = agent.get("is_super", False)

    if not is_super and agent_id != caller_id:
        return {
            "success": False,
            "error": "You can only add actions to your own panel.",
        }

    # ── validation ─────────────────────────────────────────────
    if not agent_id:
        return {"success": False, "error": "agent_id is required."}

    if not label:
        return {"success": False, "error": "label is required."}

    if action_type not in ("script", "prompt"):
        return {
            "success": False,
            "error": f"Invalid action_type '{action_type}'. Must be 'script' or 'prompt'.",
        }

    if not content:
        return {"success": False, "error": "content is required."}

    # ── serialize params to JSON string ────────────────────────
    if params is None:
        params_str = "[]"
    elif isinstance(params, str):
        # already a JSON string — validate it parses
        try:
            json.loads(params)
            params_str = params
        except json.JSONDecodeError:
            return {"success": False, "error": "params must be a valid JSON string or array."}
    elif isinstance(params, list):
        try:
            params_str = json.dumps(params)
        except (TypeError, ValueError) as e:
            return {"success": False, "error": f"Failed to serialize params: {e}"}
    else:
        return {"success": False, "error": "params must be an array of parameter definitions."}

    # ── defaults ───────────────────────────────────────────────
    if sort_order is None:
        sort_order = 0
    elif not isinstance(sort_order, int):
        try:
            sort_order = int(sort_order)
        except (ValueError, TypeError):
            return {"success": False, "error": "sort_order must be an integer."}

    # ── slash command ──────────────────────────────────────────
    slash_command, slash_error = validate_slash_command(agent_id, slash_command)
    if slash_error:
        return {"success": False, "error": slash_error}

    # ── create action ──────────────────────────────────────────
    try:
        db = PanelDB(agent_id)
        action = db.add_action(
            label=label,
            action_type=action_type,
            content=content,
            params=params_str,
            sort_order=sort_order,
            confirm_dialog=confirm_dialog,
            slash_command=slash_command,
        )
        try:
            from backend.event_stream import event_stream
            event_stream.emit('panel_updated', {'agent_id': agent_id})
        except Exception:
            pass
        return {"success": True, "action": action}
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"Failed to add action: {e}"}
