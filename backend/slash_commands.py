"""Slash command registry and executor for agent sessions.

Commands are parsed and executed in the backend so they work on all channels
(Telegram, web, etc.) without any frontend-specific logic.
"""

import logging
import re
import os
import threading
from typing import Optional, Dict, Any, Callable, Tuple

_logger = logging.getLogger(__name__)

# Command handler signature: (session_id, agent_id, external_user_id, channel_id, args) -> str
CommandHandler = Callable[[str, str, str, Optional[str], str], str]


class SlashCommand:
    """Represents a single slash command."""

    def __init__(self, name: str, handler: CommandHandler, description: str = "", parameters: list = None):
        self.name = name
        self.handler = handler
        self.description = description
        self.parameters = parameters or []
        self.accepts_args = bool(self.parameters)

    def to_dict(self) -> dict:
        """Return a dict suitable for JSON serialization to the frontend."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "accepts_args": self.accepts_args,
        }


class SlashCommandRegistry:
    """Registry for slash commands. Supports dynamic registration."""

    def __init__(self):
        self._commands: Dict[str, SlashCommand] = {}
        self._providers: Dict[str, Callable[[str], list]] = {}

    def register(self, name: str, handler: CommandHandler, description: str = "", parameters: list = None):
        """Register a command handler."""
        self._commands[name] = SlashCommand(name, handler, description, parameters)

    def register_provider(self, key: str, provider: Callable[[str], list]):
        """Register a per-agent command provider.

        `provider(agent_id)` returns a list of SlashCommand objects that exist only
        for that agent (e.g. panel actions with an assigned slash command).
        Statically registered commands always win on a name clash.
        """
        self._providers[key] = provider

    def get(self, name: str) -> Optional[SlashCommand]:
        """Get a statically registered command by name."""
        return self._commands.get(name)

    def list_commands(self) -> list:
        """Return list of statically registered SlashCommand objects."""
        return list(self._commands.values())

    def provided_commands(self, agent_id: str) -> list:
        """Return provider commands for an agent, minus any static name clash."""
        out = []
        for key, provider in list(self._providers.items()):
            try:
                for cmd in provider(agent_id) or []:
                    if cmd.name not in self._commands:
                        out.append(cmd)
            except Exception:
                _logger.warning("Slash command provider %r failed for agent %s", key, agent_id, exc_info=True)
        return out

    def resolve(self, name: str, agent_id: str) -> Optional[SlashCommand]:
        """Get a command by name for an agent — static first, then providers."""
        cmd = self._commands.get(name)
        if cmd:
            return cmd
        return next((c for c in self.provided_commands(agent_id) if c.name == name), None)


def _expand_slash_list(raw_value: str, all_names: set) -> set:
    """Expand comma-separated list with wildcard '*' and inverse mode '!' support."""
    if not raw_value or not raw_value.strip():
        return set()
    raw = raw_value.strip()
    if raw == '*':
        return set(all_names)
    if raw.startswith('!'):
        allowed = {c.strip() for c in raw[1:].split(',') if c.strip()}
        return set(all_names) - allowed
    return {c.strip() for c in raw.split(',') if c.strip()}


def _persist_session_agent_state(chat_db, session_id: str, ms) -> None:
    """Merge-write session-scoped AgentState fields for slash commands."""
    import json

    raw = chat_db.get_session_state(session_id)
    try:
        session_data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        session_data = {}
    if not isinstance(session_data, dict):
        session_data = {}
    data = json.loads(ms.serialize())
    session_data.update({
        'mode': data.get('mode', 'plan'),
        'tasks': data.get('tasks', []),
        'next_task_id': data.get('next_task_id', 1),
        'plan_file': data.get('plan_file'),
        'states': data.get('states', {}),
        'auto_trivial': data.get('auto_trivial', False),
        'atg': data.get('atg'),
        'cmp': data.get('cmp'),
    })
    chat_db.upsert_session_state(session_id, json.dumps(session_data))


def list_available_commands(agent_id: str, channel_id: Optional[str] = None) -> list:
    """Return registered commands available to an agent on the given channel."""
    commands = command_registry.list_commands() + command_registry.provided_commands(agent_id)
    try:
        from models.db import db
        super_agent = db.get_super_agent()
        is_super = bool(super_agent and super_agent.get('id') == agent_id)
        agent = db.get_agent(agent_id)
        workplace_id = agent.get('workplace_id') if agent else None
        workplace = db.get_workplace(workplace_id) if workplace_id else None
        can_cd = is_super or bool(workplace and workplace.get('type') in ('remote', 'tunnel'))
        has_subagent = is_super or 'subagent' in db.get_agent_skills(agent_id)
        disabled_raw = (agent.get('disabled_slash_commands') or '') if agent else ''
    except Exception:
        is_super = can_cd = has_subagent = False
        disabled_raw = ''

    disabled_set = _expand_slash_list(disabled_raw, {cmd.name for cmd in commands})

    available = []
    for cmd in commands:
        if cmd.name in {'cd', 'cwd'} and not can_cd:
            continue
        if cmd.name in {'restart', 'shutdown'} and not is_super:
            continue
        if cmd.name == 'sub' and not has_subagent:
            continue
        if not is_super and cmd.name in disabled_set:
            continue
        available.append(cmd)
    return sorted(available, key=lambda c: c.name)


# Global registry instance
command_registry = SlashCommandRegistry()

SUBAGENT_USER_DIRECT_PREFIX = (
    "You were spawned directly by the user via the `/sub` command"
    " \u2014 not delegated by your parent agent."
    " Execute the task below directly and report your result."
    " Since your response will appear in the parent agent\u2019s chat,"
    " begin your response with a brief line about the user request,"
    " so the parent agent has context.\n\n"
    "--- USER TASK ---\n"
)


def parse_command(message: str) -> Optional[Tuple[str, str]]:
    """Parse a message and extract command + args if it starts with /.

    Returns (command_name, args_string) or None if not a command.
    """
    if not message or not message.startswith("/"):
        return None

    # Match /command, /command args, or /command:sub args (hyphens allowed
    # in names; the :sub part becomes the first args token, e.g.
    # "/panel:build foo" -> ("panel", "build foo"))
    match = re.match(r"^/([\w-]+)(?::([\w-]+))?(?:\s+(.*))?$", message.strip(), re.DOTALL)
    if not match:
        return None

    cmd_name = match.group(1).lower()
    sub = match.group(2)
    args = match.group(3) or ""
    if sub:
        args = f"{sub} {args}".strip()
    return (cmd_name, args)


def execute_command(
    cmd_name: str,
    args: str,
    session_id: str,
    agent_id: str,
    external_user_id: str,
    channel_id: Optional[str] = None,
) -> Optional[str]:
    """Execute a slash command and return the response text.

    Returns the command response string, or None if the command is not found
    (caller should then treat the message as normal chat).
    """
    cmd = command_registry.resolve(cmd_name, agent_id)
    if not cmd:
        return None  # Unknown command — fall through to normal LLM processing

    return cmd.handler(session_id, agent_id, external_user_id, channel_id, args)


# ==================== Built-in Command Handlers ====================


def _register_builtins():
    """Register all built-in slash commands."""

    # /clear [ar] — Clear chat history and agent LLM log; `ar` also archives it.
    def clear_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        from models.db import db
        import os
        import config

        # Archive only when explicitly requested with the `ar` argument.
        archive_requested = "ar" in set(args.strip().lower().split())
        no_archive = not archive_requested

        db.clear_session(session_id, agent_id, no_archive=no_archive)

        # Clear in-memory loaded skill state so skill badges disappear from session state UI
        from backend.agent_runtime import agent_runtime
        agent_runtime._session_skill_mds.pop(session_id, None)
        agent_runtime._session_skill_tools.pop(session_id, None)

        # Reset agent state so next turn starts fresh in plan mode (no stale execute state).
        from backend.agent_state import AgentState
        fresh = AgentState()
        # Per-session: save to session_state
        import json
        session_data = {
            'mode': fresh.mode,
            'tasks': fresh.tasks,
            'next_task_id': fresh._next_task_id,
            'plan_file': fresh.plan_file,
            'states': fresh.states,
            'auto_trivial': fresh.auto_trivial,
            'atg': None,   # full-replace already wipes these; explicit for clarity
            'cmp': None,
        }
        db.upsert_session_state(session_id, json.dumps(session_data), agent_id=agent_id)
        # Global: reset focus only
        global_data = {'focus': fresh.focus, 'focus_reason': fresh.focus_reason}
        db.upsert_agent_state(json.dumps(global_data), agent_id)

        now = __import__('datetime').datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        # Truncate agent's llm.log file
        log_path = os.path.join("logs", "agents", agent_id, "llm.log")
        if os.path.exists(log_path):
            with open(log_path, "w") as f:
                f.write(f"# LLM Log — Cleared on {now} UTC\n")

        # Truncate agent's sessrecap.log file
        recap_path = os.path.join("logs", "agents", agent_id, "sessrecap.log")
        if os.path.exists(recap_path):
            with open(recap_path, "w") as f:
                f.write(f"# Session Recap Log — Cleared on {now} UTC\n")

        # Emit session_clear event
        try:
            from backend.event_stream import event_stream
            event_stream.emit('session_clear', {'session_id': session_id, 'agent_id': agent_id})
        except Exception:
            pass

        if no_archive and config.SESSION_ARCHIVE:
            return "History cleared without archive"
        return "History cleared."

    command_registry.register(
        "clear",
        clear_handler,
        "Clear chat history (`/clear ar` archives it)",
        parameters=[{"name": "archive", "options": ["ar"]}],
    )

    # /help — Show available commands
    def help_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        lines = ["**Available commands:**"]
        for cmd in list_available_commands(agent_id, channel_id):
            lines.append(f"- `/{cmd.name}` — {cmd.description}")
        return "\n".join(lines)

    command_registry.register(
        "help",
        help_handler,
        "Show available commands",
    )

    # /summary — Force regenerate session summary
    def summary_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        from models.db import db
        from backend.agent_runtime import agent_runtime

        agent = db.get_agent(agent_id)
        if not agent:
            return "Error: Agent not found."

        # Trigger summarization for this session
        updated = agent_runtime.summarize_session(agent, session_id)
        if updated:
            return "Session summary has been regenerated."
        else:
            return "Summary is already up to date, or there are not enough messages to summarize."

    command_registry.register(
        "summary",
        summary_handler,
        "Force regenerate session summary",
    )

    # /investigate — Send investigation request to another agent with session context
    def investigate_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        from models.db import db

        # Parse target agent-id and context
        parts = args.strip().split(None, 1)
        if not parts or not parts[0]:
            return "Usage: /investigate <agent-id> <context>"
        target_agent_id = parts[0].strip().lower()
        if target_agent_id == agent_id.lower():
            return "Cannot investigate the current agent. Choose a different agent."
        context = parts[1].strip() if len(parts) > 1 else ""

        # Validate context
        if not context:
            return "Context is required. Usage: /investigate <agent-id> <context>"

        # Validate target agent exists and is enabled
        target = db.get_agent(target_agent_id)
        if not target or not target.get("enabled", True):
            return f"Agent '{target_agent_id}' not found or is disabled."

        # Get current agent info
        current = db.get_agent(agent_id)
        current_name = current.get("name", agent_id) if current else agent_id

        # Build session log path
        jsonl_id = session_id.split("-")[-1]
        session_log_path = f"agents/{agent_id}/sessions/{jsonl_id}.jsonl"

        # Build investigation message
        # The platform owner is asking the target agent to investigate the
        # current agent's session — phrase it from the owner's perspective.
        target_name = target.get("name", target_agent_id)
        message = (
            f"[INVESTIGATION REQUEST from the platform owner]\n\n"
            f"The platform owner has asked you to investigate another agent's session.\n\n"
            f"Agent under investigation: {current_name} ({agent_id})\n"
            f"Session: {session_id}\n"
            f"Session log: {session_log_path}\n"
            f"Owner's request: {context}\n\n"
            f"Please investigate the session log above and report back to the owner."
        )

        # Clear the target agent's investigation session before delivering the
        # request, so stale/unrelated history from a previous investigation does
        # not pollute the LLM context. Resolve the exact session notify_agent
        # will deliver into (target agent + this sender's agent-message user id).
        _AGENT_MSG_PREFIX = "__agent__"
        target_external_user_id = f"{_AGENT_MSG_PREFIX}{agent_id}"
        try:
            target_session_id = db.get_or_create_session(
                target_agent_id, target_external_user_id, None)
            db.clear_session(target_session_id, target_agent_id)

            # Reset per-session agent state so the investigation starts fresh
            # (plan mode, no stale tasks). Do NOT touch the target's global
            # agent_state or logs — those are not specific to this session.
            from backend.agent_runtime import agent_runtime
            agent_runtime._session_skill_mds.pop(target_session_id, None)
            agent_runtime._session_skill_tools.pop(target_session_id, None)

            from backend.agent_state import AgentState
            import json
            fresh = AgentState()
            db.upsert_session_state(target_session_id, json.dumps({
                'mode': fresh.mode,
                'tasks': fresh.tasks,
                'next_task_id': fresh._next_task_id,
                'plan_file': fresh.plan_file,
                'states': fresh.states,
                'auto_trivial': fresh.auto_trivial,
            }), agent_id=target_agent_id)

            from backend.event_stream import event_stream
            event_stream.emit('session_clear', {
                'session_id': target_session_id, 'agent_id': target_agent_id})
        except Exception:
            # Clearing is best-effort; still deliver the investigation request.
            pass

        # Deliver via notify_agent (same mechanism as send_agent_message)
        from backend.agent_runtime.notifier import notify_agent

        result = notify_agent(
            agent_id=target_agent_id,
            tag="SYSTEM/Owner",
            message=message,
            external_user_id=target_external_user_id,
            channel_id=None,
            dedup=False,
            metadata={
                "agent_message": True,
                "from_agent_id": agent_id,
                "from_agent_name": current_name,
                "investigation_request": True,
                "investigation_session_id": session_id,
                "investigation_session_log": session_log_path,
            },
        )

        if not result.get("success"):
            reason = result.get("reason", "unknown")
            return f"Failed to deliver investigation request to {target_name}: {reason}."

        return f"Investigation request sent to **{target_name}**."

    command_registry.register(
        "investigate",
        investigate_handler,
        "Send investigation request to another agent with session context",
        parameters=[{"name": "agent_id"}, {"name": "context"}],
    )


    # /stop — Stop current agent processing loop
    def stop_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        from backend.agent_runtime import agent_runtime  # global singleton (lazy import to avoid circular dep)
        agent_runtime.request_stop(session_id)
        return "Stop signal sent."

    command_registry.register(
        "stop",
        stop_handler,
        "Stop the agent's current processing loop",
    )

    # /cwd — Show current workspace directory
    def cwd_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        import json as _json

        from models.db import db

        agent = db.get_agent(agent_id)
        if not agent:
            return "Error: Agent not found."

        # Determine which workspace to show
        is_super = False
        try:
            super_agent = db.get_super_agent()
            is_super = super_agent and super_agent.get('id') == agent_id
        except Exception:
            pass

        if is_super:
            workspace = agent.get('workspace')
            if not workspace:
                return "No workspace directory configured."
            return f"Current workspace: {workspace}"

        # Check for remote/tunnel workplace
        workplace_id = agent.get('workplace_id')
        if workplace_id:
            workplace = db.get_workplace(workplace_id)
            if workplace and workplace.get('type') in ('remote', 'tunnel'):
                cfg_raw = workplace.get('config', '{}')
                if isinstance(cfg_raw, str):
                    cfg = _json.loads(cfg_raw)
                else:
                    cfg = cfg_raw or {}
                workspace = cfg.get('workspace_path')
                if not workspace:
                    return "No workspace directory configured."
                return f"Current workspace: {workspace}"

        return "Permission denied: /cwd is only available to the super agent or agents with remote/tunnel workplaces."

    command_registry.register(
        "cwd",
        cwd_handler,
        "Show current workspace directory",
        parameters=[],
    )

    # /cd — Change workspace directory (super agent or remote/tunnel workplace)
    def cd_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        import json as _json

        from models.db import db
        from backend.workplaces.manager import workplace_manager

        # Determine if this agent is allowed to use /cd:
        #   - super agent (local/Docker-based)
        #   - agent with a remote or tunnel workplace
        is_super = False
        try:
            super_agent = db.get_super_agent()
            is_super = super_agent and super_agent.get('id') == agent_id
        except Exception:
            pass

        agent = db.get_agent(agent_id)
        if not agent:
            return "Error: Agent not found."

        workplace_id = agent.get('workplace_id')
        is_remote_or_tunnel = False
        if workplace_id:
            try:
                workplace = db.get_workplace(workplace_id)
                if workplace and workplace.get('type') in ('remote', 'tunnel'):
                    is_remote_or_tunnel = True
            except Exception:
                pass

        if not is_super and not is_remote_or_tunnel:
            return (
                "Permission denied: /cd is only available to the super agent "
                "or agents with remote/tunnel workplaces."
            )

        if not args or not args.strip():
            return "Usage: /cd [path] — change workspace directory"

        raw_path = args.strip()

        # Path sanitization (.. rejection) applies to all
        sanitized = raw_path.replace('\\', '/')
        if '..' in sanitized.split('/'):
            return f"Error: path contains '..' which is not allowed: {raw_path}"

        if is_super:
            # Super agent: local filesystem — expand, verify, update agent.workspace,
            # and recreate Docker container so the new workspace is mounted.
            new_path = os.path.expanduser(raw_path)
            if '..' in new_path.split(os.sep):
                return f"Error: path contains '..' which is not allowed: {new_path}"
            new_path = os.path.abspath(new_path)
            if not os.path.isdir(new_path):
                return f"Error: directory does not exist: {new_path}"

            db.update_agent(agent_id, {'workspace': new_path})

            from backend.tools.runpy import _destroy_container
            _destroy_container(session_id)

            return f"Workspace changed to: {new_path}"

        # Remote / tunnel agent: update the workplace config (skip
        # os.path.expanduser / os.path.abspath / os.path.isdir — meaningless
        # for remote filesystems). Also update the in-memory backend so the
        # change takes effect immediately.
        workplace = db.get_workplace(workplace_id)
        cfg_raw = workplace.get('config', '{}') if workplace else '{}'
        if isinstance(cfg_raw, str):
            cfg = _json.loads(cfg_raw)
        else:
            cfg = cfg_raw or {}
        cfg['workspace_path'] = raw_path
        db.update_workplace(workplace_id, {'config': cfg})
        workplace_manager.set_backend_workspace(workplace_id, raw_path)

        return f"Workspace changed to: {raw_path}"

    command_registry.register(
        "cd",
        cd_handler,
        "Change workspace directory",
        parameters=[{"name": "path"}],
    )


    # /restart — Restart the service (super agent only)
    def restart_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        from models.db import db

        super_agent = db.get_super_agent()
        if not super_agent or super_agent.get('id') != agent_id:
            return "Permission denied: /restart is only available to the super agent."

        # Persist caller info so the new process can send "Evonic ready!" after boot
        import json
        db.set_setting('restart_ready_needed', json.dumps({
            'channel_id': channel_id,
            'external_user_id': external_user_id,
            'session_id': session_id,
            'agent_id': agent_id,
        }))

        # Clear fallback flag from agent_state before restart so the agent
        # starts with its primary model after reboot
        try:
            from models.chat import agent_chat_manager as _restart_cm
            _restart_raw = _restart_cm.get(agent_id).get_agent_state()
            if _restart_raw:
                _restart_data = json.loads(_restart_raw)
                if _restart_data.pop('active_fallback_model_id', None):
                    _restart_cm.get(agent_id).upsert_agent_state(json.dumps(_restart_data))
        except Exception:
            pass

        from backend.restart import restart_service
        restart_service()
        return "Restarting..."

    command_registry.register(
        "restart",
        restart_handler,
        "Restart the service (super agent only)",
    )

    # /shutdown — Shut down the server completely (super agent only)
    def shutdown_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        from models.db import db

        super_agent = db.get_super_agent()
        if not super_agent or super_agent.get('id') != agent_id:
            return "Permission denied: /shutdown is only available to the super agent."

        def _do_shutdown():
            import time
            time.sleep(1.5)  # brief delay so response is sent first

            # Stop all channels cleanly
            from backend.channels.registry import channel_manager
            channel_manager.stop_all()
            time.sleep(1.0)

            os._exit(0)

        from backend.restart import stop_service
        stop_service(fallback=_do_shutdown)
        return "Shutting down..."

    command_registry.register(
        "shutdown",
        shutdown_handler,
        "Shut down the Evonic server completely (super agent only)",
    )

    # /plan - Switch agent to plan mode
    def plan_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        from backend.agent_state import AgentState
        from models.chat import agent_chat_manager

        # Create a fresh AgentState in plan mode
        ms = AgentState()

        _db = agent_chat_manager.get(agent_id)
        _persist_session_agent_state(_db, session_id, ms)

        # Reset focus in global agent_state (focus is cross-session).
        _db.upsert_agent_state(ms.serialize())

        return "Switched to plan mode."

    command_registry.register(
        "plan",
        plan_handler,
        "Switch to plan mode",
    )

    # /exec — Switch agent to execute mode
    def exec_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        from models.db import db
        from backend.agent_state import AgentState
        from models.chat import agent_chat_manager

        # Check if agent state is enabled for this agent
        agent = db.get_agent(agent_id)
        if not agent:
            return "Error: Agent not found."

        if not agent.get("enable_agent_state"):
            return "Agent state is not enabled for this agent."

        # Load current per-session state
        _db = agent_chat_manager.get(agent_id)
        session_content = _db.get_session_state(session_id)

        if session_content:
            ms = AgentState.deserialize(session_content)
        else:
            ms = AgentState()  # fresh plan-mode state

        # Transition to execute mode
        result = ms.set_mode(
            "execute",
            reason="slash command /exec",
            bypass_plan_requirement=True,
        )
        if "error" in result:
            return f"Error: {result['error']}"

        _persist_session_agent_state(_db, session_id, ms)

        return "Switched to execute mode."

    command_registry.register(
        "exec",
        exec_handler,
        "Switch to execute mode",
    )

    def unfocus_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        from backend.agent_state import AgentState
        from models.chat import agent_chat_manager

        content = agent_chat_manager.get(agent_id).get_agent_state()
        if not content:
            return "Tidak ada agent state aktif."
        ms = AgentState.deserialize(content)
        if not ms.focus:
            return "Focus mode sudah off."
        reason = ms.focus_reason or "unknown"
        ms.focus = False
        ms.focus_reason = None
        agent_chat_manager.get(agent_id).upsert_agent_state(ms.serialize())
        return (f"Focus mode cleared (was: {reason}). "
                f"Agent sekarang bisa menerima semua session.")

    command_registry.register(
        "unfocus",
        unfocus_handler,
        "Force-clear focus mode — use when agent is stuck in focus after a failed task",
    )

    # /status — Show agent status information
    def status_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        from models.db import db
        from backend.agent_state import AgentState
        from models.chat import agent_chat_manager

        agent = db.get_agent(agent_id)
        if not agent:
            return "Error: Agent not found."

        # Detect platform: messaging channels need compact output
        is_compact = False
        if channel_id:
            channel = db.get_channel(channel_id)
            if channel:
                ch_type = channel.get("type", "")
                is_compact = ch_type in ("telegram", "whatsapp", "whatsapp_shared")

        lines = []
        if is_compact:
            lines.append(f"STATUS \u2014 {agent.get('name', agent_id)}")
            lines.append(f"Session: {session_id}")
        else:
            lines.append(f"**Status \u2014 {agent.get('name', agent_id)}**")
            lines.append(f"Session: {session_id}")

        # Model — resolved via model_id column → llm_models table
        model = db.get_agent_model(agent_id)
        if model:
            model_name = model.get("name", "unknown")
            model_id = model.get("model_name", "")
            if model_id:
                lines.append(f"Model: {model_name} ({model_id})")
            else:
                lines.append(f"Model: {model_name}")
        else:
            lines.append("Model: unknown")

        # Agent state: per-session (mode/plan_file) from session_state, global (focus) from agent_state
        _db = agent_chat_manager.get(agent_id)
        session_content = _db.get_session_state(session_id)
        if session_content:
            sess_ms = AgentState.deserialize(session_content)
            lines.append(f"Mode: {sess_ms.mode}")
            task_counts = {
                status: sum(1 for task in sess_ms.tasks if task.get("status") == status)
                for status in ("pending", "in_progress", "done")
            }
            lines.append(
                f"Tasks: {task_counts['pending']} pending, "
                f"{task_counts['in_progress']} in progress, {task_counts['done']} done"
            )
            if sess_ms.cmp and sess_ms.cmp.get('paths'):
                _paths = sess_ms.cmp['paths']
                _active = _paths.get(sess_ms.cmp.get('active_id')) or {}
                _preserved = sum(1 for p in _paths.values()
                                 if p.get('status') in ('preserved', 'dormant'))
                _archived = sum(1 for p in _paths.values() if p.get('status') == 'archived')
                lines.append(
                    f"Paths: {len(_paths)} (active: {_active.get('id')} "
                    f"\"{_active.get('title', '')[:40]}\"; "
                    f"{_preserved} preserved, {_archived} archived)")
            if sess_ms.plan_file:
                # Try per-agent path first, then fallback to legacy centralized path
                project_root = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
                agent_plan = os.path.normpath(os.path.join(project_root, 'agents', agent_id, sess_ms.plan_file))
                legacy_plan = os.path.normpath(os.path.join(project_root, sess_ms.plan_file))
                if os.path.exists(agent_plan) or os.path.exists(legacy_plan):
                    lines.append(f"Plan file: {sess_ms.plan_file}")
        else:
            lines.append("Mode: plan")
        # Focus (global) from agent_state
        state_content = _db.get_agent_state()
        if state_content:
            ms = AgentState.deserialize(state_content)
            if ms.focus:
                reason = f" \u2014 {ms.focus_reason}" if ms.focus_reason else ""
                lines.append(f"Focus: yes{reason}")
            else:
                lines.append("Focus: no")
        else:
            lines.append("Focus: no")

        # Active model badge: check if fallback is active
        if state_content:
            try:
                _state_data = json.loads(state_content) if isinstance(state_content, str) else state_content
                _fb_active_id = _state_data.get('active_fallback_model_id')
                if _fb_active_id:
                    _active_m = db.get_model_by_id(_fb_active_id)
                    if _active_m:
                        _am_name = _active_m.get('name', _fb_active_id)
                        lines.append(f"Active Model: {_am_name} (fallback)")
                    else:
                        lines.append(f"Active Model: {_fb_active_id} (fallback, unknown)")
                else:
                    # Show primary
                    _prim_name = model.get('name', model.get('model_name', 'unknown')) if model else 'unknown'
                    lines.append(f"Active Model: {_prim_name} (primary)")
            except Exception:
                pass

        # Workplace
        workplace_id = agent.get("workplace_id")
        if workplace_id:
            workplace = db.get_workplace(workplace_id)
            if workplace:
                wp_name = workplace.get("name", "unknown")
                wp_type = workplace.get("type", "unknown")
                wp_status = workplace.get("status", "disconnected")
                lines.append(f"Workplace: {wp_name} ({wp_type}, {wp_status})")
            else:
                lines.append("Workplace: not found")
        else:
            lines.append("Workplace: none")

        # Workspace
        workspace = agent.get("workspace")
        if workspace:
            lines.append(f"Workspace: {workspace}")
        else:
            lines.append("Workspace: not configured")

        # Toggles
        sandbox = "enabled" if agent.get("sandbox_enabled") else "disabled"
        safety = "enabled" if agent.get("safety_checker_enabled") else "disabled"
        vision = "enabled" if agent.get("vision_enabled") else "disabled"
        agent_msg = "enabled" if agent.get("agent_messaging_enabled") else "disabled"
        if is_compact:
            lines.append(f"Toggles: Sandbox={sandbox}, Safety={safety}, Vision={vision}, Msg={agent_msg}")
        else:
            lines.append("Toggles:")
            lines.append(f"  Sandbox: {sandbox}")
            lines.append(f"  Safety Checker: {safety}")
            lines.append(f"  Vision: {vision}")
            lines.append(f"  Agent Messaging: {agent_msg}")

        # Tools and skills count
        tools = db.get_agent_tools(agent_id)
        skills = db.get_agent_skills(agent_id)
        if is_compact:
            lines.append(f"Tools: {len(tools)}  |  Skills: {len(skills)}")
        else:
            lines.append(f"Tools: {len(tools)}")
            lines.append(f"Skills: {len(skills)}")

        # Channels
        channels = db.get_channels(agent_id)
        if channels:
            from backend.channels.registry import channel_manager
            if is_compact:
                ch_parts = []
                for ch in channels:
                    ch_name = ch.get("name", "unknown")
                    ch_type = ch.get("type", "unknown")
                    ch_id = ch.get("id", "")
                    is_connected = channel_manager.is_running(ch_id)
                    status = "connected" if is_connected else "disconnected"
                    ch_parts.append(f"{ch_name} ({ch_type})={status}")
                lines.append(f"Channels: {', '.join(ch_parts)}")
            else:
                lines.append("Channels:")
                for ch in channels:
                    ch_name = ch.get("name", "unknown")
                    ch_type = ch.get("type", "unknown")
                    ch_id = ch.get("id", "")
                    is_connected = channel_manager.is_running(ch_id)
                    status = "connected" if is_connected else "disconnected"
                    lines.append(f"  {ch_name} ({ch_type}) \u2014 {status}")

        # Web: double newline between every field so markdown renders each as
        # a separate paragraph (single \n would collapse into one line).
        # Telegram/WhatsApp: single newline for a compact, clean layout.
        if is_compact:
            return "\n".join(lines)
        else:
            return "\n\n".join(lines)

    command_registry.register(
        "status",
        status_handler,
        "Show agent status information",
    )

    # /model — Show or set the agent's LLM model
    def model_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        from models.db import db

        import json as _json

        if not args or not args.strip():
            # No args — show current model only (with fallback awareness)
            current = db.get_agent_model(agent_id)
            if not current:
                return "No model configured. Type /model list to see available models, or /model <number> to set one."

            # Check for active fallback model in agent_state
            try:
                state_content = db.get_agent_state(agent_id)
                if state_content:
                    state_data = _json.loads(state_content) if isinstance(state_content, str) else state_content
                    fb_active_id = state_data.get("active_fallback_model_id")
                    if fb_active_id:
                        fb_model = db.get_model_by_id(fb_active_id)
                        if fb_model:
                            fb_name = fb_model.get("name", fb_active_id)
                            fb_mn = fb_model.get("model_name", "")
                            fb_sc = fb_model.get("shortcode", "?")
                            if fb_mn:
                                return (
                                    f"**Current model:** {fb_name} ({fb_mn}) [#{fb_sc}] (fallback)\n\n"
                                    "Type /model list to see all available models. /model <number> to switch."
                                )
                            else:
                                return (
                                    f"**Current model:** {fb_name} [#{fb_sc}] (fallback)\n\n"
                                    "Type /model list to see all available models. /model <number> to switch."
                                )
            except Exception:
                pass

            # No fallback — show primary
            sc = current.get("shortcode", "?")
            name = current.get("name", "unknown")
            mn = current.get("model_name", "")
            if mn:
                return (
                    f"**Current model:** {name} ({mn}) [#{sc}]\n\n"
                    "Type /model list to see all available models. /model <number> to switch."
                )
            else:
                return (
                    f"**Current model:** {name} [#{sc}]\n\n"
                    "Type /model list to see all available models. /model <number> to switch."
                )

        if args.strip().lower() in ("list", "ls"):
            # Full listing grouped by provider
            current = db.get_agent_model(agent_id)
            current_id = current.get("id") if current else None
            providers = db.get_providers()
            all_models = db.get_enabled_llm_models()

            if not all_models:
                return "No models configured. Add models in Settings > Models."

            models_by_prov = {}
            for m in all_models:
                prov = m.get("provider", "unknown")
                models_by_prov.setdefault(prov, []).append(m)

            prov_names = {p["id"]: p.get("name", p["id"]) for p in providers}

            is_compact = False
            if channel_id:
                channel = db.get_channel(channel_id)
                if channel:
                    ch_type = channel.get("type", "")
                    is_compact = ch_type in ("telegram", "whatsapp", "whatsapp_shared")
            dot = "." if is_compact else "\\."

            def _sort_key(m):
                sc = m.get("shortcode")
                return sc if isinstance(sc, int) else 1_000_000

            lines = ["**Available Models**", ""]
            for prov_id in sorted(models_by_prov.keys()):
                prov_label = prov_names.get(prov_id, prov_id)
                lines.append(f"**{prov_label}**")
                lines.append("")
                for m in sorted(models_by_prov[prov_id], key=_sort_key):
                    sc = m.get("shortcode", "?")
                    name = m.get("name", "unknown")
                    model_name = m.get("model_name", "")
                    is_current = " ✓" if m.get("id") == current_id else ""
                    if model_name:
                        lines.append(f"{sc}{dot} {name} ({model_name}){is_current}")
                    else:
                        lines.append(f"{sc}{dot} {name}{is_current}")
                lines.append("")

            if current:
                sc = current.get("shortcode", "?")
                lines.append(f"**Current:** {current.get('name', 'unknown')} (#{sc})")
            else:
                lines.append("**Current:** none")
            lines.append("")
            lines.append("Type /model <number> or /model <provider/model> to switch.")
            if is_compact:
                return "\n".join(lines)
            return "\n\n".join(l for l in lines if l)

        # Set model
        new_model_id = args.strip()

        # Try shortcode first (numeric input)
        model = None
        if new_model_id.isdigit():
            model = db.get_model_by_shortcode(int(new_model_id))

        if not model:
            model = db.get_model_by_id(new_model_id)
        if not model:
            model = db.get_model_by_model_name(new_model_id)

        if not model:
            return f"Model '{new_model_id}' not found. Type /model to see available models."

        success = db.set_agent_model(agent_id, model["id"])
        if not success:
            return f"Failed to set model to '{new_model_id}'."

        sc = model.get("shortcode", "?")
        model_name = model.get("name", "unknown")
        model_model = model.get("model_name", "")
        if model_model:
            return f"Model set to: {model_name} ({model_model}) [#{sc}]"
        else:
            return f"Model set to: {model_name} [#{sc}]"

    command_registry.register(
        "model",
        model_handler,
        "Show or switch LLM model — /model, /model list|ls, /model [number|provider/model]",
        parameters=[{"name": "action", "options": ["current", "list", "set"]}, {"name": "model"}],
    )



    # /sub — Spawn a sub-agent with a direct task (requires subagent skill)
    def sub_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        if not args or not args.strip():
            return "Usage: /sub <task description>"

        from models.db import db

        # Super agents have implicit access to all skills
        super_agent = db.get_super_agent()
        is_super = super_agent and super_agent.get('id') == agent_id
        if not is_super:
            skills = db.get_agent_skills(agent_id)
            if "subagent" not in skills:
                return "This command requires the `subagent` skill to be assigned to this agent."

        # Fetch parent agent
        parent_agent = db.get_agent(agent_id)
        if not parent_agent:
            return "Error: Agent not found."

        # Spawn sub-agent
        from backend.subagent_manager import subagent_manager
        try:
            sub_id = subagent_manager.spawn(parent_agent)
        except ValueError as e:
            return f"Failed to spawn sub-agent: {e}"

        # Resolve report_to destination
        from backend.agent_report_to import resolve_report_to_for_subagent_spawn
        report_to_id, report_to_channel_id, _ = resolve_report_to_for_subagent_spawn(
            agent_id, external_user_id, channel_id
        )

        # Build task message with direct-spawn prefix
        task_text = args.strip()
        message = SUBAGENT_USER_DIRECT_PREFIX + task_text

        # Send task message to the sub-agent
        from backend.agent_runtime.notifier import notify_agent
        result = notify_agent(
            agent_id=sub_id,
            tag="SUBSPAWN",
            message=message,
            external_user_id=f"__agent__{agent_id}",
            channel_id=channel_id,
            dedup=False,
            trigger_llm=True,
            metadata={
                "agent_message": True,
                "from_agent_id": agent_id,
                "subagent_user_direct": True,
                "report_to_id": report_to_id,
                "report_to_channel_id": report_to_channel_id,
            },
        )

        if not result.get("success"):
            reason = result.get("reason", "unknown")
            return f"Sub-agent spawned ({sub_id}) but failed to deliver task: {reason}."

        return f"Sub-agent spawned: **{sub_id}** — task sent."

    command_registry.register(
        "sub",
        sub_handler,
        "Spawn a sub-agent with a direct task",
    )

    # /detach — Hand off the running long-running process to a background watcher
    def detach_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        from backend.agent_runtime import agent_runtime
        from backend.agent_runtime import monitors
        from backend.agent_runtime.background_jobs import background_jobs

        jobs = background_jobs.running_for_session(session_id)
        if not jobs:
            return (
                "No running background process found for this session. "
                "Check with /jobs."
            )

        already = monitors.monitored_job_ids(agent_id, session_id)
        pending = [j for j in jobs if j.job_id not in already]
        if not pending:
            names = ", ".join(f"`{j.command}`" for j in jobs)
            return (f"Already monitored: {names}.\n"
                    "I'll report back when they finish. See /jobs.")

        # End the current polling turn so the agent stops waiting and can chat.
        agent_runtime.request_stop(session_id)

        agent = {
            'agent_id': agent_id, 'session_id': session_id,
            'user_id': external_user_id, 'channel_id': channel_id,
        }
        attached, failed = [], []
        for job in pending:
            res = monitors.attach(agent, target={'job_id': job.job_id},
                                  when={'on_exit': True},
                                  note='attached via /detach')
            (failed if res.get('error') else attached).append(job)

        if not attached:
            return "Failed to detach: could not attach the monitor."

        names = ", ".join(f"`{j.command}`" for j in attached)
        msg = (f"Monitoring {len(attached)} background job(s): {names}.\n"
               "They keep running — I'll report back when they finish (even if "
               "the server restarts). Check anytime with /jobs.")
        if failed:
            msg += f"\nCould not monitor {len(failed)} job(s)."
        return msg

    command_registry.register(
        "detach",
        detach_handler,
        "Stop waiting on the running long-running process and monitor it instead",
    )

    # /jobs — List background jobs tracked for this session
    def jobs_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        import time as _time
        from backend.agent_runtime import monitors
        from backend.agent_runtime.background_jobs import (
            background_jobs, refresh_statuses)
        from models.db import db

        # Nothing polls these in the background, so refresh on demand — one
        # round-trip for the whole session.
        try:
            agent = db.get_agent(agent_id) or {}
            refresh_statuses(session_id, {**agent, 'agent_id': agent_id})
        except Exception:
            pass

        by_job = {}
        loose = []
        for m in monitors.list_for_session(agent_id, session_id):
            (by_job.setdefault(m['job_id'], []) if m.get('job_id')
             else loose).append(m)

        lines = []
        for j in background_jobs.list_for_session(session_id):
            if j.status == 'running':
                state = f"⏳ running, {int(_time.time() - j.started_at)}s"
            else:
                code = '' if j.exit_code is None else f" (exit {j.exit_code})"
                state = f"✅ {j.status}{code}"
            watchers = by_job.get(j.job_id) or []
            watch = (" · 👁 " + "; ".join(m['condition'] for m in watchers)
                     if watchers else " · unmonitored")
            log = f" · log: {j.log_file}" if j.log_file else ""
            lines.append(f"- `{j.job_id}` `{j.command}` — {state}{watch}{log}")

        for m in loose:
            lines.append(f"- `{m['monitor_id']}` watching `{m['watching']}` "
                         f"— 👁 {m['condition']}")

        if not lines:
            return ("No background jobs or monitors for this session. "
                    "Background processes run unwatched unless a monitor is "
                    "attached to them.")
        return "**Background jobs:**\n" + "\n".join(lines)

    command_registry.register(
        "jobs",
        jobs_handler,
        "List background jobs for this session and their monitors",
    )

    # /kb-organize — Manually trigger KB organizer sub-agent for the current agent
    def kb_organize_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        from backend.agent_runtime.memory_manager import (
            trigger_kb_organizer, resolve_kb_organizer_mode, sefton_tidy_agent,
        )
        from models.db import db
        agent = db.get_agent(agent_id)
        if agent and resolve_kb_organizer_mode(agent) == 'sefton':
            return sefton_tidy_agent(agent_id)
        return trigger_kb_organizer(agent_id, session_id)

    command_registry.register(
        "kb-organize",
        kb_organize_handler,
        "Manually trigger KB organizer sub-agent for the current agent",
    )

    # /dump -- Dump current session as JSONL file for download
    def dump_handler(
        session_id: str,
        agent_id: str,
        external_user_id: str,
        channel_id: Optional[str],
        args: str,
    ) -> str:
        """Dump the current agent session JSONL file and send it to the user."""
        import shutil
        from models.chatlog import _AGENTS_DIR
        from backend.agent_runtime import agent_runtime

        # Resolve the JSONL file path (same logic as ChatLog.__init__)
        hash_part = session_id
        if hash_part.startswith(f'{agent_id}-'):
            hash_part = hash_part[len(agent_id) + 1:]
        jsonl_path = os.path.join(_AGENTS_DIR, agent_id, 'sessions', f'{hash_part}.jsonl')

        if not os.path.exists(jsonl_path):
            return f"Session log not found: {jsonl_path}"

        if os.path.getsize(jsonl_path) == 0:
            return "Session log is empty. No messages to dump yet."

        # Create a snapshot copy so the live file is not affected
        dump_path = f"/tmp/dump-{agent_id}-{hash_part}.jsonl"
        try:
            shutil.copy2(jsonl_path, dump_path)
        except Exception as e:
            _logger.error("Failed to copy session log: %s", e, exc_info=True)
            return f"Failed to create dump file: {e}"

        # Send the dump file to the user via the chat UI
        caption = f"Session dump: {session_id}"
        mime_type = "application/jsonl"
        try:
            success = agent_runtime.send_file_as_bot(
                session_id, dump_path, caption, mime_type
            )
        except Exception as e:
            _logger.error("Failed to send dump file: %s", e, exc_info=True)
            # Clean up the dump file on failure
            try:
                os.remove(dump_path)
            except Exception:
                pass
            return f"Failed to send dump file: {e}"

        return f"Session dump sent as: dump-{agent_id}-{hash_part}.jsonl"

    command_registry.register(
        "dump",
        dump_handler,
        "Dump current session as JSONL file for download",
    )

# Register builtins at import time
_register_builtins()
