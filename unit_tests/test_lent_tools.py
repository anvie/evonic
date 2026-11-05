"""A plugin may lend its own tools to a message, and to the session that message belongs to.

``_apply_lent_tools`` merges two sources: ``metadata['lend_tools']`` carried by the message, and the grants
remembered for ``session_id``. Both are validated by ``backend.agent_runtime.session_tools``: an id must be
``plugin:<owner>:<fn>`` of the plugin named in ``lend_tools_from``, that plugin must be enabled, its backend
must exist, a core tool may never be shadowed, and only an agent-to-agent session may hold a grant.

All of this runs in an agent worker thread, with no Flask application context — which is the defect these
tests pin down: ``plugin_manager.get_plugin`` reads ``current_app.url_map`` and used to kill every turn
that carried lent tools.
"""

import os
import sys
import time

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
    """A fake plugin registry: ``privnt`` enabled, with a backend for each of its tools."""
    from backend.plugin_manager import plugin_manager

    state = {'enabled': True, 'defs': list(DEFS), 'backends': {'privnt_digest_queue', 'privnt_other'}}

    monkeypatch.setattr(plugin_manager, 'is_plugin_enabled', lambda pid: state['enabled'] and pid == OWNER)
    monkeypatch.setattr(plugin_manager, 'get_all_plugin_tool_defs', lambda: [dict(d) for d in state['defs']])
    monkeypatch.setattr(plugin_manager, 'find_plugin_tool_backend',
                        lambda name, pid=None: ((f'/plugins/{pid}/backend/tools/{name}.py', pid)
                                                if name in state['backends'] else (None, None)))
    return state


@pytest.fixture
def store(monkeypatch):
    """A fake grant table and session row, on the real ``db`` singleton."""
    from models.db import db

    grants = {}
    session = {'external_user_id': '__agent__privnt', 'agent_id': 'aisyah'}

    def grant_session_tools(session_id, tool_ids, owner, agent_id=None, granted_by=None,
                            ttl_seconds=259200):
        for tool_id in tool_ids:
            grants.setdefault(session_id, {})[tool_id] = {
                'session_id': session_id, 'tool_id': tool_id, 'owner': owner,
                'granted_by': granted_by, 'expires_at': time.time() + ttl_seconds,
            }
        return list(tool_ids)

    def list_session_tool_grants(session_id, agent_id=None):
        rows = grants.get(session_id, {}).values()
        return [dict(r) for r in rows if r['expires_at'] > time.time()]

    def revoke_session_tools(session_id, agent_id=None, tool_ids=None):
        rows = grants.get(session_id, {})
        removed = 0
        for tool_id in (tool_ids or list(rows)):
            if rows.pop(tool_id, None) is not None:
                removed += 1
        return removed

    def get_session_with_details(session_id, **kwargs):
        return dict(session)

    monkeypatch.setattr(db, 'grant_session_tools', grant_session_tools)
    monkeypatch.setattr(db, 'list_session_tool_grants', list_session_tool_grants)
    monkeypatch.setattr(db, 'revoke_session_tools', revoke_session_tools)
    monkeypatch.setattr(db, 'get_session_with_details', get_session_with_details)
    return {'grants': grants, 'session': session}


def lend(names, owner=OWNER):
    return {'lend_tools': list(names), 'lend_tools_from': owner}


def names(tools):
    return [t['function']['name'] for t in tools]


def test_without_metadata_nothing_is_lent(plugins):
    context, tools = {'id': 'aisyah'}, [{'type': 'function', 'function': {'name': 'remember'}}]
    for metadata in (None, {}, {'lend_tools': []}, {'lend_tools': [QUEUE]}, {'lend_tools_from': OWNER}):
        ids, out = _apply_lent_tools(context, ['remember'], list(tools), metadata)
        assert ids == ['remember']
        assert names(out) == ['remember']
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
    all_ids = [f'plugin:privnt:t{i}' for i in range(LEND_TOOLS_MAX + 4)]
    plugins['defs'] = [{'id': n, 'function': {'name': n.rsplit(':', 1)[1]}} for n in all_ids]
    plugins['backends'] = {n.rsplit(':', 1)[1] for n in all_ids}
    ids, tools = _apply_lent_tools({}, [], [], lend(all_ids))
    assert len(tools) == LEND_TOOLS_MAX == len(ids)


def test_lending_needs_no_flask_application_context(plugins, monkeypatch):
    """Regression: the UI accessor ``get_plugin`` must not be on this path — it needs ``current_app``."""
    from backend.plugin_manager import plugin_manager

    def outside_app_context(plugin_id):
        raise RuntimeError('Working outside of application context.')

    monkeypatch.setattr(plugin_manager, 'get_plugin', outside_app_context)
    ids, tools = _apply_lent_tools({}, [], [], lend([QUEUE]))
    assert ids == [QUEUE] and names(tools) == ['privnt_digest_queue']


def test_a_failing_grant_never_breaks_the_turn(plugins, store, monkeypatch):
    from models.db import db

    def boom(*args, **kwargs):
        raise RuntimeError('database is locked')

    monkeypatch.setattr(db, 'grant_session_tools', boom)
    ids, tools = _apply_lent_tools({}, [], [], lend([QUEUE]), session_id='s1', agent_id='aisyah')
    assert ids == [QUEUE] and names(tools) == ['privnt_digest_queue']    # the turn goes on


def test_a_session_grant_is_honoured_on_a_later_turn(plugins, store):
    first = _apply_lent_tools({'id': 'aisyah'}, [], [], lend([QUEUE]),
                              session_id='s1', agent_id='aisyah')
    assert first[0] == [QUEUE]
    assert set(store['grants']['s1']) == {QUEUE}                        # remembered for the session

    context = {'id': 'aisyah'}
    ids, tools = _apply_lent_tools(context, ['remember'], [], {}, session_id='s1', agent_id='aisyah')
    assert ids == ['remember', QUEUE] and names(tools) == ['privnt_digest_queue']
    assert context['assigned_tool_ids'] == ['remember', QUEUE]

    # ... and another session of the same agent has nothing
    other, tools = _apply_lent_tools({}, [], [], {}, session_id='s2', agent_id='aisyah')
    assert other == [] and tools == []


def test_an_expired_grant_is_gone(plugins, store):
    _apply_lent_tools({}, [], [], lend([QUEUE]), session_id='s1', agent_id='aisyah')
    store['grants']['s1'][QUEUE]['expires_at'] = time.time() - 1
    ids, tools = _apply_lent_tools({}, [], [], {}, session_id='s1', agent_id='aisyah')
    assert ids == [] and tools == []


def test_a_human_session_cannot_hold_a_grant(plugins, store):
    store['session']['external_user_id'] = '628123456789@s.whatsapp.net'
    ids, tools = _apply_lent_tools({}, [], [], lend([QUEUE]), session_id='s1', agent_id='aisyah')
    assert ids == [QUEUE] and names(tools) == ['privnt_digest_queue']    # this turn only
    assert store['grants'] == {}                                        # never remembered

    later = _apply_lent_tools({}, [], [], {}, session_id='s1', agent_id='aisyah')
    assert later == ([], [])


def test_a_disabled_plugin_takes_its_lent_tools_away(plugins, store):
    _apply_lent_tools({}, [], [], lend([QUEUE]), session_id='s1', agent_id='aisyah')
    plugins['enabled'] = False
    ids, tools = _apply_lent_tools({}, [], [], {}, session_id='s1', agent_id='aisyah')
    assert ids == [] and tools == []
    assert store['grants']['s1'] == {}                                  # the dead grant is dropped


def test_a_grant_can_be_revoked(plugins, store):
    from backend.agent_runtime import session_tools

    _apply_lent_tools({}, [], [], lend([QUEUE]), session_id='s1', agent_id='aisyah')
    assert session_tools.revoke('s1', 'aisyah') == 1
    assert _apply_lent_tools({}, [], [], {}, session_id='s1', agent_id='aisyah') == ([], [])
