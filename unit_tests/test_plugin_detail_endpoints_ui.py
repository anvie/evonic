"""UI regression tests for the "HTTP/API Endpoints" section on the plugin
detail page (task #35).

The endpoint list is rendered client-side by ``templates/plugin_detail.html``
from ``pluginData.endpoints`` (shape from
``PluginManager.get_plugin_endpoints``: ``{path, methods[], endpoint}``).

These tests pin two contracts:

* the *page* contract — the endpoints card ships a search input plus
  Previous/Next-only pagination controls and wires them to the renderer;
* the *behaviour* — the extracted renderer JS, executed under Node with a tiny
  DOM stub, filters by query and paginates the filter result, with the two
  features composing (search narrows the list, pagination walks the narrowed
  set). Skips if Node.js is unavailable, matching
  ``test_frontend_diff_highlighting.py``.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import app
import routes.plugins as plugins_routes
from backend.plugin_manager import plugin_manager

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "plugin_detail.html"

PLUGIN_ID = "endpoints_ui_demo"


@pytest.fixture
def client():
    with app.test_client() as test_client:
        yield test_client


def login(client):
    with client.session_transaction() as session:
        session["authenticated"] = True


def fake_manifest(endpoint_count=26):
    """A deterministic plugin manifest: `endpoint_count` endpoints with
    alternating method sets, plus one `/health` route."""
    endpoints = []
    for i in range(1, endpoint_count):
        endpoints.append({
            "path": f"/plugin/demo/items/{i}",
            "methods": ["GET"] if i % 2 else ["GET", "POST"],
            "endpoint": f"demo.items_{i}",
        })
    endpoints.append({"path": "/plugin/demo/health", "methods": ["GET"], "endpoint": "demo.health"})
    return {
        "id": PLUGIN_ID,
        "name": "Endpoints UI Demo",
        "version": "1.0.0",
        "enabled": True,
        "category": "user",
        "events": [],
        "variables": [],
        "config": {},
        "endpoints": endpoints,
        "endpoint_count": len(endpoints),
        "readme": "",
    }


# ---------------------------------------------------------------------------
# 1. Page contract: markup + wiring
# ---------------------------------------------------------------------------

def test_endpoints_section_ships_search_and_pager(client, monkeypatch):
    login(client)
    monkeypatch.setattr(plugin_manager, "get_plugin", lambda _pid: fake_manifest())
    response = client.get(f"/plugins/{PLUGIN_ID}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    # Search surface.
    assert 'id="endpoints-search"' in html
    assert 'id="endpoints-search-wrap"' in html

    # Pagination surface: Previous + Next controls and a page indicator.
    assert 'id="endpoints-pagination"' in html
    assert 'id="endpoints-prev"' in html
    assert 'id="endpoints-next"' in html
    assert 'id="endpoints-page-info"' in html

    # The controls are icon-only (Lucide chevrons) \u2014 no "Previous"/"Next" text.
    assert re.search(r'id="endpoints-prev"[^>]*>\s*<i data-lucide="chevron-left"', html)
    assert re.search(r'id="endpoints-next"[^>]*>\s*<i data-lucide="chevron-right"', html)
    assert ">Previous<" not in html
    assert ">Next<" not in html
    # Icon-only buttons still carry accessible labels.
    assert 'aria-label="Previous page"' in html
    assert 'aria-label="Next page"' in html

    # Renderer wiring present.
    for marker in (
        "function renderEndpoints(",
        "function renderEndpointsPage(",
        "function filteredEndpoints(",
        "function setupEndpointsControls(",
    ):
        assert marker in html, marker
    assert "ENDPOINTS_PER_PAGE" in html
    assert "filtered.slice(start, start + ENDPOINTS_PER_PAGE)" in html


def test_pagination_has_no_numbered_page_navigation(client, monkeypatch):
    login(client)
    monkeypatch.setattr(plugin_manager, "get_plugin", lambda _pid: fake_manifest())
    html = client.get(f"/plugins/{PLUGIN_ID}").get_data(as_text=True)

    # Exactly the two controls — no per-page number links.
    assert len(re.findall(r'<button[^>]*id="endpoints-(?:prev|next)"', html)) == 2
    assert re.search(r'endpoints-page-\d', html) is None
    assert "page-link" not in html
    assert "page-number" not in html


# ---------------------------------------------------------------------------
# 2. Behaviour: run the real renderer JS under Node with a DOM stub
# ---------------------------------------------------------------------------

def _extract_endpoints_js() -> str:
    source = TEMPLATE.read_text(encoding="utf-8")
    start = source.index("// ==================== HTTP/API Endpoints")
    end = source.index("// ==================== About / README")
    return source[start:end]


_NODE_HARNESS = r"""
const fs = require('fs');

function makeEl(id) {
  const el = { id, value: '', disabled: false, innerHTML: '', textContent: '', _h: {}, _cls: new Set() };
  el.classList = {
    add: (...c) => c.forEach(x => el._cls.add(x)),
    remove: (...c) => c.forEach(x => el._cls.delete(x)),
    contains: c => el._cls.has(c),
    toggle: (c, f) => { (f === undefined ? !el._cls.has(c) : f) ? el._cls.add(c) : el._cls.delete(c); },
  };
  el.addEventListener = (ev, fn) => { el._h[ev] = fn; };
  el.click = () => { if (el._h.click) el._h.click(); };
  el.input = v => { el.value = v; if (el._h.input) el._h.input(); };
  return el;
}

const ids = ['endpoints-list', 'endpoint-count', 'endpoints-pagination',
             'endpoints-page-info', 'endpoints-prev', 'endpoints-next',
             'endpoints-search', 'endpoints-search-wrap'];
const reg = {};
ids.forEach(i => { reg[i] = makeEl(i); });

globalThis.document = { getElementById: id => reg[id] || null, createElement: () => makeEl('tmp') };
globalThis.esc = s => String(s == null ? '' : s);

const block = fs.readFileSync(process.argv[2], 'utf8');
const fn = new Function(block + '\nreturn { renderEndpoints, renderEndpointsPage };');
const api = fn();
api.renderEndpoints(JSON.parse(fs.readFileSync(process.argv[3], 'utf8')));

function count() { return (reg['endpoints-list'].innerHTML.match(/plugin\/demo\//g) || []).length; }
function snap() {
  return {
    items: count(),
    info: reg['endpoints-page-info'].textContent,
    badge: reg['endpoint-count'].textContent,
    prevDisabled: reg['endpoints-prev'].disabled,
    nextDisabled: reg['endpoints-next'].disabled,
    pagerHidden: reg['endpoints-pagination']._cls.has('hidden'),
    searchHidden: reg['endpoints-search-wrap']._cls.has('hidden'),
    emptyMsg: reg['endpoints-list'].innerHTML.includes('No endpoints match your search'),
  };
}

const out = { initial: snap() };
reg['endpoints-next'].click();
out.afterNext = snap();
reg['endpoints-next'].click();
out.page3 = snap();
reg['endpoints-next'].click();
out.clamped = snap();
reg['endpoints-prev'].click();
out.afterPrev = snap();

reg['endpoints-search'].input('health');
out.searchHealth = snap();
reg['endpoints-search'].input('zzz-no-match');
out.searchNone = snap();
reg['endpoints-search'].input('');
out.searchCleared = snap();

api.renderEndpoints([]);
out.empty = snap();

process.stdout.write(JSON.stringify(out));
"""


def _run_harness(tmp_path, endpoints):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the endpoints renderer behaviour test")

    import json

    block = tmp_path / "endpoints_block.js"
    block.write_text(_extract_endpoints_js(), encoding="utf-8")
    data = tmp_path / "endpoints.json"
    data.write_text(json.dumps(endpoints), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(_NODE_HARNESS, encoding="utf-8")

    proc = subprocess.run(
        [node, str(harness), str(block), str(data)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_search_and_pagination_compose(tmp_path):
    result = _run_harness(tmp_path, fake_manifest()["endpoints"])
    total = len(fake_manifest()["endpoints"])  # 26 -> 3 pages of 10

    # Page 1 of 3.
    assert result["initial"]["items"] == 10
    assert result["initial"]["info"] == "Page 1 of 3"
    assert result["initial"]["prevDisabled"] is True
    assert result["initial"]["nextDisabled"] is False
    assert result["initial"]["pagerHidden"] is False
    assert result["initial"]["searchHidden"] is False
    assert result["initial"]["badge"] == f"({total})"

    # Next -> page 2, then page 3 (the remainder).
    assert result["afterNext"]["info"] == "Page 2 of 3"
    assert result["afterNext"]["prevDisabled"] is False
    assert result["page3"]["info"] == "Page 3 of 3"
    assert result["page3"]["items"] == total - 20
    assert result["page3"]["nextDisabled"] is True
    # Next beyond the last page clamps (no blank page).
    assert result["clamped"]["info"] == "Page 3 of 3"
    # Previous walks back.
    assert result["afterPrev"]["info"] == "Page 2 of 3"


def test_search_filters_and_resets_to_first_page(tmp_path):
    result = _run_harness(tmp_path, fake_manifest()["endpoints"])

    # Search narrows to a single endpoint -> one page, pager hidden, badge shows the ratio.
    assert result["searchHealth"]["items"] == 1
    assert result["searchHealth"]["badge"] == "(1/26)"
    assert result["searchHealth"]["pagerHidden"] is True

    # A query with no matches shows the empty-filter message.
    assert result["searchNone"]["items"] == 0
    assert result["searchNone"]["emptyMsg"] is True

    # Clearing the query restores the full list on page 1.
    assert result["searchCleared"]["items"] == 10
    assert result["searchCleared"]["badge"] == "(26)"
    assert result["searchCleared"]["info"] == "Page 1 of 3"


def test_empty_endpoint_list_hides_controls(tmp_path):
    result = _run_harness(tmp_path, [])
    assert result["empty"]["items"] == 0
    assert result["empty"]["searchHidden"] is True
    assert result["empty"]["pagerHidden"] is True
    assert result["empty"]["badge"] == "(0)"
