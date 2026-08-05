/* approval-modal.js — Global approval modal dialog via event stream */

(function() {
'use strict';

const MODAL_HTML = `
<div id="ev-approval-modal" class="hidden fixed inset-0 z-[110] flex items-center justify-center p-4">
    <div id="ev-approval-overlay" class="absolute inset-0 bg-black/60 backdrop-blur-sm transition-opacity"></div>
    <div id="ev-approval-box" class="relative bg-white dark:bg-gray-800 rounded-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto shadow-2xl transform transition-all scale-95 opacity-0 ring-1 ring-gray-200 dark:ring-gray-700">
        <div class="p-5">
            <!-- Header -->
            <div class="flex items-start gap-3 mb-4">
                <div class="flex-shrink-0 w-10 h-10 rounded-full bg-orange-100 dark:bg-orange-900/40 flex items-center justify-center">
                    <svg class="w-5 h-5 text-orange-600 dark:text-orange-400" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
                        <path fill-rule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495ZM10 6a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 6Zm0 9a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z" clip-rule="evenodd"/>
                    </svg>
                </div>
                <div class="flex-1 min-w-0">
                    <h3 id="ev-approval-title" class="text-base font-bold text-gray-900 dark:text-gray-100">Approval Required</h3>
                    <p id="ev-approval-agent" class="text-xs text-gray-500 dark:text-gray-400 mt-0.5"></p>
                </div>
            </div>

            <!-- Description -->
            <div id="ev-approval-desc" class="text-sm text-gray-700 dark:text-gray-200 mb-3"></div>

            <!-- Risk level badge -->
            <div class="flex items-center gap-2 mb-3">
                <span class="text-xs font-semibold text-gray-500 dark:text-gray-400">Risk level:</span>
                <span id="ev-approval-risk" class="text-xs font-bold px-2 py-0.5 rounded-full border"></span>
                <span id="ev-approval-score" class="text-xs text-gray-400 dark:text-gray-500"></span>
            </div>

            <!-- Tool info -->
            <div class="mb-3 p-3 bg-gray-50 dark:bg-gray-900/50 rounded-lg">
                <div class="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-1">Tool</div>
                <code id="ev-approval-tool" class="text-sm font-mono text-gray-800 dark:text-gray-200 bg-white/60 dark:bg-gray-700/60 px-2 py-0.5 rounded"></code>
            </div>

            <!-- Code snippet (if available) -->
            <div id="ev-approval-code-block" class="hidden mb-3">
                <div class="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-1">Code to execute</div>
                <pre class="text-xs bg-gray-900 dark:bg-black text-gray-100 rounded-lg p-3 overflow-auto max-h-60 whitespace-pre-wrap break-all"><code id="ev-approval-code"></code></pre>
            </div>

            <!-- Reasons -->
            <div id="ev-approval-reasons-block" class="hidden mb-3">
                <div class="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-1">Reasons</div>
                <ul id="ev-approval-reasons" class="text-xs text-gray-600 dark:text-gray-300 list-disc list-inside space-y-0.5"></ul>
            </div>

            <!-- Action buttons -->
            <div class="flex gap-3 mt-4">
                <button id="ev-approval-approve-btn" class="flex-1 px-4 py-2.5 rounded-lg text-sm font-semibold bg-green-600 text-white hover:bg-green-700 active:bg-green-800 transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
                    <span class="flex items-center justify-center gap-2">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
                        Approve
                    </span>
                </button>
                <button id="ev-approval-reject-btn" class="flex-1 px-4 py-2.5 rounded-lg text-sm font-semibold bg-red-600 text-white hover:bg-red-700 active:bg-red-800 transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
                    <span class="flex items-center justify-center gap-2">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
                        Reject
                    </span>
                </button>
            </div>

            <!-- Status message -->
            <div id="ev-approval-status" class="hidden text-xs font-semibold mt-3 text-center"></div>
        </div>
    </div>
</div>`;

let _modalRoot = null;
let _currentData = null;
let _open = false;

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = String(text == null ? '' : text);
    return div.innerHTML;
}

// Render `code` as escaped HTML with the dangerous spans wrapped in <mark> so the
// reviewer can immediately see *where* the risk is. `highlights` are {start,end}
// character offsets (from the backend safety pipeline) into `code`. Overlapping
// spans are merged. Exposed globally so the chat timeline card reuses it verbatim.
function evHighlightCode(code, highlights) {
    code = String(code == null ? '' : code);
    if (!highlights || !highlights.length) return escapeHtml(code);
    // Sort + merge overlapping/adjacent spans, clamped to the code bounds.
    const spans = highlights
        .map(function(h) { return {start: Math.max(0, h.start | 0), end: Math.min(code.length, h.end | 0)}; })
        .filter(function(s) { return s.end > s.start; })
        .sort(function(a, b) { return a.start - b.start; });
    if (!spans.length) return escapeHtml(code);
    const merged = [spans[0]];
    for (let i = 1; i < spans.length; i++) {
        const last = merged[merged.length - 1];
        if (spans[i].start <= last.end) last.end = Math.max(last.end, spans[i].end);
        else merged.push(spans[i]);
    }
    let html = '';
    let cursor = 0;
    const MARK = '<mark class="ev-code-hl" style="background:#f59e0b;color:#111827;border-radius:2px;padding:0 1px;">';
    for (const s of merged) {
        if (s.start > cursor) html += escapeHtml(code.slice(cursor, s.start));
        html += MARK + escapeHtml(code.slice(s.start, s.end)) + '</mark>';
        cursor = s.end;
    }
    if (cursor < code.length) html += escapeHtml(code.slice(cursor));
    return html;
}
window.evHighlightCode = evHighlightCode;

function ensureModal() {
    if (_modalRoot) return;
    const frag = document.createElement('div');
    frag.innerHTML = MODAL_HTML;
    _modalRoot = frag.firstElementChild;
    document.body.appendChild(_modalRoot);

    document.getElementById('ev-approval-reject-btn').addEventListener('click', function() {
        resolveApproval('reject');
    });
    document.getElementById('ev-approval-approve-btn').addEventListener('click', function() {
        resolveApproval('approve');
    });
}

function closeModal() {
    if (!_modalRoot || !_open) return;
    _open = false;
    const box = document.getElementById('ev-approval-box');
    box.classList.remove('scale-100', 'opacity-100');
    box.classList.add('scale-95', 'opacity-0');
    setTimeout(function() {
        if (_open) return;  // Guard: openModal was called before hide timeout
        _modalRoot.classList.add('hidden');
    }, 200);
}

function openModal() {
    ensureModal();
    _open = true;
    _modalRoot.classList.remove('hidden');
    requestAnimationFrame(function() {
        if (!_open) return;  // Guard: closeModal was called before this animation frame
        const box = document.getElementById('ev-approval-box');
        box.classList.remove('scale-95', 'opacity-0');
        box.classList.add('scale-100', 'opacity-100');
    });
}

function populateModal(data) {
    ensureModal();
    const agentName = data.source_agent_name || data.agent_name || data.agent_id || 'Unknown';
    const agentId = data.agent_id || '';
    document.getElementById('ev-approval-agent').textContent = 'Agent: ' + agentName + (agentId ? ' (' + agentId + ')' : '');

    const info = data.approval_info || {};
    const desc = info.description || info.summary || 'This action requires approval.';
    document.getElementById('ev-approval-desc').textContent = desc;

    const riskLevel = (info.risk_level || 'medium').toLowerCase();
    const riskEl = document.getElementById('ev-approval-risk');
    riskEl.textContent = riskLevel.charAt(0).toUpperCase() + riskLevel.slice(1);
    if (riskLevel === 'high') {
        riskEl.className = 'text-xs font-bold px-2 py-0.5 rounded-full border text-red-600 dark:text-red-400 border-red-300 dark:border-red-700 bg-red-50 dark:bg-red-950/40';
    } else if (riskLevel === 'low') {
        riskEl.className = 'text-xs font-bold px-2 py-0.5 rounded-full border text-yellow-600 dark:text-yellow-400 border-yellow-300 dark:border-yellow-700 bg-yellow-50 dark:bg-yellow-950/40';
    } else {
        riskEl.className = 'text-xs font-bold px-2 py-0.5 rounded-full border text-orange-600 dark:text-orange-400 border-orange-300 dark:border-orange-700 bg-orange-50 dark:bg-orange-950/40';
    }

    const scoreEl = document.getElementById('ev-approval-score');
    if (data.score != null) {
        scoreEl.textContent = 'Score: ' + data.score;
        scoreEl.classList.remove('hidden');
    } else {
        scoreEl.classList.add('hidden');
    }

    const toolName = data.tool || data.tool_name || 'Unknown';
    const args = data.args || data.tool_args || {};
    const filePath = info.file_path || args.file_path || null;
    if (filePath) {
        document.getElementById('ev-approval-tool').textContent = toolName + ': ' + filePath;
    } else {
        document.getElementById('ev-approval-tool').textContent = toolName;
    }
    const codeSnippet = args.script || args.code || null;
    const codeBlock = document.getElementById('ev-approval-code-block');
    if (codeSnippet) {
        codeBlock.classList.remove('hidden');
        const codeEl = document.getElementById('ev-approval-code');
        codeEl.innerHTML = evHighlightCode(codeSnippet, info.highlights);
        // Center the first dangerous span within the scrollable <pre> only — avoid
        // scrollIntoView, which could scroll the outer modal/page instead.
        requestAnimationFrame(function() {
            const mark = codeEl.querySelector('.ev-code-hl');
            const pre = codeEl.parentElement;
            if (mark && pre) {
                const preRect = pre.getBoundingClientRect();
                const markRect = mark.getBoundingClientRect();
                pre.scrollTop += (markRect.top - preRect.top) - pre.clientHeight / 2 + markRect.height / 2;
            }
        });
    } else {
        codeBlock.classList.add('hidden');
    }

    const reasons = data.reasons || [];
    const reasonsBlock = document.getElementById('ev-approval-reasons-block');
    if (reasons.length > 0) {
        reasonsBlock.classList.remove('hidden');
        const ul = document.getElementById('ev-approval-reasons');
        ul.innerHTML = reasons.map(function(r) {
            return '<li>' + escapeHtml(r) + '</li>';
        }).join('');
    } else {
        reasonsBlock.classList.add('hidden');
    }

    // Reset buttons and status
    document.getElementById('ev-approval-approve-btn').disabled = false;
    document.getElementById('ev-approval-reject-btn').disabled = false;
    document.getElementById('ev-approval-status').classList.add('hidden');
}

async function resolveApproval(decision) {
    if (!_currentData) return;
    const data = _currentData;
    const agentId = data.agent_id;
    const approvalId = data.approval_id;

    const approveBtn = document.getElementById('ev-approval-approve-btn');
    const rejectBtn = document.getElementById('ev-approval-reject-btn');
    const statusEl = document.getElementById('ev-approval-status');

    approveBtn.disabled = true;
    rejectBtn.disabled = true;

    try {
        const res = await fetch('/api/agents/' + encodeURIComponent(agentId) + '/chat/approve', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({approval_id: approvalId, decision: decision}),
        });
        if (res.ok) {
            statusEl.textContent = decision === 'approve' ? '\u2713 Approved' : '\u2717 Rejected';
            statusEl.className = 'text-xs font-semibold mt-3 text-center ' + (decision === 'approve' ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400');
            statusEl.classList.remove('hidden');
            setTimeout(closeModal, 1500);
        } else if (res.status === 409) {
            // Already resolved (e.g. from another tab) — just close the modal
            closeModal();
        } else {
            const body = await res.json().catch(function() { return {}; });
            statusEl.textContent = body.error || 'Failed to submit decision.';
            statusEl.className = 'text-xs font-semibold mt-3 text-center text-red-600 dark:text-red-400';
            statusEl.classList.remove('hidden');
            approveBtn.disabled = false;
            rejectBtn.disabled = false;
        }
    } catch (e) {
        statusEl.textContent = 'Network error. Please try again.';
        statusEl.className = 'text-xs font-semibold mt-3 text-center text-red-600 dark:text-red-400';
        statusEl.classList.remove('hidden');
        approveBtn.disabled = false;
        rejectBtn.disabled = false;
    }
}

// Listen for approval events dispatched as custom DOM events
document.addEventListener('evonic:approval-required', function(e) {
    _currentData = e.detail || {};
    if (!_currentData.approval_id) return;
    populateModal(_currentData);
    openModal();
});

// Also listen for resolution events to auto-close if already approved elsewhere
document.addEventListener('evonic:approval-resolved', function(e) {
    const data = e.detail || {};
    if (_currentData && data.approval_id === _currentData.approval_id) {
        closeModal();
    }
});

// Global realtime listener: subscribe via RealtimeClient to the unified stream.
// This receives approval events (any agent, any session) via the 'approvals' channel.
(function() {
    var _sse = null;
    var _reconnectTimer = null;
    var _enabled = false;
    var _realtimeHandlersBound = false;

    // --- Polling safety-net -------------------------------------------------
    // SSE 'approval_required' delivery is best-effort: a single dropped event
    // (ring overflow, producer race, transient stall) leaves the modal missing
    // for the whole 300s the agent blocks. This poll guarantees the modal shows
    // regardless of SSE. It runs only while the tab is visible AND at least one
    // agent is busy (an approval-blocked agent stays busy for its whole turn),
    // so it costs nothing when the workspace is idle.
    var POLL_INTERVAL_MS = 3000;   // pending-approval poll cadence while busy
    var BUSY_REFRESH_MS = 9000;    // server busy-state refresh cadence (SSE-independent)
    var _pollTimer = null;
    var _busyAgents = {};          // agent_id -> true
    var _lastBusyRefresh = 0;

    function _anyBusy() {
        for (var k in _busyAgents) { if (_busyAgents[k]) return true; }
        return false;
    }

    // Refresh busy state from the server. This is the robustness anchor: busy
    // detection must NOT depend on the 'status' SSE channel, because the exact
    // failure this poll guards against (SSE not delivering) also drops the
    // agent_busy_changed events. Polling /api/agents/busy on a slow cadence means
    // we still learn an agent is busy — and then start polling for its approval —
    // even when SSE is fully dead. Returns a promise so the tick can chain on it.
    function _refreshBusy() {
        _lastBusyRefresh = Date.now();
        return fetch('/api/agents/busy', { headers: { 'Accept': 'application/json' } })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) {
                _busyAgents = {};
                if (d && d.busy) {
                    Object.keys(d.busy).forEach(function (aid) { _busyAgents[aid] = true; });
                }
            })
            .catch(function () {});
    }

    function _fetchPending() {
        return fetch('/api/approvals/pending', { headers: { 'Accept': 'application/json' } })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) {
                if (!d) return;
                var pend = d.pending || [];
                var ids = {};
                pend.forEach(function (p) { if (p.approval_id) ids[p.approval_id] = p; });
                // Close a modal whose approval is no longer pending — covers a
                // missed 'approval_resolved' event and server-side timeout.
                if (_open && _currentData && _currentData.approval_id &&
                    !ids[_currentData.approval_id]) {
                    closeModal();
                    return;
                }
                // Open the modal for a pending approval that isn't shown yet —
                // covers a missed 'approval_required' event.
                if (!_open && pend.length > 0) {
                    _currentData = pend[0];
                    populateModal(pend[0]);
                    openModal();
                }
            })
            .catch(function () {});
    }

    // Timer tick. Two cadences:
    //  • Slow (BUSY_REFRESH_MS): an SSE-independent safety sweep — refresh busy
    //    state AND check pending approvals UNCONDITIONALLY. This is what makes the
    //    net truly robust: an approval is caught even if SSE is dead and even if
    //    the agent's busy flag lapsed (e.g. a long turn whose 600s busy-TTL
    //    expired while it sat blocked on the approval).
    //  • Fast (POLL_INTERVAL_MS): while an agent is busy or a modal is open, poll
    //    pending so the modal appears/closes within ~3s.
    // Idle + visible costs two tiny in-memory reads every ~9s; nothing when hidden.
    function _pollPending() {
        if (document.visibilityState !== 'visible') return;
        if ((Date.now() - _lastBusyRefresh) >= BUSY_REFRESH_MS) {
            _refreshBusy().then(_fetchPending);
            return;
        }
        if (_anyBusy() || _open) _fetchPending();
    }

    function _startPoll() {
        if (_pollTimer) return;
        _refreshBusy();
        _pollTimer = setInterval(_pollPending, POLL_INTERVAL_MS);
    }

    function _stopPoll() {
        if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
    }

    function _bindBusyTracking(rt) {
        // Track agent busy/idle from the shared 'status' channel so the poll
        // only fires while a turn is in progress.
        rt.on('status', 'agent_busy_changed', function (data) {
            if (!data || !data.agent_id) return;
            if (data.busy) _busyAgents[data.agent_id] = true;
            else delete _busyAgents[data.agent_id];
        });
    }

    function _connectSSE() {
        if (!_enabled) return;
        if (_sse) return;

        // Use shared RealtimeClient if available (consolidated singleton)
        if (typeof getSharedRealtime !== 'undefined') {
            var rt = getSharedRealtime();
            if (!_realtimeHandlersBound) {
                rt.on('approvals', 'approval_required', function(data) {
                    if (!data.approval_id) return;
                    _currentData = data;
                    populateModal(data);
                    openModal();
                });
                rt.on('approvals', 'approval_resolved', function(data) {
                    if (_currentData && data.approval_id === _currentData.approval_id) {
                        closeModal();
                    }
                });
                _bindBusyTracking(rt);
                _realtimeHandlersBound = true;
            }
            // Shared connection — lifecycle managed by getSharedRealtime()
            _sse = { close: function() { /* shared */ } };
            return;
        }

        // Fallback: standalone RealtimeClient
        if (typeof RealtimeClient !== 'undefined') {
            var rt = window._evApprovalRT = window._evApprovalRT || new RealtimeClient({
                channels: 'approvals'
            });
            if (!_realtimeHandlersBound) {
                rt.on('approvals', 'approval_required', function(data) {
                    if (!data.approval_id) return;
                    _currentData = data;
                    populateModal(data);
                    openModal();
                });
                rt.on('approvals', 'approval_resolved', function(data) {
                    if (_currentData && data.approval_id === _currentData.approval_id) {
                        closeModal();
                    }
                });
                _realtimeHandlersBound = true;
            }
            rt.start();
            _sse = { close: function() { rt.stop(); } };
            return;
        }

        // Fallback: old EventSource
        var es;
        try {
            es = new EventSource('/api/approvals/stream');
            _sse = es;
        } catch (e) {
            return;
        }

        es.addEventListener('approval_required', function(e) {
            var data = JSON.parse(e.data);
            if (!data.approval_id) return;
            _currentData = data;
            populateModal(data);
            openModal();
        });

        es.addEventListener('approval_resolved', function(e) {
            var data = JSON.parse(e.data);
            if (_currentData && data.approval_id === _currentData.approval_id) {
                closeModal();
            }
        });

        es.onerror = function() {
            es.close();
            if (_sse === es) _sse = null;
            if (!_enabled) return;
            if (_reconnectTimer) clearTimeout(_reconnectTimer);
            // Reconnect after a short delay
            _reconnectTimer = setTimeout(function() {
                _reconnectTimer = null;
                _connectSSE();
            }, 3000);
        };
    }

    function _startSSE() {
        _enabled = true;
        _startPoll();   // pull-based safety-net, independent of the SSE connection
        if (_sse) return;
        _connectSSE();
    }

    function _closeSSE() {
        _enabled = false;
        _stopPoll();
        if (_reconnectTimer) {
            clearTimeout(_reconnectTimer);
            _reconnectTimer = null;
        }
        if (!_sse) return;
        try {
            _sse.close();
        } catch (e) {}
        _sse = null;
    }

    // Start SSE when the page loads and when it becomes visible
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _startSSE);
    } else {
        _startSSE();
    }
    document.addEventListener('visibilitychange', function() {
        if (document.visibilityState === 'visible') {
            _startSSE();
            _refreshBusy();   // refresh busy state after being backgrounded
        }
    });
    window.addEventListener('pagehide', _closeSSE);
    window.addEventListener('beforeunload', _closeSSE);
    window.addEventListener('pageshow', _startSSE);
})();
})();
