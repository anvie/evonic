"""Fixed-host Google Gemini image-generation provider.

This adapter deliberately exposes a small, reviewed subset of the Generative
Language API.  The API host, resource path, authentication header, and model
allowlist are adapter-controlled; agent tool arguments cannot change them.
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
from .network import BoundedHttpClient, SafeEndpoint, bounded_timeout

_GOOGLE_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_GOOGLE_IMAGE_MODEL = "gemini-2.5-flash-image"
_GOOGLE_GEMINI_31_IMAGE_MODEL = "gemini-3.1-flash-image"
_SUPPORTED_MODELS = (_GOOGLE_IMAGE_MODEL, _GOOGLE_GEMINI_31_IMAGE_MODEL)

# Registry-validated sizes that map to a native Gemini aspectRatio. Gemini decides
# the pixel dimensions; these sizes only select the model's native ratio.
_ASPECT_RATIO_BY_SIZE = {
    "768x1024": "3:4",
    "768x1344": "9:16",
    "832x1248": "2:3",
    "1024x768": "4:3",
    "1344x768": "16:9",
    "1248x832": "3:2",
}


class GoogleGeminiProvider(ImageProvider):
    """Generate a single PNG image using Google's approved Gemini model."""

    id = "google-gemini"
    display_name = "Google Gemini image generation"
    capabilities = ProviderCapabilities(
        supported_sizes=(
            "512x512",
            "768x768",
            "1024x1024",
            "768x1024",
            "768x1344",
            "832x1248",
            "1024x768",
            "1344x768",
            "1248x832",
        ),
        max_images_per_request=1,
        supported_output_formats=("png", "jpeg"),
        supported_models=_SUPPORTED_MODELS,
    )
    config_fields: Sequence[ProviderConfigField] = (
        ProviderConfigField(
            "google_gemini_api_key", "Google Gemini API Key", required=True, secret=True,
            description="Google Generative Language API key. It is write-only and never returned to agents.",
        ),
        ProviderConfigField(
            "google_gemini_model", "Google Gemini Model", default=_GOOGLE_IMAGE_MODEL,
            choices=_SUPPORTED_MODELS,
            description="Approved Google image-generation model.",
        ),
        ProviderConfigField(
            "google_gemini_timeout_seconds", "Google Gemini Timeout (seconds)", type="number", default=60,
            description="Bounded request timeout from 1 through 120 seconds.",
        ),
    )

    @staticmethod
    def _credential(config: Mapping[str, Any]) -> str:
        value = config.get("google_gemini_api_key")
        if not isinstance(value, str) or not value.strip():
            raise ImageGenerationError(SafeErrorCode.PROVIDER_CONFIGURATION, "Google Gemini requires an administrator-configured credential.")
        return value.strip()

    @staticmethod
    def _model(request: ImageGenerationRequest, config: Mapping[str, Any]) -> str:
        model = request.model or config.get("google_gemini_model", _GOOGLE_IMAGE_MODEL)
        if not isinstance(model, str) or model not in _SUPPORTED_MODELS:
            raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "The selected Google Gemini model is not approved.")
        return model

    def _client(self, config: Mapping[str, Any]) -> BoundedHttpClient:
        return BoundedHttpClient(SafeEndpoint(
            base_url=_GOOGLE_API_BASE_URL,
            timeout_seconds=bounded_timeout(config.get("google_gemini_timeout_seconds")),
        ))

    @staticmethod
    def _headers(credential: str) -> Mapping[str, str]:
        return {"x-goog-api-key": credential}

    def test_connection(self, config: Mapping[str, Any]) -> None:
        """Verify Google credentials against a fixed model resource."""
        credential = self._credential(config)
        model = self._model(ImageGenerationRequest(prompt="connection test"), config)
        self._client(config).json("GET", f"/models/{model}", headers=self._headers(credential))

    @staticmethod
    def _artifacts(response: Mapping[str, Any]) -> tuple[ImageArtifact, ...]:
        prompt_feedback = response.get("promptFeedback")
        if isinstance(prompt_feedback, Mapping) and prompt_feedback.get("blockReason"):
            raise ImageGenerationError(SafeErrorCode.CONTENT_REJECTED, "The provider rejected the image prompt.")

        candidates = response.get("candidates")
        if not isinstance(candidates, list):
            raise ImageGenerationError(SafeErrorCode.GENERATION_FAILED, "The provider returned an invalid image response.")

        artifacts: list[ImageArtifact] = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            if candidate.get("finishReason") in {"SAFETY", "RECITATION", "BLOCKLIST"}:
                raise ImageGenerationError(SafeErrorCode.CONTENT_REJECTED, "The provider rejected the image prompt.")
            content = candidate.get("content")
            parts = content.get("parts") if isinstance(content, Mapping) else None
            if not isinstance(parts, list):
                continue
            for part in parts:
                inline_data = part.get("inlineData") if isinstance(part, Mapping) else None
                if not isinstance(inline_data, Mapping):
                    continue
                mime_type = inline_data.get("mimeType")
                encoded = inline_data.get("data")
                if mime_type not in {"image/png", "image/jpeg"} or not isinstance(encoded, str):
                    raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider returned an invalid image artifact.")
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError) as exc:
                    raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider returned an invalid image artifact.") from exc
                signatures = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}
                if not data.startswith(signatures[mime_type]):
                    raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider returned invalid image data.")
                extension = ".png" if mime_type == "image/png" else ".jpg"
                artifacts.append(ImageArtifact(mime_type=mime_type, filename=f"google-gemini-{len(artifacts) + 1}{extension}", data=data))

        if not artifacts:
            raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "The provider returned no image artifacts.")
        return tuple(artifacts)

    def generate(self, request: ImageGenerationRequest, config: Mapping[str, Any]) -> ImageGenerationResult:
        credential = self._credential(config)
        model = self._model(request, config)
        payload = {
            "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        aspect_ratio = _ASPECT_RATIO_BY_SIZE.get(request.size)
        if aspect_ratio is not None:
            # Non-square sizes select a native Gemini aspect ratio; the model still
            # decides the pixel dimensions.
            payload["generationConfig"]["imageConfig"] = {"aspectRatio": aspect_ratio}
        response = self._client(config).json(
            "POST", f"/models/{model}:generateContent", payload, headers=self._headers(credential),
        )
        artifacts = self._artifacts(response)
        if len(artifacts) != request.count:
            raise ImageGenerationError(SafeErrorCode.GENERATION_FAILED, "The provider returned an unexpected number of image artifacts.")
        response_model = response.get("modelVersion")
        return ImageGenerationResult(
            provider_id=self.id,
            artifacts=artifacts,
            model=response_model if isinstance(response_model, str) and response_model else model,
        )
