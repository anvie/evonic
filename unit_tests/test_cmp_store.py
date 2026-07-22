"""Tests for the CMP path store (pure dict ops, no I/O/LLM)."""

import json

import pytest

from backend.agent_runtime.cmp import store
from backend.agent_state import AgentState


def _ms(mode='execute', plan_file='plan/p1.md', atg=None):
    ms = AgentState(mode=mode)
    ms.plan_file = plan_file
    ms.atg = atg
    return ms


def _init(ms=None, ts=1000):
    ms = ms or _ms()
    ms.cmp = store.new_cmp(ms, title='first task', goal='do the first thing',
                           now_ts=ts)
    return ms


# ── Init / create ────────────────────────────────────────────────────────────

def test_new_cmp_first_path():
    ms = _init(ts=1000)
    cmp = ms.cmp
    assert cmp['active_id'] == 'A1'
    p1 = cmp['paths']['A1']
    assert p1['status'] == 'active'
    assert p1['segments'] == [[999, None]]
    assert p1['title'] == 'first task'


def test_create_path_branches_and_resets_task_state():
    atg = {'status': 'done', 'dag': {'root_goal': 'g'}}
    ms = _init(_ms(mode='execute', plan_file='plan/old.md', atg=atg), ts=1000)
    record = store.create_path(ms.cmp, ms, 'invoice task', goal='make invoice',
                               depends_on=['A1'], now_ts=2000)

    assert record['id'] == 'B1'  # dependency child → next level letter
    assert ms.cmp['active_id'] == 'B1'
    assert record['depends_on'] == ['A1']
    assert record['segments'] == [[1999, None]]

    # old path suspended: segment closed at turn boundary, state snapshotted
    p1 = ms.cmp['paths']['A1']
    assert p1['status'] == 'preserved'
    assert p1['segments'] == [[999, 1999]]
    assert p1['mode'] == 'execute'
    assert p1['plan_file'] == 'plan/old.md'
    assert p1['atg'] == atg

    # ms reset to a fresh plan cycle (the re-arm mutations)
    assert ms.mode == 'plan'
    assert ms.plan_file is None
    assert ms.atg is None
    assert ms.auto_trivial is False


def test_create_trivial_path_starts_execute_and_round_trips_state():
    ms = _init(_ms(mode='plan', plan_file='plan/old.md'), ts=1000)
    record = store.create_path(ms.cmp, ms, 'push dev', now_ts=2000,
                               trivial=True)
    assert ms.mode == 'execute' and ms.auto_trivial is True
    assert ms.plan_file is None and ms.atg is None

    store.switch_to(ms.cmp, ms, 'A1', now_ts=3000)
    assert ms.mode == 'plan' and ms.auto_trivial is False
    assert ms.plan_file == 'plan/old.md'
    store.switch_to(ms.cmp, ms, record['id'], now_ts=4000)
    assert ms.mode == 'execute' and ms.auto_trivial is True


def test_create_path_unknown_dependency_raises():
    ms = _init()
    with pytest.raises(ValueError):
        store.create_path(ms.cmp, ms, 'x', depends_on=['Z9'])


# ── Switch / snapshot round-trip ─────────────────────────────────────────────

def test_switch_restores_task_state_round_trip():
    atg1 = {'status': 'executing', 'dag': {'root_goal': 'task one',
                                           'nodes': {'n1': {'status': 'done'}}}}
    ms = _init(_ms(mode='execute', plan_file='plan/one.md', atg=atg1), ts=1000)
    store.create_path(ms.cmp, ms, 'task two', now_ts=2000)
    ms.mode = 'execute'
    ms.plan_file = 'plan/two.md'
    ms.atg = {'status': 'compiled', 'dag': {'root_goal': 'task two'}}

    target = store.switch_to(ms.cmp, ms, 'A1', now_ts=3000)

    # P1's full state restored verbatim — fixes the single-slot ms.atg
    assert ms.mode == 'execute'
    assert ms.plan_file == 'plan/one.md'
    assert ms.atg == atg1
    # stored snapshot cleared while live (no double truth)
    assert target['atg'] is None and target['plan_file'] is None
    # A2 snapshotted
    p2 = ms.cmp['paths']['A2']
    assert p2['status'] == 'preserved'
    assert p2['plan_file'] == 'plan/two.md'
    assert p2['atg']['status'] == 'compiled'
    # segments: P1 reopened, P2 closed
    assert ms.cmp['paths']['A1']['segments'] == [[999, 1999], [2999, None]]
    assert p2['segments'] == [[1999, 2999]]
    assert ms.cmp['active_id'] == 'A1'


def test_switch_to_unknown_or_active_raises():
    ms = _init()
    with pytest.raises(ValueError):
        store.switch_to(ms.cmp, ms, 'Z9')
    with pytest.raises(ValueError):
        store.switch_to(ms.cmp, ms, 'A1')  # already active
    # error message lists valid targets
    store.create_path(ms.cmp, ms, 'second')
    try:
        store.switch_to(ms.cmp, ms, 'Z9')
    except ValueError as e:
        assert 'A1' in str(e)


# ── Lifecycle (full → preserved → archived → pruned) / caps ──────────────────


def test_preserved_archives_when_over_max_cap():
    """When more than MAX_PRESERVED paths are preserved, the oldest ones
    (by state_since) get archived — the cap is strictly enforced."""
    ms = _init(ts=1000)
    # Create MAX_PRESERVED more independent paths → MAX_PRESERVED preserved + 1 active
    for i in range(2, store.MAX_PRESERVED + 3):
        store.create_path(ms.cmp, ms, f'path{i}', now_ts=i * 1000)
    # MAX_PRESERVED+1 preserved > cap → oldest A1 must archive
    last_id = f'A{store.MAX_PRESERVED + 2}'
    result = store.tick_lifecycle(ms.cmp, now_ts=(store.MAX_PRESERVED + 3) * 1000)
    assert result == {'archived': ['A1'], 'pruned': []}
    assert ms.cmp['paths']['A1']['status'] == 'archived'
    for i in range(2, store.MAX_PRESERVED + 2):
        assert ms.cmp['paths'][f'A{i}']['status'] == 'preserved'
    assert ms.cmp['paths'][last_id]['status'] == 'active'
    # atg snapshot cleared on archive
    assert ms.cmp['paths']['A1']['atg'] is None


def test_archived_prunes_after_three_days():
    ms = _init(ts=1000)
    store.create_path(ms.cmp, ms, 'second', now_ts=2000)
    t_arch = 2100   # hand-craft A1 as archived (preserved cap won't archive it)
    store._set_status(ms.cmp['paths']['A1'], 'archived', t_arch)
    store.tick_lifecycle(ms.cmp, now_ts=t_arch + store.ARCHIVED_TTL_MS)
    assert 'A1' in ms.cmp['paths']                             # exactly at TTL: kept
    result = store.tick_lifecycle(ms.cmp, now_ts=t_arch + store.ARCHIVED_TTL_MS + 1)
    assert result['pruned'] == ['A1']
    assert 'A1' not in ms.cmp['paths']                         # record fully removed


def test_prune_blocked_while_a_living_path_depends_on_it():
    """Pruning a referenced parent would re-root the child's established
    edge — dead subtrees must decay bottom-up instead."""
    ms = _init(ts=1000)
    store.create_path(ms.cmp, ms, 'child', depends_on=['A1'], now_ts=2000)
    store.create_path(ms.cmp, ms, 'other', now_ts=3000)        # B1 suspended
    ms.cmp['paths']['A1'].update(status='archived', state_since=3000)
    result = store.tick_lifecycle(ms.cmp, now_ts=3001 + store.ARCHIVED_TTL_MS)
    assert result['pruned'] == []                              # A1 protected by B1
    assert 'A1' in ms.cmp['paths']
    # once the child is gone, the parent can prune
    del ms.cmp['paths']['B1']
    result = store.tick_lifecycle(ms.cmp, now_ts=3002 + store.ARCHIVED_TTL_MS)
    assert result['pruned'] == ['A1']


def test_reactivation_resets_the_lifecycle_clock():
    ms = _init(ts=1000)
    N = store.MAX_PRESERVED
    # Create N more paths → N preserved exactly at cap (none archived)
    for i in range(2, N + 2):
        store.create_path(ms.cmp, ms, f'path{i}', now_ts=i * 1000)
    # Manually archive A1 to simulate lifecycle action
    store._set_status(ms.cmp['paths']['A1'], 'archived', (N + 1) * 1000)
    assert ms.cmp['paths']['A1']['status'] == 'archived'

    store.switch_to(ms.cmp, ms, 'A1', now_ts=5200)           # reactivate
    p1 = ms.cmp['paths']['A1']
    assert p1['status'] == 'active' and p1['state_since'] == 5200

    store.create_path(ms.cmp, ms, 'extra', now_ts=5300)      # suspend A1 again
    assert p1['status'] == 'preserved'                       # NOT archived (fresh clock)
    # A1(5300) freshest, A2(2000) oldest → A2 archived
    result = store.tick_lifecycle(ms.cmp, now_ts=5400)
    assert result == {'archived': ['A2'], 'pruned': []}
    assert p1['status'] == 'preserved'  # A1 still preserved (fresh clock)


def test_restore_lineage_promotes_archived_ancestors_within_three_hops():
    """A1 <- B1 <- C1 <- D1 <- E1(active): archived ancestors within 3 hops
    of the active node (B1, C1, D1) restore to preserved; the 4th hop (A1)
    stays archived."""
    ms = _init(ts=1000)
    for title, dep in (('b', 'A1'), ('c', 'B1'), ('d', 'C1'), ('e', 'D1')):
        store.create_path(ms.cmp, ms, title, depends_on=[dep], now_ts=2000)
    for pid in ('A1', 'B1', 'C1', 'D1'):
        ms.cmp['paths'][pid].update(status='archived', state_since=2000)
    promoted = store.restore_lineage(ms.cmp, 'E1', now_ts=3000)
    assert sorted(promoted) == ['B1', 'C1', 'D1']
    assert ms.cmp['paths']['A1']['status'] == 'archived'       # 4th hop untouched
    for pid in ('B1', 'C1', 'D1'):
        assert ms.cmp['paths'][pid]['status'] == 'preserved'
        assert ms.cmp['paths'][pid]['state_since'] == 3000


def test_active_lineage_is_exempt_from_archiving():
    ms = _init(ts=1000)
    store.create_path(ms.cmp, ms, 'child', depends_on=['A1'], now_ts=2000)
    # B1 active @2000, A1 preserved @2000 (B1's parent → protected)
    N = store.MAX_PRESERVED
    for i in range(1, N + 2):
        store.create_path(ms.cmp, ms, f'z{i}', now_ts=3000 + i * 1000)

    # Switch back to B1 (which depends on A1) → A1 is a protected ancestor
    store.switch_to(ms.cmp, ms, 'B1', now_ts=(N + 3) * 1000 + 100)
    # B1 active, protected={A1}. Unprotected preserved = A2..A{N+2} = N+1 > cap → oldest A2 archived
    result = store.tick_lifecycle(ms.cmp, now_ts=(N + 3) * 1000 + 200)
    assert 'A1' not in result['archived']
    assert result['archived'] == ['A2']
    assert ms.cmp['paths']['A1']['status'] == 'preserved'


def test_legacy_dormant_status_normalizes():
    ms = _init(ts=1000)
    N = store.MAX_PRESERVED
    for i in range(2, N + 2):
        store.create_path(ms.cmp, ms, f'path{i}', now_ts=i * 1000)
    # N preserved, exactly at cap — no archiving
    p1 = ms.cmp['paths']['A1']
    p1['status'] = 'dormant'                                   # pre-lifecycle data
    del p1['state_since']
    assert store.path_status(p1) == 'preserved'
    # ticks treat it as preserved, with last_active as the clock fallback
    result = store.tick_lifecycle(ms.cmp, now_ts=(N + 1) * 1000 + 100)
    assert result['archived'] == []

    # Create one more to push over cap → A1 (oldest, with last_active fallback) archived
    store.create_path(ms.cmp, ms, 'extra', now_ts=(N + 2) * 1000)
    result = store.tick_lifecycle(ms.cmp, now_ts=(N + 2) * 1000 + 100)
    assert result['archived'] == ['A1']
    assert p1['status'] == 'archived'


def test_caps_fully_remove_oldest_archived():
    ms = _init(ts=1000)
    for i in range(store.MAX_PATHS + 3):
        store.create_path(ms.cmp, ms, f'task {i}', now_ts=2000 + i)
    # preserved paths are never cap-removed, so the graph can exceed the cap
    assert len(ms.cmp['paths']) == store.MAX_PATHS + 4
    for path in ms.cmp['paths'].values():
        if path['status'] == 'preserved':
            path['status'] = 'archived'
    store.enforce_caps(ms.cmp)
    assert len(ms.cmp['paths']) == store.MAX_PATHS      # prune semantics: removed
    assert 'A1' not in ms.cmp['paths']                  # oldest archived went first


def test_card_field_caps():
    card = store.clamp_card_fields({
        'title': 'x' * 100, 'goal': 'g' * 500, 'outcome': 'o' * 500,
        'key_facts': [f'f{i}' * 200 for i in range(10)],
        'artifacts': [f'a{i}' for i in range(20)],
    })
    assert len(card['title']) == store.TITLE_MAX
    assert len(card['goal']) == store.GOAL_MAX
    assert len(card['key_facts']) == store.KEY_FACTS_MAX
    assert all(len(f) <= store.KEY_FACT_CHARS for f in card['key_facts'])
    assert len(card['artifacts']) == store.ARTIFACTS_MAX


# ── Serialization ────────────────────────────────────────────────────────────

def test_cmp_survives_agent_state_round_trip():
    ms = _init(_ms(atg={'status': 'done'}), ts=1000)
    store.create_path(ms.cmp, ms, 'second', now_ts=2000)
    restored = AgentState.deserialize(ms.serialize())
    assert restored.cmp == json.loads(json.dumps(ms.cmp))
    assert restored.cmp['active_id'] == 'A2'


def test_dependency_ancestors_bounded():
    ms = _init(ts=1000)
    store.create_path(ms.cmp, ms, 'b', depends_on=['A1'], now_ts=2000)
    store.create_path(ms.cmp, ms, 'c', depends_on=['B1'], now_ts=3000)
    assert store.dependency_ancestors(ms.cmp, 'C1') == ['B1', 'A1']
    assert store.dependency_ancestors(ms.cmp, 'C1', max_depth=1) == ['B1']
    assert store.dependency_ancestors(ms.cmp, 'A1') == []


# ── apply_card_delta (single-pass turn op, deterministic layer) ──────────────

def test_apply_card_delta_updates_only_card_content():
    ms = _init(ts=1000)
    p1 = ms.cmp['paths']['A1']
    p1['action'] = 'do first'
    before = {k: p1[k] for k in ('id', 'title', 'action', 'depends_on',
                                 'segments', 'status')}
    store.apply_card_delta(p1, {
        'outcome': 'draft reviewed by user',
        'new_facts': ['user wants weekly cadence'],
        'new_artifacts': ['/out/report.pdf'],
        # hostile fields the delta op must ignore (node identity is immutable)
        'title': 'RENAMED', 'action': 'renamed', 'id': 'Z9',
        'depends_on': ['Z9'], 'status': 'archived',
    })
    assert p1['outcome'] == 'draft reviewed by user'
    assert p1['key_facts'] == ['user wants weekly cadence']
    assert p1['artifacts'] == ['/out/report.pdf']
    for key, val in before.items():
        assert p1[key] == val, key


def test_apply_card_delta_appends_dedupes_and_caps():
    ms = _init(ts=1000)
    p1 = ms.cmp['paths']['A1']
    store.apply_card_delta(p1, {'new_facts': ['fact 1', 'fact 2']})
    store.apply_card_delta(p1, {'new_facts': ['fact 2', 'fact 3']})  # dedupe
    assert p1['key_facts'] == ['fact 1', 'fact 2', 'fact 3']
    # overflow keeps the newest facts
    store.apply_card_delta(p1, {'new_facts': [f'fact {i}' for i in range(4, 10)]})
    assert len(p1['key_facts']) == store.KEY_FACTS_MAX
    assert p1['key_facts'][-1] == 'fact 9' and 'fact 1' not in p1['key_facts']
    # empty outcome never clobbers the existing one
    p1['outcome'] = 'standing outcome'
    store.apply_card_delta(p1, {'outcome': '', 'new_facts': None})
    assert p1['outcome'] == 'standing outcome'


def test_apply_card_delta_clamps_and_survives_garbage():
    ms = _init(ts=1000)
    p1 = ms.cmp['paths']['A1']
    store.apply_card_delta(p1, {'outcome': 'o' * 999,
                                'new_facts': ['f' * 999, '', 42],
                                'new_artifacts': 'not-a-list'})
    assert len(p1['outcome']) == store.OUTCOME_MAX
    assert p1['key_facts'] == ['f' * store.KEY_FACT_CHARS, '42']
    assert p1['artifacts'] == []
    store.apply_card_delta(p1, None)          # non-dict deltas are no-ops
    store.apply_card_delta(p1, 'garbage')


def test_transcript_search_prioritizes_user_evidence_and_cumulative_overlap(monkeypatch):
    cmp = {'active_id': 'A4', 'paths': {
        'A1': {'id': 'A1'}, 'A2': {'id': 'A2'},
        'A3': {'id': 'A3'}, 'A4': {'id': 'A4'},
    }}
    entries = {
        'A1': [{'type': 'final', 'content': 'store clothing return pickup items'}],
        'A2': [{'type': 'user', 'content': 'pick up dry cleaning from the store'}],
        'A3': [
            {'type': 'user', 'content': 'return boots to the store'},
            {'type': 'user', 'content': 'pick up replacement clothing'},
        ],
    }
    monkeypatch.setattr('backend.agent_runtime.cmp.compactor.collect_path_entries',
                        lambda _, path: entries.get(path['id'], []))

    hits = store.search_cmp_transcripts(
        cmp, object(), 'How many clothing items do I pick up or return from a store?')

    assert [h['id'] for h in hits] == ['A3', 'A2', 'A1']
    assert hits[0]['excerpts'] == ['return boots to the store',
                                    'pick up replacement clothing']
