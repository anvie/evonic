"""
panel_update_action — update an existing action button on an agent's panel.
"""

import json

from plugins.panel.db import PanelDB, validate_slash_command


def execute(agent: dict, args: dict) -> dict:
    """Update an existing action button on an agent's panel.

    Authorization: agent can only update actions on their own panel,
    unless they are the super agent.

    Only agent_id and action_id are required — all other fields are
    optional and will only update when explicitly provided.

    Args:
        agent_id: The agent ID whose panel the action belongs to.
        action_id: The ID of the action to update.
        label: New display label (optional).
        action_type: New action type, 'script' or 'prompt' (optional).
        content: New script body or prompt text (optional).
        params: New parameter definitions (optional).
        sort_order: New display order position (optional).
        enabled: Enable or disable the action (optional).
        slash_command: Chat slash command that runs this action; "" clears it (optional).

    Returns:
        {success: true, action: {...}} on success
        {success: false, error: "..."} on failure
    """
    agent_id = args.get("agent_id", "").strip()
    action_id = args.get("action_id")

    # ── authorization ──────────────────────────────────────────
    caller_id = agent.get("id", "")
    is_super = agent.get("is_super", False)

    if not is_super and agent_id != caller_id:
        return {
            "success": False,
            "error": "You can only update actions on your own panel.",
        }

    # ── validation ─────────────────────────────────────────────
    if not agent_id:
        return {"success": False, "error": "agent_id is required."}

    if action_id is None:
        return {"success": False, "error": "action_id is required."}

    if not isinstance(action_id, int):
        try:
            action_id = int(action_id)
        except (ValueError, TypeError):
            return {"success": False, "error": "action_id must be an integer."}

    # ── build update fields ────────────────────────────────────
    fields = {}

    if "label" in args and args["label"] is not None:
        label = args["label"].strip()
        if not label:
            return {"success": False, "error": "label cannot be empty."}
        fields["label"] = label

    if "action_type" in args and args["action_type"] is not None:
        action_type = args["action_type"].strip().lower()
        if action_type not in ("script", "prompt"):
            return {
                "success": False,
                "error": f"Invalid action_type '{action_type}'. Must be 'script' or 'prompt'.",
            }
        fields["action_type"] = action_type

    if "content" in args and args["content"] is not None:
        fields["content"] = args["content"]

    if "params" in args and args["params"] is not None:
        p = args["params"]
        if isinstance(p, str):
            try:
                json.loads(p)
                fields["params"] = p
            except json.JSONDecodeError:
                return {"success": False, "error": "params must be a valid JSON string or array."}
        elif isinstance(p, list):
            try:
                fields["params"] = json.dumps(p)
            except (TypeError, ValueError) as e:
                return {"success": False, "error": f"Failed to serialize params: {e}"}
        else:
            return {"success": False, "error": "params must be an array of parameter definitions."}

    if "sort_order" in args and args["sort_order"] is not None:
        sort_order = args["sort_order"]
        if not isinstance(sort_order, int):
            try:
                sort_order = int(sort_order)
            except (ValueError, TypeError):
                return {"success": False, "error": "sort_order must be an integer."}
        fields["sort_order"] = sort_order

    if "enabled" in args and args["enabled"] is not None:
        if not isinstance(args["enabled"], bool):
            return {"success": False, "error": "enabled must be a boolean."}
        fields["enabled"] = args["enabled"]

    if "confirm_dialog" in args and args["confirm_dialog"] is not None:
        if not isinstance(args["confirm_dialog"], bool):
            return {"success": False, "error": "confirm_dialog must be a boolean."}
        fields["confirm_dialog"] = args["confirm_dialog"]

    if "slash_command" in args and args["slash_command"] is not None:
        if not isinstance(args["slash_command"], str):
            return {"success": False, "error": "slash_command must be a string."}
        normalized, slash_error = validate_slash_command(
            agent_id, args["slash_command"], exclude_action_id=action_id
        )
        if slash_error:
            return {"success": False, "error": slash_error}
        fields["slash_command"] = normalized

    if not fields:
        return {"success": False, "error": "No fields to update were provided."}

    # ── update action ──────────────────────────────────────────
    try:
        db = PanelDB(agent_id)
        action = db.update_action(action_id, **fields)
        if action is None:
            return {
                "success": False,
                "error": f"Action with id {action_id} not found for agent '{agent_id}'.",
            }
        try:
            from backend.event_stream import event_stream
            event_stream.emit('panel_updated', {'agent_id': agent_id})
        except Exception:
            pass
        return {"success": True, "action": action}
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"Failed to update action: {e}"}
