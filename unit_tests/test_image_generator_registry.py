"""Focused tests for the Image Generator skill manifest and provider registry."""

import importlib.util
import json
import os
import sys

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(ROOT_DIR, "skills", "image-generator")
BACKEND_DIR = os.path.join(SKILL_DIR, "backend")

if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from providers import (  # noqa: E402
    DeterministicMockProvider,
    ImageArtifact,
    ImageGenerationError,
    ImageGenerationRequest,
    ImageGenerationResult,
    ImageProviderRegistry,
    SafeErrorCode,
    provider_registry,
)


def test_skill_manifest_registers_disabled_lazy_generation_tool():
    """The feature gate prevents generation tools from entering contexts by default."""
    with open(os.path.join(SKILL_DIR, "skill.json"), encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
    with open(os.path.join(SKILL_DIR, "tools.json"), encoding="utf-8") as tools_file:
        tools = json.load(tools_file)

    assert manifest["id"] == "image-generator"
    assert manifest["default_enabled"] is False
    assert manifest["lazy_tools"] is True
    assert manifest["tools_file"] == "tools.json"
    assert [tool["function"]["name"] for tool in tools] == ["generate_image"]
    assert tools[0]["function"]["parameters"]["required"] == ["prompt"]

    variables = {variable["name"]: variable for variable in manifest["variables"]}
    assert variables["default_provider"]["default"] == ""
    assert variables["allowed_providers"]["default"] == "mock"
    assert variables["allow_local_providers"]["type"] == "boolean"
    assert variables["automatic1111_endpoint"]["default"] == ""
    assert variables["automatic1111_trusted_hosts"]["default"] == ""
    assert variables["provider_api_key"]["type"] == "secret"
    assert [provider.id for provider in provider_registry.list()] == ["automatic1111", "mock"]


def test_registry_resolves_explicit_and_default_provider():
    registry = ImageProviderRegistry()
    provider = DeterministicMockProvider()
    registry.register(provider)

    assert registry.resolve("mock", None) is provider
    assert registry.resolve(None, "mock") is provider
    assert [registered.id for registered in registry.list()] == ["mock"]


def test_registry_returns_safe_errors_for_missing_or_invalid_requests():
    registry = ImageProviderRegistry()
    registry.register(DeterministicMockProvider())

    with pytest.raises(ImageGenerationError) as missing:
        registry.resolve(None, None)
    assert missing.value.code is SafeErrorCode.PROVIDER_CONFIGURATION
    assert "No image provider" in missing.value.message

    with pytest.raises(ImageGenerationError) as invalid:
        registry.generate(ImageGenerationRequest(prompt="", size="1024x1024"), {}, default_provider_id="mock")
    assert invalid.value.code is SafeErrorCode.INVALID_REQUEST
    assert "prompt" in invalid.value.message.lower()


def test_mock_provider_returns_deterministic_validated_artifacts():
    registry = ImageProviderRegistry()
    registry.register(DeterministicMockProvider())
    request = ImageGenerationRequest(prompt="a mountain at sunrise", size="512x512", count=2, seed=42)

    first = registry.generate(request, {}, default_provider_id="mock")
    second = registry.generate(request, {}, provider_id="mock")

    assert first == second
    assert first.model == "deterministic-mock-v1"
    assert len(first.artifacts) == 2
    assert all(artifact.mime_type == "image/png" for artifact in first.artifacts)
    assert all(artifact.data.startswith(b"\x89PNG\r\n\x1a\n") for artifact in first.artifacts)


def test_registry_rejects_unsafe_artifact_output():
    class UnsafeProvider(DeterministicMockProvider):
        id = "unsafe"

        def generate(self, request, config):
            return ImageGenerationResult(
                provider_id=self.id,
                artifacts=(ImageArtifact(mime_type="text/plain", filename="bad.txt", data=b"not an image"),),
            )

    registry = ImageProviderRegistry()
    registry.register(UnsafeProvider())

    with pytest.raises(ImageGenerationError) as error:
        registry.generate(ImageGenerationRequest(prompt="test"), {}, default_provider_id="unsafe")
    assert error.value.code is SafeErrorCode.ARTIFACT_INVALID
