"""Approved AUTOMATIC1111-compatible local image provider.

The adapter exposes only a fixed txt2img request schema.  Workflow JSON, endpoint
paths, and request headers are administrator- or adapter-controlled, never agent
inputs.
"""
from __future__ import annotations

import base64
from typing import Any, Mapping, Sequence

from .base import (
    ImageArtifact,
    ImageGenerationError,
    ImageGenerationRequest,
    ImageGenerationResult,
    ImageProvider,
    ProviderCapabilities,
    ProviderConfigField,
    SafeErrorCode,
)
from .network import BoundedHttpClient, configured_endpoint


class Automatic1111Provider(ImageProvider):
    """Fixed-schema adapter for a locally approved AUTOMATIC1111 API."""

    id = "automatic1111"
    display_name = "AUTOMATIC1111-compatible local API"
    is_local = True
    capabilities = ProviderCapabilities(
        supported_sizes=("512x512", "768x768", "1024x1024"),
        max_images_per_request=4,
        supported_output_formats=("png",),
        supports_negative_prompt=True,
        supports_seed=True,
    )
    config_fields: Sequence[ProviderConfigField] = (
        ProviderConfigField("automatic1111_endpoint", "AUTOMATIC1111 Endpoint", required=True,
                            description="Administrator-approved API base URL."),
        ProviderConfigField("automatic1111_trusted_hosts", "Trusted Local Hosts", required=True,
                            description="Comma-separated exact hostnames approved for this provider."),
        ProviderConfigField("automatic1111_timeout_seconds", "Request Timeout (seconds)", type="number", default=60,
                            description="Bounded between 1 and 120 seconds."),
        ProviderConfigField("automatic1111_steps", "Generation Steps", type="number", default=20,
                            description="Administrator-controlled default between 1 and 100."),
    )

    def _client(self, config: Mapping[str, Any]) -> BoundedHttpClient:
        return BoundedHttpClient(configured_endpoint(config, "automatic1111", local=True))

    @staticmethod
    def _steps(config: Mapping[str, Any]) -> int:
        try:
            steps = int(config.get("automatic1111_steps", 20))
        except (TypeError, ValueError) as exc:
            raise ImageGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "Local provider steps must be an integer.") from exc
        if not 1 <= steps <= 100:
            raise ImageGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "Local provider steps are outside the permitted range.")
        return steps

    def test_connection(self, config: Mapping[str, Any]) -> None:
        """Verify the approved endpoint responds without leaking its details."""
        self._client(config).json("GET", "/sdapi/v1/options")

    def generate(self, request: ImageGenerationRequest, config: Mapping[str, Any]) -> ImageGenerationResult:
        payload: dict[str, Any] = {
            "prompt": request.prompt,
            "width": int(request.size.split("x", 1)[0]),
            "height": int(request.size.split("x", 1)[1]),
            "batch_size": request.count,
            "n_iter": 1,
            "steps": self._steps(config),
        }
        if request.negative_prompt:
            payload["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            payload["seed"] = request.seed
        response = self._client(config).json("POST", "/sdapi/v1/txt2img", payload)
        images = response.get("images")
        if not isinstance(images, list) or len(images) != request.count:
            raise ImageGenerationError(SafeErrorCode.GENERATION_FAILED, "The provider returned an invalid image response.")
        artifacts = []
        for index, encoded in enumerate(images, start=1):
            if not isinstance(encoded, str):
                raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider returned an invalid image artifact.")
            try:
                data = base64.b64decode(encoded.split(",", 1)[-1], validate=True)
            except (ValueError, TypeError) as exc:
                raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider returned an invalid image artifact.") from exc
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider returned an invalid PNG artifact.")
            artifacts.append(ImageArtifact(mime_type="image/png", filename=f"automatic1111-{index}.png", data=data))
        return ImageGenerationResult(provider_id=self.id, artifacts=artifacts, model=request.model)
