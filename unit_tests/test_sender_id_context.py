"""Regression tests for trusted channel sender identity context."""

import unittest
from unittest.mock import patch

from backend.agent_runtime import context
from models.chat import is_human_facing_external_user_id


class SenderIdContextTests(unittest.TestCase):
    def test_sender_id_is_rendered_with_display_name(self):
        with patch.object(context.db, 'get_user_display_name', return_value='Robin'):
            result = context.build_user_identity_context('telegram-main', 'sender-42')

        self.assertIn('## Current User', result)
        self.assertIn('**Robin**', result)
        self.assertIn('Channel sender ID: `sender-42`.', result)
        self.assertIn('This identity is provided by the chat channel', result)

    def test_web_session_without_channel_renders_sender_id_without_lookup(self):
        with patch.object(context.db, 'get_user_display_name') as lookup:
            result = context.build_user_identity_context(None, 'web_test')

        lookup.assert_not_called()
        self.assertIn('## Current User', result)
        self.assertIn('Channel sender ID: `web_test`.', result)
        self.assertNotIn('You are currently speaking with:', result)

    def test_sender_id_is_rendered_without_display_name(self):
        for display_name in (None, '', 'unknown'):
            with self.subTest(display_name=display_name):
                with patch.object(context.db, 'get_user_display_name', return_value=display_name):
                    result = context.build_user_identity_context('web', 'web_test')

                self.assertIn('## Current User', result)
                self.assertIn('Channel sender ID: `web_test`.', result)
                self.assertNotIn('You are currently speaking with:', result)

    def test_sender_id_is_rendered_when_display_name_lookup_fails(self):
        with patch.object(context.db, 'get_user_display_name', side_effect=RuntimeError('unavailable')):
            result = context.build_user_identity_context('web', 'web_test')

        self.assertIn('Channel sender ID: `web_test`.', result)

    def test_missing_sender_does_not_render_context(self):
        self.assertIsNone(context.build_user_identity_context(None, ''))

    def test_internal_identities_are_not_human_facing(self):
        for external_user_id in ('__agent__helper', '__scheduler__', '__system__notice'):
            with self.subTest(external_user_id=external_user_id):
                self.assertFalse(is_human_facing_external_user_id(external_user_id))
                self.assertIsNone(context.build_user_identity_context('web', external_user_id))

    def test_runtime_and_prefetch_use_shared_identity_renderer_with_deduplication(self):
        with open('backend/agent_runtime/runtime.py') as source_file:
            runtime_source = source_file.read()
        with open('backend/agent_runtime/prefetch.py') as source_file:
            prefetch_source = source_file.read()

        for source in (runtime_source, prefetch_source):
            self.assertIn('_ctx.build_user_identity_context(', source)
            self.assertIn('"## Current User" in (m.get("content") or "")', source)


if __name__ == '__main__':
    unittest.main()
