"""Tests for update_manager._version_tuple and version comparison logic."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.update_manager as um
from backend.update_manager import _version_tuple


class TestVersionTuple(unittest.TestCase):

    # -- Standard semver -----------------------------------------------------

    def test_full_semver_with_v_prefix(self):
        self.assertEqual(_version_tuple('v0.2.5'), (0, 2, 5))

    def test_full_semver_without_v_prefix(self):
        self.assertEqual(_version_tuple('0.2.5'), (0, 2, 5))

    def test_major_minor_only(self):
        self.assertEqual(_version_tuple('v0.2'), (0, 2, 0))

    def test_major_only(self):
        self.assertEqual(_version_tuple('v1'), (1, 0, 0))

    # -- Pre-release / build metadata ----------------------------------------

    def test_prerelease_suffix(self):
        self.assertEqual(_version_tuple('v1.2.3-beta.1'), (1, 2, 3))

    def test_build_metadata_suffix(self):
        self.assertEqual(_version_tuple('v1.0.0+build.42'), (1, 0, 0))

    def test_prerelease_and_build(self):
        self.assertEqual(_version_tuple('v2.0.0-rc.1+build.5'), (2, 0, 0))

    # -- Unparseable / edge cases --------------------------------------------

    def test_none_returns_zero_tuple(self):
        self.assertEqual(_version_tuple(None), (0, 0, 0))

    def test_empty_string_returns_zero_tuple(self):
        self.assertEqual(_version_tuple(''), (0, 0, 0))

    def test_non_numeric_string_returns_zero_tuple(self):
        self.assertEqual(_version_tuple('main'), (0, 0, 0))

    def test_head_returns_zero_tuple(self):
        self.assertEqual(_version_tuple('HEAD'), (0, 0, 0))

    def test_branch_name_returns_zero_tuple(self):
        self.assertEqual(_version_tuple('dev-feature'), (0, 0, 0))

    # -- Comparison behaviour (the actual bug guard) -------------------------

    def test_newer_latest_is_greater(self):
        self.assertGreater(_version_tuple('v0.2.5'), _version_tuple('v0.2.0'))

    def test_older_latest_is_not_greater(self):
        # v0.2.0 must NOT be considered an upgrade over v0.2.5
        self.assertFalse(_version_tuple('v0.2.0') > _version_tuple('v0.2.5'))

    def test_same_version_is_not_greater(self):
        self.assertFalse(_version_tuple('v0.2.5') > _version_tuple('v0.2.5'))

    def test_major_version_bump(self):
        self.assertGreater(_version_tuple('v1.0.0'), _version_tuple('v0.9.9'))

    def test_minor_version_bump(self):
        self.assertGreater(_version_tuple('v0.3.0'), _version_tuple('v0.2.9'))

    def test_unparseable_never_triggers_update(self):
        # An unparseable latest tag should not be treated as newer than any real version
        self.assertFalse(_version_tuple('main') > _version_tuple('v0.1.0'))

    # -- Pre-release version security tests (FINDING-010) --------------------

    def test_prerelease_less_than_stable(self):
        """Pre-release versions should be less than their stable counterparts."""
        # This is the core security fix: v2.0.0-alpha should NOT be treated as >= v2.0.0
        self.assertLess(_version_tuple('v2.0.0-alpha'), _version_tuple('v2.0.0'))

    def test_prerelease_not_upgrade_from_stable(self):
        """Pre-release should never be considered an upgrade from stable."""
        # Prevents version downgrade attack: v2.0.0-alpha should not trigger upgrade from v1.0.0
        self.assertFalse(_version_tuple('v2.0.0-alpha') > _version_tuple('v2.0.0'))

    def test_rc_less_than_stable(self):
        """Release candidates should be less than stable releases."""
        self.assertLess(_version_tuple('v1.0.0-rc.1'), _version_tuple('v1.0.0'))

    def test_beta_less_than_rc(self):
        """Beta versions should be less than release candidates."""
        self.assertLess(_version_tuple('v1.0.0-beta'), _version_tuple('v1.0.0-rc.1'))

    def test_alpha_less_than_beta(self):
        """Alpha versions should be less than beta versions."""
        self.assertLess(_version_tuple('v1.0.0-alpha'), _version_tuple('v1.0.0-beta'))

    def test_dev_version_less_than_stable(self):
        """Development versions should be less than stable releases."""
        self.assertLess(_version_tuple('v1.0.0.dev1'), _version_tuple('v1.0.0'))

    def test_stable_upgrade_over_prerelease(self):
        """Stable version should be considered an upgrade over pre-release."""
        self.assertGreater(_version_tuple('v1.0.0'), _version_tuple('v1.0.0-rc.1'))

    def test_prerelease_ordering(self):
        """Pre-release versions should be ordered correctly."""
        # v1.0.0-alpha.1 < v1.0.0-alpha.2 < v1.0.0-beta < v1.0.0-rc.1 < v1.0.0
        self.assertLess(_version_tuple('v1.0.0-alpha.1'), _version_tuple('v1.0.0-alpha.2'))
        self.assertLess(_version_tuple('v1.0.0-alpha.2'), _version_tuple('v1.0.0-beta'))
        self.assertLess(_version_tuple('v1.0.0-beta'), _version_tuple('v1.0.0-rc.1'))
        self.assertLess(_version_tuple('v1.0.0-rc.1'), _version_tuple('v1.0.0'))


class TestApplyUpdate(unittest.TestCase):
    """apply_update must reinstall, repair, smoke-test, and roll back on failure."""

    def setUp(self):
        # git: fetch ok, rev-parse HEAD -> 'oldsha', reset ok.
        def fake_git(*args):
            if args[0] == 'rev-parse':
                return 0, 'oldsha', ''
            return 0, '', ''
        self._git = mock.patch.object(um, '_git_run', side_effect=fake_git)
        self.git = self._git.start()
        self.addCleanup(self._git.stop)
        # Avoid touching the on-disk state file during apply_update.
        self._rec = mock.patch.object(um, '_record_previous_commit')
        self._rec.start()
        self.addCleanup(self._rec.stop)
        # doctor --fix is best-effort; keep it green and side-effect free.
        self._doc = mock.patch.object(um, '_run_doctor_fix', return_value=(True, ''))
        self._doc.start()
        self.addCleanup(self._doc.stop)

    def test_success(self):
        with mock.patch.object(um, '_reinstall_deps', return_value=(True, '')), \
                mock.patch.object(um, '_smoke_test', return_value=(True, '')):
            result = um.apply_update('v1.2.3')
        self.assertEqual(result, {'success': True})

    def test_rolls_back_when_smoke_test_fails(self):
        with mock.patch.object(um, '_reinstall_deps', return_value=(True, '')), \
                mock.patch.object(um, '_smoke_test', return_value=(False, 'ImportError: boom')):
            result = um.apply_update('v1.2.3')
        self.assertIn('error', result)
        # Last git call must be the rollback to the recorded prior commit.
        self.git.assert_called_with('reset', '--hard', 'oldsha')

    def test_rolls_back_when_deps_fail(self):
        with mock.patch.object(um, '_reinstall_deps', return_value=(False, 'pip exploded')), \
                mock.patch.object(um, '_smoke_test') as smoke:
            result = um.apply_update('v1.2.3')
        self.assertIn('error', result)
        smoke.assert_not_called()  # never smoke-test if deps failed
        self.git.assert_called_with('reset', '--hard', 'oldsha')

    def test_records_previous_commit_before_reset(self):
        with mock.patch.object(um, '_reinstall_deps', return_value=(True, '')), \
                mock.patch.object(um, '_smoke_test', return_value=(True, '')):
            um.apply_update('v1.2.3')
        um._record_previous_commit.assert_called_once_with('oldsha')

    def test_fetch_failure_aborts_before_changes(self):
        with mock.patch.object(um, '_git_run', return_value=(1, '', 'network down')) as git, \
                mock.patch.object(um, '_reinstall_deps') as deps:
            result = um.apply_update('v1.2.3')
        self.assertIn('error', result)
        deps.assert_not_called()
        # Only the fetch was attempted; no reset.
        self.assertEqual(git.call_args_list[0][0][0], 'fetch')


class TestRollback(unittest.TestCase):
    """Rollback must be deterministic: prefer the recorded commit over reflog."""

    def test_resolve_target_prefers_recorded_commit(self):
        with mock.patch.object(um, '_load_persisted_state',
                               return_value={'previous_commit': 'recorded'}):
            self.assertEqual(um._resolve_rollback_target(), 'recorded')

    def test_resolve_target_falls_back_to_reflog(self):
        with mock.patch.object(um, '_load_persisted_state', return_value={}), \
                mock.patch.object(um, '_git_run', return_value=(0, 'reflogsha', '')):
            self.assertEqual(um._resolve_rollback_target(), 'reflogsha')

    def test_resolve_target_empty_when_nothing_known(self):
        with mock.patch.object(um, '_load_persisted_state', return_value={}), \
                mock.patch.object(um, '_git_run', return_value=(1, '', 'no reflog')):
            self.assertEqual(um._resolve_rollback_target(), '')

    def test_apply_rollback_success(self):
        with mock.patch.object(um, '_resolve_rollback_target', return_value='goodsha'), \
                mock.patch.object(um, '_git_run', return_value=(0, '', '')) as git, \
                mock.patch.object(um, '_repair_and_verify', return_value=(True, '')):
            result = um.apply_rollback()
        self.assertEqual(result, {'success': True, 'target': 'goodsha'})
        git.assert_called_with('reset', '--hard', 'goodsha')

    def test_apply_rollback_no_target(self):
        with mock.patch.object(um, '_resolve_rollback_target', return_value=''), \
                mock.patch.object(um, '_repair_and_verify') as repair:
            result = um.apply_rollback()
        self.assertIn('error', result)
        repair.assert_not_called()

    def test_apply_rollback_reports_repair_failure(self):
        with mock.patch.object(um, '_resolve_rollback_target', return_value='goodsha'), \
                mock.patch.object(um, '_git_run', return_value=(0, '', '')), \
                mock.patch.object(um, '_repair_and_verify',
                                  return_value=(False, 'ImportError: boom')):
            result = um.apply_rollback()
        self.assertIn('error', result)


if __name__ == '__main__':
    unittest.main()
