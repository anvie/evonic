from pathlib import Path

from backend.slash_commands import command_registry, execute_command


ROOT = Path(__file__).resolve().parents[1]


def test_command_metadata_distinguishes_no_arg_and_parameterized_commands():
    cwd = command_registry.get("cwd").to_dict()
    model = command_registry.get("model").to_dict()

    assert cwd["accepts_args"] is False
    assert cwd["parameters"] == []
    assert model["accepts_args"] is True
    assert [item["name"] for item in model["parameters"]] == ["action", "model"]
    assert model["parameters"][0]["options"] == ["current", "list", "set"]


def test_model_without_arguments_reports_current_model(monkeypatch):
    from models.db import db

    monkeypatch.setattr(db, "get_agent_model", lambda _agent_id: {
        "id": "provider/model-id",
        "name": "Primary Model",
        "model_name": "model-id",
        "shortcode": 7,
    })

    result = execute_command("model", "", "session", "agent", "user")

    assert result == (
        "**Current model:** Primary Model (model-id) [#7]\n\n"
        "Type /model list to see all available models. /model <number> to switch."
    )


def test_model_list_is_explicit(monkeypatch):
    from models.db import db

    monkeypatch.setattr(db, "get_agent_model", lambda _agent_id: None)
    monkeypatch.setattr(db, "get_providers", lambda: [])
    monkeypatch.setattr(db, "get_enabled_llm_models", lambda: [])

    result = execute_command("model", "list", "session", "agent", "user")

    assert result == "No models configured. Add models in Settings > Models."


def test_frontend_executes_only_no_arg_suggestions_and_renders_parameter_hint():
    source = (ROOT / "templates/agent_detail.html").read_text(encoding="utf-8")

    assert "if (executeNoArgs && !command.accepts_args) sendChat();" in source
    assert "selectChatCommand(choices[chatCommandIndex < 0 ? 0 : chatCommandIndex], true);" in source
    assert "renderChatCommandHint(commandInputContext(value));" in source
    assert "parameter.options.join(' | ')" in source
    assert "parameter.required ? `<${parameter.placeholder || parameter.name}>`" in source
