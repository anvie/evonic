"""Regression guards for attachment capability checks in the chat drop zone."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _drop_handler() -> str:
    sessions = (ROOT / "templates/sessions.html").read_text(encoding="utf-8")
    handler_start = sessions.index("zone.addEventListener('drop', function(e)")
    handler_end = sessions.index("    });", handler_start)
    return sessions[handler_start:handler_end]


def test_disabled_attachments_are_rejected_before_drop_modal_opens():
    handler = _drop_handler()

    capability_guard = handler.index("if (!currentAttachmentsEnabled)")
    file_type_guard = handler.index("if (!_isFileAccepted(file))")
    modal_open = handler.index("openChatDropModal(file);")

    assert capability_guard < file_type_guard < modal_open
    assert "This agent does not support file attachments." in handler
    assert "Enable attachments in Agent Settings to upload files." in handler
    assert "showToast" in handler[capability_guard:file_type_guard]
    assert "return;" in handler[capability_guard:file_type_guard]


def test_enabled_attachment_drop_still_reaches_modal():
    handler = _drop_handler()

    assert "if (!currentAttachmentsEnabled)" in handler
    assert "if (!_isFileAccepted(file))" in handler
    assert handler.rstrip().endswith("openChatDropModal(file);")
