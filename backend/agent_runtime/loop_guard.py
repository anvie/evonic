"""Post-force-stop supervision for the LLM tool loop.

Background
----------
The tool loop detects a repeating ``tool|args`` pair and injects a force-stop
instruction telling the model to stop and answer.  The old implementation then
counted *individual tool calls* and hard-terminated the turn after the third
one.  Because tool batches routinely contain 2-4 parallel calls, an agent that
had clearly obeyed (stopped repeating and started doing real work) was killed
two calls later -- a false positive.

This module evaluates obedience once per **tool batch** and requires the
offending ``tool|args`` key to persist across several consecutive batches
before the turn is terminated.  Any batch without the key resets the counter,
so a single compliant batch is enough to clear the suspicion.
"""

from __future__ import annotations

import json
from typing import Iterable, Optional, Sequence

#: How many consecutive batches may still contain the offending key before the
#: turn is hard-terminated.
POST_FORCE_STOP_MAX_BATCHES = 3


class PostForceStopSupervisor:
    """Batch-granularity supervisor for post-force-stop tool batches."""

    def __init__(self, max_offending_batches: int = POST_FORCE_STOP_MAX_BATCHES) -> None:
        self.max_offending_batches = max(1, int(max_offending_batches))
        self.trigger_key: Optional[str] = None
        self.offending_batches = 0
        self.judged_batches = 0
        # The batch during which the stop was injected is not judged: the model
        # produced it before it could read the instruction.
        self._skip_next_batch = False
        self.active = False

    # -- lifecycle -------------------------------------------------------
    def on_force_stop(self, trigger_key: Optional[str]) -> None:
        """Record the offending ``tool|args`` key and arm supervision."""
        self.trigger_key = trigger_key
        self.offending_batches = 0
        self.judged_batches = 0
        self._skip_next_batch = True
        self.active = True

    # -- evaluation ------------------------------------------------------
    def evaluate_batch(self, batch_keys: Sequence[str],
                       repeated_keys: Iterable[str] = ()) -> bool:
        """Judge one completed tool batch.

        Args:
            batch_keys:    ``tool|args`` keys issued in this batch.
            repeated_keys: keys already known to repeat (window count at or above
                the loop detector's threshold). A model that swaps to a *new*
                repeated pattern after the stop is still looping, so it counts as
                offending even though it no longer matches ``trigger_key``. This
                preserves the old protection against "loop on something else"
                without killing genuinely varied work.

        Returns ``True`` when the turn must be terminated.
        """
        if not self.active:
            return False

        if self._skip_next_batch:
            # Batch during which the stop was injected -- not the model's answer.
            self._skip_next_batch = False
            return False

        self.judged_batches += 1
        keys = list(batch_keys or ())
        offending = bool(self.trigger_key) and self.trigger_key in keys
        if not offending and repeated_keys:
            repeated = set(repeated_keys)
            offending = any(k in repeated for k in keys)

        if offending:
            self.offending_batches += 1
        else:
            # Behaviour changed: the agent stopped repeating the offending call.
            self.offending_batches = 0

        return self.offending_batches >= self.max_offending_batches

    # -- diagnostics -----------------------------------------------------
    def describe(self) -> str:
        return (
            f"offending_batches={self.offending_batches}"
            f"/{self.max_offending_batches}"
            f" judged_batches={self.judged_batches}"
        )


def tool_call_key(fn_name: str, args) -> str:
    """Stable ``tool|args`` key, matching the loop detector's format."""
    if args is None:
        return f"{fn_name}|"
    try:
        return f"{fn_name}|{json.dumps(args, sort_keys=True, default=str)}"
    except (TypeError, ValueError):
        return f"{fn_name}|"


def batch_keys_from_records(records: Iterable[Sequence]) -> list[str]:
    """Build ``tool|args`` keys from ``(tc, fn_name, args, pt)`` records."""
    keys: list[str] = []
    for rec in records:
        try:
            fn_name, args = rec[1], rec[2]
        except (IndexError, TypeError):
            continue
        keys.append(tool_call_key(fn_name, args))
    return keys
