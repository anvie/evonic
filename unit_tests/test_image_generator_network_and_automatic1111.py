"""Security and fixed-schema tests for the approved local image provider."""

import base64
import os
import socket
import sys

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(ROOT_DIR, "skills", "image-generator", "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from providers import ImageGenerationError, ImageGenerationRequest, SafeErrorCode
from providers.automatic1111 import Automatic1111Provider
from providers.network import BoundedHttpClient, SafeEndpoint, configured_endpoint


def _config(**overrides):
    config = {
        "allow_local_providers": True,
        "automatic1111_endpoint": "http://127.0.0.1:7860/api",
        "automatic1111_trusted_hosts": "127.0.0.1",
        "automatic1111_timeout_seconds": 60,
        "automatic1111_steps": 20,
    }
    config.update(overrides)
    return config


def test_local_endpoint_requires_explicit_opt_in_and_exact_trusted_host(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [(None, None, None, None, ("127.0.0.1", 0))])

    with pytest.raises(ImageGenerationError) as disabled:
        configured_endpoint(_config(allow_local_providers=False), "automatic1111", local=True)
    assert disabled.value.code is SafeErrorCode.PERMISSION_DENIED

    with pytest.raises(ImageGenerationError) as untrusted:
        configured_endpoint(_config(automatic1111_trusted_hosts="localhost"), "automatic1111", local=True)
    assert untrusted.value.code is SafeErrorCode.PERMISSION_DENIED

    endpoint = configured_endpoint(_config(), "automatic1111", local=True)
    assert endpoint.base_url == "http://127.0.0.1:7860/api"


def test_cloud_endpoint_requires_https_and_public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [(None, None, None, None, ("127.0.0.1", 0))])
    with pytest.raises(ImageGenerationError) as private:
        configured_endpoint({"cloud_endpoint": "https://example.test"}, "cloud", local=False)
    assert private.value.code is SafeErrorCode.PERMISSION_DENIED

    with pytest.raises(ImageGenerationError) as insecure:
        configured_endpoint({"cloud_endpoint": "http://example.test"}, "cloud", local=False)
    assert insecure.value.code is SafeErrorCode.PROVIDER_CONFIGURATION


def test_client_preserves_fixed_base_path_and_rejects_path_traversal(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [(None, None, None, None, ("8.8.8.8", 0))])
    client = BoundedHttpClient(SafeEndpoint("https://example.test/api", 5))
    assert client._url_for_path("/v1/generate") == "https://example.test/api/v1/generate"
    with pytest.raises(ImageGenerationError):
        client._url_for_path("/../metadata")
    with pytest.raises(ImageGenerationError):
        client._url_for_path("//metadata")


def test_automatic1111_uses_only_fixed_request_schema(monkeypatch):
    provider = Automatic1111Provider()
    captured = {}
    png = b"\x89PNG\r\n\x1a\nvalid"

    class Client:
        def json(self, method, path, payload=None):
            captured.update(method=method, path=path, payload=payload)
            return {"images": [base64.b64encode(png).decode()]}

    monkeypatch.setattr(provider, "_client", lambda _config: Client())
    result = provider.generate(
        ImageGenerationRequest(prompt="cat", size="512x512", count=1, negative_prompt="blur", seed=7),
        _config(),
    )

    assert captured == {
        "method": "POST", "path": "/sdapi/v1/txt2img",
        "payload": {"prompt": "cat", "width": 512, "height": 512, "batch_size": 1,
                    "n_iter": 1, "steps": 20, "negative_prompt": "blur", "seed": 7},
    }
    assert result.artifacts[0].data == png


def test_automatic1111_rejects_invalid_image_payload(monkeypatch):
    provider = Automatic1111Provider()

    class Client:
        def json(self, *_args, **_kwargs):
            return {"images": ["not-base64"]}

    monkeypatch.setattr(provider, "_client", lambda _config: Client())
    with pytest.raises(ImageGenerationError) as error:
        provider.generate(ImageGenerationRequest(prompt="cat", size="512x512"), _config())
    assert error.value.code is SafeErrorCode.ARTIFACT_INVALID
