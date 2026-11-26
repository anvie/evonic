"""Tool-calling eval: the file-tool mocks share one workspace per run, and a run
that is still calling tools at the turn cap is stopped and penalised."""

import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from evaluator import engine as engine_mod
from evaluator.engine import DEFAULT_WORKSPACE, MAX_TURNS_PENALTY, TOOL_CALLING_MAX_TURNS, EvaluationEngine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def engine():
    eng = EvaluationEngine.__new__(EvaluationEngine)
    eng._log = lambda *a, **k: None
    eng.total_tokens = eng.total_duration_ms = 0
    return eng


@pytest.fixture
def tools(engine):
    """Run a tool's real mock from tools/<name>.json against one shared workspace."""
    workspace = dict(DEFAULT_WORKSPACE)

    def call(name, **args):
        code = json.load(open(os.path.join(ROOT, "tools", f"{name}.json")))["mock_response"]
        return engine._execute_python_mock(code, args, workspace)

    call.workspace = workspace
    return call


def test_a_written_file_can_be_read_back_and_listed(tools):
    created = tools("write_file", file_path="helpers.py", content="def round_price(x):\n    return round(x, 2)\n")
    assert created["result"] == "success" and created["created"] is True

    assert tools("read_file", file_path="helpers.py")["result"].startswith("1: def round_price")
    assert tools("read_file", file_path="/workspace/helpers.py")["result"].startswith("1: def round_price")
    assert "helpers.py" in tools("bash", script="ls -la")["stdout"].split("\n")
    assert tools("bash", script="cat helpers.py")["stdout"].startswith("def round_price")
    assert "./helpers.py" in tools("runpy", code="from evonic import tree\nprint(tree())")["stdout"]


def test_a_file_outside_the_project_is_readable_too(tools):
    tools("write_file", file_path="/tmp/probe.py", content="X = 1\n")
    assert tools("read_file", file_path="/tmp/probe.py")["result"] == "1: X = 1\n2: "
    assert "/tmp/probe.py" not in tools("bash", script="ls")["stdout"]


def test_write_file_refuses_an_existing_file(tools):
    refused = tools("write_file", file_path="utils.py", content="x")
    assert refused["isError"] is True
    assert "format_currency" in tools("read_file", file_path="utils.py")["result"]


def test_str_replace_edits_the_shared_file(tools):
    assert tools("str_replace", file_path="discount.py", old_str="price - (price * percent)",
                 new_str="price - (price * percent / 100)")["replacements"] == 1
    assert "percent / 100" in tools("read_file", file_path="discount.py")["result"]
    missing = tools("str_replace", file_path="discount.py", old_str="no such text", new_str="x")
    assert "not found" in missing["error"]


def test_bash_echo_and_chained_commands_run_each_part(tools):
    out = tools("bash", script='echo "=== dirs ==="; ls; echo "---"; pwd')
    assert out["stdout"].split("\n") == ["=== dirs ===", *DEFAULT_WORKSPACE.keys(), "---", "/workspace"]
    assert out["exit_code"] == 0


def test_bash_reports_a_missing_file(tools):
    out = tools("bash", script="cat nope.py")
    assert out["exit_code"] == 1 and "No such file" in out["stderr"]
    assert tools("bash", script="wc -l utils.py")["stdout"] == "5 utils.py"


def test_bash_keeps_the_grep_answers_other_tests_rely_on(tools):
    assert "calculate_discount" in tools("bash", script="grep -rn calculate_discount .")["stdout"]
    assert "authenticate" in tools("bash", script="grep -rn authenticate .")["stdout"]


def test_runpy_shows_literal_prints_and_keeps_the_discount_answer(tools):
    assert tools("runpy", code='print("hello")\nprint("world")')["stdout"] == "hello\nworld\n"
    assert tools("runpy", code="x = 1")["stdout"] == "OK\n"
    assert tools("runpy", code="from discount import calculate_discount\nprint(calculate_discount(100, 10))")["stdout"] == "Result: 90\n"


def test_each_run_starts_from_the_default_workspace(engine):
    """The workspace is rebuilt per loop, so one run's writes never leak into the next."""
    from evaluator.tools import tool_framework

    class Client:
        def __init__(self):
            self.n = 0

        def chat_completion(self, messages, tools=None, **kw):
            self.n += 1
            return {"success": True, "duration_ms": 1, "total_tokens": 1}

        def extract_content_with_thinking(self, resp):
            if self.n > 1:
                return {"content": "done", "thinking": None, "tool_calls": []}
            args = {"file_path": "leak.py", "content": "x"}
            return {"content": "", "thinking": None,
                    "tool_calls": [{"id": "c", "type": "function",
                                    "function": {"name": "write_file", "arguments": json.dumps(args)}}]}

    mock_code = json.load(open(os.path.join(ROOT, "tools", "write_file.json")))["mock_response"]
    for _ in range(2):
        out = engine._run_tool_calling_loop(
            "w", tool_framework.tools, {"write_file": mock_code}, mock_response_types={"write_file": "python"},
            enable_planning=False, run_llm_client=Client())
        assert out["conversation_log"][0]["tool_results"][0]["result"]["result"] == "success"


class _Scripted:
    """Scripted LLM: turn(n) returns (tool name, args) for model turn n, or None to answer.

    The engine also asks for a plan and a closing summary with tools=None; those
    get plain text and do not count as turns.
    """

    def __init__(self, turn=None):
        self.n = 0
        self.turn = turn or (lambda n: ("read_file", {"file_path": f"f{n}.py"}))

    def chat_completion(self, messages, tools=None, **kw):
        return {"success": True, "duration_ms": 1, "total_tokens": 1, "no_tools": tools is None}

    def extract_content_with_thinking(self, resp):
        if resp.get("no_tools"):
            return {"content": "ok", "thinking": None, "tool_calls": []}
        self.n += 1
        step = self.turn(self.n)
        if step is None:
            return {"content": "done", "thinking": None, "tool_calls": []}
        name, args = step
        return {"content": "", "thinking": None,
                "tool_calls": [{"id": f"c{self.n}", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)}}]}


CHAIN = [("bash", {"script": "ls"}), ("read_file", {"file_path": "utils.py"}),
         ("write_file", {"file_path": "h.py", "content": "x"}), ("runpy", {"code": "x=1"})]


def _run_add_feature(engine, client, **overrides):
    from evaluator.test_manager import test_manager
    test = dict(test_manager.get_test("tool_code_add_feature_5"), **overrides)
    with mock.patch.object(engine_mod.db, "save_individual_test_result"), \
            mock.patch.object(engine_mod.test_logger, "log_test"):
        return engine._run_single_configurable_test(test, "tool_calling", 5, "fake-model", 0, run_llm_client=client)


def test_loop_stops_at_the_given_turn_cap(engine):
    from evaluator.tools import tool_framework
    out = engine._run_tool_calling_loop(
        "go", tool_framework.tools, {"read_file": {"result": "x"}}, enable_planning=False,
        run_llm_client=_Scripted(), max_turns=4)
    assert out["iterations"] == 4 and out["stop_reason"] == "max_turns"


def test_tool_calling_tests_are_capped_at_fifty_turns_by_default():
    assert TOOL_CALLING_MAX_TURNS == 50 and MAX_TURNS_PENALTY == 0.5


def test_a_run_that_hits_the_turn_cap_is_penalised(engine):
    # The chain is satisfied (score 1.0 on its own), then the model keeps calling tools.
    client = _Scripted(lambda n: CHAIN[n - 1] if n <= len(CHAIN) else ("bash", {"script": f"echo {n}"}))
    res = _run_add_feature(engine, client, max_tool_turns=8)

    assert client.n == 8
    assert res.details["max_turns_exceeded"] is True and res.details["max_turns"] == 8
    assert res.score == pytest.approx(1.0 * MAX_TURNS_PENALTY)
    assert res.status == "failed"


def test_a_run_that_finishes_before_the_cap_is_not_penalised(engine):
    res = _run_add_feature(engine, _Scripted(lambda n: CHAIN[n - 1] if n <= len(CHAIN) else None))
    assert not res.details.get("max_turns_exceeded")
    assert res.score == 1.0 and res.status == "passed"
