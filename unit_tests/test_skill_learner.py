"""Coverage for the skill learner's gates and reporting (backend/agent_runtime/skill_learner.py)."""
import pytest

from backend import agent_oneshot, ephemeral_sessions
from backend.agent_runtime import skill_learner
from models.db import db

OK = {"exit_code": 0}
LONG_TRACE = [{"tool": "bash", "args": {"script": "ls"}, "result": OK}] * 5


class InlineThread:
    """Runs the learner in the calling thread so a test can assert on its effects."""

    def __init__(self, target, daemon=None):
        self._target = target

    def start(self):
        self._target()


@pytest.fixture
def learner(monkeypatch):
    """Wire the learner to fakes; returns a dict the test can inspect and steer."""
    state = {"learned": [], "notes": [], "agent": {"id": "agent-a", "learned_skills_enabled": 1}}
    monkeypatch.setattr(db, "get_agent", lambda agent_id: state["agent"])
    monkeypatch.setattr(ephemeral_sessions, "is_memory_blocked", lambda session_id: False)
    monkeypatch.setattr(skill_learner, "_learn", lambda *args: state["learned"].append(args))
    monkeypatch.setattr(skill_learner, "_note",
                        lambda agent_id, session_id, user, text, **meta: state["notes"].append(meta))
    monkeypatch.setattr(skill_learner.threading, "Thread", InlineThread)
    monkeypatch.setattr(skill_learner, "_running", set())
    monkeypatch.setattr(skill_learner, "_last_run", {})
    return state


def _event(**overrides):
    return {"agent_id": "agent-a", "session_id": "s1", "external_user_id": "web",
            "tool_trace": LONG_TRACE, "response": "done", "is_error": False, **overrides}


def test_long_turn_triggers_the_learner(learner):
    skill_learner.on_turn_complete(_event())
    assert len(learner["learned"]) == 1


def test_error_then_success_triggers_even_when_short(learner):
    trace = [{"tool": "bash", "args": {}, "result": {"error": "boom"}},
             {"tool": "bash", "args": {}, "result": OK}]
    skill_learner.on_turn_complete(_event(tool_trace=trace))
    assert len(learner["learned"]) == 1


@pytest.mark.parametrize("overrides", [
    {"tool_trace": LONG_TRACE[:2]},                              # too short, no errors
    {"is_error": True},
    {"external_user_id": "__agent__oneshot__skilllearner_1"},    # the learner's own session
])
def test_turns_that_must_not_trigger(learner, overrides):
    skill_learner.on_turn_complete(_event(**overrides))
    assert learner["learned"] == []


def test_feature_off_or_sub_agent_does_nothing(learner):
    learner["agent"] = {"id": "agent-a", "learned_skills_enabled": 0}
    skill_learner.on_turn_complete(_event())
    learner["agent"] = None   # sub-agents have no agents row
    skill_learner.on_turn_complete(_event())
    assert learner["learned"] == []


def test_cooldown_blocks_a_second_run(learner):
    skill_learner.on_turn_complete(_event())
    skill_learner.on_turn_complete(_event())
    assert len(learner["learned"]) == 1


def test_inline_edit_of_a_learned_skill_is_reported(learner):
    trace = [{"tool": "str_replace", "args": {"file_path": "/_self/skills/make-pdf/SYSTEM.md"}, "result": {"result": "success"}},
             {"tool": "write_file", "args": {"file_path": "/_self/skills/broken/SYSTEM.md"}, "result": {"error": "denied"}},
             {"tool": "write_file", "args": {"file_path": "/_self/kb/note.md"}, "result": {"result": "success"}}]
    skill_learner.on_turn_complete(_event(tool_trace=trace))
    assert learner["notes"] == [{"stage": "saved", "id": "make-pdf"}]


def _run_learn(monkeypatch, *, decision, write=None, reply=None):
    """Run _learn with a fake one-shot; `write` mutates the fake skill listing."""
    skills = {"old": 1.0}
    notes, asked = [], []

    def fake_ask(agent_id, message, **kwargs):
        asked.append(message)
        if write:
            write(skills)
        return reply or {"success": True, "reply": "DONE"}

    monkeypatch.setattr(skill_learner, "_decide", lambda *args: decision)
    monkeypatch.setattr(agent_oneshot, "ask", fake_ask)
    monkeypatch.setattr(skill_learner.learned_skills, "list_skills",
                        lambda agent, include_invalid=False: [
                            {"id": sid, "updated_at": mtime, "brief": "", "description": ""}
                            for sid, mtime in skills.items()])
    monkeypatch.setattr(skill_learner.learned_skills, "validate", lambda agent, sid: None)
    monkeypatch.setattr(skill_learner, "_note",
                        lambda agent_id, session_id, user, text, **meta: notes.append(meta))
    skill_learner._learn({"id": "agent-a"}, "s1", "web", "make a pdf", LONG_TRACE, "done")
    return notes, asked


def test_decision_skip_makes_no_llm_call_and_no_notes(monkeypatch):
    notes, asked = _run_learn(monkeypatch, decision="skip")
    assert notes == [] and asked == []


def test_created_and_updated_skills_are_reported(monkeypatch):
    def write(skills):
        skills["old"] = 2.0
        skills["make-pdf"] = 2.0

    notes, asked = _run_learn(monkeypatch, decision=None, write=write)

    assert notes == [{"stage": "reviewing"}, {"stage": "updated", "id": "old"},
                     {"stage": "created", "id": "make-pdf"}]
    assert "make a pdf" in asked[0] and "/_self/skills/<skill_id>/" in asked[0]


def test_no_change_and_failure_are_reported(monkeypatch):
    notes, _ = _run_learn(monkeypatch, decision="create")
    assert notes[-1] == {"stage": "none"}
    notes, _ = _run_learn(monkeypatch, decision="create",
                          reply={"success": False, "error": "timed out"})
    assert notes[-1] == {"stage": "failed"}
