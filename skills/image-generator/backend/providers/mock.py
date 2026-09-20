"""Deterministic provider reserved for automated tests and local development."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from .base import (
    ImageArtifact,
    ImageGenerationRequest,
    ImageGenerationResult,
    ImageProvider,
    ProviderCapabilities,
    ProviderConfigField,
)

# A tiny, valid, transparent 1x1 PNG.  Its stable bytes make tests deterministic.
_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082"
)


class DeterministicMockProvider(ImageProvider):
    """Offline provider that never calls a network and always returns valid PNGs."""

    id = "mock"
    display_name = "Deterministic Mock"
    is_local = True
    capabilities = ProviderCapabilities(
        supported_sizes=("256x256", "512x512", "1024x1024"),
        max_images_per_request=4,
        supported_output_formats=("png",),
        supported_models=("deterministic-mock-v1",),
        supports_negative_prompt=True,
        supports_seed=True,
        supports_style=True,
        supports_transparency=True,
        supports_reference_images=True,
    )
    config_fields = (
        ProviderConfigField(
            name="IMAGE_GENERATOR_MOCK_ENABLED",
            label="Enable deterministic mock provider",
            type="boolean",
            default=False,
            description="Allows the offline deterministic provider for automated tests and local development.",
        ),
    )

    def generate(self, request: ImageGenerationRequest, config: Mapping[str, Any]) -> ImageGenerationResult:
        artifacts = []
        for index in range(request.count):
            digest = hashlib.sha256(
                f"{request.prompt}|{request.size}|{request.seed}|{index}".encode("utf-8")
            ).hexdigest()[:12]
            artifacts.append(
                ImageArtifact(
                    mime_type="image/png",
                    filename=f"mock-{digest}.png",
                    data=_PNG_1X1,
                    revised_prompt=request.prompt,
                )
            )
        return ImageGenerationResult(
            provider_id=self.id,
            artifacts=tuple(artifacts),
            model="deterministic-mock-v1",
            request_id=hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()[:16],
        )
