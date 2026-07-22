"""Tests for CMP map + card rendering (goal-spec notation: level-based ids,
action-labelled parent edges, `*` active marker, IPPC snippet cards)."""

from backend.agent_runtime.cmp import store
from backend.agent_runtime.cmp.render import RENDER_MAX_CHARS, render_cmp_section, render_map
from backend.agent_state import AgentState


def _session():
    """Goal-spec scenario: report (A1), blog (A2), invoices (B1, B2 off A1)."""
    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(ms, title='Perusahaan A', goal='laporan mingguan',
                           now_ts=1000)
    a1 = ms.cmp['paths']['A1']
    a1['action'] = 'create report'
    a1['outcome'] = 'laporan mingguan selesai'
    a1['key_facts'] = ['laporan di /reports/weekly-a.pdf']
    a1['artifacts'] = ['/reports/weekly-a.pdf']
    a2 = store.create_path(ms.cmp, ms, 'blog', goal='tulisan blog', now_ts=2000)
    a2['action'] = 'create article'
    return ms


def test_goal_scenario_map_shape():
    ms = _session()
    b1 = store.create_path(ms.cmp, ms, 'client X', goal='invoice',
                           depends_on=['A1'], now_ts=3000)
    b1['action'] = 'create invoice'
    b2 = store.create_path(ms.cmp, ms, 'client Y', goal='invoice',
                           depends_on=['A1'], now_ts=4000)
    b2['action'] = 'create invoice'

    mermaid = render_map(ms.cmp)
    assert 'flowchart TD' in mermaid
    assert 'Agent((Agent))' in mermaid
    # root paths hang off Agent with action labels
    assert 'Agent -->|create report| A1["Perusahaan A"]' in mermaid
    assert 'Agent -->|create article| A2["blog"]' in mermaid
    # dependency children hang off their parent, level letter incremented
    assert 'A1 -->|create invoice| B1["client X"]' in mermaid
    assert 'A1 -->|create invoice| B2["client Y *"]' in mermaid  # active marker


def test_return_moves_star_back():
    ms = _session()
    store.create_path(ms.cmp, ms, 'client X', depends_on=['A1'], now_ts=3000)
    store.switch_to(ms.cmp, ms, 'A1', now_ts=4000)
    mermaid = render_map(ms.cmp)
    assert 'A1["Perusahaan A *"]' in mermaid
    assert '"client X *"' not in mermaid


def test_sibling_indices_share_level_counter():
    ms = _session()  # A1, A2 exist
    store.create_path(ms.cmp, ms, 'client X', depends_on=['A1'], now_ts=3000)
    store.create_path(ms.cmp, ms, 'client Y', depends_on=['A1'], now_ts=4000)
    # sejajar (same level, different parent) → next index at that level
    store.create_path(ms.cmp, ms, 'blog asset', depends_on=['A2'], now_ts=5000)
    assert set(ms.cmp['paths']) == {'A1', 'A2', 'B1', 'B2', 'B3'}
    # third level
    store.create_path(ms.cmp, ms, 'reminder email', depends_on=['B1'], now_ts=6000)
    assert 'C1' in ms.cmp['paths']


def test_section_tiers_active_full_others_snippets():
    ms = _session()
    section = render_cmp_section(ms.cmp)
    # active path (A2 blog): full card
    assert 'A2 — blog' in section and '(active)' in section
    # offloaded A1: snippet only, but present (IPPC — never disappears)
    assert 'Offloaded paths' in section
    assert '- A1 Perusahaan A' in section
    assert 'laporan di /reports/weekly-a.pdf' not in section  # detail offloaded
    assert 'switch_path' in section


def test_dependency_ancestors_stay_compact():
    ms = _session()
    store.create_path(ms.cmp, ms, 'client X', goal='invoice untuk client X',
                      depends_on=['A1'], now_ts=3000)
    section = render_cmp_section(ms.cmp)
    # B1 active full; A1 pinned compact (dependency); A2 snippet
    assert 'dependency of active path' in section
    assert 'laporan di /reports/weekly-a.pdf' in section  # ancestor key fact pinned
    assert '- A2 blog' in section


def test_archived_paths_render_title_only():
    """Archived = the next rung down in context cost: the snippet (outcome/
    goal summary) is offloaded too, leaving only the title line."""
    ms = _session()
    ms.cmp['paths']['A1']['status'] = 'archived'
    section = render_cmp_section(ms.cmp)
    assert 'Archived paths (title + tags' in section
    assert '- A1 Perusahaan A [archived]' in section
    assert 'laporan mingguan selesai' not in section   # outcome offloaded


def test_legacy_dormant_status_renders_as_preserved_snippet():
    ms = _session()
    ms.cmp['paths']['A1']['status'] = 'dormant'        # pre-lifecycle data
    section = render_cmp_section(ms.cmp)
    assert '- A1 Perusahaan A [preserved] — laporan mingguan selesai' in section


def test_root_label_is_dynamic_but_id_stays_stable():
    ms = _session()
    mermaid = render_map(ms.cmp, 'Aisyah Krasan')
    # display label = agent name; mermaid node ID stays identifier-safe
    assert 'Agent((Aisyah Krasan))' in mermaid
    assert 'Agent -->|create report| A1' in mermaid
    # default fallback
    assert 'Agent((Agent))' in render_map(ms.cmp)
    # state render threads the name through
    rendered = ms.render(cmp_enabled=True, agent_name='Aisyah')
    assert 'Agent((Aisyah))' in rendered


def test_mermaid_label_sanitized():
    ms = AgentState()
    ms.cmp = store.new_cmp(ms, title='weird "title" [x]|y', now_ts=1000)
    ms.cmp['paths']['A1']['action'] = 'do|"bad"'
    mermaid = render_map(ms.cmp)
    assert '|dobad|' in mermaid
    assert 'weird title xy' in mermaid


def test_render_cap():
    ms = _session()
    for i in range(15):
        store.create_path(ms.cmp, ms, f'task {i} ' + 'x' * 50,
                          goal='g' * 290, now_ts=3000 + i)
        ms.cmp['paths'][ms.cmp['active_id']]['key_facts'] = ['f' * 190] * 6
    section = render_cmp_section(ms.cmp)
    assert len(section) <= RENDER_MAX_CHARS + 50


def test_empty_cmp_renders_nothing():
    assert render_cmp_section(None) == ''
    assert render_cmp_section({}) == ''


def test_map_is_append_only_modulo_active_marker():
    """The user-facing invariant: once a node and its edge are on the map,
    they never change — successive renders may only append lines and move
    the `*` marker. Holds by construction (immutable id/title/action/deps)."""
    def _stable_lines(mermaid):
        return {line.replace(' *"]', '"]') for line in mermaid.splitlines()}

    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(ms, title='Perusahaan A', now_ts=1000)
    ms.cmp['paths']['A1']['action'] = 'create report'
    snapshots = [render_map(ms.cmp)]

    a2 = store.create_path(ms.cmp, ms, 'blog', now_ts=2000)
    a2['action'] = 'create article'
    snapshots.append(render_map(ms.cmp))

    b1 = store.create_path(ms.cmp, ms, 'client X', depends_on=['A1'], now_ts=3000)
    b1['action'] = 'create invoice'
    snapshots.append(render_map(ms.cmp))

    # per-turn card deltas + switches never disturb established lines
    store.apply_card_delta(b1, {'outcome': 'invoice terkirim',
                                'new_facts': ['nomor INV-042']})
    store.switch_to(ms.cmp, ms, 'A1', now_ts=4000)
    snapshots.append(render_map(ms.cmp))

    for earlier, later in zip(snapshots, snapshots[1:]):
        assert _stable_lines(earlier) <= _stable_lines(later)
