"""Tests for robust LLM JSON extraction (extract_first_json)."""

from backend.agent_runtime.llm_json import extract_first_json


def test_plain_object():
    assert extract_first_json('{"a": 1}') == {'a': 1}


def test_fenced_block():
    assert extract_first_json('reasoning…\n```json\n{"a": 1}\n```\nmore') == {'a': 1}


def test_trailing_prose_with_braces():
    # the live failure: valid object followed by extra data ("Extra data" in
    # json.loads with a first-brace..last-brace slice)
    text = ('{"n1": {"verdict": "proceed", "reason": ""}} '
            'All calls look fine. Note: {n2 was skipped}.')
    assert extract_first_json(text) == {'n1': {'verdict': 'proceed', 'reason': ''}}


def test_leading_prose_with_braces():
    text = 'Thinking about {stuff} first… answer: {"verdict": "ok"}'
    assert extract_first_json(text) == {'verdict': 'ok'}


def test_two_objects_returns_first():
    assert extract_first_json('{"a": 1} {"b": 2}') == {'a': 1}


def test_non_dict_top_level_skipped():
    assert extract_first_json('[1, 2, 3]') is None
    assert extract_first_json('nothing here') is None
    assert extract_first_json('') is None
    assert extract_first_json(None) is None


# ── complete_truncated_json ──────────────────────────────────────────────────

def test_truncated_after_nested_object_and_comma():
    from backend.agent_runtime.llm_json import complete_truncated_json
    # the live CMP failure: budget ran out right after new_path
    text = ('{\n  "route": "indep_branch",\n  "target": null,\n'
            '  "new_path": {\n    "title": "Info Acara X",\n'
            '    "action": "find info"\n  },')
    assert complete_truncated_json(text) == {
        'route': 'indep_branch', 'target': None,
        'new_path': {'title': 'Info Acara X', 'action': 'find info'}}


def test_truncated_mid_string_and_mid_value():
    from backend.agent_runtime.llm_json import complete_truncated_json
    assert complete_truncated_json('{"route": "continue", "card": {"outcome": "halfway don') \
        == {'route': 'continue', 'card': {'outcome': 'halfway don'}}
    assert complete_truncated_json('{"route": "return", "target":') \
        == {'route': 'return', 'target': None}
    assert complete_truncated_json('{"route": "continue", "card": {"new_facts": ["a", "b"') \
        == {'route': 'continue', 'card': {'new_facts': ['a', 'b']}}


def test_truncated_repair_gives_up_safely():
    from backend.agent_runtime.llm_json import complete_truncated_json
    assert complete_truncated_json('{"rou') is None          # dangling bare key
    assert complete_truncated_json('{"a": 1}') is None       # complete object
    assert complete_truncated_json('no braces at all') is None
    assert complete_truncated_json('') is None
    assert complete_truncated_json(None) is None
