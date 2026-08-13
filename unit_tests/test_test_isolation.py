"""Regression checks for side-effect-free test imports."""

from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def test_agent_runtime_does_not_start_background_work_in_tests(monkeypatch):
    monkeypatch.setenv('EVONIC_TESTING', '1')
    from backend.agent_runtime.runtime import AgentRuntime

    with patch('backend.realtime_store.realtime_store.interrupt_stale_turns') as recover, \
         patch.object(AgentRuntime, '_register_signal_handlers') as signals, \
         patch('backend.agent_runtime.runtime.atexit.register') as register, \
         patch('backend.agent_runtime.runtime.TurnPrefetcher'):
        runtime = AgentRuntime()

    assert runtime._workers == []
    recover.assert_not_called()
    signals.assert_not_called()
    register.assert_not_called()


def test_app_startup_and_rate_limit_databases_are_test_isolated(tmp_path):
    app_source = (ROOT / 'app.py').read_text(encoding='utf-8')
    assert "_testing = _os.environ.get('EVONIC_TESTING') == '1'" in app_source
    assert 'and not _smoke_test and not _testing' in app_source

    import models.api_rate_limit as api_rate_limit
    import models.rate_limit as login_rate_limit

    assert Path(api_rate_limit._DB_PATH).parent == tmp_path
    assert Path(login_rate_limit._RATE_LIMIT_DB).parent == tmp_path
