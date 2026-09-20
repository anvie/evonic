"""Generate and persist validated image artifacts for the Image Generator skill."""

from __future__ import annotations

from io import BytesIO
import ipaddress
import os
import re
import socket
from typing import Any, Mapping
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image, UnidentifiedImageError

from ..providers import (
    ImageArtifact,
    ImageGenerationError,
    ImageGenerationRequest,
    SafeErrorCode,
    provider_registry,
)

_MAX_PROMPT_LENGTH = 4_000
_MAX_NEGATIVE_PROMPT_LENGTH = 4_000
_MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
_MAX_IMAGE_DIMENSION = 8_192
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_EXTENSION_BY_MIME_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _error(code: SafeErrorCode, message: str) -> dict:
    return {"status": "error", "error": {"code": code.value, "message": message}}


def _configured_providers(value: Any) -> set[str]:
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def _boolean(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _positive_int(value: Any, default: int, field: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, f"{field} must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, f"{field} must be an integer.") from exc
    if parsed < 1:
        raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, f"{field} must be at least 1.")
    return parsed


def _optional_text(args: Mapping[str, Any], field: str, max_length: int) -> str | None:
    value = args.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, f"{field} must be text.")
    value = value.strip()
    if not value:
        return None
    if len(value) > max_length:
        raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, f"{field} is too long.")
    return value


def _normalize_request(args: Mapping[str, Any], configured_limit: int) -> ImageGenerationRequest:
    prompt = _optional_text(args, "prompt", _MAX_PROMPT_LENGTH)
    if not prompt:
        raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "An image prompt is required.")

    size = _optional_text(args, "size", 32) or _optional_text(args, "aspect_ratio", 32) or "1024x1024"
    # The provider-neutral contract currently represents supported aspect ratios as sizes.
    if not re.fullmatch(r"\d{2,5}x\d{2,5}", size):
        raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "size must use WIDTHxHEIGHT notation.")

    count = _positive_int(args.get("count"), 1, "count")
    if count > configured_limit:
        raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "Requested image count exceeds the configured limit.")

    seed = args.get("seed")
    if seed is not None:
        if isinstance(seed, bool):
            raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "seed must be an integer.")
        try:
            seed = int(seed)
        except (TypeError, ValueError) as exc:
            raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "seed must be an integer.") from exc

    output_format = (_optional_text(args, "output_format", 16) or "png").lower().lstrip(".")
    if output_format not in {"png", "jpeg", "jpg", "webp", "gif"}:
        raise ImageGenerationError(SafeErrorCode.INVALID_REQUEST, "output_format must be png, jpeg, webp, or gif.")
    model = _optional_text(args, "model", 256)

    return ImageGenerationRequest(
        prompt=prompt,
        size=size,
        count=count,
        negative_prompt=_optional_text(args, "negative_prompt", _MAX_NEGATIVE_PROMPT_LENGTH),
        seed=seed,
        model=model,
        output_format="jpeg" if output_format == "jpg" else output_format,
    )


def _resolve_provider(args: Mapping[str, Any], config: Mapping[str, Any]):
    explicit = _optional_text(args, "provider", 128)
    default = str(config.get("default_provider") or "").strip() or None
    provider = provider_registry.resolve(explicit, default)
    if provider.id not in _configured_providers(config.get("allowed_providers")):
        raise ImageGenerationError(SafeErrorCode.PROVIDER_DISABLED, "The selected image provider is not enabled for this skill.")
    if provider.is_local and not _boolean(config.get("allow_local_providers")):
        raise ImageGenerationError(SafeErrorCode.PROVIDER_DISABLED, "Local image providers are not enabled for this skill.")
    return provider


def _validated_filename(filename: str, mime_type: str, index: int) -> str:
    if not _SAFE_FILENAME.fullmatch(filename) or ".." in filename:
        raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned an unsafe image filename.")
    extension = _EXTENSION_BY_MIME_TYPE.get(mime_type)
    if not extension:
        raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned an unsupported image MIME type.")
    stem, supplied_extension = os.path.splitext(filename)
    if supplied_extension.lower() != extension:
        filename = f"{stem}{extension}"
    return f"generated-{index + 1}-{filename}"


def _is_public_https_url(value: str) -> bool:
    """Allow only HTTPS URLs resolving to publicly routable addresses."""
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
        return bool(addresses) and all(not ipaddress.ip_address(address).is_private and not ipaddress.ip_address(address).is_loopback and not ipaddress.ip_address(address).is_link_local and not ipaddress.ip_address(address).is_reserved for address in addresses)
    except (OSError, ValueError):
        return False


def _download_image(url: str) -> bytes:
    """Download a provider image without following redirects or reaching private networks."""
    if not _is_public_https_url(url):
        raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned an unsafe image URL.")
    opener = build_opener(HTTPRedirectHandler())
    opener.redirect_request = lambda *_args, **_kwargs: None
    try:
        with opener.open(Request(url, headers={"Accept": "image/*"}), timeout=15) as response:
            content_type = response.headers.get_content_type()
            if not content_type.startswith("image/"):
                raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider URL did not return an image.")
            data = response.read(_MAX_ARTIFACT_BYTES + 1)
    except ImageGenerationError:
        raise
    except Exception as exc:
        raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider image URL could not be retrieved.") from exc
    if len(data) > _MAX_ARTIFACT_BYTES:
        raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Generated image exceeds the maximum artifact size.")
    return data


def _validate_image_bytes(artifact: ImageArtifact) -> tuple[bytes, str, int, int]:
    data = artifact.data if artifact.data is not None else _download_image(artifact.url or "")
    if len(data) > _MAX_ARTIFACT_BYTES:
        raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Generated image exceeds the maximum artifact size.")
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
        with Image.open(BytesIO(data)) as image:
            actual_mime = Image.MIME.get(image.format, "").lower()
            width, height = image.size
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned undecodable image data.") from exc
    if actual_mime != artifact.mime_type.lower():
        raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Generated image MIME type does not match its content.")
    if not (0 < width <= _MAX_IMAGE_DIMENSION and 0 < height <= _MAX_IMAGE_DIMENSION):
        raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Generated image dimensions are outside allowed limits.")
    return data, actual_mime, width, height


def _persist_image(agent: Mapping[str, Any], filename: str, data: bytes) -> str:
    from backend.tools.save_artifact import _artifacts_dir, _chown_to_run_as, _resolve_run_as_user
    from backend.tools._workspace import effective_agent_id

    agent_id = effective_agent_id(dict(agent))
    if not agent_id:
        raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Agent context has no artifact destination.")
    directory = _artifacts_dir(agent_id, _resolve_run_as_user(dict(agent)))
    filepath = os.path.join(directory, filename)
    with open(filepath, "xb") as artifact_file:
        artifact_file.write(data)
    _chown_to_run_as(filepath, _resolve_run_as_user(dict(agent)))
    return filename


def execute(agent: dict, args: dict) -> dict:
    """Generate images through one configured provider and return artifact metadata."""
    try:
        from backend.skills_manager import skills_manager

        config = skills_manager.get_skill_config("image-generator")
        configured_limit = _positive_int(config.get("max_images_per_request"), 1, "max_images_per_request")
        request = _normalize_request(args, configured_limit)
        provider = _resolve_provider(args, config)
        result = provider_registry.generate(request, config, provider_id=provider.id)
        if len(result.artifacts) > request.count:
            raise ImageGenerationError(SafeErrorCode.ARTIFACT_INVALID, "Provider returned more images than requested.")

        artifacts = []
        for index, artifact in enumerate(result.artifacts):
            data, mime_type, width, height = _validate_image_bytes(artifact)
            filename = _validated_filename(artifact.filename, mime_type, index)
            stored_filename = _persist_image(agent, filename, data)
            artifacts.append({
                "filename": stored_filename,
                "mime_type": mime_type,
                "size": len(data),
                "width": width,
                "height": height,
            })
        return {"status": "success", "provider": result.provider_id, "model": result.model, "artifacts": artifacts}
    except ImageGenerationError as exc:
        return _error(exc.code, exc.message)
    except FileExistsError:
        return _error(SafeErrorCode.ARTIFACT_INVALID, "Generated artifact filename already exists; retry the request.")
    except Exception:
        return _error(SafeErrorCode.GENERATION_FAILED, "Image generation could not be completed.")
