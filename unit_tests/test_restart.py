"""Tests for the shared in-place restart helper (backend.restart)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.restart as restart


class TestRestartInPlace(unittest.TestCase):
    """restart_in_place() must stop channels + scheduler, then re-exec."""

    def _patches(self, stop_all=None, shutdown=None):
        """Build the common patch set; callers override stop_all/shutdown."""
        channel_manager = mock.MagicMock()
        channel_manager.stop_all = stop_all or mock.MagicMock()
        scheduler = mock.MagicMock()
        scheduler.shutdown = shutdown or mock.MagicMock()
        registry = mock.MagicMock(channel_manager=channel_manager)
        sched_mod = mock.MagicMock(scheduler=scheduler)
        return channel_manager, scheduler, registry, sched_mod

    def _run(self, registry, sched_mod, execv):
        with mock.patch.dict(sys.modules, {
            'backend.channels.registry': registry,
            'backend.scheduler': sched_mod,
        }), \
                mock.patch.object(restart.time, 'sleep'), \
                mock.patch.object(restart.os, 'execv', execv), \
                mock.patch.object(restart.os, 'chdir'), \
                mock.patch.object(restart.os, 'close_range', create=True), \
                mock.patch.object(restart.os, 'closerange', create=True):
            restart.restart_in_place(delay=0)

    def test_stops_channels_scheduler_then_execs(self):
        channel_manager, scheduler, registry, sched_mod = self._patches()
        execv = mock.MagicMock()
        self._run(registry, sched_mod, execv)

        channel_manager.stop_all.assert_called_once()
        scheduler.shutdown.assert_called_once()
        execv.assert_called_once()
        # Re-exec the interpreter against app.py.
        args = execv.call_args[0]
        self.assertTrue(args[1][-1].endswith('app.py'))

    def test_execs_even_if_stop_all_fails(self):
        """A channel-stop error must not prevent the re-exec."""
        _, scheduler, registry, sched_mod = self._patches(
            stop_all=mock.MagicMock(side_effect=RuntimeError('boom'))
        )
        execv = mock.MagicMock()
        self._run(registry, sched_mod, execv)
        execv.assert_called_once()

    def test_execs_even_if_scheduler_shutdown_fails(self):
        _, _, registry, sched_mod = self._patches(
            shutdown=mock.MagicMock(side_effect=RuntimeError('boom'))
        )
        execv = mock.MagicMock()
        self._run(registry, sched_mod, execv)
        execv.assert_called_once()


class TestScheduleRestart(unittest.TestCase):

    def test_spawns_daemon_thread(self):
        with mock.patch.object(restart.threading, 'Thread') as Thread:
            restart.schedule_restart(delay=0)
        Thread.assert_called_once()
        self.assertEqual(Thread.call_args.kwargs['target'], restart.restart_in_place)
        self.assertTrue(Thread.call_args.kwargs['daemon'])
        Thread.return_value.start.assert_called_once()


if __name__ == '__main__':
    unittest.main()
