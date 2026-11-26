"""
Unit tests for post-force-stop supervision (loop_guard).

Regression target: an agent that obeys a loop-detection force-stop and starts
doing real work was hard-terminated on its 3rd tool call. Termination must now
require the offending ``tool|args`` key to persist across consecutive batches.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

from backend.agent_runtime.loop_guard import (
    PostForceStopSupervisor,
    POST_FORCE_STOP_MAX_BATCHES,
    batch_keys_from_records,
    tool_call_key,
)

STUCK = tool_call_key("read_file", {"path": "a.md", "offset": 1})


class TestInactive(unittest.TestCase):
    def test_no_verdict_before_force_stop(self):
        sup = PostForceStopSupervisor()
        self.assertFalse(sup.evaluate_batch([STUCK] * 5))


class TestInjectionBatch(unittest.TestCase):
    def test_batch_containing_injection_is_not_judged(self):
        sup = PostForceStopSupervisor(max_offending_batches=1)
        sup.on_force_stop(STUCK)
        # The batch during which the stop was injected is skipped entirely.
        self.assertFalse(sup.evaluate_batch([STUCK]))
        # The next offending batch does count.
        self.assertTrue(sup.evaluate_batch([STUCK]))


class TestCompliantAgentNotTerminated(unittest.TestCase):
    def test_different_tools_after_stop(self):
        sup = PostForceStopSupervisor()
        sup.on_force_stop(STUCK)
        sup.evaluate_batch([STUCK])  # injection batch
        for i in range(10):
            keys = [tool_call_key("str_replace", {"file_path": f"f{i}.md"}),
                    tool_call_key("read_file", {"path": f"{i}.md"})]
            self.assertFalse(sup.evaluate_batch(keys), f"killed on compliant batch {i}")

    def test_same_tool_with_different_args(self):
        sup = PostForceStopSupervisor()
        sup.on_force_stop(STUCK)
        sup.evaluate_batch([STUCK])
        for i in range(5):
            self.assertFalse(sup.evaluate_batch(
                [tool_call_key("read_file", {"path": f"doc{i}.md", "offset": 1})]))


class TestRealLoopTerminated(unittest.TestCase):
    def test_terminates_after_max_offending_batches(self):
        sup = PostForceStopSupervisor(max_offending_batches=POST_FORCE_STOP_MAX_BATCHES)
        sup.on_force_stop(STUCK)
        self.assertFalse(sup.evaluate_batch([STUCK]))  # injection batch, skipped
        results = [sup.evaluate_batch([STUCK, STUCK])
                   for _ in range(POST_FORCE_STOP_MAX_BATCHES)]
        self.assertFalse(all(results[:POST_FORCE_STOP_MAX_BATCHES - 1]))
        self.assertTrue(results[-1])

    def test_threshold_is_configurable(self):
        sup = PostForceStopSupervisor(max_offending_batches=1)
        sup.on_force_stop(STUCK)
        sup.evaluate_batch([STUCK])
        self.assertTrue(sup.evaluate_batch([STUCK]))


class TestResetSemantics(unittest.TestCase):
    def test_loop_then_compliant_is_not_terminated(self):
        sup = PostForceStopSupervisor(max_offending_batches=2)
        sup.on_force_stop(STUCK)
        sup.evaluate_batch([STUCK])          # injection batch, skipped
        self.assertFalse(sup.evaluate_batch([STUCK]))          # 1 offending
        self.assertFalse(sup.evaluate_batch([tool_call_key("bash", {"script": "ls"})]))
        self.assertEqual(sup.offending_batches, 0)

    def test_loop_resumes_after_reset(self):
        sup = PostForceStopSupervisor(max_offending_batches=2)
        sup.on_force_stop(STUCK)
        sup.evaluate_batch([STUCK])
        sup.evaluate_batch([STUCK])                                # 1
        sup.evaluate_batch([tool_call_key("bash", {"script": "ls"})])  # reset
        self.assertFalse(sup.evaluate_batch([STUCK]))              # 1 again
        self.assertTrue(sup.evaluate_batch([STUCK]))               # 2 -> terminate


class TestNewLoopAfterStop(unittest.TestCase):
    """A loop that merely moved to a different tool|args must still be caught."""

    OTHER = tool_call_key("read_file", {"path": "b.md", "offset": 40})

    def test_repeated_new_key_terminates(self):
        sup = PostForceStopSupervisor(max_offending_batches=2)
        sup.on_force_stop(STUCK)
        sup.evaluate_batch([STUCK])                     # injection batch
        self.assertFalse(sup.evaluate_batch([self.OTHER], repeated_keys={self.OTHER}))
        self.assertTrue(sup.evaluate_batch([self.OTHER], repeated_keys={self.OTHER}))

    def test_varied_work_without_repeats_survives(self):
        sup = PostForceStopSupervisor(max_offending_batches=2)
        sup.on_force_stop(STUCK)
        sup.evaluate_batch([STUCK])
        for i in range(6):
            batch = [tool_call_key("str_replace", {"file_path": f"f{i}.md"})]
            self.assertFalse(sup.evaluate_batch(batch, repeated_keys=set()))


class TestKeyBuilding(unittest.TestCase):
    def test_records_to_keys(self):
        records = [
            ({"id": "1"}, "read_file", {"path": "a.md"}, {}),
            ({"id": "2"}, "bash", None, {}),
        ]
        keys = batch_keys_from_records(records)
        self.assertEqual(keys[0], tool_call_key("read_file", {"path": "a.md"}))
        self.assertEqual(keys[1], "bash|")

    def test_key_is_order_insensitive(self):
        self.assertEqual(tool_call_key("t", {"a": 1, "b": 2}),
                         tool_call_key("t", {"b": 2, "a": 1}))


if __name__ == "__main__":
    unittest.main()
