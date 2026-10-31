/*
 * Browser notifications for agent questions and tool approvals.
 *
 * Loaded on every page by templates/base.html. While notification permission is
 * granted, one tab per browser (elected with the Web Locks API) keeps its own
 * realtime connection on the 'questions' and 'approvals' channels open, even
 * while hidden (the shared client disconnects hidden tabs). It notifies when:
 *
 *   - an agent asks a question (ask_user_question), unless a focused Evonic tab
 *     is already showing that agent's chat;
 *   - a command needs approval, unless an Evonic tab is focused (the approval
 *     modal is already on screen there).
 *
 * Tabs report their path and focus over a BroadcastChannel so the elected tab
 * can make that call. Each question/approval notifies at most once per browser
 * (remembered in localStorage), and its notification closes once it is resolved.
 * Notifications need a secure context: HTTPS or localhost.
 *
 * window.EvonicNotify.promptElement(className) returns a "Notify me" button for
 * hosts (the question card, the approval modal) to show while permission has
 * not been asked yet, or null.
 */
(function () {
    'use strict';
    if (window.EvonicNotify) return;

    const NAME = 'evonic-browser-notify';
    const SEEN_KEY = 'evonic-notified';
    const SEEN_TTL_MS = 24 * 3600 * 1000;
    const HEARTBEAT_MS = 15000;
    const TAB_STALE_MS = 45000;     // a tab that stopped reporting no longer counts as focused
    const BODY_MAX = 140;

    const supported = 'Notification' in window && window.isSecureContext;
    const tabId = Math.random().toString(36).slice(2);
    const tabs = new Map();         // tabId -> {path, focused, at}
    const shown = new Map();        // notification tag -> Notification
    const memorySeen = new Set();   // fallback when localStorage is unavailable
    const bus = 'BroadcastChannel' in window ? new BroadcastChannel(NAME) : null;
    let started = false;

    // ── Which tab is focused, and where ─────────────────────────────────────

    function report() {
        const state = {
            type: 'tab', tabId: tabId, path: location.pathname, at: Date.now(),
            focused: document.visibilityState === 'visible' && document.hasFocus(),
        };
        tabs.set(tabId, state);
        if (bus) bus.postMessage(state);
    }

    if (bus) {
        bus.onmessage = function (e) {
            const msg = e.data || {};
            if (msg.type === 'tab') tabs.set(msg.tabId, msg);
            else if (msg.type === 'gone') tabs.delete(msg.tabId);
            else if (msg.type === 'hello') report();
        };
        bus.postMessage({ type: 'hello' });
    }
    ['focus', 'blur', 'popstate'].forEach(function (name) { window.addEventListener(name, report); });
    document.addEventListener('visibilitychange', report);
    window.addEventListener('pagehide', function () {
        if (bus) bus.postMessage({ type: 'gone', tabId: tabId });
    });
    setInterval(report, HEARTBEAT_MS);
    report();

    function focusedTabWhere(test) {
        const now = Date.now();
        for (const state of tabs.values()) {
            if (state.focused && now - state.at < TAB_STALE_MS && test(state.path || '')) return true;
        }
        return false;
    }

    function agentPath(agentId) {
        return '/agents/' + encodeURIComponent(agentId || '');
    }

    // ── Notify once per question/approval per browser ───────────────────────

    function firstTime(tag) {
        try {
            const now = Date.now();
            const seen = JSON.parse(localStorage.getItem(SEEN_KEY) || '{}');
            Object.keys(seen).forEach(function (key) {
                if (now - seen[key] > SEEN_TTL_MS) delete seen[key];
            });
            const fresh = !(tag in seen);
            seen[tag] = now;
            localStorage.setItem(SEEN_KEY, JSON.stringify(seen));
            return fresh;
        } catch (_) {
            if (memorySeen.has(tag)) return false;
            memorySeen.add(tag);
            return true;
        }
    }

    function clip(text) {
        text = String(text || '').replace(/\s+/g, ' ').trim();
        return text.length > BODY_MAX ? text.slice(0, BODY_MAX - 1) + '…' : text;
    }

    function show(tag, title, body, url) {
        if (!firstTime(tag) || Notification.permission !== 'granted') return;
        let notification;
        try {
            notification = new Notification(title, { body: clip(body), tag: tag });
        } catch (_) {
            return;  // e.g. Android Chrome only allows notifications from a service worker
        }
        notification.onclick = function () {
            window.focus();
            if (url && location.pathname.replace(/\/$/, '') !== url) location.href = url;
            notification.close();
        };
        notification.onclose = function () { shown.delete(tag); };
        shown.set(tag, notification);
    }

    function close(tag) {
        const notification = shown.get(tag);
        if (notification) notification.close();
    }

    // ── Events ──────────────────────────────────────────────────────────────

    function onQuestion(data) {
        if (!data || !data.question_id) return;
        const tag = 'ask:' + data.question_id;
        const url = agentPath(data.agent_id);
        if (focusedTabWhere(function (path) { return path.replace(/\/$/, '') === url; })) {
            firstTime(tag);  // seen on screen; do not notify on a later reconnect either
            return;
        }
        const more = data.count > 1 ? ' (+' + (data.count - 1) + ' more)' : '';
        show(tag, (data.agent_name || data.agent_id || 'An agent') + ' is asking you',
             (data.question || data.header || 'The agent needs your input.') + more, url);
    }

    function onApproval(data) {
        if (!data || !data.approval_id) return;
        const tag = 'approval:' + data.approval_id;
        if (focusedTabWhere(function () { return true; })) {
            firstTime(tag);  // the approval modal is on screen in that tab
            return;
        }
        const args = data.args || {};
        const command = args.script || args.code || args.file_path || '';
        const info = data.approval_info || {};
        const tool = data.tool || 'a tool';
        show(tag, (data.source_agent_name || data.agent_id || 'An agent') + ' needs your approval',
             command ? tool + ': ' + command : (info.description || 'Approve or reject running ' + tool + '.'),
             null);
    }

    function startConnection() {
        if (started || typeof RealtimeClient === 'undefined') return;
        started = true;
        const rt = new RealtimeClient({ channels: 'questions,approvals', pauseWhenHidden: false });
        rt.on('questions', 'question_required', onQuestion);
        rt.on('questions', 'question_resolved', function (d) { if (d) close('ask:' + d.question_id); });
        rt.on('approvals', 'approval_required', onApproval);
        rt.on('approvals', 'approval_resolved', function (d) { if (d) close('approval:' + d.approval_id); });
        rt.start();
    }

    function start() {
        if (!supported || Notification.permission !== 'granted') return;
        if (navigator.locks && navigator.locks.request) {
            // Held until this tab goes away; then the next tab takes over.
            navigator.locks.request(NAME, function () {
                startConnection();
                return new Promise(function () {});
            });
        } else {
            startConnection();  // no Web Locks: every tab connects; firstTime() still dedupes
        }
    }

    // ── Permission prompt for host UIs ──────────────────────────────────────

    function promptElement(className) {
        if (!('Notification' in window) || Notification.permission !== 'default') return null;
        const el = document.createElement(window.isSecureContext ? 'button' : 'span');
        el.className = className || '';
        el.setAttribute('data-ev-notify-prompt', '');
        if (!window.isSecureContext) {
            el.textContent = 'Browser notifications need HTTPS';
            el.title = 'Open Evonic over HTTPS (or on localhost) to get a browser notification when an agent needs you.';
            return el;
        }
        el.type = 'button';
        el.textContent = 'Notify me in this browser';
        el.title = 'Get a browser notification when an agent asks a question or needs approval, even while this tab is in the background.';
        el.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            Notification.requestPermission().then(function (permission) {
                document.querySelectorAll('[data-ev-notify-prompt]').forEach(function (node) { node.remove(); });
                if (permission === 'granted') start();
            });
        });
        return el;
    }

    window.EvonicNotify = { promptElement: promptElement };
    start();
})();
