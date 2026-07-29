from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_repo_file(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_global_sse_streams_close_on_page_navigation():
    approval_modal = read_repo_file("static/js/approval-modal.js")
    agent_sidebar = read_repo_file("static/js/agent-sidebar.js")
    agents_page = read_repo_file("templates/agents.html")

    assert "var _sse = null;" in approval_modal
    assert "function _closeSSE()" in approval_modal
    assert "RealtimeClient" in approval_modal
    assert "window.addEventListener('pagehide', _closeSSE);" in approval_modal
    assert "window.addEventListener('beforeunload', _closeSSE);" in approval_modal
    assert "window.addEventListener('pageshow', _startSSE);" in approval_modal

    assert "var _busySSE = null;" in agent_sidebar
    assert "if (_busySSE) return;" in agent_sidebar
    assert "RealtimeClient" in agent_sidebar
    assert "evonic:agent-busy-changed" in agent_sidebar
    assert "function resyncBusyState()" in agent_sidebar
    assert "evonic:agent-busy-resync" in agent_sidebar
    assert "_busySSE.close();" in agent_sidebar
    assert "window.addEventListener('pagehide', closeBusyRealtime);" in agent_sidebar
    assert "window.addEventListener('beforeunload', closeBusyRealtime);" in agent_sidebar
    assert "window.addEventListener('pageshow', function ()" in agent_sidebar

    assert "let agentStatusEventsSubscribed = false;" in agents_page
    assert "function subscribeAgentStatusEvents()" in agents_page
    assert "document.addEventListener('evonic:agent-busy-changed'" in agents_page
    assert "document.addEventListener('evonic:agent-busy-resync'" in agents_page
    assert "window.addEventListener('pageshow', loadBusyAgents);" in agents_page
    assert "new RealtimeClient({" not in agents_page
    assert "new EventSource(`/api/agents/status/stream`)" not in agents_page
    assert "new EventSource('/api/agents/status/stream')" not in agents_page


def test_state_changed_sse_reaches_agent_state_listener():
    """The 'state_changed' SSE event must be listened for by the transport
    (source module AND the generated bundle — rebuild scripts/build_chat_ui.py
    if the bundle assert fails) and bridged to the document-level
    'evonic:agent-state-changed' event that the agent detail / sessions pages
    already handle with a debounced state re-fetch."""
    transport = read_repo_file("static/js/chat-ui/transport.js")
    bundle = read_repo_file("static/js/chat-ui.js")
    for src in (transport, bundle):
        assert "'state_changed'" in src
        assert "new CustomEvent('evonic:agent-state-changed'" in src
    agent_detail = read_repo_file("templates/agent_detail.html")
    assert "document.addEventListener('evonic:agent-state-changed'" in agent_detail


def test_sessions_refresh_restores_empty_buffer_thinking_placeholder():
    sessions = read_repo_file("templates/sessions.html")

    # Busy ownership is authoritative after refresh. Buffered events enrich the
    # current turn, but may be empty while a synchronous Explorer call is active.
    busy_pos = sessions.index("const ownsActiveTurn =")
    replay_pos = sessions.index("const replayEvents =", busy_pos)
    assert busy_pos < replay_pos
    assert "`/api/agents/${encodeURIComponent(selectedAgentId)}/busy`" in sessions
    assert "busyState && busyState.busy && busyState.session_id === sessionId" in sessions

    # A confirmed active selected session gets exactly one SSE-owned placeholder,
    # immediate Stop affordance, and persisted active reasoning state.
    assert "function _beginSessionTurn(startTs = null, anchor = null)" in sessions
    assert "if (!restoredReasoning && ownsActiveTurn)" in sessions
    assert "showStopBtn(true);" in sessions
    assert "saveReasoningState(sessionId, resumeSeq);" in sessions
    assert "if (!restoredReasoning) clearReasoningState();" in sessions

    # The first lifecycle event adopts the optimistic/restored bubble. Terminal
    # sequence state prevents stale replay and poll callbacks from reopening it.
    selection_guard = (
        "_selectGeneration !== gen || currentSessionId !== sessionId || "
        "currentAgentId !== selectedAgentId"
    )
    assert sessions.count(selection_guard) >= 3
    assert "evtName === 'turn_begin'" in sessions
    assert "_completeSessionTurn(seq);" in sessions
    assert "afterSeq = Math.max(afterSeq, _sessionTurn.lastSeq, _sessionTurn.terminalSeq);" in sessions
    assert "clearReasoningState();" in sessions
    assert "function disconnectSessionStream()" in sessions


def test_agent_detail_refresh_restores_only_matching_busy_session():
    detail = read_repo_file("templates/agent_detail.html")
    restore = detail[
        detail.index("async function restoreActiveReasoning()"):
        detail.index("let chatPollTimer = null")
    ]

    # /busy is authoritative even when replay contains a stale incomplete tail.
    # Idle and cross-session snapshots must both return before any bubble is created.
    replay_pos = restore.index("const replayEvents =")
    busy_pos = restore.index("const [eRes, busyRes] = await Promise.all")
    busy_guard_pos = restore.index(
        "!busyState.busy || busyState.session_id !== sessionId", busy_pos
    )
    bubble_pos = restore.index("let thinkingId = chatUI.showThinkingIndicator", busy_guard_pos)
    assert busy_pos < replay_pos < bubble_pos
    assert "`/api/agents/${encodeURIComponent(AGENT_ID)}/busy`" in restore
    assert "if (!replayEvents.length)" not in restore[replay_pos:bubble_pos]
    assert "let _restoringActiveReasoning = false;" in detail

    # The parallel fetch pair is followed by an agent/session epoch guard, and the
    # restored placeholder is reused by stream and poll rather than duplicated.
    guard = "epoch !== window._agentEpoch || sessionId !== _chatSessionId"
    assert guard in restore
    assert "_currentTurn = { abortController: null, thinkingId" in restore
    assert "chatUI.connectThinkingStream(" in restore
    assert "pollForResponse(thinkingId);" in restore
    assert "function _destroyCurrentTurn()" in detail
    assert "chatUI.removeThinkingIndicator(_currentTurn.thinkingId);" in detail
