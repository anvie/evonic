"""UI regression tests for the Kanban image-attachment experience.

Task #36 replaced the raw, unstyled ``<input type="file">`` in the task detail
modal (and unified the attachment pickers) with theme-matched, accessible
dropzones that support drag & drop, thumbnail previews, a selected-file counter,
per-file remove actions and a real upload-progress bar.

These tests lock that contract in place by asserting the template still exposes
the required markup hooks and JavaScript helpers. They are deliberately
structural (no browser required) so they run with the rest of the plugin suite.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "kanban.html"
SOURCE = TEMPLATE.read_text(encoding="utf-8")

# Committed Tailwind build artifact. Classes that only ever appear in plugin
# templates are NOT in this bundle, so the attachment UI must not depend on
# them (it uses the template's own ``.kb-*`` rules for interactive states).
COMPILED_CSS = Path(__file__).resolve().parents[3] / "static" / "css" / "tailwind.css"

# HTML tag sets used by the balance check below.
_BLOCK_TAGS = {
    "div",
    "span",
    "ul",
    "li",
    "label",
    "a",
    "button",
    "form",
    "textarea",
    "select",
    "option",
    "p",
}


def _between(start: str, end: str) -> str:
    """Return the template slice between two unique anchors."""
    start_idx = SOURCE.index(start)
    end_idx = SOURCE.index(end, start_idx)
    return SOURCE[start_idx:end_idx]


def _function_body(name: str) -> str:
    """Return the source of a top-level ``function name(...) { ... }`` block."""
    match = re.search(r"function\s+" + re.escape(name) + r"\s*\(", SOURCE)
    assert match, f"function {name}() not found in {TEMPLATE.name}"
    open_brace = SOURCE.index("{", match.end())
    depth = 0
    for idx in range(open_brace, len(SOURCE)):
        char = SOURCE[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[open_brace : idx + 1]
    raise AssertionError(f"unbalanced braces while reading function {name}()")


COMMENT_COMPOSER = _between("<!-- Comments panel -->", "<!-- Process panel -->")
# Both fragments are sliced from a complete wrapper element so tag balance can
# be asserted meaningfully without a full HTML document.
DETAIL_EDIT_ATTACHMENTS = _between(
    '<div class="mb-5">\n'
    + " " * 24
    + '<div class="mb-1.5 flex items-center justify-between gap-3">',
    "cancelEditFromDetail()",
)
CREATE_FORM_ATTACHMENTS = _between(
    '<div class="mb-5">\n'
    + " " * 16
    + '<div class="flex items-center justify-between gap-3 mb-1.5">',
    'id="modal-submit"',
)


class _TagBalanceParser(HTMLParser):
    """Track nesting depth of container tags inside a markup fragment."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.min_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in _BLOCK_TAGS:
            self.depth -= 1
            self.min_depth = min(self.min_depth, self.depth)


# ---------------------------------------------------------------------------
# Comment composer (the UI shown in the task report screenshot)
# ---------------------------------------------------------------------------


def test_comment_composer_no_longer_uses_a_raw_native_file_input():
    """The picker must be a styled dropzone, not an OS "Browse..." button."""
    assert 'id="comment-files"' in COMMENT_COMPOSER
    input_tag = COMMENT_COMPOSER.split('id="comment-files"', 1)[1].split(">", 1)[0]
    assert 'class="sr-only"' in input_tag, "file input must be visually hidden"
    assert 'onchange="handleCommentFilesSelected(this)"' in input_tag

    # The old raw native-input chrome is gone everywhere in the template.
    assert "file:mr-2" not in SOURCE
    assert "file:mr-3" not in SOURCE
    assert "file:py-1" not in SOURCE
    assert 'for="comment-files" class="text-xs font-medium' not in SOURCE


def test_comment_dropzone_is_labelled_and_keyboard_accessible():
    assert '<label for="comment-files" id="comment-files-dropzone"' in COMMENT_COMPOSER
    assert 'title="Attach images to this comment"' in COMMENT_COMPOSER
    assert 'class="kb-dropzone"' in COMMENT_COMPOSER
    # focus-within keeps a visible focus ring when the hidden input is focused.
    assert ".kb-dropzone:hover, .kb-dropzone:focus-within" in SOURCE
    assert ".kb-dropzone:focus-within { box-shadow:" in SOURCE
    assert "cursor: pointer;" in SOURCE


def test_comment_composer_exposes_accessible_error_and_count_regions():
    error_region = COMMENT_COMPOSER.split('id="comment-files-error"', 1)[1].split(">", 1)[0]
    assert 'role="alert"' in error_region
    assert 'aria-live="polite"' in error_region

    count_badge = COMMENT_COMPOSER.split('id="comment-files-count"', 1)[1].split(">", 1)[0]
    assert 'aria-live="polite"' in count_badge


def test_comment_dropzone_supports_drag_and_drop():
    assert 'ondragenter="handleCommentFilesDrag(event, true)"' in COMMENT_COMPOSER
    assert 'ondragover="handleCommentFilesDrag(event, true)"' in COMMENT_COMPOSER
    assert 'ondragleave="handleCommentFilesDrag(event, false)"' in COMMENT_COMPOSER
    assert 'ondrop="handleCommentFilesDrop(event)"' in COMMENT_COMPOSER


def test_comment_thumbnails_use_a_responsive_grid():
    assert 'id="comment-files-list" class="kb-file-grid"' in SOURCE
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in SOURCE
    assert "@media (min-width: 640px) { .kb-file-grid" in SOURCE


def test_comment_composer_has_an_upload_progress_bar():
    assert 'id="comment-upload-progress" class="hidden mt-2"' in COMMENT_COMPOSER
    track = COMMENT_COMPOSER.split('id="comment-upload-progress-bar"', 1)[1].split(">", 1)[0]
    assert 'role="progressbar"' in track
    assert 'aria-valuemin="0"' in track
    assert 'aria-valuemax="100"' in track
    assert 'aria-valuenow="0"' in track
    assert 'id="comment-upload-progress-fill"' in COMMENT_COMPOSER
    assert 'id="comment-upload-progress-pct"' in COMMENT_COMPOSER
    assert 'id="comment-upload-progress-label"' in COMMENT_COMPOSER


# ---------------------------------------------------------------------------
# Detail edit panel — same design language as the comment composer
# ---------------------------------------------------------------------------


def test_detail_edit_panel_uses_the_same_styled_dropzone():
    assert 'id="detail-task-files"' in DETAIL_EDIT_ATTACHMENTS
    input_tag = DETAIL_EDIT_ATTACHMENTS.split('id="detail-task-files"', 1)[1].split(">", 1)[0]
    assert 'class="sr-only"' in input_tag
    assert 'onchange="handleDetailFilesSelected(this)"' in input_tag

    assert '<label for="detail-task-files" id="detail-task-files-dropzone"' in SOURCE
    assert 'title="Add attachments to this task"' in SOURCE
    assert 'ondrop="handleDetailFilesDrop(event)"' in DETAIL_EDIT_ATTACHMENTS
    assert 'ondragenter="handleDetailFilesDrag(event, true)"' in DETAIL_EDIT_ATTACHMENTS


def test_detail_edit_panel_has_progress_and_thumbnail_grid():
    assert 'id="detail-task-upload-progress"' in DETAIL_EDIT_ATTACHMENTS
    assert 'id="detail-task-upload-progress-bar"' in DETAIL_EDIT_ATTACHMENTS
    assert 'role="progressbar"' in DETAIL_EDIT_ATTACHMENTS
    assert 'id="detail-task-files-list" class="kb-file-grid"' in SOURCE
    assert 'id="detail-task-files-count"' in DETAIL_EDIT_ATTACHMENTS


def test_create_task_form_dropzone_keeps_parity():
    """The Add/Edit task form keeps its dropzone and gains the shared counter."""
    assert 'id="task-files-dropzone"' in CREATE_FORM_ATTACHMENTS
    assert 'id="task-files-count"' in CREATE_FORM_ATTACHMENTS
    assert 'id="task-files-list" class="kb-file-grid"' in SOURCE
    assert 'class="kb-dropzone kb-dropzone-lg"' in CREATE_FORM_ATTACHMENTS
    assert 'id="task-files-dropzone" class="kb-dropzone' in SOURCE


# ---------------------------------------------------------------------------
# JavaScript state + behaviour hooks
# ---------------------------------------------------------------------------


def test_comment_pending_file_state_and_helpers_exist():
    assert "let _pendingCommentFiles = [];" in SOURCE
    for fn in (
        "handleCommentFilesSelected",
        "handleCommentFilesDrag",
        "handleCommentFilesDrop",
        "_renderCommentPendingFiles",
        "_removePendingCommentFile",
        "_clearCommentPendingFiles",
    ):
        assert f"function {fn}(" in SOURCE, f"missing helper {fn}()"


def test_submit_comment_uses_the_pending_state_not_the_native_input():
    body = _function_body("submitComment")
    assert "_pendingCommentFiles.slice()" in body
    assert "$('#comment-files')[0].files" not in body
    assert "_clearCommentPendingFiles();" in body
    assert "_COMMENT_PROGRESS" in body
    # Pending images are cleared before the composer is reused for a new task.
    assert "_clearCommentPendingFiles();" in _function_body("openDetailModal")
    assert "_clearCommentPendingFiles();" in _function_body("closeDetailModal")


def test_shared_pending_file_renderer_shows_thumbnails_sizes_and_remove_action():
    assert "function _formatFileSize(" in SOURCE
    assert "function _escAttr(" in SOURCE
    assert "function _setPendingCount(" in SOURCE
    assert "function _renderPendingFiles(listId, errorId, arr, removeFn, countId)" in SOURCE
    # The old plain name-only row helper is gone.
    assert "_fileThumbHtml" not in SOURCE
    assert "No files selected.</li>" not in SOURCE

    card = _function_body("_pendingFileCard")
    assert "f.preview" in card, "thumbnail preview must be rendered"
    assert "_formatFileSize(f.size)" in card, "file size must be displayed"
    assert 'aria-label="Remove ' in card, "remove action must be labelled"
    assert 'class="kb-file-card"' in card
    assert 'class="kb-file-remove"' in card
    # Revealed on hover/focus, always visible on touch devices.
    assert ".kb-file-card:hover .kb-file-remove, .kb-file-remove:focus-visible { opacity: 1; }" in SOURCE
    assert "@media (hover: none) { .kb-file-remove { opacity: 1; } }" in SOURCE

    # Every pending list delegates to the shared card renderer.
    assert "_pendingFileCard(f, i, removeFn)" in SOURCE


def test_upload_progress_is_driven_by_xhr_upload_events():
    body = _function_body("uploadAttachments")
    assert "uploadAttachments(taskId, files, done, progressIds)" in SOURCE
    assert "const ids = progressIds || null;" in body
    assert "xhr.upload.addEventListener('progress'" in SOURCE
    assert "_setUploadProgress(ids, 0, label)" in body
    assert "done([])" in body, "upload callback contract must be preserved"

    assert "function _setUploadProgress(ids, percent, label)" in SOURCE
    assert "function _resetUploadProgress(ids)" in SOURCE
    assert "function _progressXhrFactory(ids, labelFor)" in SOURCE
    assert "aria-valuenow" in _function_body("_setUploadProgress")

    # The progress bar is wired into both upload paths.
    assert "}, _DETAIL_PROGRESS);" in _function_body("saveFromDetail")
    assert "_progressXhrFactory(files.length ? _COMMENT_PROGRESS : null" in _function_body(
        "submitComment"
    )


def test_all_file_inputs_are_hidden_and_wired():
    inputs = re.findall(r'<input type="file"[^>]*>', SOURCE, re.S)
    assert len(inputs) == 3, f"expected 3 file inputs, found {len(inputs)}"
    for tag in inputs:
        assert 'class="sr-only"' in tag, f"file input is still visible: {tag!r}"


# ---------------------------------------------------------------------------
# Structural sanity
# ---------------------------------------------------------------------------


def test_template_is_valid_jinja():
    Environment().parse(SOURCE)


def test_attachment_markup_has_no_stray_closing_tags():
    '''No fragment may close a container tag it never opened.

    The extracted fragments intentionally start and/or end mid-document, so a
    positive final depth is expected; a negative depth means real markup damage.
    '''
    for name, fragment in (
        ("comment composer", COMMENT_COMPOSER),
        ("detail edit attachments", DETAIL_EDIT_ATTACHMENTS),
        ("create form attachments", CREATE_FORM_ATTACHMENTS),
    ):
        parser = _TagBalanceParser()
        parser.feed(fragment)
        parser.close()
        assert parser.min_depth == 0, f"stray closing tag in {name} markup"


def test_comment_composer_markup_is_balanced():
    parser = _TagBalanceParser()
    parser.feed(COMMENT_COMPOSER)
    parser.close()
    assert parser.depth == 0, "unbalanced container tags in the comment composer"


# ---------------------------------------------------------------------------
# Stylesheet contract — the pickers must never render unstyled
# ---------------------------------------------------------------------------


def test_attachment_ui_uses_no_classes_missing_from_the_compiled_bundle():
    """Every Tailwind utility in the pickers must exist in the built CSS.

    ``static/css/tailwind.css`` is a committed build artifact that only bundles
    classes found in ``templates/`` and ``static/js/``.  A utility that exists
    only in the plugin template silently renders unstyled, so it is asserted
    here; interactive states live in the template's own ``.kb-*`` rules.
    """
    css = COMPILED_CSS.read_text(encoding="utf-8")

    def selector(token: str) -> str:
        return "." + re.sub(r"([:/.\[\]%#!(),>+~*=@'])", r"\\\1", token)

    fragments = [COMMENT_COMPOSER, DETAIL_EDIT_ATTACHMENTS, CREATE_FORM_ATTACHMENTS]
    fragments.append(_function_body("_pendingFileCard"))

    missing = set()
    for fragment in fragments:
        for raw in re.findall(r'class="([^"]*)"', fragment):
            for token in raw.split():
                if token.startswith("kb-") or token == "group":
                    continue
                if selector(token) not in css:
                    missing.add(token)
    assert not missing, f"classes missing from static/css/tailwind.css: {sorted(missing)}"


def test_attachment_ui_states_are_declared_in_the_template_stylesheet():
    for rule in (
        ".kb-dropzone:hover, .kb-dropzone:focus-within",
        "html.dark .kb-dropzone:hover",
        ".kb-dropzone:focus-within { box-shadow:",
        ".kb-file-card:hover .kb-file-remove, .kb-file-remove:focus-visible { opacity: 1; }",
        ".kb-file-remove:hover { background: #dc2626; }",
        ".kb-progress-fill {",
        "html.dark .kb-progress-track",
    ):
        assert rule in SOURCE, f"missing stylesheet rule: {rule}"
