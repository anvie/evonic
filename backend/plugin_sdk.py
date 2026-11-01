"""
Plugin SDK — provides the API surface for plugin event handlers.

Each handler receives a fresh PluginSDK instance with:
- send_message(): send a message to a user via an agent session channel
- http_request(): make HTTP requests to external APIs
- get_session_messages(): read messages from a session
- get_session(): get session details
- log(): log with plugin context
"""

import logging
import os
import sqlite3
import requests as http_lib
from contextlib import contextmanager
from typing import Generator, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_logger = logging.getLogger(__name__)
PLUGIN_DB_DIR = os.path.join(BASE_DIR, 'data', 'db', 'plugins')


class PluginSDK:
    def __init__(self, plugin_id: str, plugin_config: dict, event_data: dict,
                 log_callback=None):
        self.plugin_id = plugin_id
        self.config = plugin_config
        self.event = event_data
        self._log_callback = log_callback

    def send_message(self, agent_id: str, external_user_id: str,
                     channel_id: str, text: str, actions: list = None) -> dict:
        """Send a message to a user via an agent session on a specific channel.

        channel_id can be either:
        - An internal channel UUID (exact match in DB)
        - A channel type hint like 'telegram' — resolves to the first active
          channel of that type for the agent

        Resolves (or creates) a session from agent_id + external_user_id + channel_id,
        saves the message as an assistant message, and sends it via the channel.

        ``actions`` is an optional list of ``{label, namespace, payload}``
        interactive choices: buttons where the channel supports them, numbered
        text options elsewhere. Handle them with
        ``backend.channel_actions.register_channel_action_handler``.
        """
        from models.db import db
        from backend.agent_runtime import agent_runtime
        from backend.channel_actions import ChannelActionError, normalize_actions

        try:
            actions = normalize_actions(actions)
        except ChannelActionError as e:
            return {"success": False, "error": str(e)}

        resolved_channel_id = self._resolve_channel_id(agent_id, channel_id)
        if not resolved_channel_id:
            return {"success": False,
                    "error": f"No active channel found for agent '{agent_id}' "
                             f"with channel_id/type '{channel_id}'"}

        session_id = db.get_or_create_session(agent_id, external_user_id,
                                              resolved_channel_id)
        success = agent_runtime.send_as_bot(session_id, text, actions=actions or None)
        return {"success": success, "session_id": session_id}

    def send_file(self, agent_id: str, external_user_id: str,
                  channel_id: str, file_path: str,
                  caption: Optional[str] = None,
                  mime_type: Optional[str] = None) -> dict:
        """Send a file to a user via an agent session on a specific channel.

        channel_id can be either:
        - An internal channel UUID (exact match in DB)
        - A channel type hint like 'telegram' — resolves to the first active
          channel of that type for the agent

        Resolves (or creates) a session from agent_id + external_user_id +
        channel_id, then sends the file via the channel and records
        an attachment + chat entry.
        """
        from models.db import db
        from backend.agent_runtime import agent_runtime

        resolved_channel_id = self._resolve_channel_id(agent_id, channel_id)
        if not resolved_channel_id:
            return {"success": False,
                    "error": f"No active channel found for agent '{agent_id}' "
                             f"with channel_id/type '{channel_id}'"}

        session_id = db.get_or_create_session(agent_id, external_user_id,
                                              resolved_channel_id)
        success = agent_runtime.send_file_as_bot(session_id, file_path,
                                                  caption, mime_type)
        return {"success": success, "session_id": session_id}

    def _resolve_channel_id(self, agent_id: str, channel_id: str) -> str:
        """Resolve a channel_id input to an actual internal channel UUID.

        Tries in order:
        1. Exact match (already a valid channel UUID in _active)
        2. Match by channel type (e.g. 'telegram') for the given agent
        """
        from backend.channels.registry import channel_manager

        # 1. Exact match — already a valid active channel ID
        if channel_id in channel_manager._active:
            return channel_id

        # 2. Match by type — find first active channel of that type for this agent
        from models.db import db
        channels = db.get_channels(agent_id)
        for ch in channels:
            if (ch.get('type', '').lower() == channel_id.lower()
                    and ch['id'] in channel_manager._active):
                return ch['id']

        # 3. Fallback — find any active channel for this agent
        for ch in channels:
            if ch['id'] in channel_manager._active:
                return ch['id']

        return None

    def http_request(self, method: str, url: str, headers: dict = None,
                     json: dict = None, data: str = None,
                     timeout: int = 30) -> dict:
        """Make an HTTP request to an external API.

        Returns dict with status_code, body, headers, ok on success,
        or error + ok=False on failure.
        """
        try:
            resp = http_lib.request(
                method, url,
                headers=headers, json=json, data=data,
                timeout=timeout
            )
            return {
                "status_code": resp.status_code,
                "body": resp.text,
                "headers": dict(resp.headers),
                "ok": resp.ok,
            }
        except Exception as e:
            return {"error": str(e), "ok": False}

    def get_session_messages(self, session_id: str, agent_id: str = None,
                             limit: int = 50) -> list:
        """Read messages from a session."""
        from models.db import db
        return db.get_session_messages(session_id, limit=limit, agent_id=agent_id)

    def get_session(self, session_id: str) -> dict:
        """Get enriched session details (includes agent_name, channel_type, etc.)."""
        from models.db import db
        return db.get_session_with_details(session_id)

    def log(self, message: str, level: str = "info"):
        """Log a message with plugin context.

        Args:
            message: Log message text.
            level: One of 'info', 'warn', 'error'. Defaults to 'info'.
        """
        level = level if level in ('info', 'warn', 'error') else 'info'
        getattr(_logger, level, _logger.info)("[%s] %s", self.plugin_id, message)
        if self._log_callback:
            self._log_callback(self.plugin_id, level, message)

    # ==================== Scheduler ====================

    def create_schedule(self, name: str, trigger_type: str, trigger_config: dict,
                        action_type: str, action_config: dict,
                        max_runs: int = None, metadata: dict = None) -> dict:
        """Create a scheduled job owned by this plugin."""
        from backend.scheduler import scheduler
        return scheduler.create_schedule(
            name=name, owner_type='plugin', owner_id=self.plugin_id,
            trigger_type=trigger_type, trigger_config=trigger_config,
            action_type=action_type, action_config=action_config,
            max_runs=max_runs, metadata=metadata,
        )

    def cancel_schedule(self, schedule_id: str) -> bool:
        """Cancel a schedule owned by this plugin."""
        from backend.scheduler import scheduler
        return scheduler.cancel_schedule(schedule_id, owner_id=self.plugin_id)

    def list_schedules(self) -> list:
        """List all schedules owned by this plugin."""
        from backend.scheduler import scheduler
        return scheduler.list_schedules(owner_type='plugin', owner_id=self.plugin_id)

    # ==================== Slash Commands ====================

    def register_slash_command(self, name: str, description: str = '') -> 'PluginSDK':
        """
        Register a slash command that will be available to users interacting with this agent.
        
        The plugin must define a handler function named `on_slash_command_<name>` in handler.py.
        
        The handler signature:
            def on_slash_command_<name>(sdk: PluginSDK, args: str) -> str:
                ...
        
        The handler receives the PluginSDK instance and the command arguments string.
        
        Args:
            name: Command name without leading slash (e.g., 'deploy')
            description: Human-readable description for /help output
            
        Returns:
            self for chaining
        """
        import sys as _sys
        from typing import Optional as _Opt
        from backend.slash_commands import command_registry

        # Find the plugin's handler module and look for the handler function
        handler_fn = None
        for _mod_name, _mod in _sys.modules.items():
            if _mod_name.startswith('plugin_pkg_') and _mod_name.endswith('.handler'):
                handler_name = f'on_slash_command_{name}'
                handler_fn = getattr(_mod, handler_name, None)
                if handler_fn and callable(handler_fn):
                    break

        if handler_fn is None:
            _logger.warning(
                "Slash command '%s' registered by plugin '%s' has no handler function",
                name, self.plugin_id,
            )
            return self

        # Wrap the plugin handler to inject the SDK and capture the call
        def _handler(
            session_id: str,
            agent_id: str,
            external_user_id: str,
            channel_id: _Opt[str],
            args: str,
        ) -> str:
            try:
                result = handler_fn(self, args)
                return str(result) if result is not None else ''
            except Exception as e:
                _logger.error(
                    "Error executing slash command '%s' from plugin '%s': %s",
                    name, self.plugin_id, e, exc_info=True,
                )
                return f"Error: {e}"

        command_registry.register(name, _handler, description)
        return self

    # ==================== Database ====================

    @contextmanager
    def get_db_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for this plugin's database connection.
        Ensures the connection is properly closed after use.

        Yields:
            sqlite3.Connection with WAL mode and row_factory=sqlite3.Row.
        """
        conn = self.get_db()
        try:
            yield conn
        finally:
            conn.close()

    def get_db(self) -> sqlite3.Connection:
        """Get a raw SQLite connection for this plugin's database.

        IMPORTANT: You MUST manually call .close() on this connection
        to avoid file descriptor leaks. Prefer using get_db_connection().

        Returns:
            sqlite3.Connection with WAL mode and row_factory=sqlite3.Row.
        """
        db_dir = PLUGIN_DB_DIR
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, f'{self.plugin_id}.db')
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        return conn
