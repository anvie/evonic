import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from backend.agent_runtime.runtime import _apply_restart_origin_guard
from backend.tools import super_agent_tools
from models.db import db


class TestRestartGreetingSafety(unittest.TestCase):

    def setUp(self):
        self._db_path = db.db_path
        self._tls = db._tls
        self._temp_db = tempfile.NamedTemporaryFile(delete=False)
        self._temp_db.close()
        db.db_path = self._temp_db.name
        db._tls = threading.local()
        db._init_tables()

    def tearDown(self):
        db._tls = threading.local()
        db.db_path = self._db_path
        db._tls = self._tls
        os.unlink(self._temp_db.name)

    def test_consume_setting_is_one_shot(self):
        db.set_setting('restart_greeting_needed', '{"session_id":"s1"}')

        self.assertEqual(
            db.consume_setting('restart_greeting_needed'), '{"session_id":"s1"}',
        )
        self.assertEqual(db.consume_setting('restart_greeting_needed'), '')
        self.assertEqual(db.get_setting('restart_greeting_needed'), '')

    def test_restart_payload_contains_routing_only(self):
        stored = {}
        restart_service = mock.Mock()

        def set_setting(key, value):
            stored[key] = value

        with mock.patch.object(super_agent_tools.db, 'set_setting', side_effect=set_setting), \
                mock.patch.object(super_agent_tools.db, 'get_summary', side_effect=AssertionError(
                    'summary must not be replayed')), \
                mock.patch.object(super_agent_tools.db, 'get_session_messages', side_effect=AssertionError(
                    'messages must not be replayed')), \
                mock.patch('backend.restart.restart_service', restart_service):
            result = super_agent_tools._exec_restart({}, agent_context={
                'id': 'nawa',
                'channel_id': None,
                'user_id': 'robin',
                'session_id': 'session-1',
            })

        payload = json.loads(stored['restart_greeting_needed'])
        self.assertEqual(payload, {
            'channel_id': None,
            'external_user_id': 'robin',
            'session_id': 'session-1',
        })
        self.assertEqual(result, {'result': 'Restarting...'})
        restart_service.assert_called_once_with()

    def test_restart_executor_denies_restart_origin(self):
        restart_service = mock.Mock()
        with mock.patch('backend.restart.restart_service', restart_service):
            result = super_agent_tools._exec_restart({}, agent_context={
                'id': 'nawa',
                'restart_origin': True,
            })

        self.assertTrue(result['blocked'])
        self.assertIn('disabled', result['error'].lower())
        restart_service.assert_not_called()

    def test_restart_origin_guard_removes_restart_without_mutating_inputs(self):
        assigned = ['bash', 'restart', 'skill:admin:restart']
        tools = [
            {'type': 'function', 'function': {'name': 'bash'}},
            {'type': 'function', 'function': {'name': 'restart'}},
        ]
        context = {'assigned_tool_ids': assigned}

        guarded_ids, guarded_tools = _apply_restart_origin_guard(
            context, assigned, tools, {'restart_origin': True})

        self.assertEqual(guarded_ids, ['bash'])
        self.assertEqual([tool['function']['name'] for tool in guarded_tools], ['bash'])
        self.assertTrue(context['restart_origin'])
        self.assertEqual(context['assigned_tool_ids'], ['bash'])
        self.assertEqual(assigned, ['bash', 'restart', 'skill:admin:restart'])
        self.assertEqual(len(tools), 2)

    def test_restart_origin_guard_is_noop_for_normal_turn(self):
        assigned = ['restart']
        tools = [{'type': 'function', 'function': {'name': 'restart'}}]
        context = {'assigned_tool_ids': assigned}

        guarded_ids, guarded_tools = _apply_restart_origin_guard(context, assigned, tools, {})

        self.assertIs(guarded_ids, assigned)
        self.assertIs(guarded_tools, tools)
        self.assertNotIn('restart_origin', context)


if __name__ == '__main__':
    unittest.main()
