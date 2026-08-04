"""
Unit tests for the global scheduler system.

Covers:
- DB CRUD (models/db.py schedules table)
- Scheduler engine (backend/scheduler.py)
- Action executors (emit_event, agent_message, webhook)
- REST API routes (routes/scheduler.py)
- Agent skill tool backends (skills/scheduler/backend/tools/)
"""

import pytest
import sys
import os
import uuid
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.db import db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_schedule_kwargs(**overrides):
    """Return a valid set of kwargs for db.create_schedule()."""
    defaults = dict(
        schedule_id=str(uuid.uuid4()),
        name='Test Schedule',
        owner_type='agent',
        owner_id='agent-1',
        trigger_type='interval',
        trigger_config={'minutes': 5},
        action_type='emit_event',
        action_config={'event_name': 'test_event', 'payload': {}},
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# TestSchedulerDB — raw DB helpers
# ---------------------------------------------------------------------------

class TestSchedulerDB:

    def test_create_and_get_schedule(self):
        kwargs = _make_schedule_kwargs()
        db.create_schedule(**kwargs)
        fetched = db.get_schedule(kwargs['schedule_id'])
        assert fetched is not None
        assert fetched['id'] == kwargs['schedule_id']
        assert fetched['name'] == 'Test Schedule'
        assert fetched['enabled'] == 1
        assert fetched['run_count'] == 0

    def test_trigger_config_roundtrip_as_dict(self):
        """trigger_config stored as JSON but returned as dict."""
        kwargs = _make_schedule_kwargs(
            trigger_type='cron',
            trigger_config={'hour': 9, 'minute': 0, 'day_of_week': 'mon-fri'},
        )
        db.create_schedule(**kwargs)
        fetched = db.get_schedule(kwargs['schedule_id'])
        assert isinstance(fetched['trigger_config'], dict)
        assert fetched['trigger_config']['hour'] == 9

    def test_action_config_roundtrip_as_dict(self):
        """action_config stored as JSON but returned as dict."""
        kwargs = _make_schedule_kwargs(
            action_type='agent_message',
            action_config={'agent_id': 'agent-99', 'message': 'hello'},
        )
        db.create_schedule(**kwargs)
        fetched = db.get_schedule(kwargs['schedule_id'])
        assert isinstance(fetched['action_config'], dict)
        assert fetched['action_config']['agent_id'] == 'agent-99'

    def test_list_schedules_empty(self):
        result = db.get_schedules()
        assert result == []

    def test_list_schedules_filtered_by_owner(self):
        db.create_schedule(**_make_schedule_kwargs(owner_type='agent', owner_id='a1'))
        db.create_schedule(**_make_schedule_kwargs(owner_type='plugin', owner_id='p1'))
        db.create_schedule(**_make_schedule_kwargs(owner_type='agent', owner_id='a2'))

        agents = db.get_schedules(owner_type='agent')
        assert len(agents) == 2

        plugins = db.get_schedules(owner_type='plugin')
        assert len(plugins) == 1

        specific = db.get_schedules(owner_type='agent', owner_id='a1')
        assert len(specific) == 1
        assert specific[0]['owner_id'] == 'a1'

    def test_list_schedules_enabled_only(self):
        db.create_schedule(**_make_schedule_kwargs())
        disabled_id = str(uuid.uuid4())
        db.create_schedule(**_make_schedule_kwargs(schedule_id=disabled_id))
        db.update_schedule(disabled_id, enabled=0)

        enabled = db.get_schedules(enabled_only=True)
        assert len(enabled) == 1
        assert enabled[0]['enabled'] == 1

    def test_update_schedule(self):
        kwargs = _make_schedule_kwargs()
        db.create_schedule(**kwargs)
        db.update_schedule(kwargs['schedule_id'], run_count=3, last_run_at='2026-04-21T10:00:00')
        fetched = db.get_schedule(kwargs['schedule_id'])
        assert fetched['run_count'] == 3
        assert fetched['last_run_at'] == '2026-04-21T10:00:00'

    def test_update_schedule_trigger_config(self):
        kwargs = _make_schedule_kwargs()
        db.create_schedule(**kwargs)
        db.update_schedule(kwargs['schedule_id'], trigger_config={'hours': 2})
        fetched = db.get_schedule(kwargs['schedule_id'])
        assert fetched['trigger_config'] == {'hours': 2}

    def test_delete_schedule(self):
        kwargs = _make_schedule_kwargs()
        db.create_schedule(**kwargs)
        deleted = db.delete_schedule(kwargs['schedule_id'])
        assert deleted is True
        assert db.get_schedule(kwargs['schedule_id']) is None

    def test_delete_nonexistent_schedule(self):
        deleted = db.delete_schedule('nonexistent-id')
        assert deleted is False

    def test_max_runs_stored(self):
        kwargs = _make_schedule_kwargs(max_runs=3)
        db.create_schedule(**kwargs)
        fetched = db.get_schedule(kwargs['schedule_id'])
        assert fetched['max_runs'] == 3

    def test_max_runs_null_by_default(self):
        db.create_schedule(**_make_schedule_kwargs())
        fetched = db.get_schedules()[0]
        assert fetched['max_runs'] is None


# ---------------------------------------------------------------------------
# TestSchedulerEngine — Scheduler class logic (APScheduler mocked)
# ---------------------------------------------------------------------------

class TestSchedulerEngine:

    @pytest.fixture(autouse=True)
    def mock_apscheduler(self):
        """Replace the internal APScheduler instance with a mock."""
        from backend.scheduler import scheduler
        mock_sched = MagicMock()
        mock_sched.get_job.return_value = None
        original = scheduler._scheduler
        scheduler._scheduler = mock_sched
        scheduler._started = True
        yield mock_sched
        scheduler._scheduler = original
        scheduler._started = False

    def test_create_schedule_returns_schedule(self, mock_apscheduler):
        from backend.scheduler import scheduler
        result = scheduler.create_schedule(
            name='My Job', owner_type='agent', owner_id='agent-1',
            trigger_type='interval', trigger_config={'minutes': 10},
            action_type='emit_event', action_config={'event_name': 'ping', 'payload': {}},
        )
        assert result['id'] is not None
        assert result['name'] == 'My Job'
        assert result['trigger_type'] == 'interval'
        assert mock_apscheduler.add_job.called

    def test_create_date_schedule_defaults_max_runs_1(self, mock_apscheduler):
        from backend.scheduler import scheduler
        result = scheduler.create_schedule(
            name='One-shot', owner_type='agent', owner_id='agent-1',
            trigger_type='date', trigger_config={'run_date': '2026-04-21T09:00:00'},
            action_type='emit_event', action_config={'event_name': 'ping', 'payload': {}},
        )
        fetched = db.get_schedule(result['id'])
        assert fetched['max_runs'] == 1

    def test_create_schedule_persisted_to_db(self, mock_apscheduler):
        from backend.scheduler import scheduler
        result = scheduler.create_schedule(
            name='Persist Test', owner_type='plugin', owner_id='my-plugin',
            trigger_type='interval', trigger_config={'seconds': 60},
            action_type='emit_event', action_config={'event_name': 'tick', 'payload': {}},
        )
        fetched = db.get_schedule(result['id'])
        assert fetched is not None
        assert fetched['owner_id'] == 'my-plugin'

    def test_cancel_schedule_success(self, mock_apscheduler):
        from backend.scheduler import scheduler
        result = scheduler.create_schedule(
            name='To Cancel', owner_type='agent', owner_id='agent-1',
            trigger_type='interval', trigger_config={'minutes': 5},
            action_type='emit_event', action_config={'event_name': 'x', 'payload': {}},
        )
        ok = scheduler.cancel_schedule(result['id'])
        assert ok is True
        assert db.get_schedule(result['id']) is None

    def test_cancel_schedule_enforces_owner(self, mock_apscheduler):
        from backend.scheduler import scheduler
        result = scheduler.create_schedule(
            name='Owner Test', owner_type='agent', owner_id='agent-1',
            trigger_type='interval', trigger_config={'minutes': 5},
            action_type='emit_event', action_config={'event_name': 'x', 'payload': {}},
        )
        # Different owner cannot cancel
        ok = scheduler.cancel_schedule(result['id'], owner_id='agent-999')
        assert ok is False
        assert db.get_schedule(result['id']) is not None

        # Real owner can cancel
        ok = scheduler.cancel_schedule(result['id'], owner_id='agent-1')
        assert ok is True

    def test_cancel_nonexistent_schedule(self, mock_apscheduler):
        from backend.scheduler import scheduler
        ok = scheduler.cancel_schedule('does-not-exist')
        assert ok is False

    def test_list_schedules_by_owner(self, mock_apscheduler):
        from backend.scheduler import scheduler
        scheduler.create_schedule(
            name='A', owner_type='agent', owner_id='agent-A',
            trigger_type='interval', trigger_config={'minutes': 1},
            action_type='emit_event', action_config={'event_name': 'a', 'payload': {}},
        )
        scheduler.create_schedule(
            name='B', owner_type='agent', owner_id='agent-B',
            trigger_type='interval', trigger_config={'minutes': 2},
            action_type='emit_event', action_config={'event_name': 'b', 'payload': {}},
        )
        results = scheduler.list_schedules(owner_type='agent', owner_id='agent-A')
        assert len(results) == 1
        assert results[0]['name'] == 'A'

    def test_toggle_schedule_disables(self, mock_apscheduler):
        from backend.scheduler import scheduler
        result = scheduler.create_schedule(
            name='Toggle Me', owner_type='agent', owner_id='agent-1',
            trigger_type='interval', trigger_config={'minutes': 5},
            action_type='emit_event', action_config={'event_name': 'x', 'payload': {}},
        )
        toggled = scheduler.toggle_schedule(result['id'])
        assert toggled['enabled'] == 0
        # Toggle again should re-enable
        toggled2 = scheduler.toggle_schedule(result['id'])
        assert toggled2['enabled'] == 1

    def test_toggle_nonexistent_schedule(self, mock_apscheduler):
        from backend.scheduler import scheduler
        result = scheduler.toggle_schedule('nonexistent')
        assert result is None

    def test_run_now_returns_true_for_existing(self, mock_apscheduler):
        from backend.scheduler import scheduler
        with patch.object(scheduler, '_execute_action') as mock_exec:
            result = scheduler.create_schedule(
                name='Run Now Test', owner_type='agent', owner_id='agent-1',
                trigger_type='interval', trigger_config={'minutes': 5},
                action_type='emit_event', action_config={'event_name': 'x', 'payload': {}},
            )
            ok = scheduler.run_now(result['id'])
            assert ok is True
            mock_exec.assert_called_once_with(result['id'])

    def test_run_now_returns_false_for_missing(self, mock_apscheduler):
        from backend.scheduler import scheduler
        ok = scheduler.run_now('nonexistent')
        assert ok is False


# ---------------------------------------------------------------------------
# TestKbOrganizerSchedule - global Vault Janitor schedule configuration
# ---------------------------------------------------------------------------

class TestKbOrganizerSchedule:

    def test_uses_configured_nightly_time(self, monkeypatch):
        from backend.scheduler import Scheduler

        monkeypatch.setenv('EVOMEM_KB_ORGANIZER_NIGHTLY_TIME', '14:25')
        scheduler = Scheduler()

        assert (scheduler._kb_organizer_hour, scheduler._kb_organizer_minute) == (14, 25)

    def test_defaults_nightly_time_to_3_am(self, monkeypatch):
        from backend.scheduler import Scheduler

        monkeypatch.delenv('EVOMEM_KB_ORGANIZER_NIGHTLY_TIME', raising=False)
        scheduler = Scheduler()

        assert (scheduler._kb_organizer_hour, scheduler._kb_organizer_minute) == (3, 0)

    def test_invalid_nightly_time_falls_back_to_3_am(self, monkeypatch):
        from backend.scheduler import Scheduler

        monkeypatch.setenv('EVOMEM_KB_ORGANIZER_NIGHTLY_TIME', 'tomorrow')
        scheduler = Scheduler()

        assert (scheduler._kb_organizer_hour, scheduler._kb_organizer_minute) == (3, 0)

    def test_registers_janitor_at_configured_time(self, monkeypatch):
        from backend.scheduler import Scheduler

        monkeypatch.setenv('EVOMEM_KB_ORGANIZER_NIGHTLY_TIME', '14:25')
        scheduler = Scheduler()
        scheduler._scheduler = MagicMock()
        scheduler._load_from_db = MagicMock()

        scheduler.start()

        sefton_call = next(
            call for call in scheduler._scheduler.add_job.call_args_list
            if call.kwargs['id'] == 'builtin:sefton_tidy'
        )
        assert "hour='14'" in str(sefton_call.args[1])
        assert "minute='25'" in str(sefton_call.args[1])


# ---------------------------------------------------------------------------
# TestSchedulerActions — action executor methods
# ---------------------------------------------------------------------------

class TestSchedulerActions:

    @pytest.fixture(autouse=True)
    def mock_apscheduler(self):
        from backend.scheduler import scheduler
        mock_sched = MagicMock()
        mock_sched.get_job.return_value = None
        original = scheduler._scheduler
        scheduler._scheduler = mock_sched
        scheduler._started = True
        yield mock_sched
        scheduler._scheduler = original
        scheduler._started = False

    def _create_schedule(self, action_type, action_config, trigger_type='interval',
                         trigger_config=None):
        from backend.scheduler import scheduler
        return scheduler.create_schedule(
            name='Action Test',
            owner_type='agent', owner_id='agent-1',
            trigger_type=trigger_type,
            trigger_config=trigger_config or {'minutes': 5},
            action_type=action_type,
            action_config=action_config,
        )

    def test_action_emit_event(self):
        from backend.scheduler import scheduler
        s = self._create_schedule('emit_event',
                                   {'event_name': 'my_event', 'payload': {'key': 'val'}})
        with patch('backend.event_stream.event_stream') as mock_es:
            scheduler._action_emit_event(s['action_config'])
            mock_es.emit.assert_called_once_with('my_event', {'key': 'val'})

    def test_action_emit_event_default_payload(self):
        from backend.scheduler import scheduler
        s = self._create_schedule('emit_event', {'event_name': 'ping'})
        with patch('backend.event_stream.event_stream') as mock_es:
            scheduler._action_emit_event(s['action_config'])
            mock_es.emit.assert_called_once_with('ping', {})

    def test_action_agent_message(self):
        from backend.scheduler import scheduler
        s = self._create_schedule('agent_message',
                                   {'agent_id': 'agent-42', 'message': 'Hello!'})
        with patch('backend.agent_runtime.agent_runtime') as mock_rt:
            scheduler._action_static_message(s['action_config'])
            mock_rt.handle_message.assert_called_once_with(
                agent_id='agent-42',
                external_user_id='__scheduler__',
                message='Hello!',
                channel_id=None,
            )

    def test_action_webhook_post(self):
        from backend.scheduler import scheduler
        config = {'method': 'POST', 'url': 'https://example.com/hook',
                  'headers': {'X-Token': 'abc'}, 'body': {'key': 'val'}}
        with patch('backend.scheduler.http_lib') as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_req.request.return_value = mock_resp
            scheduler._action_webhook(config)
            mock_req.request.assert_called_once_with(
                'POST', 'https://example.com/hook',
                headers={'X-Token': 'abc'}, json={'key': 'val'}, timeout=30,
            )

    def test_execute_action_increments_run_count(self):
        from backend.scheduler import scheduler
        s = self._create_schedule('emit_event',
                                   {'event_name': 'tick', 'payload': {}})
        with patch('backend.event_stream.event_stream'):
            scheduler._execute_action(s['id'])
        fetched = db.get_schedule(s['id'])
        assert fetched['run_count'] == 1
        assert fetched['last_run_at'] is not None

    def test_execute_action_auto_disables_at_max_runs(self):
        from backend.scheduler import scheduler
        # max_runs=2
        result = scheduler.create_schedule(
            name='Limited', owner_type='agent', owner_id='agent-1',
            trigger_type='interval', trigger_config={'seconds': 30},
            action_type='emit_event', action_config={'event_name': 'x', 'payload': {}},
            max_runs=2,
        )
        with patch('backend.event_stream.event_stream'):
            scheduler._execute_action(result['id'])
            scheduler._execute_action(result['id'])
        fetched = db.get_schedule(result['id'])
        assert fetched['enabled'] == 0
        assert fetched['run_count'] == 2

    def test_execute_action_skips_disabled(self):
        from backend.scheduler import scheduler
        s = self._create_schedule('emit_event', {'event_name': 'x', 'payload': {}})
        db.update_schedule(s['id'], enabled=0)
        with patch('backend.event_stream.event_stream') as mock_es:
            scheduler._execute_action(s['id'])
            mock_es.emit.assert_not_called()

# ---------------------------------------------------------------------------
# TestSchedulerAPI — Flask REST API endpoints
# ---------------------------------------------------------------------------

class TestSchedulerAPI:

    @pytest.fixture(autouse=True)
    def bypass_auth(self):
        """Bypass Flask auth middleware so API routes return data, not 401/302."""
        from app import app
        original_funcs = app.before_request_funcs.get(None, []).copy()
        app.before_request_funcs[None] = [
            f for f in original_funcs if f.__name__ != 'enforce_auth'
        ]
        yield
        app.before_request_funcs[None] = original_funcs

    @pytest.fixture(autouse=True)
    def mock_apscheduler(self):
        from backend.scheduler import scheduler
        mock_sched = MagicMock()
        mock_sched.get_job.return_value = None
        original = scheduler._scheduler
        scheduler._scheduler = mock_sched
        scheduler._started = True
        yield mock_sched
        scheduler._scheduler = original
        scheduler._started = False

    def test_get_scheduler_page(self):
        from app import app
        with app.test_client() as client:
            resp = client.get('/scheduler')
            assert resp.status_code == 200
            assert b'Scheduler' in resp.data

    def test_api_list_schedules_empty(self):
        from app import app
        with app.test_client() as client:
            resp = client.get('/api/schedules')
            assert resp.status_code == 200
            data = resp.get_json()
            assert 'schedules' in data
            assert data['schedules'] == []

    def test_api_list_schedules_returns_existing(self):
        from app import app
        db.create_schedule(**_make_schedule_kwargs(name='Listed Schedule'))
        with app.test_client() as client:
            resp = client.get('/api/schedules')
            data = resp.get_json()
            assert len(data['schedules']) == 1
            assert data['schedules'][0]['name'] == 'Listed Schedule'

    def test_api_list_schedules_filter_by_owner_type(self):
        from app import app
        db.create_schedule(**_make_schedule_kwargs(owner_type='agent', owner_id='a1'))
        db.create_schedule(**_make_schedule_kwargs(owner_type='plugin', owner_id='p1'))
        with app.test_client() as client:
            resp = client.get('/api/schedules?owner_type=agent')
            data = resp.get_json()
            assert len(data['schedules']) == 1
            assert data['schedules'][0]['owner_type'] == 'agent'

    def test_api_create_schedule(self):
        from app import app
        with app.test_client() as client:
            payload = {
                'name': 'API Created',
                'trigger_type': 'interval',
                'trigger_config': {'minutes': 15},
                'action_type': 'emit_event',
                'action_config': {'event_name': 'api_test', 'payload': {}},
                'owner_type': 'user',
                'owner_id': 'admin',
            }
            resp = client.post('/api/schedules', json=payload)
            assert resp.status_code == 200
            data = resp.get_json()
            assert 'schedule' in data
            assert data['schedule']['name'] == 'API Created'

    def test_api_create_schedule_missing_fields(self):
        from app import app
        with app.test_client() as client:
            resp = client.post('/api/schedules', json={'name': 'Incomplete'})
            assert resp.status_code == 400
            data = resp.get_json()
            assert 'error' in data

    def test_api_get_schedule(self):
        from app import app
        kwargs = _make_schedule_kwargs(name='Get Me')
        db.create_schedule(**kwargs)
        with app.test_client() as client:
            resp = client.get(f'/api/schedules/{kwargs["schedule_id"]}')
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['schedule']['name'] == 'Get Me'

    def test_api_get_schedule_not_found(self):
        from app import app
        with app.test_client() as client:
            resp = client.get('/api/schedules/nonexistent')
            assert resp.status_code == 404

    def test_api_cancel_schedule(self):
        from app import app
        kwargs = _make_schedule_kwargs()
        db.create_schedule(**kwargs)
        with app.test_client() as client:
            resp = client.post(f'/api/schedules/{kwargs["schedule_id"]}/cancel')
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['success'] is True
        assert db.get_schedule(kwargs['schedule_id']) is None

    def test_api_cancel_nonexistent_schedule(self):
        from app import app
        with app.test_client() as client:
            resp = client.post('/api/schedules/nonexistent/cancel')
            assert resp.status_code == 404

    def test_api_toggle_schedule(self):
        from app import app
        kwargs = _make_schedule_kwargs()
        db.create_schedule(**kwargs)
        with app.test_client() as client:
            resp = client.post(f'/api/schedules/{kwargs["schedule_id"]}/toggle')
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['schedule']['enabled'] == 0  # was 1, now 0

    def test_api_run_now(self):
        from app import app
        from backend.scheduler import scheduler
        kwargs = _make_schedule_kwargs()
        db.create_schedule(**kwargs)
        with patch.object(scheduler, '_execute_action') as mock_exec:
            with app.test_client() as client:
                resp = client.post(f'/api/schedules/{kwargs["schedule_id"]}/run-now')
                assert resp.status_code == 200
                data = resp.get_json()
                assert data['success'] is True
            mock_exec.assert_called_once_with(kwargs['schedule_id'])

    def test_api_run_now_not_found(self):
        from app import app
        with app.test_client() as client:
            resp = client.post('/api/schedules/nonexistent/run-now')
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestSchedulerSkillTools — agent skill tool backends
# ---------------------------------------------------------------------------

class TestSchedulerSkillTools:

    @pytest.fixture(autouse=True)
    def mock_apscheduler(self):
        from backend.scheduler import scheduler
        mock_sched = MagicMock()
        mock_sched.get_job.return_value = None
        original = scheduler._scheduler
        scheduler._scheduler = mock_sched
        scheduler._started = True
        yield mock_sched
        scheduler._scheduler = original
        scheduler._started = False

    @pytest.fixture
    def agent(self):
        return {'id': 'skill-test-agent', 'name': 'Test Agent'}

    def test_create_schedule_tool(self, agent):
        from skills.scheduler.backend.tools.create_schedule import execute
        result = execute(agent, {
            'name': 'Skill Schedule',
            'trigger_type': 'interval',
            'trigger_config': {'minutes': 5},
            'action_type': 'emit_event',
            'action_config': {'event_name': 'skill_test', 'payload': {}},
        })
        assert result['status'] == 'success'
        assert 'schedule_id' in result
        assert result['name'] == 'Skill Schedule'

    def test_create_schedule_tool_defaults_agent_id(self, agent):
        """agent_message action without agent_id should default to calling agent."""
        from skills.scheduler.backend.tools.create_schedule import execute
        result = execute(agent, {
            'name': 'Reminder',
            'trigger_type': 'interval',
            'trigger_config': {'hours': 1},
            'action_type': 'agent_message',
            'action_config': {'message': 'Hello!'},  # no agent_id
        })
        assert result['status'] == 'success'
        fetched = db.get_schedule(result['schedule_id'])
        assert fetched['action_config']['agent_id'] == 'skill-test-agent'

    def test_create_schedule_tool_preserves_explicit_agent_id(self, agent):
        from skills.scheduler.backend.tools.create_schedule import execute
        result = execute(agent, {
            'name': 'Cross-Agent Reminder',
            'trigger_type': 'interval',
            'trigger_config': {'minutes': 30},
            'action_type': 'agent_message',
            'action_config': {'agent_id': 'other-agent', 'message': 'Hi!'},
        })
        fetched = db.get_schedule(result['schedule_id'])
        assert fetched['action_config']['agent_id'] == 'other-agent'

    def test_cancel_schedule_tool(self, agent):
        from skills.scheduler.backend.tools.create_schedule import execute as create
        from skills.scheduler.backend.tools.cancel_schedule import execute as cancel
        created = create(agent, {
            'name': 'To Cancel',
            'trigger_type': 'interval',
            'trigger_config': {'minutes': 5},
            'action_type': 'emit_event',
            'action_config': {'event_name': 'x', 'payload': {}},
        })
        result = cancel(agent, {'schedule_id': created['schedule_id']})
        assert result['status'] == 'success'
        assert db.get_schedule(created['schedule_id']) is None

    def test_cancel_schedule_tool_wrong_owner(self, agent):
        """Agent cannot cancel another agent's schedule."""
        from skills.scheduler.backend.tools.cancel_schedule import execute as cancel
        other_agent_id = 'other-agent'
        db.create_schedule(**_make_schedule_kwargs(owner_type='agent',
                                                    owner_id=other_agent_id))
        schedule_id = db.get_schedules(owner_id=other_agent_id)[0]['id']
        result = cancel(agent, {'schedule_id': schedule_id})
        assert result['status'] == 'error'

    def test_cancel_schedule_tool_no_id(self, agent):
        from skills.scheduler.backend.tools.cancel_schedule import execute as cancel
        result = cancel(agent, {})
        assert result['status'] == 'error'

    def test_list_schedules_tool_empty(self, agent):
        from skills.scheduler.backend.tools.list_schedules import execute
        result = execute(agent, {})
        assert result['status'] == 'success'
        assert result['count'] == 0
        assert result['schedules'] == []

    def test_list_schedules_tool_returns_own_schedules(self, agent):
        from skills.scheduler.backend.tools.create_schedule import execute as create
        from skills.scheduler.backend.tools.list_schedules import execute as lst
        create(agent, {
            'name': 'My Job',
            'trigger_type': 'interval',
            'trigger_config': {'minutes': 10},
            'action_type': 'emit_event',
            'action_config': {'event_name': 'x', 'payload': {}},
        })
        # Another agent's schedule - should not appear
        db.create_schedule(**_make_schedule_kwargs(owner_type='agent', owner_id='other'))

        result = lst(agent, {})
        assert result['status'] == 'success'
        assert result['count'] == 1
        assert result['schedules'][0]['name'] == 'My Job'

    def test_list_schedules_tool_fields(self, agent):
        from skills.scheduler.backend.tools.create_schedule import execute as create
        from skills.scheduler.backend.tools.list_schedules import execute as lst
        create(agent, {
            'name': 'Field Check',
            'trigger_type': 'interval',
            'trigger_config': {'minutes': 5},
            'action_type': 'emit_event',
            'action_config': {'event_name': 'x', 'payload': {}},
        })
        result = lst(agent, {})
        item = result['schedules'][0]
        expected_fields = {'schedule_id', 'name', 'trigger_type', 'trigger_config',
                           'action_type', 'enabled', 'next_run_at', 'last_run_at',
                           'run_count', 'max_runs'}
        assert expected_fields.issubset(item.keys())

    def test_list_schedules_tool_exclude_disabled_by_default(self, agent):
        from skills.scheduler.backend.tools.create_schedule import execute as create
        from skills.scheduler.backend.tools.list_schedules import execute as lst
        created = create(agent, {
            'name': 'Active', 'trigger_type': 'interval',
            'trigger_config': {'minutes': 5},
            'action_type': 'emit_event',
            'action_config': {'event_name': 'x', 'payload': {}},
        })
        db.create_schedule(**_make_schedule_kwargs(
            owner_type='agent', owner_id=agent['id'],
            name='Disabled',
        ))
        disabled_id = db.get_schedules(owner_id=agent['id'])[-1]['id']
        db.update_schedule(disabled_id, enabled=0)

        result = lst(agent, {'include_disabled': False})
        assert result['count'] == 1
        assert result['schedules'][0]['enabled'] == 1

        result_all = lst(agent, {'include_disabled': True})
        assert result_all['count'] == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
