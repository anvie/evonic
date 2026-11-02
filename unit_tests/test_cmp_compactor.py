"""Tests for the CMP compactor (deterministic card finalization — card prose
upkeep moved to the per-turn single-pass op, see test_cmp_detector)."""

from backend.agent_runtime.cmp import store
from backend.agent_runtime.cmp.compactor import finalize_active_card
from backend.agent_state import AgentState


class FakeChatlog:
    def __init__(self, entries):
        self.entries = sorted(entries, key=lambda e: e['ts'])

    def get_entries_between_ts(self, after_ts, up_to_ts):
        return [e for e in self.entries if after_ts < e['ts'] <= up_to_ts]

    def get_entries_after_ts(self, after_ts):
        return [e for e in self.entries if e['ts'] > after_ts]


ENTRIES = [
    {'type': 'user', 'ts': 1100, 'content': 'build a company profile site for client A'},
    {'type': 'final', 'ts': 1200, 'content': 'Site scaffolded with SvelteKit; deploy failed.'},
]

def _ms(title='client A site'):
    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(ms, title=title, now_ts=1000)
    return ms


# ── finalize_active_card (deterministic, no LLM) ─────────────────────────────

def test_finalize_fills_outcome_gap():
    ms = _ms()
    finalize_active_card(FakeChatlog(ENTRIES), ms.cmp, ms)
    p1 = ms.cmp['paths']['A1']
    assert 'deploy failed' in p1['outcome'].lower()      # last final reply
    assert p1['card_stale'] is False


def test_finalize_never_renames_the_node():
    """Regression for the live map bug: finalization used to overwrite the
    path's title/action with LLM card output, renaming established map nodes
    and their edge labels. Node identity is immutable after creation."""
    ms = _ms()
    p1 = ms.cmp['paths']['A1']
    p1['action'] = 'build site'
    finalize_active_card(FakeChatlog(ENTRIES), ms.cmp, ms)
    assert p1['title'] == 'client A site'
    assert p1['action'] == 'build site'


def test_finalize_preserves_incremental_card_content():
    """Facts/outcome accumulated by the per-turn delta op survive
    finalization — nothing is replaced."""
    ms = _ms()
    p1 = ms.cmp['paths']['A1']
    store.apply_card_delta(p1, {'outcome': 'homepage reviewed by user',
                                'new_facts': ['user wants dark theme']})
    finalize_active_card(FakeChatlog(ENTRIES), ms.cmp, ms)
    assert 'user wants dark theme' in p1['key_facts']
    assert p1['outcome'] == 'homepage reviewed by user'  # not overwritten


def test_finalize_skips_when_card_fresh():
    ms = _ms()
    p1 = ms.cmp['paths']['A1']
    p1['card_stale'] = False
    finalize_active_card(FakeChatlog(ENTRIES), ms.cmp, ms)
    assert p1['key_facts'] == [] and p1['outcome'] == ''


def test_finalize_never_raises():
    class ExplodingChatlog:
        def get_entries_between_ts(self, a, b):
            raise RuntimeError('boom')

        def get_entries_after_ts(self, a):
            raise RuntimeError('boom')

    ms = _ms()
    finalize_active_card(ExplodingChatlog(), ms.cmp, ms)  # must not raise
    assert ms.cmp['paths']['A1']['card_stale'] is False


# ── token estimates ──────────────────────────────────────────────────────────

def test_path_and_card_token_estimates():
    from backend.agent_runtime.cmp.compactor import (
        card_token_estimate, path_token_estimate)

    entries = [
        {'type': 'user', 'ts': 1100, 'content': 'buatkan laporan invoice ' * 40},
        {'type': 'tool_call', 'ts': 1200, 'function': 'bash',
         'params': {'script': 'ls -la ' * 30}},
        {'type': 'tool_output', 'ts': 1300, 'content': 'X' * 4000},
        {'type': 'final', 'ts': 1400, 'content': 'laporan selesai'},
    ]
    path = {'id': 'A1', 'segments': [[1000, None]],
            'title': 'Laporan invoice', 'goal': 'buat laporan',
            'outcome': 'selesai', 'key_facts': ['fakta a', 'fakta b'],
            'artifacts': ['/out/report.pdf']}

    transcript_tokens = path_token_estimate(FakeChatlog(entries), path)
    card_tokens = card_token_estimate(path)

    # transcript (tool dumps + prose) dwarfs the compact card
    assert transcript_tokens > 500
    assert 0 < card_tokens < 100
    assert transcript_tokens > card_tokens * 5

    # empty path → 0, never raises
    assert path_token_estimate(FakeChatlog([]), {'id': 'x', 'segments': []}) == 0
    assert card_token_estimate({'id': 'x'}) == 0


def test_path_llm_token_estimate_reflects_compacted_view():
    """Actual (llm view) must sit far below the raw estimate when a path
    carries an oversized tool output: reconstruction compacts what the raw
    chatlog stores in full."""
    from backend.agent_runtime.cmp.compactor import (path_llm_token_estimate,
                                                     path_token_estimate)
    from models.chatlog import chatlog_manager
    log = chatlog_manager.get('cmp_compactor_est_agent', 'sess-est')
    log.append({'type': 'user', 'ts': 1100, 'content': 'jalankan explorer',
                'session_id': 's'})
    log.append({'type': 'tool_call', 'ts': 1200, 'function': 'Explore',
                'params': {'query': 'x'}, 'id': 'c1', 'session_id': 's'})
    log.append({'type': 'tool_output', 'ts': 1300, 'session_id': 's',
                'content': 'hasil scan: baris temuan panjang\n' * 8000,
                'tool_call_id': 'c1', 'function': 'Explore'})
    log.append({'type': 'final', 'ts': 1400, 'content': 'selesai',
                'session_id': 's'})
    path = {'id': 'A1', 'segments': [[1000, None]]}
    raw = path_token_estimate(log, path)
    actual = path_llm_token_estimate(log, path)
    assert 0 < actual < raw / 5      # the compaction gap is visible
    # a chatlog without segment support degrades to 0, never raises
    assert path_llm_token_estimate(FakeChatlog([]), path) == 0
