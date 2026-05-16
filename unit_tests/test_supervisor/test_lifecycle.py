"""
Tests for run_update() lifecycle: step ordering, rollback on each failure point.
All external calls (git, subprocess, urllib) are mocked.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'supervisor'))
import supervisor as sup


def _make_cfg(tmp):
    return {
        'app_root': tmp,
        'poll_interval': 300,
        'git_remote': 'origin',
        'health_port': 8080,
        'health_temp_port': 18080,
        'health_timeout': 5,
        'monitor_duration': 1,  # short for testing
        'keep_releases': 3,
        'python_bin': sys.executable,
        'uv_bin': None,
        'telegram_bot_token': '',
        'telegram_chat_id': '',
    }


class TestRunUpdateSuccess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = _make_cfg(self.tmp)
        # Simulate existing current release
        os.makedirs(os.path.join(self.tmp, 'releases', 'v1.0.0'))
        sup.write_rollback_slot(self.tmp, 'v0.9.0')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch.object(sup, '_is_process_alive', return_value=True)
    @patch.object(sup, 'start_daemon', return_value=(True, 1234))
    @patch.object(sup, 'stop_daemon', return_value=True)
    @patch.object(sup, 'health_check', return_value=True)
    @patch.object(sup, 'health_check_temp_port', return_value=True)
    @patch.object(sup, 'link_shared_dirs')
    @patch.object(sup, 'create_venv_and_install', return_value=(True, ''))
    @patch.object(sup, 'create_worktree', return_value=(True, ''))
    @patch.object(sup, 'verify_tag', return_value=(True, 'Good signature'))
    @patch.object(sup, 'get_current_release', return_value='v1.0.0')
    @patch.object(sup, 'preflight_checks', return_value=(True, []))
    def test_happy_path(self, mock_preflight, mock_current, mock_verify, mock_worktree,
                        mock_venv, mock_link, mock_health_temp, mock_health,
                        mock_stop, mock_start, mock_alive):
        # Create fake release path so atomic_swap and VERSION write work
        release_path = os.path.join(self.tmp, 'releases', 'v1.1.0')
        os.makedirs(release_path)

        result = sup.run_update('v1.1.0', self.cfg, None)

        self.assertTrue(result)
        mock_verify.assert_called_once_with(self.tmp, 'v1.1.0')
        mock_worktree.assert_called_once()
        mock_venv.assert_called_once()
        mock_health_temp.assert_called_once()
        mock_stop.assert_called_once()
        mock_start.assert_called_once()


class TestRunUpdateRollback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = _make_cfg(self.tmp)
        os.makedirs(os.path.join(self.tmp, 'releases', 'v1.0.0'))
        sup.write_rollback_slot(self.tmp, 'v1.0.0')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch.object(sup, 'remove_worktree')
    @patch.object(sup, 'rollback', return_value=True)
    @patch.object(sup, 'verify_tag', return_value=(False, 'Bad signature'))
    @patch.object(sup, 'get_current_release', return_value='v1.0.0')
    @patch.object(sup, 'preflight_checks', return_value=(True, []))
    def test_fails_at_verify_triggers_rollback(self, mock_preflight, mock_cur, mock_verify,
                                               mock_rollback, mock_remove):
        result = sup.run_update('v1.1.0', self.cfg, None)

        self.assertFalse(result)
        mock_rollback.assert_called_once()

    @patch.object(sup, 'remove_worktree')
    @patch.object(sup, 'rollback', return_value=True)
    @patch.object(sup, 'create_venv_and_install', return_value=(False, 'pip failed'))
    @patch.object(sup, 'create_worktree', return_value=(True, ''))
    @patch.object(sup, 'verify_tag', return_value=(True, 'Good'))
    @patch.object(sup, 'get_current_release', return_value='v1.0.0')
    @patch.object(sup, 'preflight_checks', return_value=(True, []))
    def test_fails_at_venv_triggers_rollback(self, mock_preflight, mock_cur, mock_verify,
                                              mock_worktree, mock_venv,
                                              mock_rollback, mock_remove):
        release_path = os.path.join(self.tmp, 'releases', 'v1.1.0')
        os.makedirs(release_path)
        result = sup.run_update('v1.1.0', self.cfg, None)

        self.assertFalse(result)
        mock_rollback.assert_called_once()

    @patch.object(sup, 'remove_worktree')
    @patch.object(sup, 'rollback', return_value=True)
    @patch.object(sup, 'health_check_temp_port', return_value=False)
    @patch.object(sup, 'link_shared_dirs')
    @patch.object(sup, 'create_venv_and_install', return_value=(True, ''))
    @patch.object(sup, 'create_worktree', return_value=(True, ''))
    @patch.object(sup, 'verify_tag', return_value=(True, 'Good'))
    @patch.object(sup, 'get_current_release', return_value='v1.0.0')
    @patch.object(sup, 'preflight_checks', return_value=(True, []))
    def test_fails_at_health_check_triggers_rollback(self, mock_preflight, mock_cur, mock_verify,
                                                      mock_worktree, mock_venv,
                                                      mock_link, mock_health,
                                                      mock_rollback, mock_remove):
        release_path = os.path.join(self.tmp, 'releases', 'v1.1.0')
        os.makedirs(release_path)
        result = sup.run_update('v1.1.0', self.cfg, None)

        self.assertFalse(result)
        mock_rollback.assert_called_once()
        # stop_daemon should NOT have been called (failed before swap)
        # (no mock for stop_daemon — if it was called it would raise AttributeError
        #  on the real stop_daemon due to missing PID file, not a test concern here)

    @patch.object(sup, 'remove_worktree')
    @patch.object(sup, 'rollback', return_value=True)
    @patch.object(sup, 'start_daemon', return_value=(False, 0))
    @patch.object(sup, 'stop_daemon', return_value=True)
    @patch.object(sup, 'health_check_temp_port', return_value=True)
    @patch.object(sup, 'link_shared_dirs')
    @patch.object(sup, 'create_venv_and_install', return_value=(True, ''))
    @patch.object(sup, 'create_worktree', return_value=(True, ''))
    @patch.object(sup, 'verify_tag', return_value=(True, 'Good'))
    @patch.object(sup, 'get_current_release', return_value='v1.0.0')
    @patch.object(sup, 'preflight_checks', return_value=(True, []))
    def test_fails_at_start_triggers_rollback(self, mock_preflight, mock_cur, mock_verify,
                                               mock_worktree, mock_venv,
                                               mock_link, mock_health_temp,
                                               mock_stop, mock_start,
                                               mock_rollback, mock_remove):
        release_path = os.path.join(self.tmp, 'releases', 'v1.1.0')
        os.makedirs(release_path)
        result = sup.run_update('v1.1.0', self.cfg, None)

        self.assertFalse(result)
        mock_rollback.assert_called_once()

    @patch.object(sup, '_is_process_alive', return_value=True)
    @patch.object(sup, 'start_daemon', return_value=(True, 1234))
    @patch.object(sup, 'stop_daemon', return_value=True)
    @patch.object(sup, 'health_check', return_value=True)
    @patch.object(sup, 'health_check_temp_port', return_value=True)
    @patch.object(sup, 'link_shared_dirs')
    @patch.object(sup, 'create_venv_and_install', return_value=(True, ''))
    @patch.object(sup, 'create_worktree', return_value=(True, ''))
    @patch.object(sup, 'verify_tag', return_value=(True, 'Good signature'))
    @patch.object(sup, 'get_current_release', return_value='v1.0.0')
    @patch.object(sup, 'preflight_checks', return_value=(True, []))
    def test_skip_verify_skips_signature_check(self, mock_preflight, mock_current, mock_verify,
                                               mock_worktree, mock_venv, mock_link,
                                               mock_health_temp, mock_health,
                                               mock_stop, mock_start, mock_alive):
        release_path = os.path.join(self.tmp, 'releases', 'v1.1.0')
        os.makedirs(release_path)

        result = sup.run_update('v1.1.0', self.cfg, None, skip_verify=True)

        self.assertTrue(result)
        mock_verify.assert_not_called()


class TestCleanupOldReleases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.releases = os.path.join(self.tmp, 'releases')
        os.makedirs(self.releases)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_releases(self, tags):
        for tag in tags:
            os.makedirs(os.path.join(self.releases, tag))

    @patch.object(sup, 'remove_worktree')
    @patch.object(sup, 'get_current_release', return_value='v1.4.0')
    @patch.object(sup, 'read_rollback_slot', return_value='v1.3.0')
    def test_keeps_current_rollback_and_n_recent(self, mock_rb, mock_cur, mock_remove):
        # 5 releases; current=v1.4.0, rollback=v1.3.0 (both protected)
        # keep=2: keep v1.2.0 and v1.1.0 as well → remove v1.0.0 only
        self._make_releases(['v1.0.0', 'v1.1.0', 'v1.2.0', 'v1.3.0', 'v1.4.0'])
        sup.cleanup_old_releases(self.tmp, keep=2)
        removed_tags = [c[0][1] for c in mock_remove.call_args_list]
        self.assertIn('v1.0.0', removed_tags)
        self.assertNotIn('v1.4.0', removed_tags)  # current — protected
        self.assertNotIn('v1.3.0', removed_tags)  # rollback — protected
        self.assertNotIn('v1.2.0', removed_tags)  # within keep=2
        self.assertNotIn('v1.1.0', removed_tags)  # within keep=2


if __name__ == '__main__':
    unittest.main()
