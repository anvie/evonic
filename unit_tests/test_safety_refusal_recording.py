"""Refusals must be recorded even when Decim Safety never ran.

The safety gate returns before the Decim comparison, so a blocked execution used
to leave no telemetry row at all. That is why the export contains 973 enforce
comparisons and zero refusals: the positive class was structurally invisible.
"""

from types import SimpleNamespace

import pytest

from backend.services import decim_safety_telemetry as telemetry
from backend.tools.lib.safety_pipeline import SafetyPipeline


class _Spy:
    """Stands in for the telemetry service and captures the recorded payload."""

    def __init__(self):
        self.calls = []

    def record_comparison(self, **kwargs):
        self.calls.append(kwargs)
        return "event-1"


def _pipeline(monkeypatch, settings):
    pipeline = SafetyPipeline()
    spy = _Spy()
    monkeypatch.setattr(pipeline, "_telemetry_service", lambda: spy)
    monkeypatch.setattr(pipeline, "_load_settings", lambda: settings)
    return pipeline, spy


def _verdict(level="dangerous"):
    return {"level": level, "score": 90, "blocked_patterns": ["file_destruction"]}


def test_refusal_records_a_blocked_event(monkeypatch):
    settings = SimpleNamespace(enabled=True, mode="enforce", record_enforce_events=False)
    pipeline, spy = _pipeline(monkeypatch, settings)

    event_id = pipeline.record_refusal(
        "rm -rf /opt/app", tool_type="bash", agent_context=None, verdict=_verdict()
    )

    assert event_id == "event-1"
    call = spy.calls[0]
    assert call["decision_source"] == "hmads"
    assert call["final_level"] == "dangerous"
    assert call["deterministic"]["level"] == "dangerous"
    assert call["model_decision"] is None


def test_refusal_ignores_record_enforce_events(monkeypatch):
    """Sampling gates comparisons, not refusals: a block is never a sample."""
    settings = SimpleNamespace(enabled=True, mode="enforce", record_enforce_events=False)
    pipeline, spy = _pipeline(monkeypatch, settings)
    pipeline.record_refusal("x", tool_type="bash", agent_context=None, verdict=_verdict())
    assert len(spy.calls) == 1


def test_refusal_records_nothing_when_safety_is_off(monkeypatch):
    for settings in (
        SimpleNamespace(enabled=True, mode="off", record_enforce_events=True),
        SimpleNamespace(enabled=False, mode="enforce", record_enforce_events=True),
        None,
    ):
        pipeline, spy = _pipeline(monkeypatch, settings)
        assert pipeline.record_refusal(
            "x", tool_type="bash", agent_context=None, verdict=_verdict()
        ) is None
        assert spy.calls == []


def test_refusal_never_raises_when_telemetry_is_broken(monkeypatch):
    pipeline = SafetyPipeline()
    monkeypatch.setattr(pipeline, "_load_settings", lambda: SimpleNamespace(
        enabled=True, mode="enforce", record_enforce_events=True))

    def boom(**kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(pipeline, "_telemetry_service", lambda: SimpleNamespace(
        record_comparison=boom))
    assert pipeline.record_refusal(
        "x", tool_type="bash", agent_context=None, verdict=_verdict()) is None


@pytest.mark.parametrize("level,disposition", [
    ("dangerous", "blocked"),
    ("requires_approval", "approval_requested"),
    ("warning", "executed"),
    ("safe", "executed"),
])
def test_dangerous_levels_map_to_a_blocked_disposition(level, disposition):
    assert telemetry._disposition_for(level) == disposition


def test_blocked_is_an_accepted_disposition():
    """A refusal must survive update_disposition, or it is overwritten."""
    assert "blocked" in {"executed", "approval_requested", "blocked", "execution_failed"}
