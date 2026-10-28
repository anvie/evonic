"""A Claude Code (CLI) provider keeps api_format=claude_cli when created or edited through the API."""

import pytest


@pytest.fixture
def cli_provider():
    from models.db import db

    db.delete_provider("cli-fmt")
    yield db
    db.delete_provider("cli-fmt")


def _form_payload(**extra):
    # What the Add Provider form sends: claude_cli has no adapter of its own.
    return {"id": "cli-fmt", "name": "Claude Code", "type": "remote", "endpoint": "",
            "api_key": "", "api_format": "claude_cli",
            "adapter_key": "openai.chat_completions.v1", **extra}


def test_create_keeps_claude_cli_format(cli_provider):
    cli_provider.create_provider(_form_payload())
    assert cli_provider.get_provider("cli-fmt")["api_format"] == "claude_cli"


def test_edit_keeps_claude_cli_format(cli_provider):
    cli_provider.create_provider(_form_payload())
    cli_provider.update_provider("cli-fmt", {"name": "Renamed"})
    assert cli_provider.get_provider("cli-fmt")["api_format"] == "claude_cli"
    cli_provider.update_provider("cli-fmt", _form_payload(name="Again"))
    assert cli_provider.get_provider("cli-fmt")["api_format"] == "claude_cli"


def test_can_switch_away_from_claude_cli(cli_provider):
    cli_provider.create_provider(_form_payload())
    cli_provider.update_provider("cli-fmt", {"api_format": "openai", "adapter_key": "openai.chat_completions.v1"})
    assert cli_provider.get_provider("cli-fmt")["api_format"] == "openai"
