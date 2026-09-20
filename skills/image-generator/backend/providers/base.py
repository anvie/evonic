"""Provider-neutral contracts for image generation.

This module deliberately contains no network or provider SDK code.  Production
providers implement :class:`ImageProvider` and return the normalized types
below, allowing the future tool executor to remain provider agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence


class SafeErrorCode(str, Enum):
    """Stable, non-sensitive categories suitable for agent-facing errors."""

    INVALID_REQUEST = "invalid_request"
    PROVIDER_NOT_FOUND = "provider_not_found"
    PROVIDER_DISABLED = "provider_disabled"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_CONFIGURATION = "provider_configuration"
    PERMISSION_DENIED = "permission_denied"
    CONTENT_REJECTED = "content_rejected"
    RATE_LIMITED = "rate_limited"
    GENERATION_FAILED = "generation_failed"
    ARTIFACT_INVALID = "artifact_invalid"


class ImageGenerationError(Exception):
    """Provider failure that exposes a safe category and sanitized message."""

    def __init__(self, code: SafeErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> Dict[str, str]:
        return {"code": self.code.value, "message": self.message}


@dataclass(frozen=True)
class ProviderConfigField:
    """Declarative schema for provider-specific configuration fields."""

    name: str
    label: str
    type: str = "string"
    required: bool = False
    secret: bool = False
    default: Any = ""
    description: str = ""
    choices: Sequence[str] = ()

    def as_manifest_variable(self) -> Dict[str, Any]:
        """Convert to the existing skill variable schema where possible."""
        variable_type = "secret" if self.secret else self.type
        variable = {
            "name": self.name,
            "label": self.label,
            "type": variable_type,
            "default": self.default,
            "description": self.description,
        }
        if self.choices:
            variable["choices"] = list(self.choices)
        return variable


@dataclass(frozen=True)
class ProviderCapabilities:
    """Generation options a provider can support."""

    supported_sizes: Sequence[str]
    max_images_per_request: int = 1
    supports_negative_prompt: bool = False
    supports_seed: bool = False
    supports_style: bool = False
    supports_transparency: bool = False
    supports_reference_images: bool = False


@dataclass(frozen=True)
class ImageGenerationRequest:
    """A validated, provider-neutral request to create one or more images."""

    prompt: str
    size: str = "1024x1024"
    count: int = 1
    negative_prompt: Optional[str] = None
    seed: Optional[int] = None
    style: Optional[str] = None
    transparent_background: bool = False
    reference_image_ids: Sequence[str] = ()
    provider_options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ImageArtifact:
    """Validated image output before Evonic artifact persistence.

    Exactly one of ``data`` or ``url`` must be supplied.  ``data`` is raw image
    bytes; it is intentionally kept separate from any agent-facing artifact ID.
    """

    mime_type: str
    filename: str
    data: Optional[bytes] = None
    url: Optional[str] = None
    revised_prompt: Optional[str] = None

    def validate(self) -> None:
        if not self.mime_type.startswith("image/"):
            raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned an invalid image MIME type.")
        if not self.filename or "/" in self.filename or "\\" in self.filename:
            raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned an unsafe image filename.")
        if (self.data is None) == (self.url is None):
            raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider must return exactly one image source.")
        if self.data is not None and not self.data:
            raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned an empty image artifact.")
        if self.url is not None and not self.url.startswith("https://"):
            raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned an unsafe image URL.")


@dataclass(frozen=True)
class ImageGenerationResult:
    """Normalized successful image-generation response."""

    provider_id: str
    artifacts: Sequence[ImageArtifact]
    model: Optional[str] = None
    request_id: Optional[str] = None
    warnings: Sequence[str] = ()

    def validate(self) -> None:
        if not self.artifacts:
            raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned no image artifacts.")
        for artifact in self.artifacts:
            artifact.validate()


class ImageProvider(ABC):
    """Contract implemented by each approved image-generation provider."""

    id: str
    display_name: str
    capabilities: ProviderCapabilities
    config_fields: Sequence[ProviderConfigField] = ()
    is_local: bool = False

    @abstractmethod
    def generate(
        self,
        request: ImageGenerationRequest,
        config: Mapping[str, Any],
    ) -> ImageGenerationResult:
        """Generate images or raise :class:`ImageGenerationError`.

        Implementations must never expose credentials, raw provider payloads, or
        internal network details in error messages.
        """
