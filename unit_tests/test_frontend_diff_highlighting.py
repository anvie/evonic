import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RENDERERS = ROOT / "static/js/chat-ui/renderers.js"

_RUNNER_SCRIPT = """\
import { readFileSync } from 'fs';

const source = readFileSync('{renderers_path}', 'utf8');
const start = source.indexOf('function escape(');
const end = source.indexOf('// \\u2500\\u2500 Tool result rendering helpers');
let code = source.slice(start, end).replace(/^export\\s+/gm, '');

const document = {
    createElement() {
        return {
            textContent: '',
            get innerHTML() {
                return this.textContent
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#39;');
            }
        };
    }
};

globalThis.document = document;
const fn = new Function('document', code + '; return highlightDiff;');
const highlightDiff = fn(document);

const patch = JSON.parse(process.argv[2]);
process.stdout.write(highlightDiff(patch));
"""


def _highlight_diff(patch: str) -> str:
    if not shutil.which("node"):
        pytest.skip("Node.js is required for the frontend diff renderer test")

    script = _RUNNER_SCRIPT.replace("{renderers_path}", str(RENDERERS))

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".mjs", delete=False, encoding="utf-8"
    ) as f:
        f.write(script)
        runner_path = f.name

    try:
        result = subprocess.run(
            ["node", runner_path, json.dumps(patch)],
            cwd=str(ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout
    finally:
        os.unlink(runner_path)


def test_diff_renderer_highlights_only_changed_intraline_tokens():
    html = _highlight_diff(
        "- Ini adalah text yg belum dirubah\n"
        "+ Ini adalah text yg sudah berubah"
    )

    assert 'class="hl-diff-remove-change">belum</span>' in html
    assert 'class="hl-diff-add-change">sudah</span>' in html
    assert 'Ini adalah text yg ' in html
    assert 'class="hl-diff-remove">-' in html
    assert 'class="hl-diff-add">+' in html


def test_diff_renderer_leaves_unpaired_lines_without_intraline_marks():
    html = _highlight_diff("- removed line\n+ added line\n+ another addition")

    assert html.count("hl-diff-add-change") == 1
    assert "another addition</span>" in html


def test_diff_renderer_escapes_changed_fragments():
    html = _highlight_diff("- <script>old</script>\n+ <script>new</script>")

    assert "&lt;script&gt;" in html
    assert "<script>" not in html
