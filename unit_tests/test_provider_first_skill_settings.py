"""Regression coverage for the shared provider-first skill settings renderer."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_repo_file(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_image_generator_declares_provider_first_settings_metadata():
    manifest = json.loads(read_repo_file("skills/image-generator/skill.json"))
    settings_ui = manifest["settings_ui"]

    assert settings_ui["layout"] == "provider-first"
    assert {provider["id"] for provider in settings_ui["provider_selector"]["providers"]} == {
        "google-gemini",
        "automatic1111",
        "mock",
    }

    variables = {variable["name"]: variable for variable in manifest["variables"]}
    assert variables["google_gemini_api_key"]["provider"] == "google-gemini"
    assert variables["automatic1111_endpoint"]["provider"] == "automatic1111"
    assert variables["default_provider"]["section"] == "global"
    assert variables["automatic1111_timeout_seconds"]["advanced"] is True


def test_shared_renderer_supports_provider_first_layout_without_breaking_static_forms():
    source = read_repo_file("templates/skill_detail.html")

    assert "if (settingsUi.layout === 'provider-first' && providerSelector)" in source
    assert "form.innerHTML = variables.map(renderSettingField).join('');" in source
    assert "function renderProviderSettings(layout)" in source
    assert "id=\"settings-provider-picker\"" in source
    assert "layout.global_section?.label || 'Global defaults'" in source
    assert "Advanced settings" in source


def test_provider_switch_preserves_values_and_save_includes_inactive_provider_fields():
    source = read_repo_file("templates/skill_detail.html")

    assert "function captureSettingsState()" in source
    assert "captureSettingsState();" in source
    assert "selectedSettingsProvider = event.target.value;" in source
    assert "settingsVariables.forEach(variable =>" in source
    assert "values[variable.name] = variable.type === 'boolean' ? Boolean(value) : value;" in source


def test_renderer_exposes_status_and_accessible_provider_picker_feedback():
    source = read_repo_file("templates/skill_detail.html")

    assert "function providerStatus(provider, variables)" in source
    assert "aria-describedby=\"provider-configuration-status\"" in source
    assert "aria-live=\"polite\"" in source
    assert "Choose a provider to view its configuration fields." in source
