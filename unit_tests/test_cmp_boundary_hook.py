"""Tests for the CMP per-turn orchestrator (on_turn_boundary) and its
coexistence with the ATG re-arm."""

import pytest
from unittest.mock import patch

from backend.agent_runtime.cmp import on_turn_boundary, store
from backend.agent_state import AgentState


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail unmocked classifier calls fast so tests never use the network."""
    monkeypatch.setattr('backend.agent_runtime.cmp.detector._call_turn_llm',
                        lambda *a, **k: None)
    monkeypatch.setattr('backend.task_classifier.classify_task',
                        lambda text: 'complex')


AGENT = {'id': 'a1', 'enable_cmp': 1, 'enable_agent_state': 1}
LONG_NEW_TASK = ('please build a completely new scraper project under /tmp/scraper '
                 'that collects product prices daily')


class FakeChatlog:
    def __init__(self, user_ts=5000, entries=None):
        self.user_ts = user_ts
        self.entries = entries or []

    def get_last_entry(self, types=None):
        return {'type': 'user', 'ts': self.user_ts}

    def tail(self, limit=24, to_ts=None):
        return self.entries[-limit:]

    def get_entries_between_ts(self, a, b):
        return [e for e in self.entries if a <= e.get('ts', 0) <= b]

    def get_entries_after_ts(self, a):
        return [e for e in self.entries if e.get('ts', 0) > a]


def _detect(decision, target=None, layer='LLM', new_path=None, card_delta=None):
    return patch('backend.agent_runtime.cmp.detector.detect',
                 return_value={'decision': decision, 'target': target,
                               'layer': layer, 'new_path': new_path,
                               'card_delta': card_delta})


# ── Gates / init ─────────────────────────────────────────────────────────────

def test_none_when_not_enabled():
    assert on_turn_boundary({}, AgentState(), FakeChatlog(), 'x') is None
    assert on_turn_boundary({'enable_cmp': 1}, AgentState(), FakeChatlog(), 'x') is None
    assert on_turn_boundary({'enable_cmp': 1, 'enable_agent_state': 1,
                             'is_subagent': True}, AgentState(),
                            FakeChatlog(), 'x') is None


def test_first_turn_initializes_p1_from_current_work():
    ms = AgentState(mode='execute')
    ms.atg = {'status': 'executing', 'dag': {'root_goal': 'build the website'}}
    result = on_turn_boundary(AGENT, ms, FakeChatlog(user_ts=7000), 'lanjutkan')
    assert result['decision'] == 'init'
    p1 = ms.cmp['paths']['A1']
    assert p1['title'] == 'build the website'
    assert p1['segments'] == [[6999, None]]
    # init adopts, never disturbs, the live task state
    assert ms.atg['status'] == 'executing'


def test_first_turn_names_p1_from_the_turn_envelope():
    """No ongoing work to adopt → the single-pass call (init mode) names A1;
    the raw message stays only as mechanical fallback."""
    ms = AgentState(mode='execute')
    with _detect('continue', new_path={'title': 'Laporan Mingguan',
                                       'action': 'create report'}) as det:
        result = on_turn_boundary(AGENT, ms, FakeChatlog(),
                                  'buatkan laporan mingguan Perusahaan A dong')
    assert result['decision'] == 'init'
    assert det.call_args[1]['initializing'] is True
    p1 = ms.cmp['paths']['A1']
    assert p1['title'] == 'Laporan Mingguan'
    assert p1['action'] == 'create report'


def test_first_turn_mechanical_title_when_naming_unavailable():
    ms = AgentState(mode='execute')
    result = on_turn_boundary(AGENT, ms, FakeChatlog(),
                              'buatkan laporan mingguan Perusahaan A dong')
    assert result['decision'] == 'init'
    assert ms.cmp['paths']['A1']['title'] == 'buatkan laporan mingguan Perusahaan A dong'


# ── Decisions drive path ops ─────────────────────────────────────────────────

def _session_with_two_paths():
    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(ms, title='website', now_ts=1000)
    store.create_path(ms.cmp, ms, 'server config', now_ts=2000)
    ms.mode = 'execute'
    return ms


def test_continue_keeps_fresh_paths_preserved():
    ms = _session_with_two_paths()
    with _detect('continue'):
        result = on_turn_boundary(AGENT, ms, FakeChatlog(), 'lanjut saja')
    assert result['decision'] == 'continue'
    assert ms.cmp['active_id'] == 'A2'
    assert ms.cmp['paths']['A1']['status'] == 'preserved'  # no time passed


def test_return_switches_and_finalizes_card():
    ms = _session_with_two_paths()
    ms.plan_file = 'plan/server.md'
    with _detect('return', 'A1'), \
         patch('backend.agent_runtime.cmp.compactor.finalize_active_card') as fin:
        result = on_turn_boundary(AGENT, ms, FakeChatlog(user_ts=9000),
                                  'balik ke website yang tadi ya')
    assert result['decision'] == 'return'
    assert ms.cmp['active_id'] == 'A1'
    fin.assert_called_once()  # outgoing card finalized before the switch
    # P2 snapshotted with its plan file
    assert ms.cmp['paths']['A2']['plan_file'] == 'plan/server.md'


def test_card_delta_applied_to_active_path():
    ms = _session_with_two_paths()
    delta = {'outcome': 'nginx configured', 'new_facts': ['listens on 8080'],
             'new_artifacts': ['/etc/nginx/nginx.conf']}
    with _detect('continue', card_delta=delta):
        on_turn_boundary(AGENT, ms, FakeChatlog(), 'lanjutkan config servernya ya')
    p2 = ms.cmp['paths']['A2']
    assert p2['outcome'] == 'nginx configured'
    assert 'listens on 8080' in p2['key_facts']
    assert '/etc/nginx/nginx.conf' in p2['artifacts']


def test_card_delta_lands_on_outgoing_path_before_switch():
    """The delta describes the just-completed turn on the still-active path —
    it must be applied before the switch suspends that path."""
    ms = _session_with_two_paths()
    with _detect('return', 'A1', card_delta={'outcome': 'server config selesai'}):
        on_turn_boundary(AGENT, ms, FakeChatlog(user_ts=9000),
                         'balik ke website yang tadi ya')
    assert ms.cmp['active_id'] == 'A1'
    assert ms.cmp['paths']['A2']['outcome'] == 'server config selesai'


def test_dep_branch_creates_dependent_plan_mode_path():
    ms = _session_with_two_paths()
    ms.atg = {'status': 'done'}
    with _detect('dep_branch', 'A1'):
        result = on_turn_boundary(AGENT, ms, FakeChatlog(user_ts=9000),
                                  'sekarang buatkan invoice untuk client A dari project itu')
    assert result['decision'] == 'dep_branch'
    new_id = result['target']
    assert ms.cmp['paths'][new_id]['depends_on'] == ['A1']
    # fresh plan cycle (this IS the re-arm)
    assert ms.mode == 'plan' and ms.atg is None and ms.plan_file is None
    assert ms.auto_trivial is False


def test_trivial_branch_starts_in_execute_mode_and_restores_state():
    ms = _session_with_two_paths()
    ms.plan_file = 'plan/server.md'
    with _detect('dep_branch', 'A2'), \
         patch('backend.task_classifier.classify_task', return_value='trivial'):
        result = on_turn_boundary(AGENT, ms, FakeChatlog(user_ts=9000),
                                  'now please push to origin dev')
    trivial_id = result['target']
    assert ms.mode == 'execute' and ms.auto_trivial is True
    assert ms.plan_file is None and ms.atg is None

    store.switch_to(ms.cmp, ms, 'A2', now_ts=10000)
    assert ms.mode == 'execute' and ms.auto_trivial is False
    assert ms.plan_file == 'plan/server.md'
    store.switch_to(ms.cmp, ms, trivial_id, now_ts=11000)
    assert ms.mode == 'execute' and ms.auto_trivial is True


def test_dep_branch_on_the_active_path_creates_its_child():
    """A new sub-question growing out of the active work branches as its
    DESCENDANT (live regression: asking for the rector's name while an
    'info about university X' path was active got routed continue; the
    rubric now sends it dep_branch on the active path — this pins the
    mechanics of that route)."""
    ms = _session_with_two_paths()          # active = A2
    with _detect('dep_branch', 'A2',
                 new_path={'title': 'Rektor Universitas Maju',
                           'action': 'find info'}):
        result = on_turn_boundary(AGENT, ms, FakeChatlog(user_ts=9000),
                                  'siapa sih rektornya sekarang?')
    new_id = result['target']
    assert ms.cmp['paths'][new_id]['depends_on'] == ['A2']
    assert ms.cmp['active_id'] == new_id
    assert ms.cmp['paths']['A2']['status'] == 'preserved'
    assert ms.cmp['paths'][new_id]['title'] == 'Rektor Universitas Maju'


def test_indep_branch_creates_independent_path():
    ms = _session_with_two_paths()
    with _detect('indep_branch'):
        result = on_turn_boundary(AGENT, ms, FakeChatlog(), LONG_NEW_TASK)
    assert ms.cmp['paths'][result['target']]['depends_on'] == []


def test_branch_paths_named_from_the_turn_envelope():
    """New paths are named in-context by the same single-pass call — and the
    name is applied exactly once (immutable afterwards)."""
    ms = _session_with_two_paths()
    with _detect('indep_branch',
                 new_path={'title': 'Blog robin.blog.com', 'action': 'create article'}):
        result = on_turn_boundary(AGENT, ms, FakeChatlog(),
                                  'buatkan tulisan blog untuk robin.blog.com dong')
    record = ms.cmp['paths'][result['target']]
    assert record['title'] == 'Blog robin.blog.com'
    assert record['action'] == 'create article'


def test_branch_without_envelope_name_keeps_mechanical_title():
    ms = _session_with_two_paths()
    with _detect('indep_branch'):
        result = on_turn_boundary(AGENT, ms, FakeChatlog(), LONG_NEW_TASK)
    assert ms.cmp['paths'][result['target']]['title'] == LONG_NEW_TASK[:60]


def test_path_op_failure_degrades_to_continue():
    ms = _session_with_two_paths()
    with _detect('return', 'Z9'):  # invalid target slipped through
        result = on_turn_boundary(AGENT, ms, FakeChatlog(), 'x' * 50)
    assert result['decision'] == 'continue'
    assert ms.cmp['active_id'] == 'A2'  # untouched


def test_transcript_hits_compete_with_card_hits_for_auto_pins():
    ms = _session_with_two_paths()
    store.create_path(ms.cmp, ms, 'current question', now_ts=3000)
    with _detect('continue'), \
         patch.object(store, 'search_cmp_paths', return_value=[
             {'id': 'A1', 'score': 2}, {'id': 'A2', 'score': 2},
         ]), patch.object(store, 'search_cmp_transcripts', return_value=[
             {'id': 'A2', 'score': 4, 'excerpts': ['pick up boots']},
             {'id': 'A1', 'score': 3, 'excerpts': ['return blazer']},
         ]):
        on_turn_boundary(AGENT, ms, FakeChatlog(),
                         'How many clothing items do I pick up or return?')

    assert ms.cmp['pinned_ids'] == ['A2', 'A1']
    assert ms.cmp['pin_excerpts'] == {
        'A2': ['pick up boots'], 'A1': ['return blazer'],
    }


def test_lifecycle_archives_then_prunes_on_turn_boundaries():
    """Count-based preserved cap: when > MAX_PRESERVED, oldest archived
    on turn boundary; archived > 3 days → pruned."""
    ms = _session_with_two_paths()         # A1 preserved @2000, A2 active @2000
    # Build up MAX_PRESERVED+1 preserved to push A1 over the cap:
    N = store.MAX_PRESERVED
    for i in range(3, N + 4):
        store.create_path(ms.cmp, ms, f'path{i}', now_ts=2000 + i * 100)

    with _detect('continue'):
        on_turn_boundary(AGENT, ms, FakeChatlog(user_ts=(N + 4) * 100 + 2000),
                         'lanjutkan kerjaan server config ini ya')
    assert ms.cmp['paths']['A1']['status'] == 'archived'

    # Wait 3+ days for prune
    t_prune = (N + 4) * 100 + 2000 + store.ARCHIVED_TTL_MS + 1
    with _detect('continue'):
        on_turn_boundary(AGENT, ms,
                         FakeChatlog(user_ts=t_prune),
                         'lanjutkan lagi kerjaan server config ini ya')
    assert 'A1' not in ms.cmp['paths']


# ── Coexistence with ATG-only re-arm (runtime routing) ───────────────────────

def test_atg_only_agents_still_use_rearm():
    # the runtime gate: cmp handled → skip re-arm; cmp off → re-arm runs.
    # Verified structurally: on_turn_boundary returns None for non-cmp agents,
    # so _cmp_handled stays False and the ATG block executes.
    ms = AgentState(mode='execute')
    ms.atg = {'status': 'done', 'dag': {'root_goal': 'g'}}
    assert on_turn_boundary({'id': 'a1', 'enable_atg': 1}, ms,
                            FakeChatlog(), LONG_NEW_TASK) is None
