"""Integration coverage for the Image Generator skill tool backend."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT_DIR / "skills" / "image-generator"
BACKEND_DIR = SKILL_DIR / "backend"


def _load_tool_module():
    """Load the skill tool under a unique package so its relative imports work."""
    package_name = "test_image_generator_backend"
    for name in tuple(sys.modules):
        if name == package_name or name.startswith(f"{package_name}."):
            del sys.modules[name]

    spec = importlib.util.spec_from_file_location(
        package_name,
        BACKEND_DIR / "__init__.py",
        submodule_search_locations=[str(BACKEND_DIR)],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    assert spec.loader is not None
    spec.loader.exec_module(package)
    return __import__(f"{package_name}.tools.generate_image", fromlist=["execute"])


@pytest.fixture
def image_tool(monkeypatch, tmp_path):
    tool = _load_tool_module()
    import backend.tools.save_artifact as save_artifact

    # Exercise real byte persistence while isolating the test artifact root.
    monkeypatch.setattr(save_artifact, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "backend.skills_manager.skills_manager.get_skill_config",
        lambda _skill_id: {
            "default_provider": "mock",
            "allowed_providers": "mock",
            "allow_local_providers": True,
            "mock_enabled": True,
            "max_images_per_request": 2,
        },
    )
    return tool


def test_generate_image_persists_validated_compact_artifact(image_tool, tmp_path):
    result = image_tool.execute(
        {"id": "image-tool-test-agent"},
        {
            "prompt": "A mountain at sunrise",
            "size": "512x512",
            "count": 1,
            "negative_prompt": "fog",
            "seed": 42,
            "output_format": "png",
            "model": "deterministic-mock-v1",
        },
    )

    assert result["status"] == "success"
    assert result["provider"] == "mock"
    assert result["model"] == "deterministic-mock-v1"
    assert set(result) == {"status", "provider", "model", "artifacts"}
    assert len(result["artifacts"]) == 1
    artifact = result["artifacts"][0]
    assert set(artifact) == {"filename", "mime_type", "size", "width", "height"}
    assert artifact["filename"].startswith("generated-1-mock-")
    assert artifact["mime_type"] == "image/png"
    assert (artifact["width"], artifact["height"]) == (1, 1)
    assert artifact["size"] > 0

    persisted = tmp_path / "shared" / "agents" / "image-tool-test-agent" / "artifacts" / artifact["filename"]
    assert persisted.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_generate_image_uses_no_implicit_provider_fallback(image_tool, monkeypatch):
    monkeypatch.setattr(
        "backend.skills_manager.skills_manager.get_skill_config",
        lambda _skill_id: {
            "default_provider": "",
            "allowed_providers": "mock",
            "allow_local_providers": True,
            "mock_enabled": True,
            "max_images_per_request": 1,
        },
    )

    result = image_tool.execute({"id": "image-tool-test-agent"}, {"prompt": "A flower"})

    assert result == {
        "status": "error",
        "error": {
            "code": "provider_configuration",
            "message": "No image provider is selected. Configure a default provider or select one explicitly.",
        },
    }


def test_generate_image_enforces_allowlist_and_capabilities(image_tool, monkeypatch):
    monkeypatch.setattr(
        "backend.skills_manager.skills_manager.get_skill_config",
        lambda _skill_id: {
            "default_provider": "mock",
            "allowed_providers": "",
            "allow_local_providers": True,
            "max_images_per_request": 1,
        },
    )
    denied = image_tool.execute({"id": "image-tool-test-agent"}, {"prompt": "A flower"})
    assert denied["error"]["code"] == "provider_disabled"

    monkeypatch.setattr(
        "backend.skills_manager.skills_manager.get_skill_config",
        lambda _skill_id: {
            "default_provider": "mock",
            "allowed_providers": "mock",
            "allow_local_providers": True,
            "mock_enabled": True,
            "max_images_per_request": 1,
        },
    )
    invalid_size = image_tool.execute(
        {"id": "image-tool-test-agent"}, {"prompt": "A flower", "size": "640x480"}
    )
    assert invalid_size["error"]["code"] == "invalid_request"
    assert "size" in invalid_size["error"]["message"].lower()
