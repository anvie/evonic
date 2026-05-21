"""Tests for human-facing session identification (unreplied-chat scan, messaging)."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.chat import is_human_facing_external_user_id


class TestIsHumanFacingExternalUserId:
    def test_web_user_allowed(self):
        assert is_human_facing_external_user_id('user_123') is True

    def test_web_test_allowed(self):
        assert is_human_facing_external_user_id('web_test') is True

    def test_telegram_user_allowed(self):
        assert is_human_facing_external_user_id('76639539') is True

    def test_agent_to_agent_rejected(self):
        assert is_human_facing_external_user_id('__agent__parent_bot') is False

    def test_scheduler_rejected(self):
        assert is_human_facing_external_user_id('__scheduler__') is False

    def test_system_exact_rejected(self):
        assert is_human_facing_external_user_id('__system__') is False

    def test_system_prefixed_rejected(self):
        assert is_human_facing_external_user_id('__system__my_agent') is False

    def test_empty_rejected(self):
        assert is_human_facing_external_user_id('') is False
        assert is_human_facing_external_user_id('   ') is False

    def test_none_coerced_rejected(self):
        assert is_human_facing_external_user_id(None) is False
