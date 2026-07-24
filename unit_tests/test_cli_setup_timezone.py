"""CLI setup wizard timezone prompt regressions."""

from unittest.mock import MagicMock, patch


def _run_cli_setup(monkeypatch, inputs, non_interactive=False):
    from cli import commands
    from models.db import db

    monkeypatch.setattr(db, "has_super_agent", lambda: False)
    answers = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    run_setup = MagicMock(return_value={"success": True, "agent_id": "siwa_miwa"})
    with patch("backend.setup.run_setup", run_setup):
        commands.setup_command(non_interactive=non_interactive)
    return run_setup


def test_cli_setup_defaults_timezone(monkeypatch, capsys):
    run_setup = _run_cli_setup(
        monkeypatch,
        ["3", "", "", "", "", "", "", "n"],
    )
    assert run_setup.call_args.kwargs["timezone_name"] == "Asia/Jakarta"
    assert "Timezone    : Asia/Jakarta" in capsys.readouterr().out


def test_cli_setup_accepts_custom_timezone(monkeypatch):
    run_setup = _run_cli_setup(
        monkeypatch,
        ["3", "", "", "", "Europe/London", "", "", "n"],
    )
    assert run_setup.call_args.kwargs["timezone_name"] == "Europe/London"


def test_cli_setup_reprompts_invalid_timezone(monkeypatch, capsys):
    run_setup = _run_cli_setup(
        monkeypatch,
        ["3", "", "", "", "Jakarta", "Asia/Jakarta", "", "", "n"],
    )
    assert run_setup.call_args.kwargs["timezone_name"] == "Asia/Jakarta"
    assert "Invalid timezone" in capsys.readouterr().out


def test_cli_non_interactive_uses_default_timezone(monkeypatch):
    run_setup = _run_cli_setup(monkeypatch, [], non_interactive=True)
    assert run_setup.call_args.kwargs["timezone_name"] == "Asia/Jakarta"
