"""Every registered Image Generator provider must be switchable from the manifest.

A provider is only usable when its ``<id>_enabled`` toggle is true, and the
skill config only carries variables declared in ``skill.json``. A missing
toggle (as happened for OpenRouter) leaves the provider permanently disabled.
"""

from __future__ import annotations

import json
from pathlib import Path

SKILL_JSON = Path(__file__).resolve().parents[1] / "skills" / "image-generator" / "skill.json"
PROVIDER_IDS = ("automatic1111", "comfyui", "google-gemini", "mock", "openrouter")


def test_every_provider_has_an_enabled_toggle():
    manifest = json.loads(SKILL_JSON.read_text(encoding="utf-8"))
    variables = {v["name"]: v for v in manifest["variables"]}
    for provider_id in PROVIDER_IDS:
        name = f"{provider_id.replace('-', '_')}_enabled"
        assert name in variables, f"{name} missing from skill.json"
        assert variables[name]["type"] == "boolean"
        assert variables[name]["provider"] == provider_id


def test_default_provider_options_match_toggles():
    manifest = json.loads(SKILL_JSON.read_text(encoding="utf-8"))
    default = next(v for v in manifest["variables"] if v["name"] == "default_provider")
    assert {o["value"] for o in default["options"]} == set(PROVIDER_IDS)
