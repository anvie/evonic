"""Registry of approved Image Generator providers."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

from .base import (
    ImageGenerationError,
    ImageGenerationRequest,
    ImageGenerationResult,
    ImageProvider,
    SafeErrorCode,
)


class ImageProviderRegistry:
    """In-memory registry that resolves and validates approved providers."""

    def __init__(self) -> None:
        self._providers: Dict[str, ImageProvider] = {}

    def register(self, provider: ImageProvider) -> None:
        provider_id = getattr(provider, "id", "")
        if not provider_id or not isinstance(provider_id, str):
            raise ValueError("Image providers require a non-empty string id.")
        if provider_id in self._providers:
            raise ValueError(f"Image provider '{provider_id}' is already registered.")
        self._providers[provider_id] = provider

    def get(self, provider_id: str) -> ImageProvider:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise ImageGenerationError(SafeErrorCode.PROVIDER_NOT_FOUND, "The selected image provider is not available.")
        return provider

    def list(self) -> Iterable[ImageProvider]:
        """Return providers in stable identifier order for settings consumers."""
        return tuple(self._providers[key] for key in sorted(self._providers))

    def resolve(self, provider_id: Optional[str], default_provider_id: Optional[str]) -> ImageProvider:
        """Resolve explicit provider first, otherwise use the configured default."""
        selected = provider_id or default_provider_id
        if not selected:
            raise ImageGenerationError(
                SafeErrorCode.PROVIDER_CONFIGURATION,
                "No image provider is selected. Configure a default provider or select one explicitly.",
            )
        return self.get(selected)

    def generate(
        self,
        request: ImageGenerationRequest,
        config: Mapping[str, Any],
        provider_id: Optional[str] = None,
        default_provider_id: Optional[str] = None,
    ) -> ImageGenerationResult:
        """Generate via one registered provider and validate normalized output."""
        provider = self.resolve(provider_id, default_provider_id)
        self._validate_request(request, provider)
        result = provider.generate(request, config)
        if result.provider_id != provider.id:
            raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned a mismatched provider identifier.")
        result.validate()
        return result

    @staticmethod
    def _validate_request(request: ImageGenerationRequest, provider: ImageProvider) -> None:
        if not isinstance(request.prompt, str) or not request.prompt.strip():
            raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "An image prompt is required.")
        if request.count < 1 or request.count > provider.capabilities.max_images_per_request:
            raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "Requested image count is not supported by the selected provider.")
        if request.size not in provider.capabilities.supported_sizes:
            raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "Requested image size is not supported by the selected provider.")
        if request.output_format and request.output_format not in provider.capabilities.supported_output_formats:
            raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "Requested output format is not supported by the selected provider.")
        if request.model and provider.capabilities.supported_models and request.model not in provider.capabilities.supported_models:
            raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "Requested model is not supported by the selected provider.")
        if request.negative_prompt and not provider.capabilities.supports_negative_prompt:
            raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "The selected provider does not support negative prompts.")
        if request.seed is not None and not provider.capabilities.supports_seed:
            raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "The selected provider does not support seeds.")
        if request.style and not provider.capabilities.supports_style:
            raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "The selected provider does not support styles.")
        if request.transparent_background and not provider.capabilities.supports_transparency:
            raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "The selected provider does not support transparent backgrounds.")
        if request.reference_image_ids and not provider.capabilities.supports_reference_images:
            raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "The selected provider does not support reference images.")


# The mock adapter is intentionally the only built-in provider. It is offline,
# deterministic, and disabled at the skill feature gate by default. Production
# cloud or approved-local adapters must be explicitly added in future work.
from .mock import DeterministicMockProvider

provider_registry = ImageProviderRegistry()
provider_registry.register(DeterministicMockProvider())
