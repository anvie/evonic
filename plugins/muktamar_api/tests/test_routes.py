import io

import pytest

from plugins.muktamar_api import routes


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(routes, "_keys", lambda: ("test-key",))
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(routes.create_blueprint())
    return app.test_client()


def test_requires_bearer_key(client):
    response = client.post("/plugin/muktamar-api/v1/photo/validate")
    assert response.status_code == 401


def test_rejects_invalid_key(client):
    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_requires_draft_and_photo(client):
    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer test-key"},
    )
    assert response.status_code == 400


def test_validator_result_is_normalized_and_temp_file_cleaned(client, monkeypatch):
    seen = {}

    class FakeModule:
        @staticmethod
        def execute(agent, args):
            seen["path"] = args["attachment_path"]
            assert agent["id"] == "muktamar-agent"
            return {
                "accepted": True,
                "reason_code": "OK",
                "user_message": "accepted",
                "file_fingerprint": "secret",
                "checks": {"face_visible": True},
            }

    monkeypatch.setattr(routes, "_config", lambda: {"AGENT_ID": "muktamar-agent", "MAX_UPLOAD_BYTES": 100})
    monkeypatch.setattr(routes.db, "get_agent", lambda agent_id: {"id": agent_id, "enabled": 1}, raising=False)
    monkeypatch.setattr(routes.tool_registry, "_load_tool_module", lambda *args, **kwargs: FakeModule)

    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer test-key"},
        data={"draft_id": "1", "photo": (io.BytesIO(b"image-bytes"), "photo.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body == {
        "accepted": True,
        "reason_code": "OK",
        "user_message": "accepted",
        "checks": {"face_visible": True},
    }
    import os
    assert not os.path.exists(seen["path"])


def test_upload_limit(client, monkeypatch):
    monkeypatch.setattr(routes, "_config", lambda: {"MAX_UPLOAD_BYTES": 4})
    response = client.post(
        "/plugin/muktamar-api/v1/photo/validate",
        headers={"Authorization": "Bearer test-key"},
        data={"draft_id": "1", "photo": (io.BytesIO(b"12345"), "photo.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 413
