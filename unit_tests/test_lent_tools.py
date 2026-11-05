"""A plugin may lend its own tools to one message; nothing else may be lent.

``_apply_lent_tools`` reads ``metadata['lend_tools']`` and adds those plugin
tools to the turn's tool list (and to ``assigned_tool_ids`` so the
authorization guard lets them run). The owner named in ``lend_tools_from``
must match the tool's plugin, that plugin must be enabled and its backend must
exist. Built-ins are never lent.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.agent_runtime.runtime import LEND_TOOLS_MAX, _apply_lent_tools

OWNER = 'privnt'
QUEUE = 'plugin:privnt:privnt_digest_queue'
DEFS = [
    {'id': QUEUE, 'function': {'name': 'privnt_digest_queue', 'description': 'queue'}},
    {'id': 'plugin:privnt:privnt_other', 'function': {'name': 'privnt_other', 'description': 'other'}},
    {'id': 'plugin:garden:water', 'function': {'name': 'water', 'description': 'not ours'}},
]


@pytest.fixture
def plugins(monkeypatch):
    """A fake plugin manager: ``privnt`` enabled with a backend for each of its tools."""
    from backend.plugin_manager import plugin_manager

    state = {'enabled': True, 'defs': list(DEFS), 'backends': {'privnt_digest_queue', 'privnt_other'}}

    monkeypatch.setattr(plugin_manager, 'get_plugin', lambda pid: {'id': pid, 'enabled': state['enabled']})
    monkeypatch.setattr(plugin_manager, 'get_all_plugin_tool_defs', lambda: [dict(d) for d in state['defs']])
    monkeypatch.setattr(plugin_manager, 'find_plugin_tool_backend',
                        lambda name, pid=None: ((f'/plugins/{pid}/backend/tools/{name}.py', pid)
                                                if name in state['backends'] else (None, None)))
    return state


def lend(names, owner=OWNER):
    return {'lend_tools': list(names), 'lend_tools_from': owner}


def test_without_metadata_nothing_is_lent(plugins):
    context, tools = {'id': 'aisyah'}, [{'type': 'function', 'function': {'name': 'remember'}}]
    for metadata in (None, {}, {'lend_tools': []}, {'lend_tools': [QUEUE]}, {'lend_tools_from': OWNER}):
        ids, out = _apply_lent_tools(context, ['remember'], list(tools), metadata)
        assert ids == ['remember']
        assert [t['function']['name'] for t in out] == ['remember']
        assert 'assigned_tool_ids' not in context


def test_the_lent_tool_is_advertised_and_authorized_for_the_turn(plugins):
    context = {'id': 'aisyah'}
    ids, tools = _apply_lent_tools(context, ['remember'], [], lend([QUEUE]))
    assert ids == ['remember', QUEUE]
    assert context['assigned_tool_ids'] == ['remember', QUEUE]        # the guard reads this
    assert tools == [{'type': 'function', 'function': DEFS[0]['function']}]


def test_a_tool_of_another_plugin_is_refused(plugins):
    context = {'id': 'aisyah'}
    ids, tools = _apply_lent_tools(context, ['remember'], [], lend(['plugin:garden:water']))
    assert ids == ['remember'] and tools == []
    assert 'assigned_tool_ids' not in context


def test_a_bare_function_name_is_not_an_id(plugins):
    ids, tools = _apply_lent_tools({}, [], [], lend(['privnt_digest_queue']))
    assert ids == [] and tools == []


def test_a_disabled_plugin_lends_nothing(plugins):
    plugins['enabled'] = False
    ids, tools = _apply_lent_tools({}, [], [], lend([QUEUE]))
    assert ids == [] and tools == []


def test_a_tool_without_a_backend_is_refused(plugins):
    plugins['backends'] = set()
    ids, tools = _apply_lent_tools({}, [], [], lend([QUEUE]))
    assert ids == [] and tools == []


def test_a_builtin_is_never_lent(plugins):
    plugins['defs'] = [{'id': 'plugin:privnt:bash', 'function': {'name': 'bash', 'description': 'shell'}}]
    plugins['backends'] = {'bash'}
    ids, tools = _apply_lent_tools({}, [], [], lend(['plugin:privnt:bash']))
    assert ids == [] and tools == []


def test_a_name_already_in_the_turn_is_not_added_twice(plugins):
    present = [{'type': 'function', 'function': {'name': 'privnt_digest_queue'}}]
    ids, tools = _apply_lent_tools({}, [QUEUE], present, lend([QUEUE]))
    assert ids == [QUEUE] and tools == present


def test_the_list_is_capped(plugins):
    names = [f'plugin:privnt:t{i}' for i in range(LEND_TOOLS_MAX + 4)]
    plugins['defs'] = [{'id': n, 'function': {'name': n.rsplit(':', 1)[1]}} for n in names]
    plugins['backends'] = {n.rsplit(':', 1)[1] for n in names}
    ids, tools = _apply_lent_tools({}, [], [], lend(names))
    assert len(tools) == LEND_TOOLS_MAX == len(ids)
