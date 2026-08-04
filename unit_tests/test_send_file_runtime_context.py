"""Regression coverage for send_file policy propagation into tool contexts."""

import ast
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_POLICY_FIELD = "send_file_allowed_path_regex"


def _assigned_dicts(path: str, variable_name: str):
    tree = ast.parse((_ROOT / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        if any(isinstance(target, ast.Name) and target.id == variable_name
               for target in node.targets):
            yield node.value


def _dict_string_keys(node: ast.Dict):
    return {
        key.value
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


@pytest.mark.parametrize(
    ("path", "variable_name"),
    [
        ("backend/agent_runtime/context.py", "agent_context"),
        ("backend/agent_runtime/runtime.py", "agent_context"),
        ("backend/agent_runtime/prefetch.py", "fresh_agent_context"),
    ],
)
def test_send_file_policy_is_propagated_to_tool_contexts(path, variable_name):
    contexts = list(_assigned_dicts(path, variable_name))

    assert contexts, f"No {variable_name} dictionary found in {path}"
    assert any(_POLICY_FIELD in _dict_string_keys(context) for context in contexts), (
        f"{path} does not propagate {_POLICY_FIELD} into {variable_name}"
    )
