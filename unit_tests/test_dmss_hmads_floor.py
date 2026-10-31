"""HMADS floor on top of an accepted DMSS decision.

In enforce mode a confident model decision used to be final, so a command the
local rule engine flags (``git add -A`` is the motivating example) executed with
no approval and left no telemetry row.  With ``decim_safety.hmads_floor`` on,
HMADS still scores every scoped command and its verdict can only raise -- never
lower -- the DMSS severity.
"""

from types import SimpleNamespace

import pytest

from backend.tools.lib.decim_safety import DecimDecision, DecimResolution, DecimSettings
from backend.tools.lib.safety_pipeline import SafetyPipeline, verdict_provenance

GIT_ADD_ALL = "cd ~/repo && git add -A && git commit -q -F - <<'EOF' msg\nEOF"
SAFE_COMMAND = "ls -la /tmp"


class _FakeResolver:
    def __init__(self, decision: str, confidence: float = 0.95):
        self.decision = decision
        self.confidence = confidence
        self.calls = 0

    def resolve(self, code, tool_type, agent_context, settings):
        self.calls += 1
        return DecimResolution(
            attempted=True,
            decision=DecimDecision(
                decision=self.decision,
                confidence=self.confidence,
                model_id="test-model",
                provider_key="systemone",
                policy_version="v1",
                correlation_id="corr-1",
                latency_ms=12,
            ),
        )


class _Recorder:
    """Captures what the pipeline hands to telemetry."""

    def __init__(self):
        self.calls = []

    def __call__(self, settings, code, tool_type, agent_context, deterministic, **kwargs):
        self.calls.append({"deterministic": deterministic, **kwargs})
        return "event-1"


def _settings(**overrides):
    values = dict(enabled=True, mode="enforce", record_enforce_events=True,
                  hmads_floor=False, provider="systemone")
    values.update(overrides)
    return SimpleNamespace(**values)


def _pipeline(decision: str):
    pipeline = SafetyPipeline()
    pipeline._decim_resolver = _FakeResolver(decision)
    recorder = _Recorder()
    pipeline._record = recorder
    return pipeline, recorder


def _deterministic_calls(pipeline):
    calls = []
    original = pipeline.check_deterministic
    pipeline.check_deterministic = lambda *a, **k: calls.append(a) or original(*a, **k)
    return calls


def test_floor_off_keeps_hmads_out_of_an_allowed_enforce_decision():
    pipeline, recorder = _pipeline("allow")
    calls = _deterministic_calls(pipeline)

    result = pipeline.check(GIT_ADD_ALL, tool_type="bash",
                            agent_context=None, decim_settings=_settings(hmads_floor=False))

    assert calls == [], "HMADS must not run when the floor is off"
    assert result["level"] == "safe"
    assert result["decision_source"] == "decim"
    assert recorder.calls[0]["final_level"] == "safe"


def test_floor_raises_an_allowed_decision_to_approval():
    pipeline, recorder = _pipeline("allow")

    result = pipeline.check(GIT_ADD_ALL, tool_type="bash",
                            agent_context=None, decim_settings=_settings(hmads_floor=True))

    assert result["level"] == "requires_approval"
    assert result["requires_approval"] is True
    assert result["decision_source"] == "decim_floor"
    assert "git_staging" in result["blocked_patterns"]
    # Both engines stay visible for the audit trail.
    assert result["decim_safety"]["model_level"] == "safe"
    assert result["decim_safety"]["hmads_floor"] == "requires_approval"
    assert result["decim_safety"]["model_decision"] == "allow"


def test_floor_never_lowers_a_blocking_decision():
    pipeline, _ = _pipeline("block")

    result = pipeline.check(SAFE_COMMAND, tool_type="bash",
                            agent_context=None, decim_settings=_settings(hmads_floor=True))

    assert result["level"] == "dangerous"
    assert result["decision_source"] == "decim"


def test_floor_keeps_an_already_stricter_model_decision():
    pipeline, _ = _pipeline("review")

    result = pipeline.check(GIT_ADD_ALL, tool_type="bash",
                            agent_context=None, decim_settings=_settings(hmads_floor=True))

    assert result["level"] == "requires_approval"
    assert result["decision_source"] == "decim"


def test_telemetry_carries_the_hmads_verdict_when_floored():
    pipeline, recorder = _pipeline("allow")

    pipeline.check(GIT_ADD_ALL, tool_type="bash",
                   agent_context=None, decim_settings=_settings(hmads_floor=True))

    call = recorder.calls[0]
    assert call["decision_source"] == "decim_floor"
    assert call["final_level"] == "requires_approval"
    assert call["model_decision"] == "allow"
    assert call["deterministic"]["level"] == "requires_approval"
    assert call["deterministic"]["score"] >= 8


def test_telemetry_still_records_the_model_only_verdict_when_floor_is_off():
    pipeline, recorder = _pipeline("allow")

    pipeline.check(GIT_ADD_ALL, tool_type="bash",
                   agent_context=None, decim_settings=_settings(hmads_floor=False))

    call = recorder.calls[0]
    assert call["decision_source"] == "decim"
    assert call["deterministic"] == {}


def test_verdict_provenance_attributes_a_floored_verdict_to_the_rules():
    prov = verdict_provenance({
        "level": "requires_approval",
        "score": 8,
        "reasons": ["Git add all files (git add -A)"],
        "blocked_patterns": ["git_staging"],
        "decision_source": "decim_floor",
        "decim_safety": {"decision_source": "decim_floor", "model_decision": "allow",
                         "model_confidence": 0.936},
    })

    assert prov["source"] == "rules"
    assert prov["mode"] == "enforce"
    assert prov["decision"] == "review"
    assert prov["model_decision"] == "allow"


def test_floor_is_off_by_default():
    assert DecimSettings().hmads_floor is False


@pytest.mark.parametrize("raw,expected", [("1", True), ("0", False), ("true", True),
                                          ("false", False)])
def test_floor_setting_round_trips_through_persistence(raw, expected):
    from backend.tools.lib.decim_safety import _parse_settings

    values = {"mode": "enforce", "hmads_floor": raw}
    assert _parse_settings(values, strict=False).hmads_floor is expected


# ---------------------------------------------------------------------------
# Only user-defined HMADS rules may floor a confident DMSS decision
# ---------------------------------------------------------------------------

# Matches the built-in "file_destruction" rule (requires approval on its own).
BUILTIN_ONLY = "cd /srv/app && rm -f envtest.out && tmux -V"


def _invalidate_user_rules():
    from backend.tools.lib.custom_rule_checker import custom_rule_checker
    custom_rule_checker.invalidate_cache()


def test_builtin_rules_never_floor_a_confident_allow():
    pipeline, recorder = _pipeline("allow")
    assert pipeline.check_deterministic(BUILTIN_ONLY, "bash")["level"] == "requires_approval"

    result = pipeline.check(BUILTIN_ONLY, tool_type="bash",
                            agent_context=None, decim_settings=_settings(hmads_floor=True))

    assert result["level"] == "safe"
    assert result["decision_source"] == "decim"
    # The full HMADS verdict is still recorded for the audit trail.
    assert recorder.calls[0]["deterministic"]["level"] == "requires_approval"


def test_git_staging_is_a_user_rule_not_a_builtin():
    from backend.tools.lib.heuristic_safety import heuristic_checker
    from models.db import db

    assert heuristic_checker.check(GIT_ADD_ALL, "bash").get("score", 0) == 0
    rule = db.get_safety_rule("hmads-user-git-add-all")
    assert rule["is_system"] == 0 and rule["category"] == "git_staging"


def test_deleting_the_user_rule_removes_its_floor():
    from models.db import db

    db.delete_safety_rule("hmads-user-git-add-all")
    _invalidate_user_rules()
    try:
        pipeline, _ = _pipeline("allow")
        result = pipeline.check(GIT_ADD_ALL, tool_type="bash",
                                agent_context=None, decim_settings=_settings(hmads_floor=True))
        assert result["level"] == "safe"
        assert result["decision_source"] == "decim"
    finally:
        _invalidate_user_rules()


def test_user_rule_floor_uses_only_user_rule_score():
    from models.db import db

    db.update_safety_rule("hmads-user-git-add-all", {"weight": 4})
    _invalidate_user_rules()
    try:
        pipeline, _ = _pipeline("allow")
        # Built-in rm -f plus a weight-4 user rule: the full HMADS score is
        # dangerous territory, but the user rules alone only reach "warning".
        result = pipeline.check("rm -f out.log && " + GIT_ADD_ALL, tool_type="bash",
                                agent_context=None, decim_settings=_settings(hmads_floor=True))
        assert result["level"] == "warning"
        assert result["decision_source"] == "decim_floor"
    finally:
        _invalidate_user_rules()
