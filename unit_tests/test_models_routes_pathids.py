"""Tests that /api/models routes accept slashed 'provider/model_name' ids
(<path:model_id> converter) and canonicalize legacy ids."""

import pytest

from models.db import db


@pytest.fixture
def client():
    from app import app
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
    return c


def _create(client, provider="openrouter", model_name="anthropic/claude-3.5-sonnet",
            **extra):
    payload = {
        "name": extra.pop("name", model_name),
        "type": "remote",
        "provider": provider,
        "model_name": model_name,
    }
    payload.update(extra)
    resp = client.post("/api/models", json=payload)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["success"] is True
    return data["model_id"]


class TestSlashedIdRoutes:
    def test_create_returns_new_format_id(self, client):
        model_id = _create(client)
        assert model_id == "openrouter/anthropic/claude-3.5-sonnet"

    def test_create_and_clone_preserve_model_capabilities(self, client):
        model_id = _create(
            client,
            model_name="capable-model",
            api_format="anthropic",
            vision_supported=1,
            pdf_supported=1,
        )
        model = db.get_model_by_id(model_id)
        assert model["api_format"] == "anthropic"
        assert model["vision_supported"] == 1
        assert model["pdf_supported"] == 1

        clone_id = client.post(f"/api/models/{model_id}/clone").get_json()["model_id"]
        clone = db.get_model_by_id(clone_id)
        assert clone["vision_supported"] == 1
        assert clone["pdf_supported"] == 1

    def test_get_with_multi_segment_id(self, client):
        model_id = _create(client)
        resp = client.get(f"/api/models/{model_id}")
        assert resp.status_code == 200
        assert resp.get_json()["id"] == model_id

    def test_put_with_slashed_id(self, client):
        model_id = _create(client)
        resp = client.put(f"/api/models/{model_id}", json={"name": "Renamed"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert db.get_model_by_id(model_id)["name"] == "Renamed"

    def test_delete_with_slashed_id(self, client):
        model_id = _create(client)
        resp = client.delete(f"/api/models/{model_id}")
        assert resp.status_code == 200
        assert db.get_model_by_id(model_id) is None

    def test_clone_with_slashed_id_gets_suffix(self, client):
        model_id = _create(client)
        resp = client.post(f"/api/models/{model_id}/clone")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["model_id"] == f"{model_id}-2"
        clone = db.get_model_by_id(data["model_id"])
        assert clone["name"].startswith("Copy of")
        assert clone["is_default"] == 0

    def test_set_default_with_slashed_id(self, client):
        model_id = _create(client)
        resp = client.post(f"/api/models/{model_id}/set-default")
        assert resp.status_code == 200
        assert db.get_default_model()["id"] == model_id

    def test_encoded_slash_also_resolves(self, client):
        model_id = _create(client, provider="llama.cpp", model_name="Gemma4-12B")
        resp = client.get("/api/models/llama.cpp%2FGemma4-12B")
        assert resp.status_code == 200
        assert resp.get_json()["id"] == model_id

    def test_duplicate_explicit_id_returns_409(self, client):
        _create(client, id="my-id", model_name="m1")
        payload = {
            "name": "dup", "type": "remote", "provider": "openrouter",
            "model_name": "m2", "id": "my-id",
        }
        resp = client.post("/api/models", json=payload)
        assert resp.status_code == 409
        assert resp.get_json()["success"] is False


class TestLegacyIdRoutes:
    def _seed_legacy(self):
        old_id = "0f16e2d1-1120-4450-a3d8-e16d67c31353"
        with db._connect() as conn:
            conn.execute(
                "INSERT INTO llm_models (id, name, type, provider, base_url, "
                "model_name, legacy_id) VALUES (?, 'L', 'local', 'llama.cpp', '', "
                "'Gemma4-12B', ?)",
                ("llama.cpp/Gemma4-12B", old_id),
            )
            conn.commit()
        return old_id, "llama.cpp/Gemma4-12B"

    def test_get_by_legacy_id(self, client):
        old_id, new_id = self._seed_legacy()
        resp = client.get(f"/api/models/{old_id}")
        assert resp.status_code == 200
        assert resp.get_json()["id"] == new_id

    def test_put_by_legacy_id_updates_canonical_row(self, client):
        old_id, new_id = self._seed_legacy()
        resp = client.put(f"/api/models/{old_id}", json={"name": "Via legacy"})
        assert resp.status_code == 200
        assert db.get_model_by_id(new_id)["name"] == "Via legacy"

    def test_set_default_by_legacy_id(self, client):
        old_id, new_id = self._seed_legacy()
        resp = client.post(f"/api/models/{old_id}/set-default")
        assert resp.status_code == 200
        assert db.get_default_model()["id"] == new_id
