"""Channel-user-controlled and agent-written content must not reach innerHTML raw."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pending_renderer() -> str:
    source = (ROOT / "templates/agent_detail.html").read_text()
    start = source.index("function _cdRenderPending(")
    return source[start:source.index("async function approvePending(")]


def test_pending_approvals_escape_channel_user_fields():
    body = _pending_renderer()
    for field in ("a.user_name", "a.external_user_id", "a.pair_code"):
        assert f"escapeHtml(" in body and field in body
    assert "${a.external_user_id}" not in body
    assert "${userName}" in body and "const userName = escapeHtml(" in body


def test_plan_viewer_sanitizes_rendered_markdown():
    source = (ROOT / "templates/sessions.html").read_text()
    assert "marked.parse(data.content)) +" in source
    assert "sanitize(marked.parse(data.content))" in source
    assert "sanitize(marked.parse(newContent))" in source
