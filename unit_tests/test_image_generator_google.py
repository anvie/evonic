"""Contract tests for the fixed-host Google Gemini image-generation adapter."""

from __future__ import annotations

import base64
import os
import sys

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(ROOT_DIR, "skills", "image-generator", "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from providers import ImageGenerationError, ImageGenerationRequest, SafeErrorCode, provider_registry
from providers.google import GoogleGeminiProvider

_PNG = b"\x89PNG\r\n\x1a\nvalid"


def _config(**overrides):
    config = {
        "google_gemini_api_key": "test-key",
        "google_gemini_model": "gemini-2.5-flash-image",
        "google_gemini_timeout_seconds": 60,
    }
    config.update(overrides)
    return config


def _response(image: bytes = _PNG):
    return {
        "modelVersion": "gemini-2.5-flash-image",
        "candidates": [{
            "content": {"parts": [{
                "inlineData": {"mimeType": "image/png", "data": base64.b64encode(image).decode("ascii")},
            }]},
        }],
    }


def test_google_provider_is_registered_without_enabling_it_by_default():
    assert [provider.id for provider in provider_registry.list()] == ["automatic1111", "google-gemini", "mock", "openrouter"]
    assert GoogleGeminiProvider.id == "google-gemini"
    assert GoogleGeminiProvider.is_local is False


def test_google_provider_translates_only_the_fixed_google_request(monkeypatch):
    provider = GoogleGeminiProvider()
    captured = {}

    class Client:
        def json(self, method, path, payload=None, *, headers=None):
            captured.update(method=method, path=path, payload=payload, headers=headers)
            return _response()

    monkeypatch.setattr(provider, "_client", lambda _config: Client())
    result = provider.generate(
        ImageGenerationRequest(prompt="A green hummingbird", size="512x512", output_format="png"),
        _config(),
    )

    assert captured == {
        "method": "POST",
        "path": "/models/gemini-2.5-flash-image:generateContent",
        "payload": {
            "contents": [{"role": "user", "parts": [{"text": "A green hummingbird"}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        },
        "headers": {"x-goog-api-key": "test-key"},
    }
    assert result.provider_id == "google-gemini"
    assert result.model == "gemini-2.5-flash-image"
    assert result.artifacts[0].data == _PNG


@pytest.mark.parametrize("size,aspect_ratio", [
    ("768x1024", "3:4"),
    ("768x1344", "9:16"),
    ("832x1248", "2:3"),
    ("1024x768", "4:3"),
    ("1344x768", "16:9"),
    ("1248x832", "3:2"),
])
def test_google_provider_sends_native_aspect_ratio_for_portrait_and_landscape_sizes(monkeypatch, size, aspect_ratio):
    provider = GoogleGeminiProvider()
    captured = {}

    class Client:
        def json(self, method, path, payload=None, *, headers=None):
            captured.update(method=method, path=path, payload=payload, headers=headers)
            return _response()

    monkeypatch.setattr(provider, "_client", lambda _config: Client())
    provider.generate(ImageGenerationRequest(prompt="A portrait subject", size=size), _config())

    assert captured["payload"]["generationConfig"] == {
        "responseModalities": ["TEXT", "IMAGE"],
        "imageConfig": {"aspectRatio": aspect_ratio},
    }


def test_google_provider_requires_administrator_credential_before_request(monkeypatch):
    provider = GoogleGeminiProvider()
    monkeypatch.setattr(provider, "_client", lambda _config: pytest.fail("request must not be attempted"))

    config = _config()
    config.pop("google_gemini_api_key")
    with pytest.raises(ImageGenerationError) as error:
        provider.generate(ImageGenerationRequest(prompt="A bird"), config)

    assert error.value.code is SafeErrorCode.PROVIDER_CONFIGURATION
    assert "credential" in error.value.message.lower()


def test_google_provider_normalizes_valid_png_and_jpeg_inline_data():
    provider = GoogleGeminiProvider()
    result = provider._artifacts(_response())

    assert len(result) == 1
    assert result[0].mime_type == "image/png"
    assert result[0].filename == "google-gemini-1.png"
    assert result[0].data == _PNG

    jpeg = b"\xff\xd8\xffvalid-jpeg"
    jpeg_response = _response(jpeg)
    jpeg_response["candidates"][0]["content"]["parts"][0]["inlineData"]["mimeType"] = "image/jpeg"
    jpeg_result = provider._artifacts(jpeg_response)
    assert jpeg_result[0].mime_type == "image/jpeg"
    assert jpeg_result[0].filename == "google-gemini-1.jpg"
    assert jpeg_result[0].data == jpeg

    with pytest.raises(ImageGenerationError) as malformed:
        provider._artifacts(_response(b"not-a-png"))
    assert malformed.value.code is SafeErrorCode.ARTIFACT_INVALID


def test_google_provider_maps_safe_rejection_and_connection_requests(monkeypatch):
    provider = GoogleGeminiProvider()
    with pytest.raises(ImageGenerationError) as rejected:
        provider._artifacts({"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []})
    assert rejected.value.code is SafeErrorCode.CONTENT_REJECTED

    captured = {}

    class Client:
        def json(self, method, path, payload=None, *, headers=None):
            captured.update(method=method, path=path, payload=payload, headers=headers)
            return {"name": "models/gemini-2.5-flash-image"}

    monkeypatch.setattr(provider, "_client", lambda _config: Client())
    provider.test_connection(_config())
    assert captured == {
        "method": "GET",
        "path": "/models/gemini-2.5-flash-image",
        "payload": None,
        "headers": {"x-goog-api-key": "test-key"},
    }


def test_google_provider_rejects_unapproved_models_and_invalid_timeout():
    provider = GoogleGeminiProvider()
    with pytest.raises(ImageGenerationError) as model_error:
        provider.generate(ImageGenerationRequest(prompt="A bird", model="unapproved"), _config())
    assert model_error.value.code is SafeErrorCode.INVALID_REQUEST

    with pytest.raises(ImageGenerationError) as timeout_error:
        provider._client(_config(google_gemini_timeout_seconds=121))
    assert timeout_error.value.code is SafeErrorCode.PROVIDER_CONFIGURATION
