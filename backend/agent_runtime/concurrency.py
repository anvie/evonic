"""
Turn-level concurrency limiting for AgentRuntime.

ConcurrencyGate: resizable semaphore using Condition+counter.
ConcurrencyManager: per-agent and per-model gates.
"""
import logging
import threading
from contextlib import contextmanager
from typing import Dict, Optional

_logger = logging.getLogger(__name__)


class ConcurrencyGate:
    """A resizable semaphore. max_concurrent=0 means unlimited."""

    def __init__(self, max_concurrent: int = 0, name: str = ""):
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._active = 0
        self._max = max_concurrent
        self._name = name

    def acquire(self) -> None:
        #_logger.info("[LOCK] acquire(name=%s) - WAITING (active=%d/%d)",
        #              self._name, self._active, self._max)
        with self._condition:
            while self._max > 0 and self._active >= self._max:
                self._condition.wait()
            self._active += 1
            #_logger.info("[LOCK] acquire(name=%s) - ACQUIRED (active=%d/%d)",
            #             self._name, self._active, self._max)

    def release(self) -> None:
        #_logger.info("[LOCK] release(name=%s) - RELEASING (active=%d/%d)",
                      self._name, self._active, self._max)
        with self._condition:
            self._active -= 1
            self._condition.notify()
        #_logger.info("[LOCK] release(name=%s) - RELEASED (active=%d/%d)",
        #             self._name, self._active, self._max)

    def set_max(self, new_max: int) -> None:
        with self._condition:
            self._max = new_max
            # Wake all waiters so they re-evaluate the new limit
            self._condition.notify_all()

    def is_at_capacity(self) -> bool:
        """Non-blocking check: True if no further slot is available right now."""
        with self._lock:
            return self._max > 0 and self._active >= self._max

    @property
    def capacity_details(self) -> dict:
        """Return {active, max} for diagnostic logging."""
        with self._lock:
            return {"active": self._active, "max": self._max}

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


class ConcurrencyManager:
    """Manages per-agent and per-model turn concurrency gates."""

    def __init__(self):
        self._agent_gates: Dict[str, ConcurrencyGate] = {}
        self._model_gates: Dict[str, ConcurrencyGate] = {}
        self._gates_lock = threading.Lock()
        self._default_agent_limit: int = self._load_agent_limit()

    # ── Private helpers ───────────────────────────────────────────────────────

    def _load_agent_limit(self) -> int:
        try:
            from models.db import db
            return max(0, int(db.get_setting('max_concurrent_llm_per_agent', '1')))
        except Exception:
            return 1

    def _load_model_limit(self, model_id: str) -> int:
        try:
            from models.db import db
            model = db.get_model_by_id(model_id)
            if model:
                per_model = max(0, int(model.get('model_max_concurrent', 0) or 0))
                if per_model > 0:
                    return per_model
                # Fall back to global per-model default
                global_limit = max(0, int(db.get_setting('max_concurrent_llm_per_model', '0')))
                return global_limit
        except Exception:
            pass
        return 1

    def _get_agent_gate(self, agent_id: str) -> ConcurrencyGate:
        with self._gates_lock:
            if agent_id not in self._agent_gates:
                self._agent_gates[agent_id] = ConcurrencyGate(
                    self._default_agent_limit, name=f"agent:{agent_id}")
            return self._agent_gates[agent_id]

    def _get_model_gate(self, model_id: str) -> ConcurrencyGate:
        with self._gates_lock:
            if model_id not in self._model_gates:
                self._model_gates[model_id] = ConcurrencyGate(
                    self._load_model_limit(model_id), name=f"model:{model_id}")
            return self._model_gates[model_id]

    # ── Public API ────────────────────────────────────────────────────────────

    def refresh_agent_limit(self) -> None:
        """Re-read the global per-agent setting and update all existing gates."""
        new_limit = self._load_agent_limit()
        self._default_agent_limit = new_limit
        with self._gates_lock:
            gates = list(self._agent_gates.values())
        for gate in gates:
            gate.set_max(new_limit)

    def refresh_model_limit(self, model_id: str) -> None:
        """Re-read a model's model_max_concurrent and update its gate if it exists."""
        new_limit = self._load_model_limit(model_id)
        with self._gates_lock:
            gate = self._model_gates.get(model_id)
        if gate is not None:
            gate.set_max(new_limit)

    def refresh_all_model_limits(self) -> None:
        """Re-read limits for all existing model gates (called when global default changes)."""
        with self._gates_lock:
            model_ids = list(self._model_gates.keys())
        for model_id in model_ids:
            self.refresh_model_limit(model_id)

    def is_agent_at_capacity(self, agent_id: str) -> bool:
        """Non-blocking check: True if the per-agent concurrency gate is full.

        Returns False if no gate exists yet (agent has never started a turn).
        """
        with self._gates_lock:
            gate = self._agent_gates.get(agent_id)
        if gate is None:
            return False
        return gate.is_at_capacity()

    def get_agent_capacity_details(self, agent_id: str) -> dict:
        """Return {active, max} for the agent's concurrency gate, or {active: 0, max: 0}
        if no gate exists yet."""
        with self._gates_lock:
            gate = self._agent_gates.get(agent_id)
        if gate is None:
            return {"active": 0, "max": self._default_agent_limit}
        return gate.capacity_details

    @contextmanager
    def turn_gate(self, agent_id: str, model_id: Optional[str]):
        """Context manager: acquire agent gate then model gate (consistent order)."""
        agent_gate = self._get_agent_gate(agent_id)
        model_gate = self._get_model_gate(model_id) if model_id else None

        agent_gate.acquire()
        try:
            if model_gate is not None:
                model_gate.acquire()
            try:
                yield
            finally:
                if model_gate is not None:
                    model_gate.release()
        finally:
            agent_gate.release()
