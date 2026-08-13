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


def test_sessions_refresh_restores_active_turn_from_durable_stream():
    sessions = read_repo_file("templates/sessions.html")

    history_cursor = sessions.index("X-Evonic-Realtime-Cursor")
    connect = sessions.index("connectSessionStream(sessionId, realtimeCursor)")
    assert history_cursor < connect
    assert "/chat/events" not in sessions
    assert "`/api/agents/${encodeURIComponent(selectedAgentId)}/busy`" not in sessions
    assert "function _beginSessionTurn(startTs = null, anchor = null)" in sessions
    assert "evtName === 'turn_queued'" in sessions
    assert "showStopBtn(true);" in sessions
    assert "_completeSessionTurn(seq);" in sessions
    assert "afterSeq = Math.max(afterSeq, _sessionTurn.lastSeq, _sessionTurn.terminalSeq);" in sessions
    assert "es.addEventListener('history_resync_required'" in sessions
    assert "selectSession(sessionId).finally" in sessions

    error_handler = sessions[
        sessions.index("es.onerror = () =>"):
        sessions.index("function disconnectSessionStream()")
    ]
    assert "Last-Event-ID" in error_handler
    assert "es.close()" not in error_handler


def test_agent_detail_refresh_uses_one_persistent_session_stream():
    detail = read_repo_file("templates/agent_detail.html")
    restore = detail[
        detail.index("async function restoreActiveReasoning()"):
        detail.index("let _chatBusy = false")
    ]

    assert "X-Evonic-Realtime-Cursor" in detail
    assert "new EventSource(url)" in restore
    assert "if (_agentChatEs && _agentChatSessionId === sessionId) return;" in restore
    assert "'agent_busy_changed'" in restore
    assert "turn_queued" in restore
    assert "message_received" in restore
    assert "Last-Event-ID" in restore
    assert "/chat/events" not in restore
    assert "/busy" not in restore
    assert "pollForResponse" not in detail
    assert "if (data.agent_id && data.agent_id !== AGENT_ID) return;" in detail
    assert "es.addEventListener('history_resync_required'" in detail
    assert "loadChatHistory().finally" in detail

    soft_switch_start = detail.index("window.softSwitchAgent = async function")
    soft_switch = detail[soft_switch_start:detail.index("window.addEventListener('popstate'", soft_switch_start)]
    assert "resyncBusyBadge" not in soft_switch
    assert "showTab('chat')" in soft_switch


def test_chat_messages_sync_across_tabs_without_content_polling():
    sessions = read_repo_file("templates/sessions.html")
    detail = read_repo_file("templates/agent_detail.html")
    for source in (sessions, detail):
        assert "crypto.randomUUID" in source
        assert "client_message_id" in source
        assert "message_received" in source
        assert "_seenRealtimeMessages" in source
        assert "pollNewMessages" not in source
        assert "pollForResponse" not in source

    realtime = read_repo_file("static/js/realtime.js")
    assert "this._after = Math.max" in realtime
    assert "if (this._after) params.push('after=' + this._after);" in realtime
    assert "params.push('cursor_version=2');" in realtime
    assert "params.push('snapshot=' + (this._after ? '0' : '1'));" in realtime
    assert "this._disconnect();" in realtime
    assert "if (this._started) this._connect();" in realtime
    assert "_pauseBuffer" not in realtime


def test_realtime_regression_guards_cover_long_turns_badges_and_panels():
    realtime = read_repo_file("static/js/realtime.js")
    transport = read_repo_file("static/js/chat-ui/transport.js")
    turn = read_repo_file("static/js/chat-ui/turn.js")
    bundle = read_repo_file("static/js/chat-ui.js")
    detail = read_repo_file("templates/agent_detail.html")

    assert realtime.count("'panel_updated'") >= 2
    for source in (transport, bundle):
        assert "cursor_version=2&snapshot=1" in source
        assert "cursor_version=2&snapshot=' +" in source
        assert "(this._lastSeq > 0 ? '0' : '1')" in source
    for source in (turn, bundle):
        assert "STALE_TIMEOUT_MS" not in source
        assert "_staleTimeout" not in source

    assert "let _agentBusy = false;" in detail
    assert "updateBusyBadge(_chatBusy || _agentBusy);" in detail
    assert "updateBusyBadge(_agentBusy);" in detail
    assert "cursor_version=2&snapshot=1" in detail


def test_realtime_assets_are_cache_busted_and_legacy_buffers_are_gone():
    base = read_repo_file("templates/base.html")
    detail = read_repo_file("templates/agent_detail.html")
    sessions = read_repo_file("templates/sessions.html")
    event_stream = read_repo_file("backend/event_stream.py")
    agents = read_repo_file("routes/agents.py")
    update_manager = read_repo_file("backend/update_manager.py")
    runtime = read_repo_file("backend/agent_runtime/runtime.py")

    assert "realtime.js') }}?v=2" in base
    assert "chat-ui.js') }}?v=61" in detail
    assert "chat-ui.js') }}?v=61" in sessions
    assert "get_session_events" not in agents
    assert "register_web_listener" not in event_stream
    assert "_session_chat_seq" not in event_stream
    assert "register_listener" not in update_manager
    assert "_start_listener_cleanup" not in update_manager
    assert "result['response_message_id'] = response_message_id" in runtime
