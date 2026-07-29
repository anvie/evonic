import io

import pytest

from plugins.muktamar_api import routes


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(routes, "_keys", lambda: ("test-key",))
    monkeypatch.setattr(routes, "_cache", routes.PhotoValidationCache(str(tmp_path / "client-cache.db")))
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(routes.create_blueprint())
    return app.test_client()


@pytest.fixture()
def isolated_cache(monkeypatch, tmp_path):
    cache = routes.PhotoValidationCache(str(tmp_path / "muktamar-api.db"))
    monkeypatch.setattr(routes, "_cache", cache)
    return cache


def test_requires_bearer_key(client):
    response = client.post("/plugin/muktamar-api/v1/photo/validate")
    assert response.status_code == 401


def test_rejects_invalid_key(client):
    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_requires_photo_only(client):
    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer test-key"},
    )
    assert response.status_code == 400


def test_validator_result_is_normalized_and_temp_file_cleaned(client, monkeypatch):
    seen = {}

    class FakeModule:
        @staticmethod
        def execute_standalone(agent, args):
            seen["path"] = args["attachment_path"]
            assert agent == {}
            return {
                "accepted": True,
                "message": "accepted",
                "model_index": 1,
                "model_name": "private/provider-model-name",
            }

    monkeypatch.setattr(routes, "_config", lambda: {"MAX_UPLOAD_BYTES": 100})
    monkeypatch.setattr(routes.tool_registry, "_load_tool_module", lambda *args, **kwargs: FakeModule)

    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer test-key"},
        data={"photo": (io.BytesIO(b"image-bytes"), "photo.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body == {
        "success": True,
        "reason_code": ["OK"],
        "message": "accepted",
        "model_index": 1,
    }
    assert "model_name" not in body
    import os
    assert not os.path.exists(seen["path"])


def test_cache_hit_skips_validator_and_returns_original_result(client, monkeypatch, isolated_cache):
    image = b"same-image"
    import hashlib
    isolated_cache.put(hashlib.sha1(image).hexdigest(), {
        "success": False,
        "reason_code": ["APPROPRIATE_POSE"],
        "message": "cached result",
    })

    def fail_if_called(*args, **kwargs):
        raise AssertionError("validator must not run on a cache hit")

    monkeypatch.setattr(routes.tool_registry, "_load_tool_module", fail_if_called)
    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer test-key"},
        data={"photo": (io.BytesIO(image), "different-name.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "success": False,
        "reason_code": ["APPROPRIATE_POSE"],
        "message": "cached result",
    }


def test_cache_miss_stores_failure_result(client, monkeypatch, isolated_cache):
    class FakeModule:
        @staticmethod
        def execute_standalone(agent, args):
            return {"accepted": False, "reason_code": "NOT_PORTRAIT", "message": "portrait required"}

    monkeypatch.setattr(routes.tool_registry, "_load_tool_module", lambda *args, **kwargs: FakeModule)
    image = b"new-image"
    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer test-key"},
        data={"photo": (io.BytesIO(image), "photo.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["success"] is False
    import hashlib
    assert isolated_cache.get(hashlib.sha1(image).hexdigest()) == response.get_json()


def test_validator_failure_reasons_are_returned_as_array(client, monkeypatch):
    class FakeModule:
        @staticmethod
        def execute_standalone(agent, args):
            return {
                "accepted": False,
                "reason_code": ["APPROPRIATE_POSE", "APPROPRIATE_BACKGROUND"],
                "user_message": "Pose dan latar foto tidak sesuai.",
            }

    monkeypatch.setattr(routes, "_config", lambda: {"MAX_UPLOAD_BYTES": 100})
    monkeypatch.setattr(routes.tool_registry, "_load_tool_module", lambda *args, **kwargs: FakeModule)

    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer test-key"},
        data={"photo": (io.BytesIO(b"image-bytes"), "photo.jpg")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "success": False,
        "reason_code": ["APPROPRIATE_POSE", "APPROPRIATE_BACKGROUND"],
        "message": "Pose dan latar foto tidak sesuai.",
    }


def test_legacy_scalar_failure_reason_is_normalized_to_array(client, monkeypatch):
    class FakeModule:
        @staticmethod
        def execute_standalone(agent, args):
            return {
                "accepted": False,
                "reason_code": "NOT_PORTRAIT",
                "message": "Orientasi foto harus portrait.",
            }

    monkeypatch.setattr(routes, "_config", lambda: {"MAX_UPLOAD_BYTES": 100})
    monkeypatch.setattr(routes.tool_registry, "_load_tool_module", lambda *args, **kwargs: FakeModule)

    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer test-key"},
        data={"photo": (io.BytesIO(b"image-bytes"), "photo.jpg")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "success": False,
        "reason_code": ["NOT_PORTRAIT"],
        "message": "Orientasi foto harus portrait.",
    }


def test_upload_limit(client, monkeypatch):
    monkeypatch.setattr(routes, "_config", lambda: {"MAX_UPLOAD_BYTES": 4})
    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer test-key"},
        data={"photo": (io.BytesIO(b"12345"), "photo.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 413


def test_non_ascii_bearer_key_returns_401_not_500(client):
    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer caf\u00e9-key"},
    )
    assert response.status_code == 401
    body = response.get_json()
    assert body["error"] == "Unauthorized"


def test_config_exception_returns_503(client, monkeypatch):
    def _failing_config():
        raise RuntimeError("plugin-manager unavailable")
    monkeypatch.setattr(routes, "_config", _failing_config)
    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer test-key"},
        data={"photo": (io.BytesIO(b"valid-image"), "photo.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 503
    body = response.get_json()
    assert body["error"] == "Photo validator is unavailable"


def test_module_load_exception_returns_503_and_cleans_temp(client, monkeypatch):
    seen = {}

    def _failing_load(*args, **kwargs):
        raise ImportError("vision-dependency missing")

    monkeypatch.setattr(routes, "_config", lambda: {"MAX_UPLOAD_BYTES": 100})
    monkeypatch.setattr(routes.tool_registry, "_load_tool_module", _failing_load)

    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer test-key"},
        data={"photo": (io.BytesIO(b"test-image"), "photo.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 503
    body = response.get_json()
    assert body["error"] == "Photo validator is unavailable"


def test_validator_execution_exception_returns_503_and_cleans_temp(client, monkeypatch):
    seen = {}

    class FailingModule:
        @staticmethod
        def execute_standalone(agent, args):
            seen["path"] = args["attachment_path"]
            raise RuntimeError("Vision provider crashed")

    monkeypatch.setattr(routes, "_config", lambda: {"MAX_UPLOAD_BYTES": 100})
    monkeypatch.setattr(routes.tool_registry, "_load_tool_module", lambda *args, **kwargs: FailingModule)

    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer test-key"},
        data={"photo": (io.BytesIO(b"image-data"), "photo.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 503
    body = response.get_json()
    assert body["error"] == "Photo validator is unavailable"
    import os
    assert "path" in seen
    assert not os.path.exists(seen["path"])


def test_malformed_validator_result_returns_503_and_cleans_temp(client, monkeypatch):
    seen = {}

    class WeirdModule:
        @staticmethod
        def execute_standalone(agent, args):
            seen["path"] = args["attachment_path"]
            return "not-a-dict"

    monkeypatch.setattr(routes, "_config", lambda: {"MAX_UPLOAD_BYTES": 100})
    monkeypatch.setattr(routes.tool_registry, "_load_tool_module", lambda *args, **kwargs: WeirdModule)

    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer test-key"},
        data={"photo": (io.BytesIO(b"image-data"), "photo.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 503
    body = response.get_json()
    assert body["error"] == "Photo validator is unavailable"
    import os
    assert "path" in seen
    assert not os.path.exists(seen["path"])


def test_validator_status_error_returns_503_and_cleans_temp(client, monkeypatch):
    seen = {}

    class ErrorModule:
        @staticmethod
        def execute_standalone(agent, args):
            seen["path"] = args["attachment_path"]
            return {"status": "error", "error": "Vision service unavailable"}

    monkeypatch.setattr(routes, "_config", lambda: {"MAX_UPLOAD_BYTES": 100})
    monkeypatch.setattr(routes.tool_registry, "_load_tool_module", lambda *args, **kwargs: ErrorModule)

    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer test-key"},
        data={"photo": (io.BytesIO(b"image-data"), "photo.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 503
    body = response.get_json()
    assert body["error"] == "Photo validator is unavailable"
    import os
    assert "path" in seen
    assert not os.path.exists(seen["path"])
