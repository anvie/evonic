"""Coverage for the Agent Panel tab's edit mode.

Edit mode makes every panel button inert and reveals a pencil (rename) and a
trash (delete) control on it. These tests pin:

* the rendered fragment contract (toggle, per-button tools, CSS gating), and
* the rename/delete API the controls call.
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
    spec = importlib.util.spec_from_file_location("panel_routes_edit_mode_under_test", path)
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


def _seed(agent_id="agent-a", label="Deploy"):
    return PanelDB(agent_id).add_action(label=label, action_type="script", content="echo hi")


# ── rendered fragment: entry point ───────────────────────────────────────────


def test_renders_edit_mode_toggle_next_to_title(routes):
    html = _render(routes)
    assert 'id="pnl-edit-toggle"' in html
    assert 'class="pnl-edit-toggle"' in html
    # Toggle starts off; the hint only explains edit mode once it is on.
    assert 'aria-pressed="false"' in html
    assert 'id="pnl-edit-hint"' in html
    assert 'class="pnl-edit-hint pnl-hidden"' in html


# ── rendered fragment: per-button controls ───────────────────────────────────


def test_every_button_gets_a_pencil_and_a_trash_control(routes):
    actions = [dict(ACTION), dict(ACTION, id=2, label="Nightly", slash_command="")]
    html = _render(routes, actions)

    assert html.count('class="pnl-tool pnl-tool-edit"') == 2
    assert html.count('class="pnl-tool pnl-tool-del"') == 2
    assert html.count('class="pnl-item"') == 2


def test_controls_target_their_own_action_id(routes):
    actions = [dict(ACTION), dict(ACTION, id=2, label="Nightly", slash_command="")]
    html = _render(routes, actions)

    for action_id in (1, 2):
        assert f'pnl-tool-edit" type="button" data-action-id="{action_id}"' in html
        assert f'pnl-tool-del" type="button" data-action-id="{action_id}"' in html


def test_controls_are_only_visible_in_edit_mode(routes):
    html = _render(routes)

    # Hidden by default, revealed by the .pnl-edit-mode class on the grid.
    assert ".pnl-item-tools { display:none;" in html
    assert ".pnl-edit-mode .pnl-item-tools { display:flex; }" in html
    assert "btnGrid.classList.toggle('pnl-edit-mode', editMode)" in html


# ── rendered fragment: buttons become inert ──────────────────────────────────


def test_buttons_become_non_clickable_in_edit_mode(routes):
    html = _render(routes)

    # Pointer events off, then disabled/aria-disabled applied by setEditMode().
    assert ".pnl-edit-mode .panel-btn { pointer-events:none;" in html
    assert "btn.disabled = true;" in html
    assert "btn.setAttribute('aria-disabled', 'true');" in html
    # Defensive guard inside the run-action click handler.
    assert "if (editMode) return;" in html


# ── rendered fragment: rename / delete wiring ────────────────────────────────


def test_rename_modal_is_present_and_focus_managed(routes):
    html = _render(routes)

    assert 'id="pnl-rename-modal"' in html
    assert 'id="pnl-rename-input"' in html
    assert 'maxlength="120"' in html
    assert "renameInput.focus();" in html
    assert "if (e.key === 'Enter')" in html
    assert "else if (e.key === 'Escape')" in html


def test_controls_call_the_panel_action_api(routes):
    html = _render(routes)

    assert "method: 'PATCH'" in html
    assert "method: 'DELETE'" in html
    assert "'/plugin/panel/' + AGENT_ID + '/actions/'" in html


def test_edit_mode_survives_a_realtime_re_render(routes):
    html = _render(routes)

    assert "const EDIT_STORE_KEY = 'pnl-edit-mode:' + AGENT_ID;" in html
    assert "sessionStorage.setItem(EDIT_STORE_KEY, editMode ? '1' : '0')" in html
    # Restored at the very end, after the click handlers are bound.
    restore_at = html.index("if (sessionStorage.getItem(EDIT_STORE_KEY) === '1') setEditMode(true);")
    handler_at = html.index(".panel-btn:not([disabled])")
    assert handler_at < restore_at


def test_filter_index_is_rebuilt_after_a_rename(routes):
    html = _render(routes)
    assert "function syncButtonLabel(actionId, label, slashCommand)" in html
    assert "btn.dataset.search =" in html


def test_toggle_still_renders_when_there_are_no_actions(routes):
    html = _render(routes, [])

    assert 'id="pnl-edit-toggle"' in html
    assert "No actions yet" in html


def test_realtime_nudge_is_best_effort(routes, monkeypatch):
    # A broken event stream must never make a rename/delete fail.
    import sys
    import types

    monkeypatch.setitem(sys.modules, "backend.event_stream", types.ModuleType("backend.event_stream"))
    routes._emit_panel_updated("agent-a")


def test_labels_are_escaped_in_the_controls(routes):
    evil = dict(ACTION, label='<img src=x onerror="alert(1)">')
    html = _render(routes, [evil])

    # HTML attributes carry the escaped form...
    assert "aria-label=\"Edit &lt;img src=x" in html
    # ...and the embedded actions JSON can never close the <script> block.
    assert "<img src=x" not in html
    assert "\\u003cimg src=x" in html
    assert "</script><script" not in html


# ── API: slash command ───────────────────────────────────────────────────────


def test_edit_sets_normalizes_and_clears_slash_command(client):
    action = _seed()
    url = f"/plugin/panel/agent-a/actions/{action['id']}"

    resp = client.patch(url, json={"slash_command": " /Ship-It "})
    assert resp.status_code == 200
    assert PanelDB("agent-a").get_action(action["id"])["slash_command"] == "ship-it"
    assert PanelDB("agent-a").get_action(action["id"])["label"] == "Deploy"

    resp = client.patch(url, json={"slash_command": ""})
    assert resp.status_code == 200
    assert PanelDB("agent-a").get_action(action["id"])["slash_command"] == ""


def test_edit_rejects_invalid_or_duplicate_slash_command(client):
    first = _seed(label="One")
    second = _seed(label="Two")
    client.patch(f"/plugin/panel/agent-a/actions/{first['id']}", json={"slash_command": "go"})

    for bad in ("Bad Name!", "go"):
        resp = client.patch(
            f"/plugin/panel/agent-a/actions/{second['id']}", json={"slash_command": bad}
        )
        assert resp.status_code == 400
    assert PanelDB("agent-a").get_action(second["id"])["slash_command"] == ""


def test_edit_keeps_own_slash_command_on_resave(client):
    action = _seed()
    url = f"/plugin/panel/agent-a/actions/{action['id']}"
    client.patch(url, json={"slash_command": "go"})

    assert client.patch(url, json={"label": "Go", "slash_command": "go"}).status_code == 200


# ── API: rename ──────────────────────────────────────────────────────────────


def test_rename_updates_the_label(client):
    action = _seed()
    resp = client.patch(
        f"/plugin/panel/agent-a/actions/{action['id']}", json={"label": "Deploy prod"}
    )

    assert resp.status_code == 200
    assert resp.get_json()["action"]["label"] == "Deploy prod"
    assert PanelDB("agent-a").get_action(action["id"])["label"] == "Deploy prod"


def test_rename_trims_whitespace(client):
    action = _seed()
    resp = client.patch(
        f"/plugin/panel/agent-a/actions/{action['id']}", json={"label": "  Spaced  "}
    )

    assert resp.status_code == 200
    assert PanelDB("agent-a").get_action(action["id"])["label"] == "Spaced"


@pytest.mark.parametrize("payload", [{"label": ""}, {"label": "   "}, {"label": 12}, {}])
def test_rename_rejects_empty_or_missing_label(client, payload):
    action = _seed()
    resp = client.patch(f"/plugin/panel/agent-a/actions/{action['id']}", json=payload)

    assert resp.status_code == 400
    assert PanelDB("agent-a").get_action(action["id"])["label"] == "Deploy"


def test_rename_rejects_overlong_label(client):
    action = _seed()
    resp = client.patch(
        f"/plugin/panel/agent-a/actions/{action['id']}", json={"label": "x" * 121}
    )

    assert resp.status_code == 400


def test_rename_unknown_action_is_404(client):
    resp = client.patch("/plugin/panel/agent-a/actions/4242", json={"label": "Nope"})
    assert resp.status_code == 404


def test_rename_cannot_touch_another_agents_action(client):
    action = _seed("agent-b")
    resp = client.patch(
        f"/plugin/panel/agent-a/actions/{action['id']}", json={"label": "Stolen"}
    )

    assert resp.status_code == 404
    assert PanelDB("agent-b").get_action(action["id"])["label"] == "Deploy"


def test_rename_accepts_put_as_well(client):
    action = _seed()
    resp = client.put(
        f"/plugin/panel/agent-a/actions/{action['id']}", json={"label": "Via PUT"}
    )

    assert resp.status_code == 200
    assert PanelDB("agent-a").get_action(action["id"])["label"] == "Via PUT"


def test_route_methods_are_advertised(routes):
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(routes.create_blueprint())
    method_map = {}
    for rule in app.url_map.iter_rules():
        method_map.setdefault(str(rule.rule), set()).update(rule.methods - {"HEAD", "OPTIONS"})

    assert method_map["/plugin/panel/<agent_id>/actions/<int:action_id>"] == {
        "PATCH", "PUT", "DELETE"
    }


# ── API: delete ──────────────────────────────────────────────────────────────


def test_delete_removes_the_action(client):
    action = _seed()
    resp = client.delete(f"/plugin/panel/agent-a/actions/{action['id']}")

    assert resp.status_code == 200
    assert resp.get_json() == {"status": "deleted", "action_id": action["id"]}
    assert PanelDB("agent-a").get_action(action["id"]) is None


def test_delete_unknown_action_is_404(client):
    resp = client.delete("/plugin/panel/agent-a/actions/4242")
    assert resp.status_code == 404


def test_delete_cannot_touch_another_agents_action(client):
    action = _seed("agent-b")
    resp = client.delete(f"/plugin/panel/agent-a/actions/{action['id']}")

    assert resp.status_code == 404
    assert PanelDB("agent-b").get_action(action["id"]) is not None


def test_mutations_nudge_other_clients(routes, panel_db, monkeypatch):
    emitted = []
    monkeypatch.setattr(routes, "_emit_panel_updated", lambda agent_id: emitted.append(agent_id))

    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(routes.create_blueprint())
    app.testing = True
    client = app.test_client()

    action = _seed()
    client.patch(f"/plugin/panel/agent-a/actions/{action['id']}", json={"label": "Renamed"})
    client.delete(f"/plugin/panel/agent-a/actions/{action['id']}")

    assert emitted == ["agent-a", "agent-a"]
