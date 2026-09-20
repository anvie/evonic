"""Provider contracts and registry for the Image Generator skill."""

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
from .mock import DeterministicMockProvider
from .registry import ImageProviderRegistry, provider_registry

__all__ = [
    "DeterministicMockProvider",
    "ImageArtifact",
    "ImageGenerationError",
    "ImageGenerationRequest",
    "ImageGenerationResult",
    "ImageProvider",
    "ImageProviderRegistry",
    "ProviderCapabilities",
    "ProviderConfigField",
    "SafeErrorCode",
    "provider_registry",
]
