"""/dump must find the session log of a sub-agent (kept under /tmp, not agents/)."""
import os
import shutil
import uuid
from unittest.mock import patch

from backend.slash_commands import command_registry
from backend.subagent_manager import subagent_manager, SubAgent
from models.chat import SUB_AGENTS_TMP_DIR
from models.chatlog import chatlog_manager


def test_dump_finds_subagent_session_log():
    parent = f"p{uuid.uuid4().hex[:6]}"
    sub_id = f"{parent}_organizer_1"
    sid = f"{sub_id}-abc123"
    subagent_manager._subagents[sub_id] = SubAgent(sub_id, parent, {})
    sent = {}
    try:
        chatlog_manager.get(sub_id, sid).append({"type": "final", "content": "hi"})
        from backend.agent_runtime import agent_runtime

        def _send(session_id, path, caption, mime):
            sent["path"] = path
            sent["size"] = os.path.getsize(path)
            return True

        with patch.object(agent_runtime, "send_file_as_bot", _send):
            out = command_registry.get("dump").handler(sid, sub_id, "u", None, "")
        assert "not found" not in out
        assert sent["size"] > 0 and sent["path"].endswith("abc123.jsonl")
    finally:
        subagent_manager._subagents.pop(sub_id, None)
        shutil.rmtree(os.path.join(SUB_AGENTS_TMP_DIR, sub_id), ignore_errors=True)
        try:
            os.remove(sent.get("path", ""))
        except OSError:
            pass
