"""Regression tests for the per-model ``reasoning_effort`` setting.

Covers persistence in ``models/mixins/models.py`` (NULL means "provider
default") and validation/normalisation in ``routes/models.py``. The
request-payload passthrough for OpenAI-compatible providers is covered by
``unit_tests/test_llm_client_payload_options.py``.

Note: updates are exposed as ``PUT /api/models/<id>``; ``routes/models.py``
registers no PATCH alias for that endpoint.
"""

import pytest

from models.db import db


def _model_data(**overrides):
    data = {
        "name": "Reasoner",
        "type": "llm",
        "provider": "openrouter",
        "model_name": "deepseek/deepseek-r1",
    }
    data.update(overrides)
    return data


def _create(**overrides):
    return db.create_model(_model_data(**overrides))


@pytest.fixture
def client():
    from app import app

    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
    return c


def _create_via_api(client, **extra):
    payload = _model_data()
    payload.update(extra)
    resp = client.post("/api/models", json=payload)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["success"] is True
    return data["model_id"]


class TestPersistence:
    def test_create_model_persists_reasoning_effort(self):
        model_id = _create(reasoning_effort="max")
        assert db.get_model_by_id(model_id)["reasoning_effort"] == "max"

    def test_create_model_without_reasoning_effort_stores_null(self):
        model_id = _create()
        assert db.get_model_by_id(model_id)["reasoning_effort"] is None

    def test_update_model_sets_and_clears_reasoning_effort(self):
        model_id = _create()

        assert db.update_model(model_id, {"reasoning_effort": "low"}) is True
        assert db.get_model_by_id(model_id)["reasoning_effort"] == "low"

        assert db.update_model(model_id, {"reasoning_effort": None}) is True
        assert db.get_model_by_id(model_id)["reasoning_effort"] is None

    def test_decision_model_discards_reasoning_effort(self):
        # Chat-only settings have no meaning on a decision model; the model
        # layer must null it out like thinking/thinking_budget.
        model_id = _create(type="decision", reasoning_effort="max")
        assert db.get_model_by_id(model_id)["reasoning_effort"] is None

    def test_switching_a_model_to_decision_drops_reasoning_effort(self):
        model_id = _create(reasoning_effort="high")
        assert db.update_model(model_id, {"type": "decision"}) is True
        assert db.get_model_by_id(model_id)["reasoning_effort"] is None


class TestApiValidation:
    def test_create_accepts_and_lowercases_reasoning_effort(self, client):
        model_id = _create_via_api(client, reasoning_effort="MAX")
        assert db.get_model_by_id(model_id)["reasoning_effort"] == "max"

    def test_create_rejects_unknown_reasoning_effort(self, client):
        before = len(db.get_llm_models())
        resp = client.post("/api/models", json=_model_data(reasoning_effort="ultra"))
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["success"] is False
        assert "reasoning_effort" in body["error"]
        assert len(db.get_llm_models()) == before

    def test_create_without_reasoning_effort_stores_null(self, client):
        model_id = _create_via_api(client)
        assert db.get_model_by_id(model_id)["reasoning_effort"] is None

    def test_update_lowercases_reasoning_effort(self, client):
        model_id = _create_via_api(client)
        resp = client.put(f"/api/models/{model_id}", json={"reasoning_effort": "MAX"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert db.get_model_by_id(model_id)["reasoning_effort"] == "max"

    def test_update_rejects_unknown_reasoning_effort(self, client):
        model_id = _create_via_api(client, reasoning_effort="high")
        resp = client.put(f"/api/models/{model_id}", json={"reasoning_effort": "ultra"})
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False
        # The rejected value must not overwrite the stored one.
        assert db.get_model_by_id(model_id)["reasoning_effort"] == "high"

    def test_update_blank_reasoning_effort_clears_to_null(self, client):
        model_id = _create_via_api(client, reasoning_effort="max")
        resp = client.put(f"/api/models/{model_id}", json={"reasoning_effort": ""})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert db.get_model_by_id(model_id)["reasoning_effort"] is None

    def test_update_null_reasoning_effort_clears_to_null(self, client):
        model_id = _create_via_api(client, reasoning_effort="max")
        resp = client.put(f"/api/models/{model_id}", json={"reasoning_effort": None})
        assert resp.status_code == 200
        assert db.get_model_by_id(model_id)["reasoning_effort"] is None

    def test_clone_copies_reasoning_effort(self, client):
        model_id = _create_via_api(client, reasoning_effort="medium")
        resp = client.post(f"/api/models/{model_id}/clone")
        assert resp.status_code == 200
        clone_id = resp.get_json()["model_id"]
        assert clone_id != model_id
        assert db.get_model_by_id(clone_id)["reasoning_effort"] == "medium"
