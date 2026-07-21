"""Regression guards for attachment capability checks in the chat drop zone."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _function_body(template: str, signature: str, end_marker: str) -> str:
    source = (ROOT / template).read_text(encoding="utf-8")
    start = source.index(signature)
    end = source.index(end_marker, start)
    return source[start:end]


def _drop_handler() -> str:
    return _function_body(
        "templates/sessions.html",
        "zone.addEventListener('drop', function(e)",
        "    });",
    )


def _agent_detail_attachment_ingest() -> str:
    return _function_body(
        "templates/agent_detail.html",
        "function addChatFileAttachments(files",
        "\nfunction onChatFileSelected",
    )


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


def test_agent_detail_rejects_attachments_before_mutating_composer():
    handler = _agent_detail_attachment_ingest()

    capability_guard = handler.index("if (!CHAT_ATTACHMENTS_ENABLED)")
    file_filter = handler.index("const accepted = candidates.filter(_isFileAccepted);")
    pending_file_mutation = handler.index("_chatPendingFiles.push")
    image_reference_mutation = handler.index("_insertChatImageReference")
    preview_mutation = handler.index("_renderChatFilePreviews")

    assert capability_guard < file_filter < pending_file_mutation
    assert capability_guard < image_reference_mutation
    assert capability_guard < preview_mutation
    assert "This agent does not support file attachments." in handler
    assert "Enable attachments in Agent Settings to upload files." in handler
    assert "showToast" in handler[capability_guard:file_filter]
    assert "return;" in handler[capability_guard:file_filter]


def test_agent_detail_capability_uses_server_rendered_agent_setting():
    template = (ROOT / "templates/agent_detail.html").read_text(encoding="utf-8")

    assert (
        "const CHAT_ATTACHMENTS_ENABLED = "
        "{{ 'true' if agent.attachments_enabled else 'false' }};"
    ) in template
