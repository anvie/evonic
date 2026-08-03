from pathlib import Path


TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "templates"
    / "partials"
    / "shared_channel.html"
)


def test_routes_use_one_compact_table_at_every_viewport():
    markup = TEMPLATE.read_text(encoding="utf-8")

    assert 'id="sc-routes-table-wrap" class="overflow-x-auto"' in markup
    assert 'class="w-full min-w-[640px] table-fixed text-left text-xs"' in markup
    assert '<caption class="sr-only">Configured sender-to-agent routes</caption>' in markup
    assert '<th scope="col" class="w-[28%] px-3 py-2 font-semibold">Contact</th>' in markup
    assert '<th scope="col" class="w-[27%] px-3 py-2 font-semibold">Identifier</th>' in markup
    assert '<th scope="col" class="w-[31%] px-3 py-2 font-semibold">Assigned Agent</th>' in markup
    assert '<th scope="col" class="w-[14%] px-3 py-2 text-right font-semibold">Action</th>' in markup
    assert 'id="sc-routes-mobile"' not in markup
    assert "mobileList" not in markup
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


def test_routes_have_accessible_responsive_pagination_controls():
    markup = TEMPLATE.read_text(encoding="utf-8")

    assert 'id="sc-routes-filter"' in markup
    assert 'aria-label="Routes pagination"' in markup
    assert 'id="sc-routes-page-summary"' in markup
    assert 'aria-live="polite"' in markup
    assert 'id="sc-routes-prev"' in markup
    assert 'id="sc-routes-next"' in markup
    assert "previous.disabled = sharedChannel.routesPage === 1;" in markup
    assert "next.disabled = sharedChannel.routesPage === totalPages;" in markup
    assert "pagination.classList.toggle('hidden', totalPages <= 1);" in markup
    assert "pagination.classList.toggle('flex', totalPages > 1);" in markup
    assert "grid grid-cols-2 gap-1.5 sm:flex" in markup


def test_route_pagination_filters_resets_and_clamps_data_changes():
    markup = TEMPLATE.read_text(encoding="utf-8")

    assert "routesPageSize: 10" in markup
    assert "filterRoutes: function(value)" in markup
    assert "sharedChannel.routesPage = 1;" in markup
    assert "sharedChannel.resetRoutesView();" in markup
    assert "sharedChannel.routesPage = Math.min(sharedChannel.routesPage, totalPages);" in markup
    assert "filteredRows.slice(start, start + sharedChannel.routesPageSize)" in markup
    assert "No routes match your filter." in markup


def test_shared_channel_access_controls_are_per_number_and_persisted_live():
    markup = TEMPLATE.read_text(encoding="utf-8")

    assert 'id="sc-access-mode"' in markup
    assert 'id="sc-default-agent"' in markup
    assert 'value="assigned_only"' in markup
    assert 'value="unrestricted"' in markup
    assert 'sharedChannel.saveAccess()' in markup
    assert 'access_mode: mode' in markup
    assert 'default_agent_id: defaultAgentId' in markup
    assert 'Select an enabled default agent for unrestricted access.' in markup
    assert 'groups still require an explicit route.' in markup


def test_shared_channel_access_controls_handle_api_errors():
    markup = TEMPLATE.read_text(encoding="utf-8")

    assert "sharedChannel.showToast(data.error, 'error')" in markup
    assert "Unable to save direct-message access settings." in markup
