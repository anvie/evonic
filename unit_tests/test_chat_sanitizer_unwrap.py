"""Children of a disallowed tag must be sanitized when the tag is unwrapped."""
import subprocess
from pathlib import Path

import pytest

from unit_tests._node_runtime import node_bin

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [ROOT / "static/js/chat-ui/renderers.js", ROOT / "static/js/chat-ui.js"]

_SCRIPT = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('const ALLOWED_TAGS');
const end = source.indexOf('export function sanitize(html)') !== -1
    ? source.indexOf('export function sanitize(html)')
    : source.indexOf('function sanitize(html)');
const code = source.slice(start, end);

class El {
    constructor(tag) { this.nodeType = 1; this.tagName = tag.toUpperCase(); this.attrs = {}; this.childNodes = []; this.parentNode = null; }
    get firstChild() { return this.childNodes[0] || null; }
    get attributes() { return Object.keys(this.attrs).map(name => ({ name })); }
    getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
    setAttribute(n, v) { this.attrs[n] = v; }
    removeAttribute(n) { delete this.attrs[n]; }
    append(c) { c.parentNode = this; this.childNodes.push(c); return c; }
    insertBefore(c, ref) {
        if (c.parentNode) c.parentNode.removeChild(c);
        c.parentNode = this; this.childNodes.splice(this.childNodes.indexOf(ref), 0, c);
    }
    removeChild(c) { this.childNodes.splice(this.childNodes.indexOf(c), 1); c.parentNode = null; }
}
const Node = { ELEMENT_NODE: 1 };
const walk = new Function('Node', code + '; return _walkSanitize;')(Node);

// <div><x><y><img src="/api/a.png" onerror="evil()"></y></x></div>
const root = new El('div');
const x = root.append(new El('x'));
const y = x.append(new El('y'));
const img = y.append(new El('img'));
img.setAttribute('src', '/api/a.png');
img.setAttribute('onerror', 'evil()');
walk(root);
const out = root.childNodes[0];
console.log(JSON.stringify({ tag: out.tagName, attrs: out.attrs, parent: root.childNodes.length }));
"""


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_unwrapped_children_are_sanitized(path):
    node = node_bin()
    if not node:
        pytest.skip("A working Node.js runtime is required")
    out = subprocess.run(
        [node, "-e", _SCRIPT, str(path)], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert out == '{"tag":"IMG","attrs":{"src":"/api/a.png"},"parent":1}'
