"""Plugin pages live under the navbar's Plugins dropdown unless pinned."""

import pytest

from app import app
import routes.plugins as plugins_routes
from backend.plugin_manager import plugin_manager
from models.db import db

ITEMS = [
    {"label": "Board", "path": "/board/demo", "plugin_id": "demo", "plugin_name": "Demo Board"},
    {"label": "Audit", "path": "/demo/audit", "plugin_id": "demo", "plugin_name": "Demo Board"},
    {"label": "Usage", "path": "/usage", "plugin_id": "meter", "plugin_name": "Meter"},
]


@pytest.fixture(autouse=True)
def nav_items(monkeypatch):
    monkeypatch.setattr(plugin_manager, "get_nav_items", lambda: [dict(i) for i in ITEMS])
    saved = db.get_setting(plugins_routes.NAV_PINS_KEY)
    db.set_setting(plugins_routes.NAV_PINS_KEY, "[]")
    yield
    db.set_setting(plugins_routes.NAV_PINS_KEY, saved if saved is not None else "[]")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(app.config, "TESTING", True)
    with app.test_client() as test_client:
        with test_client.session_transaction() as session:
            session["authenticated"] = True
        yield test_client


def test_unpinned_items_are_grouped_by_plugin():
    nav = plugins_routes.build_plugin_nav(ITEMS, "/")

    assert nav["pinned"] == []
    assert [g["name"] for g in nav["groups"]] == ["Demo Board", "Meter"]
    assert [i["label"] for i in nav["groups"][0]["items"]] == ["Board", "Audit"]
    assert nav["menu_active"] is False


def test_pinned_item_is_promoted_and_stays_in_the_dropdown():
    db.set_setting(plugins_routes.NAV_PINS_KEY, '["/usage"]')

    nav = plugins_routes.build_plugin_nav(ITEMS, "/usage")

    assert [i["path"] for i in nav["pinned"]] == ["/usage"]
    assert nav["groups"][1]["items"][0]["pinned"] is True
    # The pinned link is highlighted itself, not the dropdown.
    assert nav["menu_active"] is False


def test_unpinned_plugin_page_highlights_the_dropdown():
    assert plugins_routes.build_plugin_nav(ITEMS, "/demo/audit/42")["menu_active"] is True
    assert plugins_routes.build_plugin_nav(ITEMS, "/plugins")["menu_active"] is True


def test_corrupt_pin_setting_is_ignored():
    db.set_setting(plugins_routes.NAV_PINS_KEY, "not json")
    assert plugins_routes.get_nav_pins() == []


def test_pin_api_round_trip(client):
    assert client.put("/api/plugins/nav-pins", json={"path": "/usage", "pinned": True}).status_code == 200
    assert plugins_routes.get_nav_pins() == ["/usage"]

    assert client.put("/api/plugins/nav-pins", json={"path": "/usage", "pinned": False}).status_code == 200
    assert plugins_routes.get_nav_pins() == []


def test_pin_api_rejects_unknown_paths(client):
    resp = client.put("/api/plugins/nav-pins", json={"path": "/evil", "pinned": True})
    assert resp.status_code == 400
    assert plugins_routes.get_nav_pins() == []


def test_navbar_renders_dropdown_and_pinned_links(client):
    db.set_setting(plugins_routes.NAV_PINS_KEY, '["/board/demo"]')

    html = client.get("/plugins").get_data(as_text=True)

    nav = html[html.index('<nav id="site-nav"'):html.index("</nav>")]
    menu = nav[nav.index('class="nav-dd-menu"'):]
    assert "Manage plugins" in menu
    assert 'data-path="/board/demo" data-pinned="1"' in menu
    # Every page is in the dropdown; only the pinned one also gets a top-level link.
    assert nav.count('href="/board/demo"') == 2
    assert nav.count('href="/demo/audit"') == 1
    assert nav.count('href="/usage"') == 1
