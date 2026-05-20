"""Tests for should_skip_safety helper."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.tools.lib.safety_pipeline import should_skip_safety


def test_none_agent_returns_false():
    assert should_skip_safety(None) is False


def test_empty_agent_returns_false():
    assert should_skip_safety({}) is False


def test_explicit_true_returns_true():
    assert should_skip_safety({'_skip_safety': True}) is True


def test_string_true_returns_false():
    """LLM might try to set _skip_safety to the string 'true'."""
    assert should_skip_safety({'_skip_safety': 'true'}) is False
    assert should_skip_safety({'_skip_safety': 'True'}) is False


def test_integer_one_returns_false():
    """LLM might try to set _skip_safety to 1."""
    assert should_skip_safety({'_skip_safety': 1}) is False


def test_empty_string_returns_false():
    assert should_skip_safety({'_skip_safety': ''}) is False


def test_empty_dict_returns_false():
    """LLM might try to set _skip_safety to {}."""
    assert should_skip_safety({'_skip_safety': {}}) is False


def test_empty_list_returns_false():
    assert should_skip_safety({'_skip_safety': []}) is False


def test_false_boolean_returns_false():
    assert should_skip_safety({'_skip_safety': False}) is False


def test_missing_key_returns_false():
    assert should_skip_safety({'session_id': 'abc'}) is False


def test_other_keys_ignored():
    """Only _skip_safety matters; other keys don't affect the result."""
    assert should_skip_safety({
        '_skip_safety': True,
        'session_id': 'abc',
        'is_super': True,
    }) is True
