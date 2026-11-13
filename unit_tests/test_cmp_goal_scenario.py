"""End-to-end walk of the CMP goal scenario (user's /goal spec):

  1. weekly report for Perusahaan A            -> A1 active
  2. blog article (unrelated)                   -> A2 active, A1 offloaded to card
  3. invoice for client X (uses A1)             -> B1 active (A1 -> B1 edge);
                                                   A1 stays LOADED (parent)
  4. invoice for client Y (uses A1)             -> B2 active (sibling index);
                                                   A2 + B1 offloaded, A1 loaded
  5. back to the company report                 -> A1 active again, rehydrated;
                                                   A2/B1/B2 offloaded to cards

At every step: the map shows level-based ids with action edges and the `*`
active marker; non-ancestor details are OUT of the history window (IPPC
cards only); the active path's dependency ancestors stay in memory because
the child consumes their results; returning reloads a path's detail.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """The single-pass turn op is a real LLM call — fail it fast so any
    unmocked detect() falls back mechanically (no network in tests)."""
    monkeypatch.setattr('backend.agent_runtime.cmp.detector._call_turn_llm',
                        lambda *a, **k: None)


from unittest.mock import patch

from backend.agent_runtime.cmp import assembler, on_turn_boundary
from backend.agent_runtime.cmp.render import render_map
from backend.agent_state import AgentState
from models.chatlog import chatlog_manager

AGENT = {'id': 'cmp_goal_agent', 'enable_cmp': 1, 'enable_agent_state': 1}


def _decide(decision, target=None, new_path=None):
    return patch('backend.agent_runtime.cmp.detector.detect',
                 return_value={'decision': decision, 'target': target,
                               'layer': 'LLM', 'reason': 'scripted',
                               'new_path': new_path})


def test_goal_scenario_end_to_end():
    log = chatlog_manager.get('cmp_goal_agent', 'sess-goal')
    ms = AgentState(mode='execute')

    # ── 1. weekly report → A1 ────────────────────────────────────────────────
    log.append({'type': 'user', 'ts': 1000,
                'content': 'buatkan laporan mingguan Perusahaan A', 'session_id': 's'})
    with _decide('continue', new_path={'title': 'Perusahaan A',
                                       'action': 'create report'}):
        result = on_turn_boundary(AGENT, ms, log,
                                  'buatkan laporan mingguan Perusahaan A')
    assert result['decision'] == 'init'
    log.append({'type': 'final', 'ts': 1500,
                'content': 'laporan mingguan Perusahaan A selesai', 'session_id': 's'})
    assert 'A1["Perusahaan A *"]' in render_map(ms.cmp)

    # ── 2. blog article → A2, A1 offloaded ──────────────────────────────────
    log.append({'type': 'user', 'ts': 2000,
                'content': 'buatkan tulisan blog untuk example.com', 'session_id': 's'})
    with _decide('indep_branch', new_path={'title': 'blog',
                                           'action': 'create article'}):
        result = on_turn_boundary(AGENT, ms, log,
                                  'buatkan tulisan blog untuk example.com')
    assert result['target'] == 'A2'
    log.append({'type': 'final', 'ts': 2500, 'content': 'artikel blog selesai',
                'session_id': 's'})

    mermaid = render_map(ms.cmp)
    assert 'Agent -->|create report| A1["Perusahaan A"]' in mermaid
    assert 'Agent -->|create article| A2["blog *"]' in mermaid
    # offload: A1 transcript OUT of the window, IPPC card carries the interface
    msgs = assembler.build_history(log, None, ms.cmp)
    text = ' '.join(str(m.get('content')) for m in msgs)
    assert 'laporan mingguan Perusahaan A selesai' not in text
    rendered = ms.render(cmp_enabled=True)
    assert 'Perusahaan A selesai' in rendered        # card outcome snippet stays

    # ── 3. invoice client X → B1 (dep on A1) ────────────────────────────────
    log.append({'type': 'user', 'ts': 3000,
                'content': 'buatkan invoice atas nama Perusahaan A untuk client X',
                'session_id': 's'})
    with _decide('dep_branch', 'A1', new_path={'title': 'client X',
                                               'action': 'create invoice'}):
        result = on_turn_boundary(AGENT, ms, log,
                                  'buatkan invoice atas nama Perusahaan A untuk client X')
    assert result['target'] == 'B1'
    log.append({'type': 'final', 'ts': 3500, 'content': 'invoice client X selesai',
                'session_id': 's'})
    assert 'A1 -->|create invoice| B1["client X *"]' in render_map(ms.cmp)
    # dependency ancestor A1's card is pinned while B1 is active
    assert 'dependency of active path' in ms.render(cmp_enabled=True)
    # …and A1's transcript detail is LOADED (parent of the active path),
    # while A2's stays offloaded
    msgs = assembler.build_history(log, None, ms.cmp)
    text = ' '.join(str(m.get('content')) for m in msgs)
    assert 'laporan mingguan Perusahaan A selesai' in text
    assert 'artikel blog selesai' not in text

    # ── 4. invoice client Y → B2 (sibling level index) ──────────────────────
    log.append({'type': 'user', 'ts': 4000,
                'content': 'buatkan invoice lagi tapi untuk client Y', 'session_id': 's'})
    with _decide('dep_branch', 'A1', new_path={'title': 'client Y',
                                               'action': 'create invoice'}):
        result = on_turn_boundary(AGENT, ms, log,
                                  'buatkan invoice lagi tapi untuk client Y')
    assert result['target'] == 'B2'
    log.append({'type': 'final', 'ts': 4500, 'content': 'invoice client Y selesai',
                'session_id': 's'})

    mermaid = render_map(ms.cmp)
    assert 'A1 -->|create invoice| B1["client X"]' in mermaid
    assert 'A1 -->|create invoice| B2["client Y *"]' in mermaid
    # A2 and B1 offloaded; A1 stays LOADED (dependency/parent of active B2)
    msgs = assembler.build_history(log, None, ms.cmp)
    text = ' '.join(str(m.get('content')) for m in msgs)
    assert 'client Y' in text
    assert 'laporan mingguan Perusahaan A selesai' in text   # parent loaded
    for gone in ('artikel blog selesai', 'invoice client X selesai'):
        assert gone not in text

    # ── 5. back to the report → A1 active again, rehydrated ─────────────────
    log.append({'type': 'user', 'ts': 5000,
                'content': 'laporan perusahaan tadi masih kurang laporan keuangan, tambahkan',
                'session_id': 's'})
    with _decide('return', 'A1'):
        result = on_turn_boundary(AGENT, ms, log,
                                  'laporan perusahaan tadi masih kurang laporan keuangan, tambahkan')
    assert result['decision'] == 'return'
    assert ms.cmp['active_id'] == 'A1'

    mermaid = render_map(ms.cmp)
    assert 'A1["Perusahaan A *"]' in mermaid          # star moved back
    assert '"client Y"' in mermaid and '"client Y *"' not in mermaid

    # rehydration: A1's earlier detail is BACK in the window
    msgs = assembler.build_history(log, None, ms.cmp)
    text = ' '.join(str(m.get('content')) for m in msgs)
    assert 'laporan mingguan Perusahaan A selesai' in text          # rehydrated
    assert 'laporan keuangan' in text                               # new turn
    for gone in ('artikel blog selesai', 'invoice client X selesai',
                 'invoice client Y selesai'):
        assert gone not in text                                     # others offloaded
    # their IPPC cards remain visible in the state message
    rendered = ms.render(cmp_enabled=True)
    for pid in ('A2', 'B1', 'B2'):
        assert pid in rendered
