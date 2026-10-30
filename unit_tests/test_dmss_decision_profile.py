"""DMSS Decision Profile bridge regression coverage (no command execution)."""
from types import SimpleNamespace

from backend.dmss_decision_profile import (
    PROFILE_KEY, SafetyCommandDecisionRunner, SafetyCommandOutcome,
)
from backend.tools.lib.decision_models import (
    DecisionFieldResult, DecisionProfileRef, DecisionResult, ProviderMetadata,
    TimingMetadata,
)
from backend.tools.lib.safety_pipeline import SafetyPipeline


class Checker:
    def __init__(self, score=0):
        self.score = score

    def check(self, *_args):
        return {"score": self.score, "reasons": [], "blocked_patterns": [], "matched_patterns": []}


class ProfilePipeline(SafetyPipeline):
    def __init__(self, outcome):
        super().__init__(register_defaults=False)
        self.register(Checker())
        self.outcome = outcome
        self.recorded = []

    def _load_settings(self):
        return None

    def _telemetry_service(self):
        pipeline = self

        class _Spy:
            def record_comparison(self, **kwargs):
                pipeline.recorded.append(kwargs)
                return "event-1"
        return _Spy()

    def _safety_command_profile(self):
        return {"profile_key": "safety.command"}

    def _run_safety_command_profile(self, *_args):
        return self.outcome


def test_shadow_profile_never_changes_hmads_verdict():
    pipeline = ProfilePipeline(SafetyCommandOutcome(True, mode="shadow", decision="block", confidence=.99))
    result = pipeline.check("echo safe", "bash", {})
    assert result["level"] == "safe"
    assert result["decision_source"] == "deterministic_shadow"


class CountingChecker(Checker):
    calls = 0

    def check(self, *args):
        self.calls += 1
        return super().check(*args)


def test_enforce_profile_decides_without_running_hmads():
    pipeline = ProfilePipeline(SafetyCommandOutcome(True, mode="enforce", decision="allow", confidence=.99))
    checker = CountingChecker(15)
    pipeline._checkers = [checker]
    result = pipeline.check("synthetic", "bash", {})
    assert result["level"] == "safe"
    assert result["decision_source"] == "decision_profile"
    assert checker.calls == 0


def test_enforce_profile_falls_back_to_hmads_when_unusable():
    pipeline = ProfilePipeline(SafetyCommandOutcome(True, mode="enforce", fallback_reason="timeout"))
    checker = CountingChecker(15)
    pipeline._checkers = [checker]
    result = pipeline.check("synthetic", "bash", {})
    assert result["level"] == "dangerous"
    assert result["decision_source"] == "deterministic_fallback"
    assert checker.calls == 1


def test_profile_failure_uses_exact_deterministic_fallback():
    pipeline = ProfilePipeline(SafetyCommandOutcome(True, mode="enforce", fallback_reason="low_confidence"))
    expected = pipeline.check_deterministic("synthetic", "bash", {})
    result = pipeline.check("synthetic", "bash", {})
    assert result["level"] == expected["level"]
    assert result["decision_source"] == "deterministic_fallback"
    assert result["decim_safety"]["fallback_reason"] == "low_confidence"


def test_profile_enforce_maps_review_without_executing_command():
    pipeline = ProfilePipeline(SafetyCommandOutcome(True, mode="enforce", decision="review", confidence=.99))
    result = pipeline.check("synthetic", "bash", {})
    assert result["level"] == "requires_approval"
    assert result["requires_approval"] is True


def test_profile_shadow_records_both_outputs():
    pipeline = ProfilePipeline(SafetyCommandOutcome(True, mode="shadow", decision="block",
                                                    confidence=.97, latency_ms=410))
    result = pipeline.check("echo safe", "bash", {})
    call = pipeline.recorded[0]
    assert call["settings"].mode == "shadow"
    assert call["decision_source"] == "deterministic_shadow"
    assert call["model_decision"] == "block" and call["confidence"] == .97
    assert call["latency_ms"] == 410
    assert call["deterministic"]["level"] == "safe"
    assert call["final_level"] == "safe"
    assert result["decim_safety"]["event_id"] == "event-1"


def test_profile_enforce_block_is_recorded_as_dmss_even_without_sampling():
    pipeline = ProfilePipeline(SafetyCommandOutcome(True, mode="enforce", decision="block", confidence=.99))
    result = pipeline.check("synthetic", "bash", {})
    call = pipeline.recorded[0]
    assert call["decision_source"] == "decision_profile"
    assert call["model_decision"] == "block"
    assert call["final_level"] == "dangerous"
    # The tool boundary skips record_refusal when an event id is present.
    assert result["decim_safety"]["event_id"] == "event-1"


def test_profile_enforce_allow_respects_sampling():
    pipeline = ProfilePipeline(SafetyCommandOutcome(True, mode="enforce", decision="allow", confidence=.99))
    pipeline.check("synthetic", "bash", {})
    assert pipeline.recorded == []


def test_profile_failure_is_recorded_with_reason():
    pipeline = ProfilePipeline(SafetyCommandOutcome(True, mode="shadow", fallback_reason="timeout"))
    pipeline.check("synthetic", "bash", {})
    call = pipeline.recorded[0]
    assert call["model_decision"] is None
    assert call["fallback_reason"] == "timeout"
    assert call["decision_source"] == "deterministic_fallback"


def test_profile_not_attempted_records_nothing():
    pipeline = ProfilePipeline(SafetyCommandOutcome(False, mode="off", fallback_reason="profile_disabled"))
    pipeline.check("synthetic", "bash", {})
    assert pipeline.recorded == []


class _Runtime:
    def __init__(self, result):
        self.result = result

    def invoke(self, _request):
        return self.result


class _DB:
    def __init__(self):
        self.profile = {"enabled": True, "mode": "enforce", "minimum_confidence": .8}

    def get_decision_profile(self, key):
        return self.profile if key == PROFILE_KEY else None


def test_missing_confidence_cannot_enable_dmss_enforcement():
    profile = DecisionProfileRef(PROFILE_KEY, "decim/v1")
    result = DecisionResult(profile, "decision-schema/v1", (DecisionFieldResult("decision", "allow"),),
                            ProviderMetadata("systemone", "model"), TimingMetadata(1), 1)
    runner = SafetyCommandDecisionRunner(runtime_factory=lambda: _Runtime(result), db_provider=_DB)
    outcome = runner.decide("echo safe", "bash", "sandboxed_docker")
    assert outcome.decision is None
    assert outcome.fallback_reason == "low_confidence"
