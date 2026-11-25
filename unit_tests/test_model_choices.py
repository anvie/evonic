"""GET /api/models/choices lists the enabled chat models for the shared model picker (ev-model-picker.js), with what it
shows of them and never a key or an address."""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _client():
    from app import app

    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


def test_choices_list_enabled_chat_models_without_secrets():
    from models.db import db

    db.create_provider({"id": "mc-prov", "name": "MC", "type": "remote",
                        "endpoint": "https://mc.example.invalid/v1", "api_key": "sk-mc-secret",
                        "adapter_key": "openai.chat_completions.v1"})
    db.create_model({"id": "mc-on", "name": "MC On", "type": "remote", "provider": "mc-prov",
                     "model_name": "mc-on", "enabled": True, "context_window": 32000})
    db.create_model({"id": "mc-off", "name": "MC Off", "type": "remote", "provider": "mc-prov",
                     "model_name": "mc-off", "enabled": False})

    models = {m["id"]: m for m in _client().get("/api/models/choices").get_json()["models"]}
    assert "mc-on" in models and "mc-off" not in models
    m = models["mc-on"]
    assert m["provider_name"] == "MC" and m["context_window"] == 32000 and m["is_default"] is False
    assert not any(k in m for k in ("api_key", "base_url"))


def test_the_picker_is_on_every_page_and_agent_state_uses_it():
    base = open(os.path.join(ROOT, "templates", "base.html"), encoding="utf-8").read()
    assert "js/ev-model-picker.js" in base and "js/ai-logos.js" in base
    state = open(os.path.join(ROOT, "static", "js", "agent-state.js"), encoding="utf-8").read()
    assert "evModelPicker.open(" in state
    picker = open(os.path.join(ROOT, "static", "js", "ev-model-picker.js"), encoding="utf-8").read()
    assert "window.evModelPicker" in picker and "/api/models/choices" in picker and "function evSearchBox" in picker
