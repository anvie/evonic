"""Regression coverage: kanban scanner schedule registration must be safe.

Any process that constructs a PluginManager (for example the read-only
`evonic plugin list` CLI) imports this plugin's handler. A module-level
cancel+recreate of the scanner schedules used to delete the rows the live
server was tracking: its APScheduler kept firing the jobs for the deleted
ids, every call no-op'd, and the auto-trigger stayed dead until the next
restart. These tests lock in the safe contract:

* rows are never cancelled/deleted;
* a row with the right interval is reused as-is (same id, run_count kept);
* nothing is mutated while the host scheduler is not running.
"""

import re
from unittest.mock import MagicMock, patch

import pytest
from apscheduler.schedulers.base import STATE_RUNNING, STATE_STOPPED

from plugins.kanban import handler


class _FakeAPScheduler:
    """Minimal stand-in for the inner apscheduler instance."""

    def __init__(self, jobs, running):
        self._jobs = dict(jobs)
        self.state = STATE_RUNNING if running else STATE_STOPPED

    def get_job(self, schedule_id):
        return self._jobs.get(schedule_id)


class _FakeScheduler:
    """Records every mutating call so tests can assert nothing happened."""

    def __init__(self, schedules=(), running=True, jobs=None):
        self._started = running
        self._schedules = [dict(s) for s in schedules]
        self.calls = []
        self._scheduler = _FakeAPScheduler(jobs or {}, running)

    def list_schedules(self, owner_type=None, owner_id=None):
        return [dict(s) for s in self._schedules]

    def create_schedule(self, **kwargs):
        self.calls.append(('create', kwargs))
        schedule = {'id': 'new-id', 'enabled': 1, **kwargs}
        self._schedules.append(schedule)
        return schedule

    def update_schedule(self, schedule_id, **kwargs):
        self.calls.append(('update', schedule_id, kwargs))
        return {}

    def toggle_schedule(self, schedule_id):
        self.calls.append(('toggle', schedule_id))
        return {}


class _FakeSDK:
    def __init__(self):
        self.messages = []

    def log(self, message, level='info'):
        self.messages.append((level, message))


SCAN_ROW = {
    'id': '1cc37260',
    'name': 'kanban_scan',
    'enabled': 1,
    'trigger_config': {'seconds': 300},
}


@pytest.fixture(autouse=True)
def no_global_manager_access(monkeypatch):
    """Keep _log(sdk=None) from lazily constructing the global PluginManager.

    That import path would load every plugin inside the test process and its
    kanban on_enable hook would then register schedules against the fake.
    """
    real_log = handler._log

    def safe_log(message, level='info', sdk=None):
        if sdk is None:
            return
        return real_log(message, level, sdk)

    monkeypatch.setattr(handler, '_log', safe_log)
    yield


@pytest.fixture(autouse=True)
def reset_setup_guard(monkeypatch):
    monkeypatch.setattr(handler, '_scheduler_setup_done', False)
    monkeypatch.setattr(handler, '_scanner_schedule_id', None)
    monkeypatch.setattr(handler, '_stale_scanner_schedule_id', None)
    yield


def test_not_running_leaves_existing_row_untouched():
    """A short-lived process must not touch rows the live server owns."""
    fake = _FakeScheduler([SCAN_ROW], running=False)
    with patch('backend.scheduler.scheduler', fake, create=True):
        result = handler._ensure_schedule('kanban_scan', 300, 'kanban_scan')
    assert result == '1cc37260'
    assert fake.calls == []


def test_matching_interval_reuses_row_without_writing():
    fake = _FakeScheduler([SCAN_ROW], running=True, jobs={'1cc37260': object()})
    sdk = _FakeSDK()
    with patch('backend.scheduler.scheduler', fake, create=True):
        result = handler._ensure_schedule('kanban_scan', 300, 'kanban_scan', sdk)
    assert result == '1cc37260'
    assert fake.calls == []
    assert handler._scheduler_is_running(fake) is True
    assert not [m for level, m in sdk.messages if level == 'error']


def test_interval_change_updates_in_place():
    fake = _FakeScheduler(
        [dict(SCAN_ROW, trigger_config={'seconds': 600})],
        running=True, jobs={'1cc37260': object()},
    )
    with patch('backend.scheduler.scheduler', fake, create=True):
        result = handler._ensure_schedule('kanban_scan', 300, 'kanban_scan')
    assert result == '1cc37260'
    assert fake.calls == [('update', '1cc37260', {'trigger_config': {'seconds': 300}})]


def test_missing_row_creates_schedule():
    fake = _FakeScheduler([], running=True)
    with patch('backend.scheduler.scheduler', fake, create=True):
        result = handler._ensure_schedule('kanban_scan', 300, 'kanban_scan')
    assert result == 'new-id'
    assert len(fake.calls) == 1
    _, kwargs = fake.calls[0]
    assert kwargs['name'] == 'kanban_scan'
    assert kwargs['trigger_config'] == {'seconds': 300}
    assert kwargs['action_config'] == {'event_name': 'kanban_scan', 'payload': {}}


def test_missing_job_in_running_scheduler_is_reported():
    fake = _FakeScheduler([SCAN_ROW], running=True, jobs={})
    sdk = _FakeSDK()
    with patch('backend.scheduler.scheduler', fake, create=True):
        handler._ensure_schedule('kanban_scan', 300, 'kanban_scan', sdk)
    errors = [m for level, m in sdk.messages if level == 'error']
    assert any('missing from the running scheduler' in m for m in errors)


def test_on_enable_registers_once_per_process():
    fake = _FakeScheduler([SCAN_ROW, dict(SCAN_ROW, id='5cfa4add',
                                         name='kanban_stale_scan',
                                         trigger_config={'seconds': 60})],
                          running=False)
    config = {'SCAN_INTERVAL_SECONDS': 300, 'STALE_SCAN_INTERVAL_SECONDS': 60}
    with patch('backend.scheduler.scheduler', fake, create=True), \
            patch.object(handler, '_load_config', return_value=config):
        handler.on_enable()
        handler.on_enable()
    assert fake.calls == []
    assert handler._scanner_schedule_id == '1cc37260'
    assert handler._stale_scanner_schedule_id == '5cfa4add'


def test_no_module_level_schedule_registration():
    """The module-level registration that caused the outage must stay gone.

    Checked against the source instead of reloading the module, so the test has
    no import side effects of its own.
    """
    with open(handler.__file__, encoding='utf-8') as fh:
        source = fh.read()
    assert not re.search(r'^_setup_scheduler\(\)', source, re.M)
    assert not re.search(r'^_setup_stale_scheduler\(\)', source, re.M)
    assert re.search(r'^def on_enable\(', source, re.M)


def test_interval_drift_without_live_scheduler_only_persists_config():
    """A process without a running scheduler may record an interval change, but
    must not touch APScheduler jobs or next_run_at (nothing live to drive)."""
    fake = _FakeScheduler([dict(SCAN_ROW, trigger_config={'seconds': 600})],
                          running=False, jobs={'1cc37260': object()})
    db = MagicMock()
    with patch('backend.scheduler.scheduler', fake, create=True), \
            patch.dict('sys.modules', {'models.db': MagicMock(db=db)}):
        result = handler._ensure_schedule('kanban_scan', 300, 'kanban_scan')
    assert result == '1cc37260'
    assert fake.calls == []
    db.update_schedule.assert_called_once_with(
        '1cc37260', trigger_config={'seconds': 300}
    )
