import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_repo_file(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_helper_builds_provider_optgroups():
    source = read_repo_file("static/js/ui-utils.js")

    assert "function populateModelSelect(select, models, options)" in source
    assert "document.createElement('optgroup')" in source
    assert "group.label = provider" in source
    assert "providerFor" in source
    assert "model.provider || 'Other'" in source
    # Options must be grouped in provider insertion order (Map preserves it).
    assert "new Map()" in source
    assert "groups.set(provider, []" in source


def test_helper_preserves_empty_options_and_restores_selection():
    source = read_repo_file("static/js/ui-utils.js")

    # An existing empty option (e.g. "-- Use global default --") is preserved
    # unless the caller explicitly passes emptyLabel: null to drop a placeholder.
    assert "emptyLabel === undefined" in source
    assert "first.value === ''" in source
    assert "emptyLabel: null" in source
    # The restored selection must survive the re-render.
    assert "select.value = selectedValue" in source
    assert "option.value === selectedValue" in source
    # The empty option must live outside the provider groups.
    assert "fragment.appendChild(emptyOption)" in source


def test_helper_is_exported_through_window_ui():
    source = read_repo_file("static/js/ui-utils.js")

    assert "populateModelSelect: populateModelSelect" in source
    assert "window.ui" in source


def test_agent_detail_uses_shared_helper_for_primary_and_fallback():
    source = read_repo_file("templates/agent_detail.html")

    assert "ui.populateModelSelect(sel, models, { selectedValue: currentModelId })" in source
    assert (
        "ui.populateModelSelect(fbSel, models, { selectedValue: currentFallbackModelId })"
        in source
    )
    # The old inline grouping implementation must be gone.
    assert "modelsByProvider" not in source
    assert "const fallbackGroup = document.createElement('optgroup')" not in source


def test_settings_general_uses_shared_helper_for_all_routing_selects():
    source = read_repo_file("static/js/settings-general.js")

    assert "ui.populateModelSelect(el, list, {" in source
    assert 'selectedValue: selectedId || ""' in source
    for select_id in [
        "default-model-select",
        "default-model-fallback-select",
        "vision-model-select",
        "vision-fallback-model-select",
        "vision-fallback-model-2-select",
        "kb-organizer-model-select",
        "task-classifier-model-select",
        "cmp-model-select",
    ]:
        # fill() calls may span multiple lines; match the id after fill( regardless.
        assert re.search(r'fill\(\s*"' + re.escape(select_id) + '"', source), select_id
    # Empty-state message is preserved when no models are configured.
    assert "No models configured" in source
    # Special ungrouped labels stay as explicit empty labels.
    assert "Auto-detect (first vision-capable model)" in source
    assert "None / disabled" in source


def test_evaluate_page_uses_shared_helper_with_model_name_values():
    source = read_repo_file("templates/evaluate.html")

    assert "ui.populateModelSelect(selector, models, {" in source
    assert "valueFor: m => m.model_name" in source
    assert "labelFor: m => m.name" in source
    # The "Loading models..." placeholder must be dropped, not kept as a row.
    assert "emptyLabel: null" in source
    # localStorage restoration is preserved.
    assert "localStorage.getItem('eval_selected_model')" in source
    assert "configModel" in source
    # The old flat innerHTML loop must be gone.
    assert "selector.appendChild(option)" not in source


def test_evaluate_settings_uses_shared_helper_preserving_default_option():
    source = read_repo_file("templates/evaluate_settings.html")

    assert "ui.populateModelSelect(selector, models.models || [], {" in source
    assert "labelFor: m => m.name" in source
    assert 'selectedValue: selectedId || ""' in source
    # The ungrouped "Default" option must remain in the markup.
    assert "Default (use default model)" in source
    # The old flat loop must be gone.
    assert "selector.appendChild(option)" not in source


def test_skill_detail_uses_shared_helper_preserving_default_option():
    source = read_repo_file("templates/skill_detail.html")

    assert 'select[data-options-source="models"]' in source
    assert "ui.populateModelSelect(sel, models, {" in source
    assert "labelFor: m => (m.name || m.model_name || m.id)" in source
    assert "sel.dataset.current" in source
    # The per-field ungrouped default option stays in the rendered markup.
    assert "v.empty_label" in source
    assert "Default" in source
    # The old insertAdjacentHTML flat rendering must be gone.
    assert "insertAdjacentHTML('beforeend', optsHtml)" not in source
