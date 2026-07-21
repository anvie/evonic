"""Tests for the CMP single-pass turn detector (fully LLM-led routing plus
card-delta and new-path naming in one envelope)."""

import json
from unittest.mock import MagicMock, patch

from backend.agent_runtime.cmp import prompts, store
from backend.agent_runtime.cmp.detector import detect
from backend.agent_state import AgentState

LONG_NEW_TASK = ('please build a completely new scraper project under /tmp/scraper '
                 'that collects product prices daily')


def _session():
    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(ms, title='client A website', goal='build site',
                           now_ts=1000)
    p1 = ms.cmp['paths']['A1']
    p1['artifacts'] = ['/projects/client-a-web/']
    p1['key_facts'] = ['deploy failed: missing DATABASE_URL']
    store.create_path(ms.cmp, ms, 'server config', goal='configure nginx',
                      now_ts=2000)
    ms.mode = 'execute'
    return ms


def _client(payload, key='content'):
    """MagicMock LLM client scripted with an envelope (dict) or raw text."""
    content = payload if isinstance(payload, str) else json.dumps(payload)
    client = MagicMock()
    client.chat_completion.return_value = {
        'success': True,
        'response': {'choices': [{'message': {key: content}}]}}
    return client


def _patch_llm(payload=None, key='content'):
    return patch('backend.task_classifier._get_classifier_client',
                 return_value=_client(payload if payload is not None
                                      else {'route': 'continue'}, key=key))


def _sent_prompt(client):
    return client.chat_completion.call_args[0][0][1]['content']


# ── Fully LLM-led: every non-empty message reaches the LLM ───────────────────

def test_substantive_messages_reach_the_llm():
    """Anything long enough to carry a subject reaches the LLM — no keyword
    topic matching. Short retry/ack messages are guarded (below)."""
    ms = _session()
    ms.cmp['paths']['A2']['artifacts'] = ['/etc/nginx/nginx.conf']
    for msg in (
        'oke lanjutkan sesuai plan itu saja',
        'kenapa hasil konfigurasi server production sekarang masih menunjukkan error timeout',
        'tambahkan gzip compression pada /etc/nginx/nginx.conf untuk semua response text',
        'tolong lanjutkan pekerjaan yang ada di A1 sekarang juga',
    ):
        client = _client({'route': 'continue'})
        with patch('backend.task_classifier._get_classifier_client',
                   return_value=client):
            result = detect(ms.cmp, ms, msg)
        assert result['layer'] == 'LLM'
        client.chat_completion.assert_called_once()


def test_short_retry_message_continues_without_switching():
    """Live regression: 'coba lagi' during an active turn (interrupted +
    retried) was classified return->A3 by the LLM, silently abandoning the
    active path. A short message with no path id can't express a new subject
    → guarded to continue, LLM not consulted."""
    ms = _session()  # active = A2
    for msg in ('coba lagi', 'ok', 'lanjut', 'ulangi ya'):
        client = _client({'route': 'return', 'target': 'A1'})
        with patch('backend.task_classifier._get_classifier_client',
                   return_value=client):
            result = detect(ms.cmp, ms, msg)
        assert (result['decision'], result['target'],
                result['layer']) == ('continue', None, 'guard')
        assert result['card_delta'] is None and result['new_path'] is None
        client.chat_completion.assert_not_called()


def test_guard_boundary_is_two_words():
    """The signal-strength rail sits at ≤2 words: pure retry/ack particles
    stay guarded, LLM never consulted."""
    ms = _session()
    for msg in ('coba lagi', 'ulangi ya', 'ok'):
        client = _client({'route': 'return', 'target': 'A1'})
        with patch('backend.task_classifier._get_classifier_client',
                   return_value=client):
            result = detect(ms.cmp, ms, msg)
        assert (result['decision'], result['layer']) == ('continue', 'guard')
        client.chat_completion.assert_not_called()


def test_three_word_subject_message_reaches_llm():
    """Live gap: the old ≤3-word guard force-routed 'balik ke laporan' to
    continue without the LLM ever seeing it (and skipped the card delta).
    Three-word messages carry a subject in Indonesian → they go to the LLM."""
    ms = _session()
    client = _client({'route': 'return', 'target': 'A1'})
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        result = detect(ms.cmp, ms, 'balik ke laporan')
    assert result['layer'] == 'LLM'
    assert (result['decision'], result['target']) == ('return', 'A1')
    client.chat_completion.assert_called_once()


def test_recent_dialogue_reaches_the_turn_prompt():
    """The last few turns are passed into the prompt so terse messages are
    grounded in what just happened."""
    ms = _session()
    client = _client({'route': 'continue'})
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        detect(ms.cmp, ms, 'coba lagi dong yang barusan itu',
               recent_dialogue='User: tolong hapus invoice itu\nAgent: sedang menghapus...')
    prompt = _sent_prompt(client)
    assert 'Recent conversation' in prompt
    assert 'tolong hapus invoice itu' in prompt


def test_short_message_with_explicit_path_id_still_reaches_llm():
    """The guard bypasses when a path id is named ('lanjut A1') so a
    deliberate short return still works."""
    ms = _session()
    client = _client({'route': 'return', 'target': 'A1'})
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        result = detect(ms.cmp, ms, 'lanjut A1')
    assert result['layer'] == 'LLM'
    assert (result['decision'], result['target']) == ('return', 'A1')
    client.chat_completion.assert_called_once()


def test_empty_message_skips_llm():
    ms = _session()
    client = _client({'route': 'continue'})
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        result = detect(ms.cmp, ms, '   ')
    assert result['decision'] == 'continue'
    client.chat_completion.assert_not_called()


def test_plan_mode_context_reaches_llm():
    """The removed approval guard's knowledge now travels as prompt context:
    the LLM must see that the active path awaits approval."""
    ms = _session()
    ms.mode = 'plan'
    client = _client({'route': 'continue'})
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        detect(ms.cmp, ms, 'oke lanjutkan sesuai plan itu saja')
    assert 'AWAITING USER APPROVAL' in _sent_prompt(client)


def test_kanban_return_regression():
    """Live regression: 'oke, sip, btw yg issue kanban ... udah solved kah?'
    was swallowed by the old approval guard while P4 sat in plan mode."""
    ms = _session()
    ms.mode = 'plan'
    with _patch_llm({'route': 'return', 'target': 'A1'}):
        result = detect(ms.cmp, ms,
                        'oke, sip, btw yg issue kanban state race condition tadi udah solved kah?')
    assert result['layer'] == 'LLM'
    assert (result['decision'], result['target']) == ('return', 'A1')


def test_generic_title_word_does_not_short_circuit():
    """Live regression (session 75433064): task 1 'cari bug di 3 plugin'
    swallowed task 2 via keyword overlap ('plugin'). Topic decisions must
    reach the LLM."""
    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(
        ms, title='tolong cari bug di 3 plugin paling aktif', now_ts=1000)
    store.create_path(ms.cmp, ms, 'other work', now_ts=1500)
    store.switch_to(ms.cmp, ms, 'A1', now_ts=2000)
    ms.mode = 'execute'
    with _patch_llm({'route': 'dep_branch', 'target': 'A1'}):
        result = detect(ms.cmp, ms,
                        'tolong gabungkan chart agent test di token monitor plugin')
    assert result['layer'] == 'LLM'
    assert result['decision'] == 'dep_branch'


def test_llm_sees_recent_deliverable_tail():
    """The just-delivered reply reaches the prompt — the branch cue for
    'invoice done' → 'rebuild krasan-cli' style borderline cases, and the
    substance for the card delta."""
    ms = _session()
    client = _client({'route': 'dep_branch', 'target': 'A2'})
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        result = detect(ms.cmp, ms, LONG_NEW_TASK,
                        recent_tail='Invoice INV-042 untuk Budi Contoh sudah dibuat.')
    assert 'INV-042' in _sent_prompt(client)
    assert result['decision'] == 'dep_branch'


def test_hook_passes_last_final_to_detector():
    from backend.agent_runtime.cmp import on_turn_boundary

    class Log:
        def get_last_entry(self, types=None):
            if types and 'final' in types:
                return {'type': 'final', 'ts': 4000,
                        'content': 'Invoice sudah selesai dibuat.'}
            return {'type': 'user', 'ts': 5000}

        def tail(self, limit=24, to_ts=None):
            return []

        def get_entries_between_ts(self, a, b):
            return []

        def get_entries_after_ts(self, a):
            return []

    ms = _session()
    with patch('backend.agent_runtime.cmp.detector.detect',
               return_value={'decision': 'continue', 'target': None,
                             'layer': 'LLM', 'reason': 'x'}) as det:
        on_turn_boundary({'id': 'a1', 'enable_cmp': 1, 'enable_agent_state': 1},
                         ms, Log(), 'tolong rebuild krasan-cli sekarang setelah invoice tadi')
    assert det.call_args[1]['recent_tail'] == 'Invoice sudah selesai dibuat.'


def test_llm_sees_finished_task_state():
    ms = _session()
    ms.atg = {'status': 'done', 'dag': {'root_goal': 'g'}}
    client = _client({'route': 'continue'})
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        detect(ms.cmp, ms, LONG_NEW_TASK)
    assert 'FINISHED' in _sent_prompt(client)


# ── LLM decisions + validation ───────────────────────────────────────────────

def test_llm_decisions_flow_through():
    ms = _session()
    for envelope, expected in [
        ({'route': 'indep_branch'}, ('indep_branch', None)),
        ({'route': 'return', 'target': 'A1'}, ('return', 'A1')),
        ({'route': 'dep_branch', 'target': 'A1'}, ('dep_branch', 'A1')),
        ({'route': 'continue'}, ('continue', None)),
    ]:
        with _patch_llm(envelope):
            result = detect(ms.cmp, ms, LONG_NEW_TASK)
        assert (result['decision'], result['target']) == expected
        assert result['layer'] == 'LLM'


def test_invalid_targets_degrade_safely():
    ms = _session()
    # return to unknown/active path → continue
    for target in ('Z9', 'A2'):
        with _patch_llm({'route': 'return', 'target': target}):
            assert detect(ms.cmp, ms, LONG_NEW_TASK)['decision'] == 'continue'
    # dep_branch on unknown path → independent branch
    with _patch_llm({'route': 'dep_branch', 'target': 'Z9'}):
        result = detect(ms.cmp, ms, LONG_NEW_TASK)
        assert result['decision'] == 'indep_branch' and result['target'] is None


def test_counts_llm_calls():
    ms = _session()
    with _patch_llm({'route': 'indep_branch'}):
        detect(ms.cmp, ms, LONG_NEW_TASK)
    assert ms.cmp['stats']['detector_llm_calls'] == 1


# ── Envelope passthrough (card delta + new-path naming) ──────────────────────

def test_card_delta_and_new_path_flow_through():
    ms = _session()
    envelope = {
        'route': 'indep_branch',
        'new_path': {'title': 'Scraper harga produk', 'action': 'build scraper'},
        'card': {'outcome': 'nginx configured with gzip',
                 'new_facts': ['gzip on for text responses'],
                 'new_artifacts': ['/etc/nginx/nginx.conf']},
    }
    with _patch_llm(envelope):
        result = detect(ms.cmp, ms, LONG_NEW_TASK)
    assert result['new_path'] == envelope['new_path']
    assert result['card_delta'] == envelope['card']


def test_card_delta_survives_target_downgrade():
    """A bad target invalidates the ROUTE, not the delta — the delta
    describes the active path and stays applicable."""
    ms = _session()
    card = {'outcome': 'config selesai'}
    with _patch_llm({'route': 'return', 'target': 'Z9', 'card': card}):
        result = detect(ms.cmp, ms, LONG_NEW_TASK)
    assert result['decision'] == 'continue'
    assert result['card_delta'] == card
    with _patch_llm({'route': 'dep_branch', 'target': 'Z9', 'card': card,
                     'new_path': {'title': 't', 'action': 'a'}}):
        result = detect(ms.cmp, ms, LONG_NEW_TASK)
    assert result['decision'] == 'indep_branch'
    assert result['card_delta'] == card
    assert result['new_path'] == {'title': 't', 'action': 'a'}


def test_malformed_envelope_shapes_are_dropped():
    ms = _session()
    with _patch_llm({'route': 'continue', 'card': 'not a dict',
                     'new_path': ['not', 'a', 'dict']}):
        result = detect(ms.cmp, ms, LONG_NEW_TASK)
    assert result['card_delta'] is None and result['new_path'] is None


def test_archived_paths_are_title_only_in_prompt():
    """Prompt cost follows the lifecycle tiers: an archived path contributes
    its title to the map text but no card (outcome/facts stay offloaded)."""
    ms = _session()
    p1 = ms.cmp['paths']['A1']
    p1['status'] = 'archived'
    p1['outcome'] = 'secret archived outcome detail'
    client = _client({'route': 'continue'})
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        detect(ms.cmp, ms, LONG_NEW_TASK)
    prompt = _sent_prompt(client)
    assert 'client A website' in prompt                          # title stays
    assert '(archived)' in prompt
    assert 'secret archived outcome detail' not in prompt        # no snippet
    assert 'deploy failed: missing DATABASE_URL' not in prompt   # no card


def test_map_shows_depends_on_parentage():
    """Map lines carry 'builds on <parent>' so the LLM can pick correct
    return/dep_branch targets and resolve 'back to the parent'. Root paths
    render without the annotation."""
    ms = _session()
    store.create_path(ms.cmp, ms, 'perbaiki chart', goal='fix chart',
                      depends_on=['A1'], now_ts=3000)
    client = _client({'route': 'continue'})
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        detect(ms.cmp, ms, LONG_NEW_TASK)
    prompt = _sent_prompt(client)
    assert 'builds on A1' in prompt
    assert '- A2: server config (preserved)' in prompt   # root: no annotation


def test_archived_path_keeps_parentage_but_no_card():
    """An archived path stays title-only (no card snippet) but keeps its
    parentage annotation — parent disambiguation matters most once the
    parent has aged out."""
    ms = _session()
    store.create_path(ms.cmp, ms, 'perbaiki chart', goal='fix chart',
                      depends_on=['A1'], now_ts=3000)
    store.switch_to(ms.cmp, ms, 'A2', now_ts=4000)
    b1 = ms.cmp['paths']['B1']
    b1['status'] = 'archived'
    b1['outcome'] = 'secret archived outcome detail'
    client = _client({'route': 'continue'})
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        detect(ms.cmp, ms, LONG_NEW_TASK)
    prompt = _sent_prompt(client)
    assert '(archived, builds on A1)' in prompt
    assert 'secret archived outcome detail' not in prompt


def test_turn_system_contains_routing_procedure_anchors():
    """The ordered decision procedure and few-shot block are load-bearing —
    guard against future prompt edits silently dropping them."""
    for anchor in ('IN ORDER', 'builds on', 'FINISHED', 'gak usah',
                   '### Examples', 'siapa rektornya'):
        assert anchor in prompts.TURN_SYSTEM, anchor


# ── Truncated envelopes (finish_reason=length) ───────────────────────────────

def test_truncated_envelope_is_repaired():
    """Live regression: deepseek-v4-flash burned the budget on implicit CoT
    and cut the envelope after new_path — the branch decision was lost to
    the continue fallback. Route/target/new_path come first in the envelope
    precisely so truncation repair recovers them."""
    ms = _session()
    truncated = ('{\n  "route": "indep_branch",\n  "target": null,\n'
                 '  "new_path": {\n    "title": "Info Acara X",\n'
                 '    "action": "find info"\n  },')
    with _patch_llm(truncated):
        result = detect(ms.cmp, ms, LONG_NEW_TASK)
    assert result['decision'] == 'indep_branch'
    assert result['new_path'] == {'title': 'Info Acara X', 'action': 'find info'}


def test_truncation_beyond_repair_retries_with_doubled_budget():
    ms = _session()
    client = MagicMock()
    client.chat_completion.side_effect = [
        {'success': True,
         'response': {'choices': [{'message': {'content': '{"rou'},
                                   'finish_reason': 'length'}]}},
        {'success': True,
         'response': {'choices': [{'message': {'content':
                                   json.dumps({'route': 'return',
                                               'target': 'A1'})}}]}},
    ]
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        result = detect(ms.cmp, ms, LONG_NEW_TASK)
    assert (result['decision'], result['target']) == ('return', 'A1')
    assert client.chat_completion.call_count == 2
    assert client.chat_completion.call_args_list[0][1]['max_tokens'] == 2048
    assert client.chat_completion.call_args_list[1][1]['max_tokens'] == 4096


def test_unparseable_without_length_never_retries():
    ms = _session()
    client = _client('gibberish with no json')  # finish_reason absent
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        result = detect(ms.cmp, ms, LONG_NEW_TASK)
    assert result['decision'] == 'continue'
    client.chat_completion.assert_called_once()


# ── Init naming pass ─────────────────────────────────────────────────────────

def test_init_pass_names_and_never_routes():
    """First message of the session: routing is moot (whatever the LLM says),
    only the naming is used — and the short-message guard is bypassed so a
    terse opener still gets named."""
    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(ms, title='buatkan laporan mingguan', now_ts=1000)
    envelope = {'route': 'indep_branch',
                'new_path': {'title': 'Laporan Mingguan', 'action': 'create report'}}
    client = _client(envelope)
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        result = detect(ms.cmp, ms, 'buatkan laporan mingguan',
                        initializing=True)
    assert result['decision'] == 'continue'  # never a switch/branch at init
    assert result['new_path'] == envelope['new_path']
    assert 'FIRST message' in _sent_prompt(client)


# ── Envelope parse matrix ────────────────────────────────────────────────────

def test_envelope_parse_matrix():
    ms = _session()
    cases = [
        (json.dumps({'route': 'continue'}), ('continue', None)),
        (json.dumps({'route': 'RETURN', 'target': 'a1'}), ('return', 'A1')),
        (json.dumps({'route': 'return:A1'}), ('return', 'A1')),  # fused form
        ('```json\n{"route": "dep_branch", "target": "A1"}\n```',
         ('dep_branch', 'A1')),
        ('Here is my decision:\n{"route": "indep_branch"} — done.',
         ('indep_branch', None)),
        (json.dumps({'route': 'teleport'}), ('continue', None)),  # unknown route
        ('gibberish with no json', ('continue', None)),
    ]
    for content, expected in cases:
        with _patch_llm(content):
            r = detect(ms.cmp, ms, LONG_NEW_TASK)
        assert (r['decision'], r['target']) == expected, content

    # reasoning_content fallback
    with _patch_llm(json.dumps({'route': 'indep_branch'}),
                    key='reasoning_content'):
        assert detect(ms.cmp, ms, LONG_NEW_TASK)['decision'] == 'indep_branch'

    # LLM failure → continue
    failing = MagicMock()
    failing.chat_completion.return_value = {'success': False}
    with patch('backend.task_classifier._get_classifier_client',
               return_value=failing):
        assert detect(ms.cmp, ms, LONG_NEW_TASK)['decision'] == 'continue'

    # LLM exception → continue, never raises
    raising = MagicMock()
    raising.chat_completion.side_effect = RuntimeError('boom')
    with patch('backend.task_classifier._get_classifier_client',
               return_value=raising):
        assert detect(ms.cmp, ms, LONG_NEW_TASK)['decision'] == 'continue'
