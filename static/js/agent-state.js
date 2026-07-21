/* ========================================
   Agent State Component (unified)
   Shared by agent_detail.html & sessions.html
   ======================================== */

var _stateAgentId = null;
var _stateSessionId = null;

function esc(str) {
    if (str === null || str === undefined) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/**
 * Build HTML for loaded skill badges.
 * Returns an object: { html, hasBadges }
 */
function _buildSkillBadges(skills) {
    if (!skills || skills.length === 0) return { html: '', hasBadges: false, count: 0 };

    var maxVisible = 4;
    var visible = skills.slice(0, maxVisible);
    var hidden = skills.slice(maxVisible);
    var parts = [];

    for (var i = 0; i < visible.length; i++) {
        var s = visible[i];
        // Error detection: name === skill_id means manifest wasn't found
        var isError = (s.name === s.skill_id);
        var errorClass = isError ? ' border border-yellow-400 dark:border-yellow-500' : '';
        var tooltip = isError ? 'Skill error: failed to load metadata' : (s.tool_count ? s.tool_count + ' tools' : '');
        parts.push(
            '<span class="skill-badge inline-flex items-center px-2 py-0.5 rounded text-xs font-medium' +
            ' bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-300 ml-1 group' +
            errorClass +
            '" style="transition: opacity 0.15s ease"' +
            (tooltip ? ' title="' + esc(tooltip) + '"' : '') +
            ' data-skill-id="' + esc(s.skill_id) + '">' +
            esc(s.name) +
            '<button onclick="event.stopPropagation();_unloadSkill(\'' + esc(s.skill_id) + '\')"' +
            ' class="ml-1.5 inline-flex items-center justify-center w-4 h-4 rounded-full' +
            ' opacity-0 group-hover:opacity-100' +
            ' bg-gray-300 hover:bg-red-300 dark:bg-gray-600 dark:hover:bg-red-600' +
            ' text-gray-500 hover:text-red-600 dark:text-gray-400 dark:hover:text-red-300' +
            ' transition-all cursor-pointer"' +
            ' title="Unload skill">\u00d7</button>' +
            '</span>'
        );
    }

    // Truncation: "+N more" pill
    if (hidden.length > 0) {
        parts.push(
            '<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium' +
            ' bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-300 ml-1' +
            ' cursor-pointer" style="transition: opacity 0.15s ease"' +
            ' onclick="var p=this.parentElement;var all=p.querySelectorAll(\'.skill-badge-hidden\');' +
            'for(var i=0;i<all.length;i++)all[i].classList.toggle(\'hidden\');' +
            'this.textContent=all[0].classList.contains(\'hidden\')?\'+' + hidden.length + ' more\':\'Show less\'">' +
            '+' + hidden.length + ' more</span>'
        );
        // Add hidden badges
        for (var j = 0; j < hidden.length; j++) {
            var hs = hidden[j];
            var hsError = (hs.name === hs.skill_id);
            var hsErrorClass = hsError ? ' border border-yellow-400 dark:border-yellow-500' : '';
            var hsTooltip = hsError ? 'Skill error: failed to load metadata' : (hs.tool_count ? hs.tool_count + ' tools' : '');
            parts.push(
                '<span class="skill-badge-hidden skill-badge hidden inline-flex items-center px-2 py-0.5 rounded text-xs font-medium' +
                ' bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-300 ml-1 group' +
                hsErrorClass +
                '" style="transition: opacity 0.15s ease"' +
                (hsTooltip ? ' title="' + esc(hsTooltip) + '"' : '') +
                ' data-skill-id="' + esc(hs.skill_id) + '">' +
                esc(hs.name) +
                '<button onclick="event.stopPropagation();_unloadSkill(\'' + esc(hs.skill_id) + '\')"' +
                ' class="ml-1.5 inline-flex items-center justify-center w-4 h-4 rounded-full' +
                ' opacity-0 group-hover:opacity-100' +
                ' bg-gray-300 hover:bg-red-300 dark:bg-gray-600 dark:hover:bg-red-600' +
                ' text-gray-500 hover:text-red-600 dark:text-gray-400 dark:hover:text-red-300' +
                ' transition-all cursor-pointer"' +
                ' title="Unload skill">\u00d7</button>' +
                '</span>'
            );
        }
    }

    return { html: parts.join(''), hasBadges: true, count: skills.length };
}

/**
 * Core rendering logic (no debounce).
 */
function _renderAgentStateCore(containerIds, data) {
    var empty = '<p class="text-sm text-gray-400 dark:text-gray-500 italic">No state yet.</p>';
    var hasAnyState = data.focus ||
        data.active_model ||
        (data.cmp && data.cmp.paths && data.cmp.paths.length > 0) ||
        (data.states && Object.keys(data.states).length > 0);
    if (!hasAnyState) {
        (Array.isArray(containerIds) ? containerIds : [containerIds]).forEach(function(id) {
            var el = document.getElementById(id);
            if (el) el.innerHTML = empty;
        });
        return;
    }

    // Build status cards row (Focus + Model + Skills)
    var cards = '';

    // Focus badge
    if (data.focus) {
        var reasonText = data.focus_reason ? ' \u2014 ' + esc(data.focus_reason) : '';
        cards += '<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300 ml-1">Focus' + reasonText + '</span>';
    }

    // Active model badge
    if (data.active_model) {
        var am = data.active_model;
        if (am.is_fallback) {
            cards += '<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-300 ml-1" title="Using fallback model due to primary failure">Model: ' + esc(am.name) + ' <span class="ml-1 text-[10px] opacity-75">(fallback)</span>' +
                '<button onclick="event.stopPropagation();_resetActiveModel()" class="ml-1.5 inline-flex items-center justify-center w-4 h-4 rounded-full bg-amber-200 hover:bg-red-200 dark:bg-amber-700 dark:hover:bg-red-700 text-amber-600 hover:text-red-600 dark:text-amber-300 dark:hover:text-red-300 transition-colors cursor-pointer" title="Reset to primary model">\u00d7</button>' +
                '</span>';
        } else {
            cards += '<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300 ml-1">Model: ' + esc(am.name) + '</span>';
        }
    }

    // CMP session-path map badge (clickable — opens the graph modal)
    if (data.cmp && data.cmp.paths && data.cmp.paths.length > 0) {
        _cmpMapData = data.cmp;
        var activePath = null;
        for (var pi = 0; pi < data.cmp.paths.length; pi++) {
            if (data.cmp.paths[pi].id === data.cmp.active_id) { activePath = data.cmp.paths[pi]; break; }
        }
        var activeTitle = activePath ? activePath.title : '';
        cards += '<span onclick="_openCmpMap()"' +
            ' class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium' +
            ' bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-300 ml-1' +
            ' cursor-pointer hover:bg-purple-200 dark:hover:bg-purple-800"' +
            ' title="' + esc(activeTitle) + ' — click to view the session path map">' +
            'CMP: ' + data.cmp.paths.length + ' cards</span>';
    }

    var html = '<div class="space-y-2 text-sm">';

    // Status cards row
    html += '<div class="flex flex-wrap gap-1">' + cards + '</div>';

    // Plugin states section
    if (data.states && Object.keys(data.states).length > 0) {
        html += '<div class="border-t border-gray-100 dark:border-gray-700 pt-2"><div class="text-gray-500 dark:text-gray-400 font-medium mb-1 text-xs uppercase tracking-wide">Plugin States</div><ul class="space-y-1">';
        var stateEntries = Object.entries(data.states);
        for (var si = 0; si < stateEntries.length; si++) {
            var ns = stateEntries[si][0];
            var slot = stateEntries[si][1];
            var stateVal = slot.state || 'unknown';
            var dataStr = slot.data ? JSON.stringify(slot.data) : '';
            html += '<li><div class="flex items-center gap-1"><span class="font-medium text-xs text-gray-700 dark:text-gray-200">' + esc(ns) + ':</span><code class="text-xs bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded">' + esc(stateVal) + '</code></div>';
            if (dataStr) {
                html += '<div class="text-[10px] text-gray-400 dark:text-gray-500 mt-0.5 font-mono break-all">' + esc(dataStr) + '</div>';
            }
            html += '</li>';
        }
        html += '</ul></div>';
    }

    html += '</div>';

    (Array.isArray(containerIds) ? containerIds : [containerIds]).forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.innerHTML = html;
    });
}

/**
 * Public entry point. Fetches state API and renders.
 * Pass `preloadedData` to render from an already-fetched state payload
 * (avoids a duplicate /chat/state request when the caller has one).
 */
async function renderAgentState(agentId, userId, containerIds, sessionId, preloadedData) {
    if (!agentId) return;
    _stateAgentId = agentId;
    _stateSessionId = sessionId || null;
    try {
        var data = preloadedData;
        if (!data) {
            var url = '/api/agents/' + agentId + '/chat/state?user_id=' + encodeURIComponent(userId || 'web_test');
            if (sessionId) url += '&session_id=' + encodeURIComponent(sessionId);
            var res = await fetch(url);
            if (!res.ok) { console.warn('[AgentState] API error:', res.status, res.statusText); return; }
            data = await res.json();
        }
        _renderAgentStateCore(containerIds, data);
    } catch (e) { console.error('[AgentState] error:', e); }
}

function clearAgentState(containerIds) {
    var empty = '<p class="text-sm text-gray-400 dark:text-gray-500 italic">No state yet.</p>';
    (Array.isArray(containerIds) ? containerIds : [containerIds]).forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.innerHTML = empty;
    });
}

function _resetActiveModel() {
    if (!_stateAgentId) { console.warn('[AgentState] No agent ID for reset'); return; }
    fetch('/api/agents/' + _stateAgentId + '/model/reset', { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                document.dispatchEvent(new CustomEvent('evonic:agent-state-changed'));
            } else {
                console.warn('[AgentState] Reset failed:', data.error || data.result);
            }
        })
        .catch(function(e) { console.error('[AgentState] Reset error:', e); });
}

function _unloadSkill(skillId) {
    if (!_stateAgentId || !_stateSessionId) {
        console.warn('[AgentState] No agent/session ID for skill unload');
        return;
    }
    var url = '/api/agents/' + _stateAgentId + '/skills/' + encodeURIComponent(skillId) + '/unload?session_id=' + encodeURIComponent(_stateSessionId);
    fetch(url, { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                document.dispatchEvent(new CustomEvent('evonic:agent-state-changed'));
            } else {
                console.warn('[AgentState] Skill unload failed:', data.error || data.result);
            }
        })
        .catch(function(e) { console.error('[AgentState] Skill unload error:', e); });
}

/* ========================================
   CMP Session Path Map (modal + SVG graph)
   ======================================== */

var _cmpMapData = null;
var _cmpSelectedPath = null;

var _CMP_STATUS_STYLE = {
    active:    { stroke: '#22c55e', fill: 'rgba(34,197,94,0.12)',  label: 'active' },
    preserved: { stroke: '#f59e0b', fill: 'rgba(245,158,11,0.10)', label: 'preserved' },
    archived:  { stroke: '#9ca3af', fill: 'rgba(156,163,175,0.10)', label: 'archived' }
};

/** Normalized lifecycle status ('dormant' is the legacy name of 'preserved'). */
function _cmpStatus(p) {
    var s = (p && p.status) || 'preserved';
    return s === 'dormant' ? 'preserved' : s;
}

/** Rough countdown to the next lifecycle transition ("18h", "2d"). */
function _cmpFmtEta(ms) {
    if (ms <= 0) return 'next turn';
    var h = Math.round(ms / 3600000);
    return h < 1 ? '<1h' : (h < 48 ? h + 'h' : Math.round(h / 24) + 'd');
}

function _openCmpMap() {
    if (!_cmpMapData || !_cmpMapData.paths || !_cmpMapData.paths.length) return;
    _cmpSelectedPath = _cmpMapData.active_id;
    var existing = document.getElementById('cmp-map-modal');
    if (existing) existing.remove();

    var isMobile = window.innerWidth <= 640;

    var overlay = document.createElement('div');
    overlay.id = 'cmp-map-modal';
    overlay.setAttribute('style',
        'position:fixed;inset:0;z-index:1000;display:flex;justify-content:center;' +
        'align-items:stretch;padding:0;background:rgba(0,0,0,0.55);');
    overlay.addEventListener('click', function (e) {
        if (e.target === overlay) _closeCmpMap();
    });

    var panel = document.createElement('div');
    panel.className = 'bg-white dark:bg-gray-800 shadow-xl';
    // Full screen on all viewports.
    panel.setAttribute('style',
        'width:100%;max-width:100%;height:100%;overflow-y:auto;'
        + (isMobile ? 'padding:14px;' : 'padding:20px;'));

    panel.innerHTML =
        '<div class="flex items-center justify-between mb-3">' +
        '  <h3 class="text-base font-semibold text-gray-800 dark:text-gray-100">Session Path Map</h3>' +
        '  <div class="flex items-center gap-2">' +
        '    <button onclick="_toggleCmpSource()" class="text-xs text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 underline cursor-pointer">Mermaid source</button>' +
        '    <button onclick="_closeCmpMap()" class="inline-flex items-center justify-center w-7 h-7 rounded-full bg-gray-100 hover:bg-gray-200 dark:bg-gray-700 dark:hover:bg-gray-600 text-gray-500 dark:text-gray-300 cursor-pointer" title="Close">×</button>' +
        '  </div>' +
        '</div>' +
        '<div id="cmp-map-svg" class="overflow-x-auto text-gray-800 dark:text-gray-100"></div>' +
        '<pre id="cmp-map-source" class="hidden mt-3 rounded p-2 text-[10px] font-mono overflow-x-auto whitespace-pre-wrap break-all bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200">' +
        esc(_cmpMapData.mermaid || '') + '</pre>' +
        '<div id="cmp-map-detail" class="mt-3 border-t border-gray-100 dark:border-gray-700 pt-3 text-sm"></div>';

    overlay.appendChild(panel);
    document.body.appendChild(overlay);
    document.addEventListener('keydown', _cmpEscHandler);

    document.getElementById('cmp-map-svg').innerHTML = _buildCmpSvg(_cmpMapData);
    _cmpRenderDetail();
}

function _closeCmpMap() {
    var el = document.getElementById('cmp-map-modal');
    if (el) el.remove();
    document.removeEventListener('keydown', _cmpEscHandler);
}

function _cmpEscHandler(e) { if (e.key === 'Escape') _closeCmpMap(); }

function _toggleCmpSource() {
    var el = document.getElementById('cmp-map-source');
    if (el) el.classList.toggle('hidden');
}

function _cmpSelectPath(pathId) {
    _cmpSelectedPath = pathId;
    var svgHost = document.getElementById('cmp-map-svg');
    if (svgHost) svgHost.innerHTML = _buildCmpSvg(_cmpMapData);
    _cmpRenderDetail();
}

/** ids whose transcript is loaded into context = active path + its transitive
 *  dependency ancestors (mirrors cmp/assembler.build_history). */
function _cmpLoadedSet(cmp) {
    var byId = {}, loaded = {};
    (cmp.paths || []).forEach(function (p) { byId[p.id] = p; });
    loaded[cmp.active_id] = true;
    (function walk(id) {
        var deps = (byId[id] && byId[id].depends_on) || [];
        for (var i = 0; i < deps.length; i++) {
            if (byId[deps[i]] && !loaded[deps[i]]) { loaded[deps[i]] = true; walk(deps[i]); }
        }
    })(cmp.active_id);
    return loaded;
}

function _cmpFmtTokens(n) {
    n = n || 0;
    return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n);
}

function _cmpRenderDetail() {
    var host = document.getElementById('cmp-map-detail');
    if (!host || !_cmpMapData) return;
    var p = null;
    for (var i = 0; i < _cmpMapData.paths.length; i++) {
        if (_cmpMapData.paths[i].id === _cmpSelectedPath) { p = _cmpMapData.paths[i]; break; }
    }
    if (!p) { host.innerHTML = ''; return; }
    var status = p.id === _cmpMapData.active_id ? 'active' : _cmpStatus(p);
    var st = _CMP_STATUS_STYLE[status] || _CMP_STATUS_STYLE.archived;

    // Lifecycle line: what this node costs in context right now, and when
    // it decays to the next state (preserved -1d-> archived -3d-> pruned).
    var DAY = 86400000, since = p.state_since || p.last_active || 0;
    var lifeNote;
    if (status === 'active') {
        lifeNote = 'full card + transcript in context';
    } else if (status === 'preserved') {
        lifeNote = 'summary card in context' +
            (since ? ' · archives in ' + _cmpFmtEta(since + DAY - Date.now()) : '');
    } else {
        lifeNote = 'title only in context — returning restores it' +
            (since ? ' · prunes in ' + _cmpFmtEta(since + 3 * DAY - Date.now()) : '');
    }

    // Per-path context size (independent numbers — NOT cumulative down the
    // dependency chain):
    //   Full    = this path's RAW transcript size on disk (untruncated tool
    //             outputs included). Green when currently in context.
    //   Actual  = the same transcript AS THE LLM SEES IT — segment
    //             reconstruction with tool-output compaction and rehydration
    //             tails, i.e. what it really costs when loaded. The
    //             Full→Actual gap is what compaction saves.
    //   Offload = its IPPC card — the tiny compressed summary it shrinks to
    //             once offloaded. The Actual→Offload gap is the compression CMP buys.
    var isLoaded = !!_cmpLoadedSet(_cmpMapData)[p.id];
    var full = p.tokens || 0, offloadTok = p.card_tokens || 0;
    var actual = p.llm_tokens || 0;
    var fullCls = isLoaded ? 'text-green-500 dark:text-green-400' : 'text-gray-500 dark:text-gray-300';
    var fullNote = isLoaded ? '' : ' <span class="text-[10px] font-normal opacity-70">(on return)</span>';
    var actualNote = isLoaded
        ? ' <span class="text-[10px] font-normal opacity-70">in context</span>'
        : ' <span class="text-[10px] font-normal opacity-70">(on return)</span>';
    var actualRow = actual
        ? '<div class="text-blue-500 dark:text-blue-400 font-semibold">Actual: ' +
          _cmpFmtTokens(actual) + actualNote + '</div>'
        : '';
    // Archived nodes contribute their title only — the card is NOT in
    // context until a return (or a descendant) restores them to preserved.
    var offNote = isLoaded ? '' : (status === 'archived'
        ? ' <span class="text-[10px] opacity-70">(on restore)</span>'
        : ' <span class="text-[10px] opacity-70">in context now</span>');
    var isMobile = window.innerWidth <= 640;

    // Desktop: token figures sit top-right of the header. Mobile: they move to
    // a full-width block at the very bottom (after the note).
    var ctxHeader = isMobile ? '' :
        '<div class="text-right text-xs leading-tight">' +
        '<div class="' + fullCls + ' font-semibold">Full: ' + _cmpFmtTokens(full) + fullNote + '</div>' +
        actualRow +
        '<div class="text-gray-400 dark:text-gray-500">Offload: ' + _cmpFmtTokens(offloadTok) + offNote + '</div></div>';

    var html = '<div class="flex items-start justify-between gap-2 mb-1">' +
        '<div class="flex items-center gap-2">' +
        '<span class="font-semibold text-gray-800 dark:text-gray-100">' + esc(p.id) + ' — ' + esc(p.title || '(untitled)') + '</span>' +
        '<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium" style="color:' + st.stroke + ';background:' + st.fill + '">' +
        (status === 'active' ? 'ACTIVE' : esc(st.label)) + '</span></div>' +
        ctxHeader + '</div>' +
        '<div class="text-[10px] text-gray-400 dark:text-gray-500 mb-1">' + lifeNote + '</div>';
    if (p.goal) html += '<div class="text-gray-600 dark:text-gray-300 text-xs mb-1"><span class="font-medium">Goal:</span> ' + esc(p.goal) + '</div>';
    if (p.outcome) html += '<div class="text-gray-600 dark:text-gray-300 text-xs mb-1"><span class="font-medium">Outcome:</span> ' + esc(p.outcome) + '</div>';
    if (p.key_facts && p.key_facts.length) {
        html += '<ul class="text-xs text-gray-500 dark:text-gray-400 mt-1 space-y-0.5">';
        for (var k = 0; k < p.key_facts.length; k++) html += '<li>• ' + esc(p.key_facts[k]) + '</li>';
        html += '</ul>';
    }
    if (p.artifacts && p.artifacts.length) {
        html += '<div class="text-[10px] text-gray-400 dark:text-gray-500 mt-1 font-mono break-all">' + esc(p.artifacts.join('  ')) + '</div>';
    }
    if (p.depends_on && p.depends_on.length) {
        html += '<div class="text-xs text-gray-500 dark:text-gray-400 mt-1">depends on: ' + esc(p.depends_on.join(', ')) + '</div>';
    }
    if (p.tags && p.tags.length) {
        html += '<div class="mt-1.5 flex flex-wrap gap-1">';
        for (var ti = 0; ti < p.tags.length; ti++) {
            html += '<span class="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-700 ' +
                'text-gray-600 dark:text-gray-300">#' + esc(p.tags[ti]) + '</span>';
        }
        html += '</div>';
    }
    if (isMobile) {
        html += '<div class="mt-3 pt-2 border-t border-gray-100 dark:border-gray-700 flex gap-4 text-xs">' +
            '<div><span class="' + fullCls + ' font-semibold">Full: ' + _cmpFmtTokens(full) + '</span>' + fullNote + '</div>' +
            (actual ? '<div><span class="text-blue-500 dark:text-blue-400 font-semibold">Actual: ' +
                _cmpFmtTokens(actual) + '</span>' + actualNote + '</div>' : '') +
            '<div><span class="text-gray-500 dark:text-gray-300 font-semibold">Offload: ' + _cmpFmtTokens(offloadTok) + '</span>' + offNote + '</div></div>';
    }
    host.innerHTML = html;
}

/**
 * Offline SVG rendering of the session graph as a RADIAL tree (mind-map):
 * the Agent hub at the center, paths fanning out in rings by dependency
 * depth, smooth curved connectors carrying the action label. Node fill
 * encodes CMP status (active/preserved/archived); the active node glows.
 * Click a node to inspect its card. No mermaid.js dependency.
 */
// Deterministic avatar background color — MUST mirror agent-sidebar.js so the
// hub color matches the agent's sidebar avatar exactly.
var _CMP_AVATAR_COLORS = [
    'hsl(200, 70%, 40%)', 'hsl(260, 60%, 45%)', 'hsl(330, 60%, 42%)',
    'hsl(160, 55%, 35%)', 'hsl(30, 70%, 38%)', 'hsl(290, 50%, 40%)',
    'hsl(80, 50%, 32%)', 'hsl(10, 65%, 40%)',
];
function _cmpAvatarColor(id) {
    var h = 0, s = String(id || '');
    for (var i = 0; i < s.length; i++) { h = ((h << 5) - h) + s.charCodeAt(i); h |= 0; }
    return _CMP_AVATAR_COLORS[Math.abs(h) % _CMP_AVATAR_COLORS.length];
}

function _buildCmpSvg(cmp) {
    var paths = cmp.paths || [];
    var ROOT = '__agent__';
    var NODE_W = 128, NODE_H = 40;
    var R1 = 170, RING = 148;          // radius of first ring, then per level
    var PAD = 24;
    var isDark = document.documentElement.classList.contains('dark');

    // Tree: primary parent = first known dependency, else the Agent hub.
    var byId = {};
    for (var i = 0; i < paths.length; i++) byId[paths[i].id] = paths[i];
    var children = {}; children[ROOT] = [];
    var extraDeps = [];
    for (var j = 0; j < paths.length; j++) {
        var deps = (paths[j].depends_on || []).filter(function (d) { return byId[d]; });
        var parent = deps.length ? deps[0] : ROOT;
        (children[parent] = children[parent] || []).push(paths[j].id);
        for (var k = 1; k < deps.length; k++) extraDeps.push([paths[j].id, deps[k]]);
    }

    // Leaf counts drive angular allocation (a wide subtree gets a wide arc).
    var leaves = {};
    function countLeaves(id) {
        var kids = children[id] || [];
        if (!kids.length) return (leaves[id] = 1);
        var t = 0; for (var c = 0; c < kids.length; c++) t += countLeaves(kids[c]);
        return (leaves[id] = t);
    }
    countLeaves(ROOT);

    // "Loaded" set: the active path plus its transitive dependency ancestors —
    // exactly the paths whose transcript stays in the context window (mirrors
    // cmp/assembler.build_history). Rendered dark-green to signal "in memory".
    var loaded = _cmpLoadedSet(cmp);

    // Radial placement: center at math-origin; angle = middle of the node's
    // arc, radius = depth * ring. Start fanning from the top (-90°).
    var node = {};   // id -> {x, y, r, angle, depth}
    node[ROOT] = { x: 0, y: 0, r: 0, angle: 0, depth: 0 };
    function place(id, a0, a1, depth) {
        var kids = children[id] || [];
        var total = leaves[id] || 1, cursor = a0;
        for (var c = 0; c < kids.length; c++) {
            var span = (a1 - a0) * (leaves[kids[c]] / total);
            var mid = cursor + span / 2;
            var r = depth === 0 ? R1 : node[id].r + RING;
            node[kids[c]] = { x: r * Math.cos(mid), y: r * Math.sin(mid),
                              r: r, angle: mid, depth: depth + 1 };
            place(kids[c], cursor, cursor + span, depth + 1);
            cursor += span;
        }
    }
    var START = -Math.PI / 2;
    place(ROOT, START, START + 2 * Math.PI, 0);

    // Bounds → shift into positive canvas coords. Reserve room for the center
    // hub (r=46) plus the agent-name label below it (~+62).
    var minX = -60, maxX = 60, minY = -56, maxY = 74;
    for (var id in node) {
        var n = node[id];
        minX = Math.min(minX, n.x - NODE_W / 2); maxX = Math.max(maxX, n.x + NODE_W / 2);
        minY = Math.min(minY, n.y - NODE_H / 2); maxY = Math.max(maxY, n.y + NODE_H / 2);
    }
    var offX = -minX + PAD, offY = -minY + PAD;
    var width = maxX - minX + 2 * PAD, height = maxY - minY + 2 * PAD;
    var cx = offX, cy = offY;   // hub center (math-origin shifted)
    function px(id) { return node[id].x + offX; }
    function py(id) { return node[id].y + offY; }

    // Responsive: the viewBox drives scaling — width:100% up to the natural
    // size means the map shrinks to fit a narrow (mobile) container instead of
    // overflowing, and never upscales past its natural size on desktop.
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' +
        Math.round(width) + ' ' + Math.round(height) +
        '" style="width:100%;max-width:' + Math.round(width) +
        'px;height:auto;display:block;margin:0 auto">';

    // ── curved edges (drawn first, under nodes) ──────────────────────────────
    function polar(r, a) { return [cx + r * Math.cos(a), cy + r * Math.sin(a)]; }
    function edge(childId, dashed) {
        var deps = (byId[childId].depends_on || []).filter(function (d) { return byId[d]; });
        var parentId = deps.length ? deps[0] : ROOT;
        var pc = node[parentId], cc = node[childId];
        var rmid = (pc.r + cc.r) / 2;
        var pAngle = pc.r === 0 ? cc.angle : pc.angle;
        var c1 = polar(rmid, pAngle), c2 = polar(rmid, cc.angle);
        var stroke = dashed ? '#8b5cf6' : '#9ca3af';
        var d = 'M ' + px(parentId).toFixed(1) + ' ' + py(parentId).toFixed(1) +
                ' C ' + c1[0].toFixed(1) + ' ' + c1[1].toFixed(1) + ', ' +
                c2[0].toFixed(1) + ' ' + c2[1].toFixed(1) + ', ' +
                px(childId).toFixed(1) + ' ' + py(childId).toFixed(1) + '';
        var out = '<path d="' + d + '" fill="none" stroke="' + stroke + '" stroke-width="1.3"' +
                  (dashed ? ' stroke-dasharray="4 3"' : '') + ' opacity="0.7"/>';
        if (!dashed) {
            var label = byId[childId].action || '';
            if (label) {
                var lx = (px(parentId) + px(childId)) / 2, ly = (py(parentId) + py(childId)) / 2;
                out += '<text x="' + lx.toFixed(1) + '" y="' + (ly - 2).toFixed(1) +
                    '" text-anchor="middle" font-size="9" fill="' + (isDark ? 'currentColor' : '#64748b') + '"' +
                    ' opacity="0.6" font-style="italic">' + esc(label.slice(0, 22)) + '</text>';
            }
        }
        return out;
    }
    var edges = '';
    for (var e = 0; e < paths.length; e++) edges += edge(paths[e].id, false);
    for (var x = 0; x < extraDeps.length; x++) {
        // secondary dependency: dashed straight-ish curve to the extra parent
        var from = node[extraDeps[x][0]], to = node[extraDeps[x][1]];
        if (!from || !to) continue;
        edges += '<path d="M ' + (from.x + offX).toFixed(1) + ' ' + (from.y + offY).toFixed(1) +
            ' Q ' + cx.toFixed(1) + ' ' + cy.toFixed(1) + ', ' +
            (to.x + offX).toFixed(1) + ' ' + (to.y + offY).toFixed(1) +
            '" fill="none" stroke="#8b5cf6" stroke-width="1.1" stroke-dasharray="3 3" opacity="0.55"/>';
    }

    // ── nodes ────────────────────────────────────────────────────────────────
    var nodes = '';
    for (var m = 0; m < paths.length; m++) {
        var p = paths[m], nn = node[p.id];
        var status = p.id === cmp.active_id ? 'active' : _cmpStatus(p);
        var st = _CMP_STATUS_STYLE[status] || _CMP_STATUS_STYLE.archived;
        var isActive = p.id === cmp.active_id, isSel = p.id === _cmpSelectedPath;
        var isLoaded = !!loaded[p.id];
        // Archived = title-only in context: dashed outline, dimmed — visually
        // "just a label" until a return (or an active descendant) restores it.
        var isArchived = status === 'archived' && !isLoaded;
        // Opaque fills so the connector lines never show through the node.
        // Green fill = memory loaded into context (active = bright, ancestors
        // = darker green); neutral slate otherwise. Stroke keeps status color,
        // but the loaded chain gets a green stroke to reinforce "in context".
        var fill = isActive ? (isDark ? '#17402a' : '#dcfce7') :
                   (isLoaded ? (isDark ? '#0f2c1c' : '#f0fdf4') :
                               (isDark ? '#232c3d' : '#f8fafc'));
        var stroke = isActive ? '#22c55e' : (isLoaded ? '#3f9e6a' : st.stroke);
        var nx = nn.x + offX - NODE_W / 2, ny = nn.y + offY - NODE_H / 2;
        var title = (p.title || '').length > 15 ? (p.title || '').slice(0, 14) + '…' : (p.title || '');
        nodes += '<g style="cursor:pointer"' + (isArchived ? ' opacity="0.55"' : '') +
            ' onclick="_cmpSelectPath(\'' + esc(p.id) + '\')">';
        nodes += '<rect x="' + nx.toFixed(1) + '" y="' + ny.toFixed(1) + '" width="' + NODE_W +
            '" height="' + NODE_H + '" rx="9" fill="' + fill + '" stroke="' + stroke +
            '" stroke-width="' + (isActive ? 2.5 : 1.2) + '"' +
            (isArchived ? ' stroke-dasharray="4 3"' : '') +
            (isActive || isSel ? ' filter="drop-shadow(0 0 4px ' + stroke + ')"' : '') + '/>';
        nodes += '<text x="' + (nx + 9).toFixed(1) + '" y="' + (ny + 17).toFixed(1) +
            '" font-size="11" font-weight="600" fill="' + (isDark ? 'currentColor' : '#1e293b') + '">' + esc(p.id) +
            (isActive ? ' ●' : '') + '</text>';
        nodes += '<text x="' + (nx + 9).toFixed(1) + '" y="' + (ny + 32).toFixed(1) +
            '" font-size="10" fill="' + (isDark ? 'currentColor' : '#475569') + '" opacity="0.75">' + esc(title) + '</text>';
        nodes += '<text x="' + (nx + NODE_W - 8).toFixed(1) + '" y="' + (ny + 17).toFixed(1) +
            '" text-anchor="end" font-size="8" fill="' + st.stroke + '">' +
            (isActive ? 'ACTIVE' : esc(st.label)) + '</text>';
        nodes += '</g>';
    }

    // ── central Agent hub: the agent's avatar (image if any, else initial +
    //    sidebar-matching color) ────────────────────────────────────────────
    var agentLabel = String(cmp.agent_name || cmp.agentName || 'Agent').slice(0, 24);
    var hubR = 46;
    var initial = (agentLabel.charAt(0) || 'A').toUpperCase();
    var bg = _cmpAvatarColor(cmp.agent_id || agentLabel);
    var cxs = cx.toFixed(1), cys = cy.toFixed(1);

    // Base colored circle + initial — shown as-is when there's no avatar, and
    // as the fallback layer beneath the image if it fails to load.
    var hub = '<circle cx="' + cxs + '" cy="' + cys + '" r="' + hubR +
        '" fill="' + bg + '" stroke="#6366f1" stroke-width="2"/>' +
        '<text x="' + cxs + '" y="' + (cy + 8).toFixed(1) +
        '" text-anchor="middle" font-size="24" font-weight="700" fill="#ffffff">' +
        esc(initial) + '</text>';

    if (cmp.has_avatar && cmp.agent_id) {
        var clip = 'cmp-hub-clip';
        var url = '/api/agents/' + encodeURIComponent(cmp.agent_id) + '/avatar?size=small';
        hub = '<defs><clipPath id="' + clip + '"><circle cx="' + cxs + '" cy="' + cys +
                '" r="' + hubR + '"/></clipPath></defs>' + hub +
            '<image href="' + esc(url) + '" x="' + (cx - hubR).toFixed(1) + '" y="' +
                (cy - hubR).toFixed(1) + '" width="' + (hubR * 2) + '" height="' + (hubR * 2) +
                '" clip-path="url(#' + clip + ')" preserveAspectRatio="xMidYMid slice"/>' +
            '<circle cx="' + cxs + '" cy="' + cys + '" r="' + hubR +
                '" fill="none" stroke="#6366f1" stroke-width="2"/>';
    }

    // Agent name below the avatar.
    hub += '<text x="' + cxs + '" y="' + (cy + hubR + 16).toFixed(1) +
        '" text-anchor="middle" font-size="12" font-weight="700" fill="currentColor">' +
        esc(agentLabel) + '</text>';

    return svg + edges + nodes + hub + '</svg>';
}
