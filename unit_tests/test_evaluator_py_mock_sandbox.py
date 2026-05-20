"""Tests for evaluator engine Python mock sandbox."""

import sys
import os
import queue

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from evaluator.engine import EvaluationEngine


def _run_mock(py_code, args=None):
    """Helper: call _execute_python_mock on a real engine instance."""
    if args is None:
        args = {}
    ev = EvaluationEngine()
    ev.log_queue = queue.Queue()
    return ev._execute_python_mock(py_code, args)


def test_basic_mock_sets_result():
    r = _run_mock("result = args.get('x', 0) * 2", {'x': 21})
    assert r == 42, r


def test_mock_with_math():
    r = _run_mock("import math; result = math.pi", {})
    assert 'error' in r, r


def test_mock_math_available_without_import():
    r = _run_mock("result = math.sqrt(16)", {})
    assert r == 4.0, r


def test_mock_with_json():
    r = _run_mock("result = json.dumps({'ok': True})", {})
    assert '"ok": true' in r or '{"ok": true}' in r, r


def test_mock_with_re():
    r = _run_mock("result = re.sub(r'\\d+', 'N', 'a1b2c3')", {})
    assert r == 'aNbNcN', r


def test_sandbox_blocks_import():
    r = _run_mock("import os; result = os.getcwd()", {})
    assert 'error' in r, r
    assert 'import' in r['error'].lower(), r


def test_sandbox_blocks_import_from():
    r = _run_mock("from os import getcwd; result = getcwd()", {})
    assert 'error' in r, r


def test_sandbox_blocks_dunder_class():
    r = _run_mock("result = ().__class__", {})
    assert 'error' in r, r
    assert '__class__' in r['error'], r


def test_sandbox_blocks_dunder_bases():
    r = _run_mock("x = (); result = x.__bases__", {})
    assert 'error' in r, r


def test_sandbox_blocks_dunder_subclasses():
    r = _run_mock("result = object.__subclasses__()", {})
    assert 'error' in r, r
    assert '__subclasses__' in r['error'], r


def test_sandbox_blocks_dunder_globals():
    r = _run_mock("result = len.__globals__", {})
    assert 'error' in r, r


def test_sandbox_blocks_dunder_builtins():
    r = _run_mock("result = {}.__builtins__", {})
    assert 'error' in r, r


def test_sandbox_blocks_exec_call():
    r = _run_mock("exec('result = 1')", {})
    assert 'error' in r, r
    assert 'exec' in r['error'].lower(), r


def test_sandbox_blocks_eval_call():
    r = _run_mock("result = eval('1+1')", {})
    assert 'error' in r, r


def test_sandbox_blocks_classdef():
    r = _run_mock("class Foo: pass\nresult = Foo()", {})
    assert 'error' in r, r


def test_sandbox_blocks_import_call_via_ast():
    r = _run_mock("__import__('os').system('echo pwned')", {})
    assert 'error' in r, r


def test_mock_no_result_returns_error():
    r = _run_mock("x = 42", {})
    assert 'error' in r, r
    assert 'did not set result' in r['error'], r


def test_mock_syntax_error():
    r = _run_mock("result = [invalid syntax!!!", {})
    assert 'error' in r, r
    assert 'syntax' in r['error'].lower(), r


def test_mock_complex_logic():
    code = """
data = args.get('items', [])
filtered = [x for x in data if x > 10]
result = sum(filtered)
"""
    r = _run_mock(code, {'items': [5, 15, 3, 20, 8]})
    assert r == 35, r


def test_sandbox_blocks_dunder_mro():
    r = _run_mock("result = int.__mro__", {})
    assert 'error' in r, r
    assert '__mro__' in r['error'], r


def test_sandbox_blocks_dunder_code():
    r = _run_mock("result = len.__code__", {})
    assert 'error' in r, r


def test_sandbox_blocks_dunder_reduce():
    r = _run_mock("result = ().__reduce__()", {})
    assert 'error' in r, r


def test_sandbox_blocks_dunder_getattr():
    r = _run_mock("result = object.__getattribute__", {})
    assert 'error' in r, r
