/**
 * Agent Sidebar — persistent left sidebar across all Evonic pages.
 * Shows agent avatars sorted by recent activity, with busy-state indicators,
 * hover tooltips, and click-to-navigate to agent detail chat tab.
 */

/** Simple hash function for deterministic avatar background colors */
function _sidebarHash(str) {
    var h = 0;
    for (var i = 0; i < str.length; i++) {
        h = ((h << 5) - h) + str.charCodeAt(i);
        h |= 0;
    }
    return Math.abs(h);
}

/** HSL color palette for avatar backgrounds — vibrant, dark-friendly */
var _AVATAR_COLORS = [
    'hsl(200, 70%, 40%)',
    'hsl(260, 60%, 45%)',
    'hsl(330, 60%, 42%)',
    'hsl(160, 55%, 35%)',
    'hsl(30, 70%, 38%)',
    'hsl(290, 50%, 40%)',
    'hsl(80, 50%, 32%)',
    'hsl(10, 65%, 40%)',
];

function _sidebarAvatarColor(agentId) {
    return _AVATAR_COLORS[_sidebarHash(agentId) % _AVATAR_COLORS.length];
}

/** --- Unread state (persists across page navigations via sessionStorage) --- */
var _UNREAD_KEY = 'evonic_unread_agents';

/** Last unseen final-response payload per agent: { agentId: {agent_name, response, session_id, external_user_id} } */
var _UNREAD_PAYLOAD_KEY = 'evonic_unread_payloads';

function _getUnreadSet() {
    try {
        return new Set(JSON.parse(sessionStorage.getItem(_UNREAD_KEY) || '[]'));
    } catch (_) { return new Set(); }
}

function _markUnread(agentId) {
    try {
        var s = _getUnreadSet();
        s.add(agentId);
        sessionStorage.setItem(_UNREAD_KEY, JSON.stringify(Array.from(s)));
    } catch (_) {}
}

function _getUnreadPayloads() {
    try {
        return JSON.parse(sessionStorage.getItem(_UNREAD_PAYLOAD_KEY) || '{}') || {};
    } catch (_) { return {}; }
}

function _storeUnreadPayload(agentId, payload) {
    try {
        var map = _getUnreadPayloads();
        map[agentId] = {
            agent_name: payload.agent_name || '',
            // Bubble shows ~140 chars; cap stored text to protect sessionStorage quota
            response: String(payload.response || '').substring(0, 500),
            session_id: payload.session_id || '',
            external_user_id: payload.external_user_id || ''
        };
        sessionStorage.setItem(_UNREAD_PAYLOAD_KEY, JSON.stringify(map));
    } catch (_) {}
}

function _getUnreadPayload(agentId) {
    var p = _getUnreadPayloads()[agentId];
    return (p && p.response) ? p : null;
}

function _clearUnread(agentId) {
    try {
        var s = _getUnreadSet();
        s.delete(agentId);
        sessionStorage.setItem(_UNREAD_KEY, JSON.stringify(Array.from(s)));
    } catch (_) {}
    try {
        var map = _getUnreadPayloads();
        delete map[agentId];
        sessionStorage.setItem(_UNREAD_PAYLOAD_KEY, JSON.stringify(map));
    } catch (_) {}
    var avatar = document.querySelector(
        '#agent-sidebar .agent-avatar[data-agent-id="' + CSS.escape(agentId) + '"]'
    );
    if (avatar) avatar.removeAttribute('data-unread');
}

/** Update which avatar has the .selected ring based on current URL */
function _updateSelectedAvatar() {
    var match = window.location.pathname.match(/^\/agents\/([^/]+)/);
    var currentId = match ? decodeURIComponent(match[1]) : null;
    document.querySelectorAll('#agent-sidebar .agent-avatar').forEach(function (el) {
        el.classList.toggle('selected', currentId !== null && el.getAttribute('data-agent-id') === currentId);
    });
}

/**
 * Navigate to an agent's chat tab. On agent detail pages softSwitchAgent()
 * swaps the page in place without a full reload; everywhere else (and on
 * soft-switch failure) fall back to a normal navigation.
 */
function _navigateToAgentChat(agentId) {
    _clearUnread(agentId);
    dismissBubble(agentId);
    var dest = '/agents/' + encodeURIComponent(agentId) + '#chat';
    if (typeof window.softSwitchAgent === 'function') {
        // softSwitchAgent manages its own loading bar across the in-place swap
        window.softSwitchAgent(agentId).then(function (ok) {
            if (ok) {
                _updateSelectedAvatar();
            } else {
                window.location = dest;
            }
        });
    } else {
        // Full navigation: show the bar until the new page replaces it
        if (window.startNavProgress) window.startNavProgress();
        window.location = dest;
    }
}

/** Current tooltip element reference */
var _currentTooltip = null;

/** Fetch sidebar data and render */
async function fetchSidebarAgents() {
    try {
        var resp = await fetch('/api/dashboard/sidebar', { credentials: 'same-origin' });
        if (!resp.ok) return;
        var data = await resp.json();
        renderSidebar(data.agents || []);
    } catch (e) {
        console.warn('[agent-sidebar] fetch failed:', e);
    }
}

/** Render agent avatars inside #agent-sidebar */
function renderSidebar(agents) {
    var sidebar = document.getElementById('agent-sidebar');
    if (!sidebar) return;

    sidebar.innerHTML = '';

    agents.forEach(function (agent) {
        var avatar = document.createElement('div');
        avatar.className = 'agent-avatar';
        avatar.setAttribute('data-agent-id', agent.id);
        avatar.setAttribute('data-busy', agent.busy ? 'true' : 'false');
        if (_getUnreadSet().has(agent.id)) avatar.setAttribute('data-unread', 'true');
        avatar.setAttribute('title', agent.name);

        if (agent.avatar_path) {
            // Render custom avatar image
            var img = document.createElement('img');
            img.src = '/api/agents/' + encodeURIComponent(agent.id) + '/avatar?size=small';
            img.alt = agent.name;
            img.className = 'agent-avatar-img';
            img.onerror = function () {
                // Fallback to initial letter on load error
                img.style.display = 'none';
                var fallback = document.createElement('span');
                fallback.textContent = agent.name.charAt(0).toUpperCase();
                avatar.appendChild(fallback);
                avatar.style.backgroundColor = _sidebarAvatarColor(agent.id);
            };
            avatar.appendChild(img);
        } else {
            // No custom avatar: show initial letter with colored background
            avatar.style.backgroundColor = _sidebarAvatarColor(agent.id);
            var letter = document.createElement('span');
            letter.textContent = agent.name.charAt(0).toUpperCase();
            avatar.appendChild(letter);
        }

        avatar.addEventListener('click', function () {
            // Mobile has no hover: first tap on an unread avatar shows the
            // final-response bubble (tap the bubble to open its session);
            // tapping the avatar again navigates to agent detail as usual.
            if (window.innerWidth <= 768) {
                var payload = avatar.hasAttribute('data-unread') ? _getUnreadPayload(agent.id) : null;
                if (payload && !_activeBubbles[agent.id]) {
                    showBubblePopup(agent.id, payload.agent_name || agent.name,
                        payload.response, payload.session_id, payload.external_user_id,
                        { hover: true });
                    return;
                }
            }
            _navigateToAgentChat(agent.id);
        });

        avatar.addEventListener('mouseenter', function (e) {
            // Skip tooltip popup on mobile — tap goes directly to agent detail
            if (window.innerWidth <= 768) return;
            // Unseen final response: show its callout instead of the agent
            // tooltip so the user can click through to the session page.
            var payload = avatar.hasAttribute('data-unread') ? _getUnreadPayload(agent.id) : null;
            if (payload) {
                var entry = _activeBubbles[agent.id];
                if (entry && entry.hover) {
                    _cancelBubbleDismiss(agent.id);
                } else {
                    showBubblePopup(agent.id, payload.agent_name || agent.name,
                        payload.response, payload.session_id, payload.external_user_id,
                        { hover: true });
                }
                return;
            }
            showTooltip(e, agent);
        });

        avatar.addEventListener('mouseleave', function () {
            hideTooltip();
            // On mobile the bubble stays until the user taps it, the avatar,
            // or anywhere outside (synthetic mouseleave from taps must not
            // dismiss it before the user can tap the bubble).
            if (window.innerWidth <= 768) return;
            var entry = _activeBubbles[agent.id];
            if (entry && entry.hover) {
                _scheduleBubbleDismiss(agent.id, _BUBBLE_HOVER_GRACE);
            }
        });

        sidebar.appendChild(avatar);

        // Page-load race: apply any busy event that arrived while this agent's
        // avatar did not exist yet. The API snapshot (agent.busy above) is
        // newer than buffered events, so only promote a buffered busy:true —
        // i.e. show the spinning ring when the agent was working during load
        // even if it has since gone idle (BUSY_HOLD_MS / resyncBusyState then
        // reconcile it with the authoritative server state).
        if (_pendingBusyState[agent.id] && _pendingBusyState[agent.id].busy &&
                avatar.getAttribute('data-busy') !== 'true') {
            updateBusyAvatar({ agent_id: agent.id, busy: true });
        }
        delete _pendingBusyState[agent.id];
    });

    // Apply saved sidebar state after all elements (including burger) are rendered
    _applySidebarState();
    _updateSelectedAvatar();
}

/** Create and position tooltip */
function showTooltip(e, agent) {
    hideTooltip();

    var tooltip = document.createElement('div');
    tooltip.className = 'agent-sidebar-tooltip';

    var nameEl = document.createElement('span');
    nameEl.className = 'tt-name';
    nameEl.textContent = agent.name;

    var descEl = document.createElement('span');
    descEl.className = 'tt-desc';
    descEl.textContent = agent.description || '';

    var badge = document.createElement('span');
    badge.className = 'tt-badge ' + (agent.busy ? 'busy' : 'idle');
    badge.textContent = agent.busy ? 'Busy' : 'Idle';

    tooltip.appendChild(nameEl);
    if (agent.description) tooltip.appendChild(descEl);
    tooltip.appendChild(badge);

    document.body.appendChild(tooltip);

    // Position to the right of the avatar
    var avatarRect = e.currentTarget.getBoundingClientRect();
    var top = avatarRect.top + avatarRect.height / 2 - tooltip.offsetHeight / 2;

    // Keep tooltip within viewport vertically
    if (top < 8) top = 8;
    if (top + tooltip.offsetHeight > window.innerHeight - 8) {
        top = window.innerHeight - tooltip.offsetHeight - 8;
    }

    tooltip.style.left = (avatarRect.right + 10) + 'px';
    tooltip.style.top = top + 'px';

    _currentTooltip = tooltip;
}

/** Remove tooltip */
function hideTooltip() {
    if (_currentTooltip) {
        _currentTooltip.remove();
        _currentTooltip = null;
    }
}

/** Active bubble popup map: agentId -> { element, timer, hover } */
var _activeBubbles = {};

/** Maximum characters to show in the bubble preview */
var _BUBBLE_MAX_CHARS = 140;

/** Auto-dismiss timeout in milliseconds */
var _BUBBLE_TIMEOUT = 7000;

/** Grace period for hover bubbles — lets the pointer cross the gap from avatar to bubble */
var _BUBBLE_HOVER_GRACE = 300;

/** (Re)schedule dismissal of an active bubble after delayMs */
function _scheduleBubbleDismiss(agentId, delayMs) {
    var entry = _activeBubbles[agentId];
    if (!entry) return;
    clearTimeout(entry.timer);
    entry.timer = setTimeout(function () {
        dismissBubble(agentId);
    }, delayMs);
}

/** Cancel a pending bubble dismissal (pointer re-entered avatar or bubble) */
function _cancelBubbleDismiss(agentId) {
    var entry = _activeBubbles[agentId];
    if (entry) clearTimeout(entry.timer);
}

/**
 * Strip markdown artifacts from text for plain-text snippet display.
 * Not a full parser — just removes common markers so the bubble preview
 * reads cleanly (it is never rendered as markdown).
 */
function _stripMarkdown(text) {
    if (!text) return '';
    return text
        // HTML tags
        .replace(/<\/?[a-z][^>]*>/gi, '')
        // Code fence lines (keep the code text itself)
        .replace(/```[^\n]*\n?/g, '')
        // Inline code
        .replace(/`([^`]+)`/g, '$1')
        // Images (before links)
        .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
        // Links
        .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
        // Headings
        .replace(/^#{1,6}\s+/gm, '')
        // Blockquotes
        .replace(/^>\s?/gm, '')
        // List markers
        .replace(/^\s*([-*+]|\d+\.)\s+/gm, '')
        // Horizontal rules (before bold/italic so *** and ___ are fully stripped)
        .replace(/^(\s*[-*_]){3,}\s*$/gm, '')
        // Bold with ** (non-greedy, handles nested italic)
        .replace(/\*\*(.*?)\*\*/g, '$1')
        // Bold with __ (requires word boundary to avoid matching __init__ etc.)
        .replace(/(^|\s)__([^_]+)__(?=\s|$|[.,;:!?)])/g, '$1$2')
        // Italic with * (rarely conflicts with identifiers)
        .replace(/\*([^*]+)\*/g, '$1')
        // Italic with _ (requires word boundary to avoid matching file_names)
        .replace(/(^|\s)_([^_]+)_(?=\s|$|[.,;:!?)])/g, '$1$2')
        // Collapse whitespace — the preview is a one-line snippet
        .replace(/\s+/g, ' ')
        .trim();
}

/**
 * Truncate text to a maximum length at a word boundary.
 * Returns the truncated text with ellipsis if shortened.
 */
function _truncatePreview(text, maxLen) {
    if (!text) return '';
    text = text.trim();
    if (text.length <= maxLen) return text;

    // Try to break at the last space within the limit
    var truncated = text.substring(0, maxLen);
    var lastSpace = truncated.lastIndexOf(' ');
    if (lastSpace > maxLen * 0.6) {
        truncated = truncated.substring(0, lastSpace);
    }
    // Remove trailing punctuation before ellipsis
    truncated = truncated.replace(/[,;:.!?\\-]+$/, '');
    return truncated + '…';
}

/**
 * Dismiss a bubble popup for the given agent ID.
 * If no agentId is given, dismiss all bubbles.
 */
function dismissBubble(agentId) {
    if (agentId) {
        var entry = _activeBubbles[agentId];
        if (entry) {
            clearTimeout(entry.timer);
            entry.element.remove();
            delete _activeBubbles[agentId];
        }
    } else {
        Object.keys(_activeBubbles).forEach(function (id) {
            dismissBubble(id);
        });
    }
}

/**
 * Show a bubble popup next to an agent's avatar in the sidebar.
 * The bubble displays a truncated preview of the agent's final response.
 * Clicking the bubble navigates to the session page (when available) or the
 * agent detail chat tab.
 * opts.hover: bubble was opened by hovering an unread avatar — it stays up
 * while the pointer is over the avatar or the bubble instead of auto-dismissing.
 */
function showBubblePopup(agentId, agentName, response, sessionId, externalUserId, opts) {
    var avatar = document.querySelector(
        '#agent-sidebar .agent-avatar[data-agent-id="' + CSS.escape(agentId) + '"]'
    );
    if (!avatar) return;

    // Dismiss any existing bubble for this agent
    dismissBubble(agentId);

    var preview = _truncatePreview(_stripMarkdown(response), _BUBBLE_MAX_CHARS);
    if (!preview) return;

    var bubble = document.createElement('div');
    bubble.className = 'agent-bubble-popup';
    if (sessionId) bubble.setAttribute('data-session-id', sessionId);
    if (externalUserId) bubble.setAttribute('data-external-user-id', externalUserId);

    // Arrow pointing left toward the avatar
    var arrow = document.createElement('div');
    arrow.className = 'agent-bubble-arrow';

    // Header with agent name
    var header = document.createElement('div');
    header.className = 'agent-bubble-header';
    header.textContent = agentName || agentId;

    // Body with truncated response
    var body = document.createElement('div');
    body.className = 'agent-bubble-body';
    body.textContent = preview;

    // Close button
    var closeBtn = document.createElement('button');
    closeBtn.className = 'agent-bubble-close';
    closeBtn.innerHTML = '×';
    closeBtn.setAttribute('aria-label', 'Dismiss');
    closeBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        dismissBubble(agentId);
    });

    bubble.appendChild(arrow);
    bubble.appendChild(header);
    bubble.appendChild(body);
    bubble.appendChild(closeBtn);

    // Click on bubble (not on close button) navigates to agent chat
    bubble.addEventListener('click', function (e) {
        if (e.target === closeBtn) return;
        dismissBubble(agentId);
        var bubbleSessionId = bubble.getAttribute('data-session-id');
        var externalUserId = bubble.getAttribute('data-external-user-id');
        if (bubbleSessionId && externalUserId !== 'web_test') {
            _clearUnread(agentId);
            sessionStorage.setItem('evonic_last_session', bubbleSessionId);
            window.location = '/sessions';
        } else {
            _navigateToAgentChat(agentId);
        }
    });

    document.body.appendChild(bubble);

    // Position the bubble to the right of the avatar
    var avatarRect = avatar.getBoundingClientRect();
    var bubbleLeft = avatarRect.right + 12;
    var bubbleTop = avatarRect.top + avatarRect.height / 2 - 30;

    // Keep within viewport
    if (bubbleLeft + 280 > window.innerWidth - 12) {
        bubbleLeft = avatarRect.left - 280 - 12;
        bubble.classList.add('bubble-left');
    }
    if (bubbleTop < 8) bubbleTop = 8;
    if (bubbleTop + 80 > window.innerHeight - 8) {
        bubbleTop = window.innerHeight - 88;
    }

    bubble.style.left = bubbleLeft + 'px';
    bubble.style.top = bubbleTop + 'px';

    // Fade in
    requestAnimationFrame(function () {
        bubble.classList.add('bubble-visible');
    });

    if (opts && opts.hover) {
        // Hover mode: no auto-dismiss; pointer entering the bubble keeps it
        // alive, leaving it schedules dismissal after the grace period.
        _activeBubbles[agentId] = { element: bubble, timer: null, hover: true };
        bubble.addEventListener('mouseenter', function () {
            _cancelBubbleDismiss(agentId);
        });
        bubble.addEventListener('mouseleave', function () {
            if (window.innerWidth <= 768) return;
            _scheduleBubbleDismiss(agentId, _BUBBLE_HOVER_GRACE);
        });
    } else {
        // Push mode (real-time event): auto-dismiss after the timeout
        var timer = setTimeout(function () {
            dismissBubble(agentId);
        }, _BUBBLE_TIMEOUT);
        _activeBubbles[agentId] = { element: bubble, timer: timer };
    }
}

/**
 * Global click handler: dismiss any visible bubble when user clicks outside it.
 */
document.addEventListener('click', function (e) {
    var clickedOnBubble = e.target.closest('.agent-bubble-popup');
    var clickedOnAvatar = e.target.closest('#agent-sidebar .agent-avatar');
    if (!clickedOnBubble && !clickedOnAvatar) {
        dismissBubble();
    }
});

/**
 * Handle an agent_turn_complete event: mark the agent unread, persist the
 * final-response payload (for re-showing the bubble on avatar hover), and
 * show the push bubble — unless the user is already on that agent's page.
 */
function _onTurnComplete(payload) {
    if (window.location.pathname === '/agents/' + payload.agent_id) return;
    _markUnread(payload.agent_id);
    _storeUnreadPayload(payload.agent_id, payload);
    var av = document.querySelector('#agent-sidebar .agent-avatar[data-agent-id="' + CSS.escape(payload.agent_id) + '"]');
    if (av) av.setAttribute('data-unread', 'true');
    showBubblePopup(payload.agent_id, payload.agent_name, payload.response, payload.session_id, payload.external_user_id);
}

/** Subscribe via shared RealtimeClient for real-time busy state updates and turn-complete notifications */
var _busySSE = null;
var _busyReconnectTimer = null;
var _busyClearTimers = {};  // agent_id -> setTimeout id for minimum busy-hold debounce
var BUSY_HOLD_MS = 300;    // minimum visible duration for the spinning ring

/** Busy events received before the avatar was rendered (page-load race). */
var _pendingBusyState = {};  // agent_id -> { busy: bool, ts: number }

function updateBusyAvatar(payload) {
    if (!payload || !payload.agent_id) return;
    var avatar = document.querySelector(
        '#agent-sidebar .agent-avatar[data-agent-id="' + CSS.escape(payload.agent_id) + '"]'
    );
    if (!avatar) {
        // Page-load race: the sidebar is still showing skeleton placeholders,
        // so there is no avatar to apply this event to yet. Buffer it instead
        // of dropping it — renderSidebar() applies pending state as soon as
        // the avatar exists. Without this, a busy event that arrives during
        // page load is lost forever and the spinning ring never appears, even
        // though the agent was working (inconsistent ring on refresh).
        _pendingBusyState[payload.agent_id] = { busy: !!payload.busy, ts: Date.now() };
        return;
    }
    delete _pendingBusyState[payload.agent_id];
    if (payload.busy) {
        // Cancel any pending clear — agent is still busy
        if (_busyClearTimers[payload.agent_id]) {
            clearTimeout(_busyClearTimers[payload.agent_id]);
            delete _busyClearTimers[payload.agent_id];
        }
        avatar.setAttribute('data-busy', 'true');
    } else {
        // Cancel any previous pending clear for this agent
        if (_busyClearTimers[payload.agent_id]) {
            clearTimeout(_busyClearTimers[payload.agent_id]);
        }
        // Delay clearing to ensure the spinning ring is visible (minimum BUSY_HOLD_MS)
        _busyClearTimers[payload.agent_id] = setTimeout(function () {
            delete _busyClearTimers[payload.agent_id];
            avatar.setAttribute('data-busy', 'false');
        }, BUSY_HOLD_MS);
    }
}

function dispatchBusyChanged(payload) {
    document.dispatchEvent(new CustomEvent('evonic:agent-busy-changed', { detail: payload }));
}

function subscribeBusySSE() {
    if (typeof getSharedRealtime === 'undefined') {
        if (_busySSE) return;
        try {
            _busySSE = new EventSource('/api/agents/status/stream');
            _busySSE.addEventListener('agent_busy_changed', function (e) {
                try {
                    updateBusyAvatar(JSON.parse(e.data));
                } catch (_) {}
            });
            _busySSE.addEventListener('agent_turn_complete', function (e) {
                try {
                    _onTurnComplete(JSON.parse(e.data));
                } catch (_) {}
            });
            _busySSE.addEventListener('error', function () {
                _busySSE.close();
                _busySSE = null;
                resyncBusyState();
                if (_busyReconnectTimer) clearTimeout(_busyReconnectTimer);
                _busyReconnectTimer = setTimeout(function () {
                    _busyReconnectTimer = null;
                    subscribeBusySSE();
                }, 2000);
            });
        } catch (_) {
            _busySSE = null;
            // EventSource not supported — polling fallback already active
        }
        return;
    }

    var rt = getSharedRealtime();
    // Backward compat: pages that poll for window._evRealtime (e.g. agent_detail.html)
    window._evRealtime = rt;
    rt.on('status', 'agent_busy_changed', function (payload) {
        updateBusyAvatar(payload);
        dispatchBusyChanged(payload);
    });
    rt.on('status', 'agent_turn_complete', _onTurnComplete);
}

function resyncBusyState() {
    fetch('/api/agents/busy')
        .then(function (r) { return r.json(); })
        .then(function (data) {
            document.querySelectorAll('#agent-sidebar .agent-avatar').forEach(function (avatar) {
                var agentId = avatar.getAttribute('data-agent-id');
                var busy = !!(data.busy || {})[agentId];
                avatar.setAttribute('data-busy', busy ? 'true' : 'false');
                // The authoritative server snapshot wins: cancel any stale
                // hold-clear timer so it cannot flip a confirmed-busy avatar
                // back to idle (stale BUSY_HOLD_MS debounce race).
                if (busy && _busyClearTimers[agentId]) {
                    clearTimeout(_busyClearTimers[agentId]);
                    delete _busyClearTimers[agentId];
                }
            });
            document.dispatchEvent(new CustomEvent('evonic:agent-busy-resync', { detail: data.busy || {} }));
        })
        .catch(function () {});
}

function closeBusyRealtime() {
    if (_busyReconnectTimer) {
        clearTimeout(_busyReconnectTimer);
        _busyReconnectTimer = null;
    }
    if (_busySSE) {
        try {
            _busySSE.close();
        } catch (_) {}
        _busySSE = null;
    }
}

// Close the fallback EventSource on navigation (no-ops on the shared-RT
// path, which manages its own lifecycle via getSharedRealtime's handler).
window.addEventListener('pagehide', closeBusyRealtime);
window.addEventListener('beforeunload', closeBusyRealtime);
window.addEventListener('pageshow', function () {
    resyncBusyState();
    subscribeBusySSE();
});

/** Toggle sidebar collapsed state */
function toggleSidebar() {
    var sidebar = document.getElementById('agent-sidebar');
    var burger = document.getElementById('sidebar-toggle-btn');
    if (!sidebar) return;

    var collapsed = sidebar.classList.toggle('collapsed');
    if (burger) {
        burger.classList.toggle('collapsed', collapsed);
    }
    try {
        localStorage.setItem('evonic-sidebar-collapsed', collapsed ? '1' : '0');
    } catch (_) {}
}

/** Apply saved sidebar state from localStorage */
function _applySidebarState() {
    var sidebar = document.getElementById('agent-sidebar');
    if (!sidebar) return;
    var burger = document.getElementById('sidebar-toggle-btn');

    var collapsed;
    try {
        var saved = localStorage.getItem('evonic-sidebar-collapsed');
        if (saved !== null) {
            collapsed = saved === '1';
        } else {
            // Default: collapsed on mobile, open on desktop
            collapsed = window.innerWidth <= 768;
        }
    } catch (_) {
        collapsed = window.innerWidth <= 768;
    }

    if (collapsed) {
        sidebar.classList.add('collapsed');
        if (burger) burger.classList.add('collapsed');
    }
}

// Update selected ring when browser navigates back/forward (soft-switch uses pushState)
window.addEventListener('popstate', _updateSelectedAvatar);

/** Initialize the sidebar */
function initSidebar() {
    var sidebar = document.getElementById('agent-sidebar');
    if (!sidebar) return;

    fetchSidebarAgents().then(function () {
        resyncBusyState();
    });
    subscribeBusySSE();
}
