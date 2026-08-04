"""Focused coverage for the evaluation-model setting."""

from unittest.mock import Mock, patch

import pytest

from models.db import db


@pytest.fixture
def client():
    from app import app

    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


def _create_model(model_id="judge-model"):
    db.create_model({
        "id": model_id,
        "name": "Judge Model",
        "type": "local",
        "provider": "llama.cpp",
        "model_name": "judge",
        "enabled": 1,
    })
    return model_id


def test_evaluation_model_setting_persists_and_clears(client):
    model_id = _create_model()

    response = client.put("/api/eval-settings", json={
        "key": "eval_model_id", "value": model_id,
    })
    assert response.status_code == 200
    assert response.get_json() == {"success": True, "value": model_id}
    assert client.get("/api/eval-settings").get_json()["eval_model_id"] == model_id

    response = client.put("/api/eval-settings", json={
        "key": "eval_model_id", "value": "",
    })
    assert response.status_code == 200
    assert db.get_setting("eval_model_id", "missing") == ""


def test_evaluation_model_setting_rejects_unknown_model(client):
    response = client.put("/api/eval-settings", json={
        "key": "eval_model_id", "value": "unknown-model",
    })
    assert response.status_code == 400
    assert response.get_json()["error"] == "Model not found"


def test_custom_evaluator_uses_client_override():
    from evaluator.custom_evaluator import CustomEvaluator

    judge_client = Mock()
    judge_client.chat_completion.return_value = {"content": '{"score": 5}'}
    judge_client.extract_content.return_value = '{"score": 5}'
    evaluator = CustomEvaluator({
        "id": "judge",
        "eval_prompt": "Score {response}",
    }, llm_client_override=judge_client)

    result = evaluator.evaluate("answer", expected=None)

    assert result.score == 1.0
    judge_client.chat_completion.assert_called_once()


def test_engine_builds_configured_evaluation_client():
    from evaluator import engine

    model_config = {"id": "judge-model", "model_name": "judge"}
    with patch.object(engine.db, "get_setting", return_value="judge-model"), \
         patch.object(engine.db, "get_model_by_id", return_value=model_config), \
         patch("evaluator.llm_client.LLMClient") as client_class:
        client = engine._build_evaluation_llm_client()

    client_class.assert_called_once_with(model_config)
    assert client is client_class.return_value
