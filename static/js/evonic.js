/**
 * Evonic — Unified global JS entry point.
 * Loaded once on every page via base.html.
 */
(function () {
    'use strict';

    var agentsCache = null;
    var agentsPromise = null;   // deduplicate concurrent fetchAgents() calls
    var overlayEl = null;
    var panelEl = null;
    var inputEl = null;
    var listEl = null;
    var countEl = null;
    var selectedIndex = 0;
    var shown = [];               // agents currently listed (already ranked)
    var lastFocused = null;
    var currentQuery = '';

    // ============================================================
    //  Agent Quick Search (Ctrl+G / Cmd+G) — command-palette style
    // ============================================================

    // Disabled agents cannot process messages, so they are never offered here even though
    // /api/agents returns them (the management pages need the full list).
    function isAgentEnabled(a) {
        return !(a.enabled === 0 || a.enabled === false || a.enabled === '0');
    }

    function fetchAgents() {
        if (agentsCache) return Promise.resolve(agentsCache);
        if (agentsPromise) return agentsPromise;  // deduplicate concurrent calls
        agentsPromise = fetch('/api/agents')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                agentsCache = (data.agents || []).filter(function (a) {
                    return a.id && a.name && isAgentEnabled(a);
                });
                agentsPromise = null;
                return agentsCache;
            })
            .catch(function () {
                agentsCache = [];
                agentsPromise = null;
                return agentsCache;
            });
        return agentsPromise;
    }

    function getInitial(name) {
        if (!name) return '?';
        return name.charAt(0).toUpperCase();
    }


    function esc(str) {
        return String(str == null ? '' : str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function currentAgentId() {
        var m = location.pathname.match(/^\/agents\/([^\/?#]+)/);
        return m ? decodeURIComponent(m[1]) : null;
    }

    var ICON_SEARCH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>';
    var ICON_ENTER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 10 4 15 9 20"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/></svg>';

    function buildOverlay() {
        if (overlayEl) return;
        overlayEl = document.createElement('div');
        overlayEl.className = 'qs-overlay';
        overlayEl.setAttribute('role', 'dialog');
        overlayEl.setAttribute('aria-modal', 'true');
        overlayEl.setAttribute('aria-label', 'Search agents');
        overlayEl.innerHTML =
            '<div class="qs-panel">' +
              '<div class="qs-input-row">' + ICON_SEARCH +
                '<input class="qs-input" type="text" placeholder="Search agents by name or ID…" autocomplete="off" spellcheck="false" ' +
                       'role="combobox" aria-expanded="true" aria-controls="qs-list" aria-autocomplete="list">' +
                '<kbd class="qs-kbd">esc</kbd>' +
              '</div>' +
              '<div class="qs-list" id="qs-list" role="listbox"></div>' +
              '<div class="qs-footer">' +
                '<span class="qs-hint"><kbd>↑</kbd><kbd>↓</kbd>navigate</span>' +
                '<span class="qs-hint"><kbd>↵</kbd>open</span>' +
                '<span class="qs-hint qs-hint-tab"><kbd>⌘</kbd><kbd>↵</kbd>new tab</span>' +
                '<span class="qs-count" aria-live="polite"></span>' +
              '</div>' +
            '</div>';
        panelEl = overlayEl.firstChild;
        inputEl = overlayEl.querySelector('.qs-input');
        listEl = overlayEl.querySelector('.qs-list');
        countEl = overlayEl.querySelector('.qs-count');

        overlayEl.addEventListener('mousedown', function (e) { if (e.target === overlayEl) closeOverlay(); });
        inputEl.addEventListener('input', onInput);
        inputEl.addEventListener('keydown', onKeyDown);

        // Delegated hover / click: moves the highlight without rebuilding the list
        listEl.addEventListener('mousemove', function (e) {
            var item = e.target.closest('[data-index]');
            if (!item) return;
            var idx = parseInt(item.getAttribute('data-index'), 10);
            if (idx !== selectedIndex) setSelected(idx, false);
        });
        listEl.addEventListener('click', function (e) {
            var item = e.target.closest('[data-index]');
            if (item) selectAgent(shown[parseInt(item.getAttribute('data-index'), 10)], e.metaKey || e.ctrlKey);
        });
        document.body.appendChild(overlayEl);
    }

    function isOpen() { return !!overlayEl && overlayEl.classList.contains('is-open'); }

    function showOverlay() {
        buildOverlay();
        lastFocused = document.activeElement;
        currentQuery = '';
        inputEl.value = '';
        selectedIndex = 0;
        overlayEl.classList.add('is-open');
        document.documentElement.classList.add('qs-lock');
        render();                                   // skeleton while the first fetch is in flight
        fetchAgents().then(function () { if (isOpen()) { rank(); render(); } });
        setTimeout(function () { inputEl.focus(); }, 30);
    }

    function closeOverlay() {
        if (!overlayEl) return;
        overlayEl.classList.remove('is-open');
        document.documentElement.classList.remove('qs-lock');
        if (lastFocused && typeof lastFocused.focus === 'function') { try { lastFocused.focus(); } catch (_) {} }
    }

    // ---- ranking -------------------------------------------------------------------------------
    function score(q, a) {
        var name = String(a.name || '').toLowerCase();
        var id = String(a.id || '').toLowerCase();
        if (name === q || id === q) return 100;
        if (name.indexOf(q) === 0) return 85;
        if (id.indexOf(q) === 0) return 75;
        if (new RegExp('(^|[\\s_\\-/])' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).test(name)) return 65;   // a word starts with q
        if (name.indexOf(q) !== -1) return 55;
        if (id.indexOf(q) !== -1) return 45;
        // subsequence ("gl" finds "Galactus"): every char of q appears in order in the name
        var i = 0;
        for (var c = 0; c < name.length && i < q.length; c++) if (name.charAt(c) === q.charAt(i)) i++;
        return i === q.length ? 20 : 0;
    }

    function rank() {
        var agents = agentsCache || [];
        var q = currentQuery;
        if (!q) { shown = agents.slice(); return; }
        shown = agents
            .map(function (a, i) { return { a: a, s: score(q, a), i: i }; })
            .filter(function (x) { return x.s > 0; })
            .sort(function (x, y) { return (y.s - x.s) || (x.i - y.i); })
            .map(function (x) { return x.a; });
    }

    function mark(text, q) {
        var t = String(text == null ? '' : text);
        if (!q) return esc(t);
        var i = t.toLowerCase().indexOf(q);
        if (i === -1) return esc(t);
        return esc(t.slice(0, i)) + '<mark class="qs-mark">' + esc(t.slice(i, i + q.length)) + '</mark>' + esc(t.slice(i + q.length));
    }

    function onInput() {
        currentQuery = inputEl.value.trim().toLowerCase();
        rank();
        selectedIndex = 0;
        render();
    }

    // ---- rendering -----------------------------------------------------------------------------
    function render() {
        if (!agentsCache) {                              // first open, still loading
            listEl.innerHTML = '<div class="qs-skel"></div><div class="qs-skel"></div><div class="qs-skel"></div>';
            countEl.textContent = '';
            return;
        }
        if (!shown.length && !currentQuery && !agentsCache.length) {
            listEl.innerHTML = '<div class="qs-empty">No enabled agents</div>';
            countEl.textContent = '';
            return;
        }
        if (!shown.length) {
            listEl.innerHTML = '<div class="qs-empty"><span class="qs-empty-ico">' + ICON_SEARCH + '</span>' +
                'No agents match “' + esc(inputEl.value.trim()) + '”<small>Try a name or an agent ID</small></div>';
            countEl.textContent = '0 results';
            return;
        }
        var here = currentAgentId();
        var html = '<div class="qs-label">' + (currentQuery ? 'Agents' : 'Recent agents') + '</div>';
        shown.forEach(function (a, i) {
            var initial = esc(getInitial(a.name));
            var color = agentColor(String(a.id));
            html += '<div class="qs-item' + (i === selectedIndex ? ' is-selected' : '') + '" role="option" id="qs-opt-' + i + '" data-index="' + i + '" aria-selected="' + (i === selectedIndex) + '">' +
                '<img class="qs-avatar" src="/api/agents/' + encodeURIComponent(a.id) + '/avatar?size=small" alt="" loading="lazy" ' +
                     'onerror="this.outerHTML=\'<span class=\\\'qs-avatar qs-avatar-fb\\\' style=\\\'background:' + color + '\\\'>' + initial + '</span>\'">' +
                '<div class="qs-main"><div class="qs-name">' + mark(a.name, currentQuery) + '</div>' +
                '<div class="qs-id">' + mark(a.id, currentQuery) + '</div></div>' +
                (a.id === here ? '<span class="qs-badge qs-badge-cur">Current</span>' : '') +
                '<span class="qs-go" aria-hidden="true">' + ICON_ENTER + '</span></div>';
        });
        listEl.innerHTML = html;
        countEl.textContent = shown.length + (shown.length === 1 ? ' agent' : ' agents');
        inputEl.setAttribute('aria-activedescendant', 'qs-opt-' + selectedIndex);
    }

    function setSelected(idx, scroll) {
        if (!shown.length) return;
        var n = shown.length;
        idx = ((idx % n) + n) % n;                       // wraps around
        var items = listEl.querySelectorAll('.qs-item');
        if (items[selectedIndex]) { items[selectedIndex].classList.remove('is-selected'); items[selectedIndex].setAttribute('aria-selected', 'false'); }
        selectedIndex = idx;
        var el = items[idx];
        if (el) {
            el.classList.add('is-selected');
            el.setAttribute('aria-selected', 'true');
            inputEl.setAttribute('aria-activedescendant', el.id);
            if (scroll) el.scrollIntoView({ block: 'nearest' });
        }
    }

    function onKeyDown(e) {
        if (e.isComposing) return;
        if (e.key === 'Escape') { e.preventDefault(); closeOverlay(); return; }
        if (e.key === 'ArrowDown') { e.preventDefault(); setSelected(selectedIndex + 1, true); return; }
        if (e.key === 'ArrowUp')   { e.preventDefault(); setSelected(selectedIndex - 1, true); return; }
        if (e.key === 'Home' && e.ctrlKey) { e.preventDefault(); setSelected(0, true); return; }
        if (e.key === 'End' && e.ctrlKey)  { e.preventDefault(); setSelected(shown.length - 1, true); return; }
        if (e.key === 'Tab') { e.preventDefault(); setSelected(selectedIndex + (e.shiftKey ? -1 : 1), true); return; }   // focus stays in the input
        if (e.key === 'Enter') {
            e.preventDefault();
            if (shown[selectedIndex]) selectAgent(shown[selectedIndex], e.metaKey || e.ctrlKey);
        }
    }

    // Soft switch (no page reload) when already on an agent page; otherwise a normal navigation.
    function selectAgent(agent, newTab) {
        if (!agent) return;
        var url = '/agents/' + encodeURIComponent(agent.id);
        closeOverlay();
        if (newTab) { window.open(url, '_blank', 'noopener'); return; }
        if (agent.id === currentAgentId()) return;
        if (typeof window.softSwitchAgent === 'function' && currentAgentId()) {
            Promise.resolve(window.softSwitchAgent(agent.id)).then(function (ok) { if (ok === false) location.href = url; })
                .catch(function () { location.href = url; });
            return;
        }
        location.href = url;
    }

    // Deterministic color from agent id hash
    function agentColor(id) {
        var colors = [
            '#3b82f6', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6',
            '#ec4899', '#06b6d4', '#f97316', '#6366f1', '#14b8a6',
            '#e11d48', '#7c3aed', '#0891b2', '#ca8a04', '#4f46e5'
        ];
        var hash = 0;
        for (var i = 0; i < id.length; i++) {
            hash = ((hash << 5) - hash + id.charCodeAt(i)) | 0;
        }
        return colors[Math.abs(hash) % colors.length];
    }

    // ============================================================
    //  Global keyboard listener
    // ============================================================

    document.addEventListener('keydown', function (e) {
        // Ctrl+G or Cmd+G
        if ((e.ctrlKey || e.metaKey) && e.key === 'g') {
            e.preventDefault();
            if (isOpen()) closeOverlay(); else showOverlay();      // the shortcut toggles
        }

        // Escape to close overlay when it's open
        if (e.key === 'Escape' && isOpen()) {
            // handled by onKeyDown on input, but double-guard
            closeOverlay();
        }
    });

})();
