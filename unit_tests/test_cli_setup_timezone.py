"""CLI setup wizard timezone prompt regressions."""

import re
from unittest.mock import MagicMock, patch


def _run_cli_setup(monkeypatch, inputs, non_interactive=False):
    from cli import commands
    from models.db import db

    monkeypatch.setattr(db, "has_super_agent", lambda: False)
    answers = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": "")
    run_setup = MagicMock(return_value={"success": True, "agent_id": "siwa_miwa"})
    with patch("backend.setup.run_setup", run_setup), \
            patch("backend.setup.ensure_admin_password", return_value=None):
        commands.setup_command(non_interactive=non_interactive)
    return run_setup


def test_cli_setup_defaults_timezone(monkeypatch, capsys):
    run_setup = _run_cli_setup(
        monkeypatch,
        ["3", "", "", "", "", "", "", "n"],
    )
    assert run_setup.call_args.kwargs["timezone_name"] == "Asia/Jakarta"
    # The summary card pads its label column; match the pair, not the spacing.
    assert re.search(r"Timezone\s+Asia/Jakarta", capsys.readouterr().out)


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


def test_cli_non_interactive_uses_default_super_agent_name(monkeypatch):
    run_setup = _run_cli_setup(monkeypatch, [], non_interactive=True)
    assert run_setup.call_args.kwargs["agent_name"] == "Super Agent"


def test_cli_blank_super_agent_name_uses_default(monkeypatch):
    run_setup = _run_cli_setup(
        monkeypatch,
        ["3", "", "", "", "", "", "", "n"],
    )
    assert run_setup.call_args.kwargs["agent_name"] == "Super Agent"


def test_cli_setup_generates_password_when_left_empty(monkeypatch, capsys):
    from cli import commands
    from models.db import db

    monkeypatch.setattr(db, "has_super_agent", lambda: False)
    answers = iter(["3", "", "", "", "", "", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": "")
    run_setup = MagicMock(return_value={"success": True, "agent_id": "siwa_miwa"})
    with patch("backend.setup.run_setup", run_setup), \
            patch("backend.setup.ensure_admin_password", return_value="gen-Pass_123"):
        commands.setup_command()

    out = capsys.readouterr().out
    assert run_setup.call_args.kwargs["password"] == ""
    assert re.search(r"Password\s+\(auto-generated\)", out)
    assert "gen-Pass_123" in out


def test_cli_setup_reprompts_short_password(monkeypatch, capsys):
    from cli import commands
    from models.db import db

    monkeypatch.setattr(db, "has_super_agent", lambda: False)
    answers = iter(["3", "", "", "", "", "", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    typed = iter(["abc", "longenough", "longenough"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": next(typed))
    run_setup = MagicMock(return_value={"success": True, "agent_id": "siwa_miwa"})
    with patch("backend.setup.run_setup", run_setup), \
            patch("backend.setup.ensure_admin_password", return_value=None):
        commands.setup_command()

    assert run_setup.call_args.kwargs["password"] == "longenough"
    assert "at least 6 characters" in capsys.readouterr().out
