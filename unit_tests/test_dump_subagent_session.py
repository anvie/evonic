"""/dump must find a sub-agent session's log: it lives under the PARENT's agent dir."""
import os
import shutil
import uuid
from unittest.mock import patch

from backend.slash_commands import command_registry
from models.chatlog import chatlog_manager, _AGENTS_DIR


def test_dump_finds_subagent_session_log_under_parent():
    parent = f"p{uuid.uuid4().hex[:6]}"
    sub_id = f"{parent}_organizer_1"
    sid = f"{sub_id}-abc123"
    sent = {}
    try:
        # runtime writes the log with db_agent_id = parent for sub-agent sessions
        chatlog_manager.get(parent, sid).append({"type": "final", "content": "hi"})
        from backend.agent_runtime import agent_runtime
        from models.db import db

        def _send(session_id, path, caption, mime):
            sent["path"] = path
            sent["size"] = os.path.getsize(path)
            return True

        with patch.object(db, "_find_agent_for_session", return_value=parent), \
             patch.object(agent_runtime, "send_file_as_bot", _send):
            out = command_registry.get("dump").handler(sid, sub_id, "u", None, "")
        assert "not found" not in out
        assert sent["size"] > 0
    finally:
        chatlog_manager.evict(parent, sid)
        shutil.rmtree(os.path.join(_AGENTS_DIR, parent), ignore_errors=True)
        try:
            os.remove(sent.get("path", ""))
        except OSError:
            pass
