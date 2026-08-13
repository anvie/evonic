import base64
from unittest.mock import patch

import backend.tools.describe_image as describe_image
import pytest
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


@pytest.mark.parametrize("path", ["generated.png", "/workspace/generated.png"])
def test_describe_image_reads_backend_only_path(path):
    image_data = b"\x89PNG\r\n\x1a\n"
    agent = {
        "id": "backend-agent",
        "session_id": "session-1",
        "sandbox_enabled": 1,
        "workspace": "/host/workspace",
    }

    with (
        patch("backend.tools.lib.exec_backend.registry.get_backend") as get_backend,
        patch("backend.tools.describe_image._resolve_vision_models",
              return_value=([_vision_model("vision")], None)),
        patch("backend.tools.describe_image.LLMClient") as client_class,
    ):
        backend = get_backend.return_value
        backend.resolve_path.return_value = "/workspace/generated.png"
        backend.file_stat.return_value = {"exists": True, "size": len(image_data)}
        backend.cat_file_bytes.return_value = {"bytes": image_data}
        client_class.return_value.timeout = None
        client_class.return_value.chat_completion.return_value = {
            "success": True,
            "response": {"choices": [{"message": {"content": "backend image"}}]},
        }

        result = execute(agent, {"path": path})

    assert result == "backend image"
    get_backend.assert_called_once_with("session-1", agent)
    backend.resolve_path.assert_called_once_with("/host/workspace/generated.png")
    backend.file_stat.assert_called_once_with("/workspace/generated.png")
    backend.cat_file_bytes.assert_called_once_with("/workspace/generated.png")
    messages = client_class.return_value.chat_completion.call_args.kwargs["messages"]
    assert messages[1]["content"][1]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(image_data).decode("ascii")
    )


def test_describe_image_prefers_existing_host_path(tmp_path):
    image_path = tmp_path / "host.png"
    _write_png(image_path)

    with (
        patch("backend.tools.lib.exec_backend.registry.get_backend") as get_backend,
        patch("backend.tools.describe_image._resolve_vision_models",
              return_value=([_vision_model("vision")], None)),
        patch("backend.tools.describe_image.LLMClient") as client_class,
    ):
        client_class.return_value.timeout = None
        client_class.return_value.chat_completion.return_value = {
            "success": True,
            "response": {"choices": [{"message": {"content": "host image"}}]},
        }
        result = execute(
            {"id": "host-agent", "sandbox_enabled": 0},
            {"path": str(image_path)},
        )

    assert result == "host image"
    get_backend.assert_not_called()


def test_describe_image_checks_backend_size_before_reading():
    with patch("backend.tools.lib.exec_backend.registry.get_backend") as get_backend:
        backend = get_backend.return_value
        backend.resolve_path.return_value = "/workspace/large.png"
        backend.file_stat.return_value = {
            "exists": True,
            "size": describe_image._MAX_IMAGE_BYTES + 1,
        }

        result = execute(
            {"id": "backend-agent", "session_id": "session-1"},
            {"path": "/workspace/large.png"},
        )

    assert result == "Error: Image file is 10.0 MB, which exceeds the 10 MB limit."
    backend.cat_file_bytes.assert_not_called()


def test_describe_image_reports_backend_read_failures():
    with patch("backend.tools.lib.exec_backend.registry.get_backend") as get_backend:
        backend = get_backend.return_value
        backend.resolve_path.return_value = "/workspace/image.png"
        backend.file_stat.return_value = {"exists": True, "size": 8}
        backend.cat_file_bytes.return_value = {"error": "transport failed"}
        agent = {"id": "backend-agent", "session_id": "session-1"}

        assert execute(agent, {"path": "/workspace/image.png"}) == (
            "Error: Failed to read image: transport failed"
        )

        backend.cat_file_bytes.return_value = {"bytes": "not bytes"}
        assert execute(agent, {"path": "/workspace/image.png"}) == (
            "Error: Failed to read image: execution backend returned invalid data."
        )


def test_describe_image_reports_backend_lookup_failures():
    with patch("backend.tools.lib.exec_backend.registry.get_backend") as get_backend:
        backend = get_backend.return_value
        backend.resolve_path.return_value = "/workspace/missing.png"
        backend.file_stat.return_value = {"exists": False}
        agent = {"id": "backend-agent", "session_id": "session-1"}

        assert execute(agent, {"path": "/workspace/missing.png"}) == (
            "Error: File not found: /workspace/missing.png"
        )

        backend.file_stat.side_effect = RuntimeError("backend unavailable")
        assert execute(agent, {"path": "/workspace/missing.png"}) == (
            "Error: Failed to access execution environment: backend unavailable"
        )


def test_describe_image_denies_self_path_escape_without_backend_fallback():
    with patch("backend.tools.lib.exec_backend.registry.get_backend") as get_backend:
        result = execute(
            {"id": "test-agent", "session_id": "session-1"},
            {"path": "/_self/../../outside.png"},
        )

    assert result == "Error: Access denied — path escapes agent directory."
    get_backend.assert_not_called()
