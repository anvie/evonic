from unittest.mock import patch

from backend.tools.describe_image import execute


def _vision_model(model_id):
    return {"id": model_id, "name": model_id}


def _write_png(path):
    # The tool only encodes this fixture; it does not decode PNG input below 3 MB.
    path.write_bytes(b"\x89PNG\r\n\x1a\n")


def test_describe_image_reports_primary_model_for_nonrecoverable_error(tmp_path):
    image_path = tmp_path / "image.png"
    _write_png(image_path)
    result = {
        "success": False,
        "error_type": "invalid_request",
        "error_detail": "unsupported image",
    }

    with (
        patch("backend.tools.describe_image._resolve_vision_models",
              return_value=([_vision_model("primary")], None)),
        patch("backend.tools.describe_image.LLMClient") as client_class,
    ):
        client_class.return_value.timeout = None
        client_class.return_value.chat_completion.return_value = result
        error = execute({"id": "test-agent"}, {"path": str(image_path)})

    assert error == (
        "Error: Vision model call failed for primary model (primary) "
        "(invalid_request): unsupported image"
    )


def test_describe_image_reports_last_fallback_model_after_transient_failures(tmp_path):
    image_path = tmp_path / "image.png"
    _write_png(image_path)
    models = [_vision_model("primary"), _vision_model("fallback-1"), _vision_model("fallback-2")]
    failures = [
        {"success": False, "error_type": "connection_error", "error_detail": "connection refused"},
        {"success": False, "error_type": "timeout_error", "error_detail": "timed out"},
        {"success": False, "error_type": "api_error", "error_detail": "service unavailable"},
    ]

    with (
        patch("backend.tools.describe_image._resolve_vision_models", return_value=(models, None)),
        patch("backend.tools.describe_image.LLMClient") as client_class,
    ):
        client_class.return_value.timeout = None
        client_class.return_value.chat_completion.side_effect = failures
        error = execute({"id": "test-agent"}, {"path": str(image_path)})

    assert "All vision-capable models failed (3 model(s) tried)" in error
    assert "fallback model 2 (fallback-2): service unavailable" in error
