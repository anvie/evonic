"""Tests for report_to session resolution (agent messaging + subagent spawn)."""

import unittest
from unittest import mock

from backend.agent_report_to import (
    resolve_report_to_for_subagent_spawn,
    resolve_report_to_from_context,
)


class TestResolveReportToFromContext(unittest.TestCase):
    def test_human_user_id_unchanged(self):
        rid, ch, sid = resolve_report_to_from_context(
            {'user_id': 'telegram_99', 'channel_id': 'tg-1', 'session_id': 'sess_1'},
            'agent_a',
        )
        self.assertEqual(rid, 'telegram_99')
        self.assertEqual(ch, 'tg-1')
        self.assertEqual(sid, 'sess_1')

    def test_inter_agent_session_resolves_human(self):
        human = {'external_user_id': 'user_123', 'channel_id': 'ch1'}
        with mock.patch(
            'models.db.db.get_latest_human_session',
            return_value=human,
        ) as mock_lookup:
            rid, ch, sid = resolve_report_to_from_context(
                {'user_id': '__agent__agent_a', 'channel_id': '', 'session_id': 'inter_1'},
                'agent_a',
            )
        self.assertEqual(rid, 'user_123')
        self.assertEqual(ch, 'ch1')
        self.assertEqual(sid, '')  # inter-agent fallback returns empty session_id
        mock_lookup.assert_called_once_with('agent_a')

    def test_inter_agent_preserves_originating_whatsapp_session(self):
        """A chained delegation keeps its WhatsApp origin over a newer web session."""
        context = {
            'user_id': '__agent__agent_a',
            'session_id': 'agent-session',
            'origin_report_to_id': 'whatsapp_group_123',
            'origin_report_to_channel_id': 'whatsapp-1',
            'origin_session_id': 'amar-e17cfaa3',
        }
        with mock.patch('models.db.db.get_latest_human_session') as mock_lookup:
            rid, ch, sid = resolve_report_to_from_context(context, 'agent_a')
        self.assertEqual(rid, 'whatsapp_group_123')
        self.assertEqual(ch, 'whatsapp-1')
        self.assertEqual(sid, 'amar-e17cfaa3')
        mock_lookup.assert_not_called()

    def test_subagent_uses_parent_for_lookup(self):
        human = {'external_user_id': 'user_456', 'channel_id': None}
        with mock.patch(
            'models.db.db.get_latest_human_session',
            return_value=human,
        ) as mock_lookup:
            rid, ch, sid = resolve_report_to_from_context(
                {
                    'user_id': '__agent__sub_1',
                    'is_subagent': True,
                    'parent_id': 'parent_a',
                },
                'sub_1',
            )
        self.assertEqual(rid, 'user_456')
        self.assertEqual(ch, '')
        self.assertEqual(sid, '')  # inter-agent fallback returns empty session_id
        mock_lookup.assert_called_once_with('parent_a')


class TestResolveReportToForSubagentSpawn(unittest.TestCase):
    def test_spawner_human_session_used_directly(self):
        rid, ch, sid = resolve_report_to_for_subagent_spawn(
            'parent_a', 'human_user', 'web',
        )
        self.assertEqual(rid, 'human_user')
        self.assertEqual(ch, 'web')
        self.assertEqual(sid, '')

    def test_empty_spawner_user_id_looks_up_parent(self):
        human = {'external_user_id': 'user_789', 'channel_id': 'ch9'}
        with mock.patch(
            'models.db.db.get_latest_human_session',
            return_value=human,
        ) as mock_lookup:
            rid, ch, sid = resolve_report_to_for_subagent_spawn('parent_a', '', '')
        self.assertEqual(rid, 'user_789')
        self.assertEqual(ch, 'ch9')
        self.assertEqual(sid, '')
        mock_lookup.assert_called_once_with('parent_a')

    def test_inter_agent_spawner_preserves_routing(self):
        """Inter-agent spawner_user_id is returned as-is so sub-agent/explorer
        findings are forwarded back to the caller's current (inter-agent) session
        rather than resolved to the parent's human session."""
        human = {'external_user_id': 'user_abc', 'channel_id': ''}
        with mock.patch(
            'models.db.db.get_latest_human_session',
            return_value=human,
        ) as mock_lookup:
            rid, ch, sid = resolve_report_to_for_subagent_spawn(
                'parent_a', '__agent__parent_a', '',
            )
        self.assertEqual(rid, '__agent__parent_a')
        self.assertEqual(ch, '')
        self.assertEqual(sid, '')
        mock_lookup.assert_not_called()
