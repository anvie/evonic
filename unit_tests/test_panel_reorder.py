"""Coverage for drag-and-drop reordering of Agent Panel buttons.

Edit mode lets the user drag a button's grip to a new slot; the resulting order
is PUT to `/plugin/panel/<agent_id>/actions/reorder` and stored in the
`sort_order` column. These tests pin:

* the rendered fragment contract (grip control, draggable cards, CSS gating),
* the ordering API (dense sort_order, strict full-set payload, agent scoping).
"""

import importlib.util
import os
import sys

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from plugins.panel.db import PanelDB


def _load_panel_routes():
    """Load plugins/panel/routes.py the way plugin_lifecycle does."""
    path = os.path.join(BASE_DIR, "plugins", "panel", "routes.py")
    spec = importlib.util.spec_from_file_location("panel_routes_reorder_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def panel_db(tmp_path, monkeypatch):
    """Point PanelDB at a temp database for the duration of a test."""
    db_path = str(tmp_path / "panel.db")
    monkeypatch.setattr("plugins.panel.db.DB_PATH", db_path)
    original_init = PanelDB.__init__

    def _init(self, agent_id, db_path=db_path):
        original_init(self, agent_id, db_path)

    monkeypatch.setattr(PanelDB, "__init__", _init)
    return db_path


@pytest.fixture
def routes(panel_db, monkeypatch):
    """The route module with agent access + realtime emission stubbed out."""
    module = _load_panel_routes()
    monkeypatch.setattr(
        module, "_check_agent_access",
        lambda agent_id: ({"id": agent_id, "name": agent_id}, None),
    )
    monkeypatch.setattr(module, "_emit_panel_updated", lambda agent_id: None)
    return module


@pytest.fixture
def client(routes):
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(routes.create_blueprint())
    app.testing = True
    return app.test_client()


ACTION = {
    "id": 1,
    "label": "Deploy",
    "action_type": "script",
    "enabled": 1,
    "slash_command": "deploy",
    "params": "[]",
    "content": "echo hi",
    "confirm_dialog": 0,
    "sort_order": 0,
    "updated_at": "2026-01-01T00:00:00+00:00",
}


def _render(routes, actions=None, name="Agent A"):
    payload = [ACTION] if actions is None else list(actions)
    return routes._render_panel_html("agent-a", {"name": name}, payload)


def _seed_three(agent_id="agent-a"):
    db = PanelDB(agent_id)
    return [
        db.add_action(label=label, action_type="script", content="echo hi",
                      sort_order=index)
        for index, label in enumerate(("One", "Two", "Three"))
    ]


def _order(agent_id="agent-a"):
    return [a["label"] for a in PanelDB(agent_id).list_actions()]


# ── rendered fragment: drag affordances ──────────────────────────────────────

def test_every_button_gets_a_drag_grip(routes):
    actions = [dict(ACTION), dict(ACTION, id=2, label="Nightly", slash_command="")]
    html = _render(routes, actions)

    assert html.count('class="pnl-tool pnl-tool-drag"') == 2
    for action_id in (1, 2):
        # The grip is its own drag source so browsers that refuse to start a
        # drag inside a <button> still work.
        assert (
            f'class="pnl-tool pnl-tool-drag" type="button" draggable="true" '
            f'data-action-id="{action_id}"' in html
        )


def test_grip_is_labelled_for_screen_readers(routes):
    html = _render(routes)

    assert 'aria-label="Reorder Deploy"' in html
    # Keyboard hint lives in the tooltip, the mode hint mentions dragging.
    assert "Drag to reorder" in html
    assert "Drag a grip to reorder" in html


def test_cards_are_only_draggable_in_edit_mode(routes):
    html = _render(routes)

    # The grid keeps the mode class, and setEditMode() flips item.draggable.
    assert "btnGrid.classList.toggle('pnl-edit-mode', editMode)" in html
    assert "item.draggable = !!on;" in html
    assert "setReorderEnabled(editMode);" in html


def test_drop_target_is_visible_while_dragging(routes):
    html = _render(routes)

    assert ".pnl-item.pnl-dragging { opacity:.4; }" in html
    assert ".pnl-item.pnl-drop-before::before" in html
    assert ".pnl-item.pnl-drop-after::after" in html


def test_drag_handlers_are_wired_to_the_grid(routes):
    html = _render(routes)

    for event in ("dragstart", "dragover", "drop", "dragend"):
        assert f"btnGrid.addEventListener('{event}'" in html
    # The drop only happens in edit mode and only after a card was picked up.
    assert "if (!editMode) { e.preventDefault(); return; }" in html
    assert "if (!dragItem) return;" in html


def test_keyboard_reordering_uses_alt_arrows(routes):
    html = _render(routes)

    assert "btnGrid.addEventListener('keydown'" in html
    assert "if (!editMode || !e.altKey) return;" in html
    assert "e.key === 'ArrowUp' ? -1 : 1" in html


def test_reorder_persists_through_the_panel_api(routes):
    html = _render(routes)

    assert "const REORDER_URL = '/plugin/panel/' + AGENT_ID + '/actions/reorder';" in html
    assert "method: 'PUT'" in html
    assert "JSON.stringify({ order: order })" in html
    # Unchanged orders are not re-sent, and failures roll the grid back.
    assert "if (order.join(',') === orderSnapshot.join(',')) return;" in html
    assert "restoreOrder(previous);" in html


def test_grid_is_empty_of_drag_state_before_edit_mode(routes):
    html = _render(routes)

    # Reorder state is captured when the mode turns on, not at render time.
    assert "if (on) orderSnapshot = currentOrder();" in html


# ── API: reorder ─────────────────────────────────────────────────────────────

def test_reorder_persists_the_new_order(client):
    one, two, three = _seed_three()
    order = [three["id"], one["id"], two["id"]]

    resp = client.put("/plugin/panel/agent-a/actions/reorder", json={"order": order})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "reordered"
    assert [a["label"] for a in body["actions"]] == ["Three", "One", "Two"]
    assert _order() == ["Three", "One", "Two"]


def test_reorder_rewrites_sort_order_densely(client):
    one, two, three = _seed_three()
    client.put(
        "/plugin/panel/agent-a/actions/reorder",
        json={"order": [three["id"], two["id"], one["id"]]},
    )

    stored = PanelDB("agent-a").list_actions()
    assert [a["sort_order"] for a in stored] == [0, 1, 2]


def test_reorder_keeps_a_single_action_in_place(client):
    action = PanelDB("agent-a").add_action(
        label="Only", action_type="prompt", content="go", sort_order=7
    )

    resp = client.put("/plugin/panel/agent-a/actions/reorder", json={"order": [action["id"]]})

    assert resp.status_code == 200
    assert PanelDB("agent-a").get_action(action["id"])["sort_order"] == 0


def test_reorder_rejects_an_incomplete_order(client):
    one, two, _three = _seed_three()

    resp = client.put(
        "/plugin/panel/agent-a/actions/reorder", json={"order": [two["id"], one["id"]]}
    )

    assert resp.status_code == 400
    assert "every panel action" in resp.get_json()["error"]
    assert _order() == ["One", "Two", "Three"]


def test_reorder_rejects_duplicate_ids(client):
    one, two, _three = _seed_three()

    resp = client.put(
        "/plugin/panel/agent-a/actions/reorder",
        json={"order": [one["id"], one["id"], two["id"]]},
    )

    assert resp.status_code == 400
    assert _order() == ["One", "Two", "Three"]


def test_reorder_rejects_another_agents_action(client):
    foreign = _seed_three("agent-b")[0]

    resp = client.put("/plugin/panel/agent-a/actions/reorder", json={"order": [foreign["id"]]})

    assert resp.status_code == 400
    assert _order() == []


def test_reorder_rejects_boolean_ids(client):
    one, two, three = _seed_three()

    resp = client.put(
        "/plugin/panel/agent-a/actions/reorder",
        json={"order": [True, two["id"], three["id"]]},
    )

    assert resp.status_code == 400
    assert _order() == ["One", "Two", "Three"]
    assert one["id"] == 1


@pytest.mark.parametrize("payload", [
    {},
    {"order": "1,2,3"},
    {"order": None},
    {"order": [1, "2"]},
    {"order": [1, 2.5]},
])
def test_reorder_rejects_malformed_payloads(client, payload):
    _seed_three()

    resp = client.put("/plugin/panel/agent-a/actions/reorder", json=payload)

    assert resp.status_code == 400
    assert _order() == ["One", "Two", "Three"]


def test_reorder_requires_a_json_object(client):
    _seed_three()

    resp = client.put(
        "/plugin/panel/agent-a/actions/reorder", data="not json",
        content_type="text/plain",
    )

    assert resp.status_code == 400


def test_reorder_on_an_empty_panel_is_a_noop(client):
    resp = client.put("/plugin/panel/agent-a/actions/reorder", json={"order": []})

    assert resp.status_code == 200
    assert resp.get_json()["actions"] == []


def test_reorder_nudges_other_clients(routes, monkeypatch):
    emitted = []
    monkeypatch.setattr(routes, "_emit_panel_updated", lambda agent_id: emitted.append(agent_id))

    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(routes.create_blueprint())
    app.testing = True
    client = app.test_client()

    one, two, three = _seed_three()
    client.put(
        "/plugin/panel/agent-a/actions/reorder",
        json={"order": [three["id"], two["id"], one["id"]]},
    )

    assert emitted == ["agent-a"]


def test_reorder_route_is_advertised(routes):
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(routes.create_blueprint())
    method_map = {}
    for rule in app.url_map.iter_rules():
        method_map.setdefault(str(rule.rule), set()).update(rule.methods - {"HEAD", "OPTIONS"})

    assert method_map["/plugin/panel/<agent_id>/actions/reorder"] == {"PUT"}


def test_db_reorder_rejects_a_foreign_agent_id(panel_db):
    one, two, _three = _seed_three()

    with pytest.raises(ValueError):
        PanelDB("agent-b").reorder_actions([two["id"], one["id"]])

    assert _order() == ["One", "Two", "Three"]
