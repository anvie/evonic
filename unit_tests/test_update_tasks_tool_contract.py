"""Regression tests for the model-facing ``update_tasks`` contract."""

from backend.tools.registry import _builtin_update_tasks_factory


def test_update_tasks_contract_requires_atomic_entries():
    """The tool prompt must tell models to split independently completable work."""
    tool_def, _ = _builtin_update_tasks_factory({})
    function = tool_def["function"]
    properties = function["parameters"]["properties"]

    description = function["description"].lower()
    assert "exactly one concrete action or outcome" in description
    assert "completed independently" in description
    assert "split multi-action work into separate entries" in description
    assert "never batch several actions into one task" in description
    assert "only one implementation task may be in_progress at a time" in description

    actions = properties["action"]["enum"]
    assert "replace" in actions
    action_description = properties["action"]["description"].lower()
    assert "sole active task" in action_description
    assert "returns every other active task to pending" in action_description
    assert "preserving its id and status" in action_description

    add_description = properties["text"]["description"].lower()
    assert "one atomic task" in add_description
    assert "independently completable" in add_description

    tasks_schema = properties["tasks"]
    assert "split multi-action work" in tasks_schema["description"].lower()
    item_description = tasks_schema["items"]["description"].lower()
    assert "exactly one concrete" in item_description
    assert "independently completable" in item_description
