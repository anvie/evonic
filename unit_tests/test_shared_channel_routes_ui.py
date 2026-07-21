from pathlib import Path


TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "templates"
    / "partials"
    / "shared_channel.html"
)


def test_routes_use_responsive_cards_and_desktop_table():
    markup = TEMPLATE.read_text(encoding="utf-8")

    assert 'id="sc-routes-mobile"' in markup
    assert 'class="hidden overflow-x-auto sm:block"' in markup
    assert '<caption class="sr-only">Configured sender-to-agent routes</caption>' in markup
    assert 'id="sc-routes-count"' in markup


def test_route_names_remain_visible_at_every_viewport():
    markup = TEMPLATE.read_text(encoding="utf-8")

    assert "var name = String(names[userId] || '').trim();" in markup
    assert "sharedChannel.esc(route.name)" in markup
    assert "Name not provided" in markup
    assert "hidden sm:table-cell" not in markup


def test_route_renderer_formats_identifier_without_changing_raw_action_value():
    markup = TEMPLATE.read_text(encoding="utf-8")

    assert "formatRouteIdentifier: function(value)" in markup
    assert "sharedChannel.formatRouteIdentifier(userId)" in markup
    assert "sharedChannel.removeRoute(\\\'" in markup
