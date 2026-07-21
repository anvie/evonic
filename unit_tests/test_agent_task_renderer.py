"""Regression guards for readable task lists in agent state sidebars."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "static/js/agent-state.js"
TEMPLATES = (
    ROOT / "templates/sessions.html",
    ROOT / "templates/agent_detail.html",
)
STYLESHEETS = (
    ROOT / "static/style.css",
    ROOT / "static/css/evonic.css",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _task_renderer_block() -> str:
    source = _read(RENDERER)
    start = source.index("function buildAgentTasksHtml(tasks) {")
    end = source.index("/**\n * Build HTML for loaded skill badges.", start)
    return source[start:end]


def _task_styles(path: Path) -> str:
    source = _read(path)
    start = source.index("/* Readable batched tasks")
    end = source.index(".tl-border-spinner", start)
    return source[start:end]


def test_shared_renderer_preserves_statuses_and_escapes_task_text():
    renderer = _task_renderer_block()

    assert "pending:" in renderer
    assert "in_progress:" in renderer
    assert "done:" in renderer
    assert "esc(task.text)" in renderer
    assert 'aria-label="Tasks"' in renderer
    assert "agent-task__status" in renderer
    assert "agent-task__text" in renderer
    assert "marked.parseInline" not in renderer


def test_both_task_views_use_only_the_shared_renderer():
    for template_path in TEMPLATES:
        template = _read(template_path)
        assert "buildAgentTasksHtml(stateData.tasks)" in template
        assert "agent-state.js') }}?v=7" in template
        assert "task-active-text" not in template
        assert "marked.parseInline(t.text)" not in template


def test_primary_stylesheet_cache_version_is_bumped():
    base = _read(ROOT / "templates/base.html")
    assert "style.css') }}?v=22" in base


def test_task_styles_are_mirrored_and_keep_long_text_static():
    primary = _task_styles(STYLESHEETS[0])
    mirrored = _task_styles(STYLESHEETS[1])

    assert primary == mirrored
    assert ".agent-tasks__list { display: grid; gap:" in primary
    assert ".agent-task__text" in primary
    assert "overflow-wrap: anywhere" in primary
    assert "white-space: pre-wrap" in primary
    assert "html.dark .agent-task--in_progress" in primary
    assert "prefers-reduced-motion: reduce" in primary
    assert ".task-active-text" not in primary
    assert "animation: task-icon-spin" in primary
