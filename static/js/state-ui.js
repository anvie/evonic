/* ========================================
   State UI — icon rows (Agent State) and sectioned Session State
   Uses helpers from agent-state.js (esc, _resetActiveModel, _unloadSkill, _openCmpMap, _cmpMapData,
   _stateAgentId, _stateSessionId). Styles: static/css/state-ui.css
   ======================================== */
(function (global) {
    'use strict';

    function I(d) {
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + d + '</svg>';
    }
    var ICON = {
        model:  I('<rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9"/><path d="M15 2v2M15 20v2M2 15h2M2 9h2M20 15h2M20 9h2M9 2v2M9 20v2"/>'),
        focus:  I('<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>'),
        cmp:    I('<rect width="8" height="8" x="3" y="3" rx="2"/><path d="M7 11v4a2 2 0 0 0 2 2h4"/><rect width="8" height="8" x="13" y="13" rx="2"/>'),
        plug:   I('<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/>'),
        gauge:  I('<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>'),
        plan:   I('<rect width="8" height="4" x="8" y="2" rx="1" ry="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M12 11h4"/><path d="M12 16h4"/><path d="M8 11h.01"/><path d="M8 16h.01"/>'),
        file:   I('<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/>'),
        tasks:  I('<path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/>'),
        skills: I('<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>'),
        term:   I('<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>'),
        text:   I('<path d="M21 6H3"/><path d="M15 12H3"/><path d="M17 18H3"/>'),
        check:  I('<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>'),
        circle: I('<circle cx="12" cy="12" r="10"/>'),
        spin:   I('<path d="M21 12a9 9 0 1 1-6.219-8.56"/>').replace('<svg ', '<svg class="state-spin" '),
        folder: I('<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>'),
        chev:   I('<path d="m9 18 6-6-6-6"/>'),
        undo:   I('<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>'),
        refresh:I('<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>')
    };
    function small(svg) { return svg.replace('<svg ', '<svg width="14" height="14" '); }

    function kv(icon, label, valueHtml) {
        return '<div class="st-kv">' + icon + '<span class="st-kv-l">' + label + '</span>' + valueHtml + '</div>';
    }

    /** Agent State card body: icon rows (model, focus, CMP, plugin states). */
    function agentRows(data) {
        data = data || {};
        var rows = '';
        if (data.active_model) {
            var am = data.active_model;
            rows += am.is_fallback
                ? kv(ICON.model, 'Model', '<span class="st-kv-v" title="Using the fallback model because the primary failed"><span class="st-dot amber"></span><span>' + esc(am.name) + '</span><button type="button" onclick="_resetActiveModel()" title="Reset to the primary model" aria-label="Reset to the primary model" style="display:inline-flex">' + small(ICON.undo) + '</button></span>')
                : kv(ICON.model, 'Model', '<span class="st-kv-v"><span class="st-dot green"></span><span>' + esc(am.name) + '</span></span>');
        }
        if (data.focus) {
            rows += kv(ICON.focus, 'Focus', '<span class="st-kv-v" title="' + esc(data.focus_reason || '') + '"><span>' + esc(data.focus_reason || 'on') + '</span></span>');
        }
        if (data.cmp_error) {
            rows += kv(ICON.cmp, 'CMP', '<span class="st-kv-v" title="' + esc(data.cmp_error) + '"><span class="st-dot amber"></span><span>unavailable</span></span>');
        }
        if (data.cmp && data.cmp.paths && data.cmp.paths.length > 0) {
            global._cmpMapData = data.cmp;
            var active = null;
            for (var i = 0; i < data.cmp.paths.length; i++) { if (data.cmp.paths[i].id === data.cmp.active_id) { active = data.cmp.paths[i]; break; } }
            var n = data.cmp.paths.length;
            rows += kv(ICON.cmp, 'CMP', '<button type="button" class="st-kv-v" onclick="_openCmpMap()" title="' + esc(active ? active.title : '') + ' — open the session path map"><span>' + n + ' card' + (n === 1 ? '' : 's') + '</span><span style="display:inline-flex;opacity:.55">' + small(ICON.chev) + '</span></button>');
        }
        if (data.states && Object.keys(data.states).length > 0) {
            Object.keys(data.states).forEach(function (ns) {
                var slot = data.states[ns] || {};
                var dataStr = slot.data ? JSON.stringify(slot.data) : '';
                rows += kv(ICON.plug, esc(ns), '<span class="st-kv-v" title="' + esc(dataStr) + '"><span>' + esc(slot.state || 'unknown') + '</span></span>');
            });
        }
        return rows || '<p class="text-sm text-gray-400 dark:text-gray-500 italic">No state yet.</p>';
    }

    /** Render agentRows into one or more containers (also records the agent/session for reset/unload helpers). */
    function renderAgentState(agentId, sessionId, containerIds, data) {
        if (!agentId) return;
        global._stateAgentId = agentId;
        global._stateSessionId = sessionId || null;
        var html = agentRows(data);
        (Array.isArray(containerIds) ? containerIds : [containerIds]).forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.innerHTML = html;
        });
    }

    function contextHtml(cu) {
        if (!cu || !cu.used) return '';
        var fmt = function (n) { return n >= 1000000 ? (n / 1000000).toFixed(1) + 'M' : (n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n)); };
        var label, bar = '';
        if (cu.max) {
            var pct = Math.min(100, cu.percent || 0);
            var color = pct > 85 ? '#ef4444' : (pct >= 60 ? '#f59e0b' : '#6366f1');
            label = fmt(cu.used) + ' / ' + fmt(cu.max) + ' · ' + pct + '%';
            bar = '<div class="st-meter" role="progressbar" aria-valuenow="' + pct + '" aria-valuemin="0" aria-valuemax="100"><div style="width:' + pct + '%;background:' + color + '"></div></div>';
        } else {
            label = '~' + fmt(cu.used) + ' tokens';
        }
        return '<div class="st-sec"><div class="st-sec-h">' + ICON.gauge + '<span>Context</span><span class="st-sec-r">' + label + '</span></div>' + bar + '</div>';
    }

    function planTreeHtml(pf) {
        var parts = String(pf).split('/').filter(Boolean);
        var file = parts.pop();
        var h = '<button type="button" class="st-plan" onclick="openPlanModal(\'' + esc(pf) + '\')" title="' + esc(pf) + ' — click to open plan">' + ICON.file + '<span class="st-plan-name">' + esc(file) + '</span></button>';
        h = '<ul><li>' + h + '</li></ul>';
        for (var i = parts.length - 1; i >= 0; i--) {
            h = '<li><div class="st-tree-dir">' + ICON.folder + '<span>' + esc(parts[i]) + '</span></div>' + h + '</li>';
            if (i > 0) h = '<ul>' + h + '</ul>';
        }
        return '<div class="st-tree">' + (parts.length ? '<ul>' + h + '</ul>' : h) + '</div>';
    }

    /** Plan file tree only — the mode (plan / execute) lives in the card header, see setModeChip(). */
    function planHtml(d) {
        if (!d.plan_file) return '';
        return '<div class="st-sec">' + planTreeHtml(d.plan_file) + '</div>';
    }

    function modeChipHtml(d) {
        if (!d || !d.mode) return '';
        var c = d.mode === 'execute' ? 'green' : 'amber';
        return '<span class="st-chip st-mode ' + c + '" title="Session mode"><span class="st-dot ' + c + '"></span>' + esc(d.mode) + ' Mode</span>';
    }

    /** Fill every .st-mode-slot (desktop + mobile card headers) with the current mode chip. When the mode changes
     *  the new chip slides in from the left and stacks over the old one, which is removed once it has landed. */
    function setModeChip(d) {
        var mode = (d && d.mode) || '';
        var html = modeChipHtml(d);
        document.querySelectorAll('.st-mode-slot').forEach(function (slot) {
            var prev = slot.dataset.mode || '';
            if (prev === mode) return;                                   // unchanged: leave the DOM (and any animation) alone
            slot.dataset.mode = mode;
            var stage = slot.querySelector('.st-mode-stage');
            if (!mode || !stage || !prev) {
                slot.innerHTML = mode ? '<span class="st-mode-stage">' + html + '</span>' : '';
                return;
            }
            var tmp = document.createElement('div');
            tmp.innerHTML = html;
            var chip = tmp.firstChild;
            // Freeze the stage at its current width, then ease it to the new chip's natural width, so the segments to the
            // right glide instead of snapping when the two chips differ in width ("Execute Mode" vs "Plan Mode").
            var from = stage.getBoundingClientRect().width;
            stage.style.width = from + 'px';
            // The old chip keeps its own rounded shape and eases to the new width with the stage (no clipping, so no square corners).
            var olds = [].slice.call(stage.querySelectorAll('.st-chip.st-mode'));
            olds.forEach(function (c) { c.style.width = c.getBoundingClientRect().width + 'px'; c.style.overflow = 'hidden'; c.style.whiteSpace = 'nowrap'; });
            chip.classList.add('st-mode-enter');
            stage.appendChild(chip);
            var to = chip.getBoundingClientRect().width;
            var ease = 'width .45s cubic-bezier(.2, .8, .2, 1)';
            requestAnimationFrame(function () {
                stage.style.transition = ease;
                stage.style.width = to + 'px';
                olds.forEach(function (c) { c.style.transition = ease; c.style.width = to + 'px'; });
            });
            var settle = function () {
                olds.forEach(function (c) { c.remove(); });
                chip.classList.remove('st-mode-enter');
                stage.style.width = stage.style.transition = '';
            };
            chip.addEventListener('animationend', settle, { once: true });
            setTimeout(settle, 800);                                     // reduced motion / no animationend
        });
    }

    /** opts.taskText(task) -> HTML for the task label (default: escaped text). */
    function tasksHtml(d, opts) {
        if (!d.tasks || !d.tasks.length) return '';
        opts = opts || {};
        var textOf = opts.taskText || function (t) { return esc(t.text); };
        var stale = new Set(Array.isArray(d.stale_task_ids) ? d.stale_task_ids : []);
        var staleBefore = Date.now() - 180000;
        var done = d.tasks.filter(function (t) { return t.status === 'done'; });
        var open = d.tasks.filter(function (t) { return t.status !== 'done'; });
        var pct = Math.round(done.length / d.tasks.length * 100);
        var row = function (t) {
            var active = t.status === 'in_progress';
            var startedAt = Number(t.in_progress_since);
            var isStale = active && (stale.has(t.id) || (startedAt > 0 && startedAt * 1000 <= staleBefore));
            var kind = t.status === 'done' ? 'done' : (active ? 'doing' : 'pending');
            var icon = kind === 'done' ? ICON.check : (active ? ICON.spin : ICON.circle);
            var badge = isStale ? '<span class="st-chip amber" style="padding:0 .375rem;font-size:.625rem" title="This task has been in progress for a while">stale</span>' : '';
            return '<li class="st-task ' + kind + '">' + icon + '<span class="' + (active ? 'task-active-text' : '') + '" style="min-width:0;flex:1">' + textOf(t) + '</span>' + badge + '</li>';
        };
        var h = '<div class="st-sec"><div class="st-sec-h">' + ICON.tasks + '<span>Tasks</span><span class="st-sec-r" title="' + done.length + ' of ' + d.tasks.length + ' done">' + pct + '%</span></div>';
        h += '<div class="st-tasks-progress"><div class="st-meter"><div style="width:' + pct + '%;background:#22c55e"></div></div></div>';
        if (done.length) {
            h += '<details class="st-done" ' + (global._stTasksDoneOpen ? 'open' : '') + ' ontoggle="_stTasksDoneOpen = this.open">'
               + '<summary class="st-done-toggle">' + ICON.chev + '<span>Completed (' + done.length + ')</span></summary><ul>' + done.map(row).join('') + '</ul></details>';
        }
        if (open.length) h += '<ul>' + open.map(row).join('') + '</ul>';
        return h + '</div>';
    }

    function skillsHtml(d) {
        var skills = Array.isArray(d.loaded_skills) ? d.loaded_skills.map(function (k) { return typeof k === 'string' ? { skill_id: k, name: k } : k; }) : [];
        if (!skills.length) return '';
        return '<div class="st-sec"><div class="st-sec-h">' + ICON.skills + '<span>Skills</span><span class="st-sec-r">' + skills.length + '</span></div><div class="st-chips">'
            + skills.map(function (sk) {
                var err = sk.name === sk.skill_id;
                var tip = err ? 'Skill error: failed to load metadata' : (sk.tool_count ? sk.tool_count + ' tools' : '');
                return '<span class="st-chip ' + (err ? 'amber' : '') + '" title="' + esc(tip) + '">' + esc(sk.name)
                    + '<button type="button" title="Unload skill" aria-label="Unload ' + esc(sk.name) + '" onclick="event.stopPropagation();_unloadSkill(\'' + esc(sk.skill_id) + '\')">&times;</button></span>';
            }).join('') + '</div></div>';
    }

    /** All Session State sections except the summary. opts.bgHtml: pre-rendered background-process section. */
    function sessionSections(d, opts) {
        opts = opts || {};
        return contextHtml(d.context_usage) + planHtml(d) + tasksHtml(d, opts) + (opts.bgHtml || '') + skillsHtml(d);
    }

    function summaryHtml(markdownHtml) {
        return '<div class="st-summary"><div class="st-sec-h">' + ICON.text + '<span>Summary</span></div><div class="recap-prose">' + String(markdownHtml).replace(/^\s*<h[1-3][^>]*>\s*summary\s*<\/h[1-3]>\s*/i, '') + '</div></div>';
    }

    /** Spin a refresh button's icon while fn() runs (at least 600ms so the click visibly registers). */
    function spinRefresh(btn, fn) {
        if (btn.disabled) return Promise.resolve();
        var svg = btn.querySelector('svg');
        btn.disabled = true;
        if (svg) svg.classList.add('state-spin');
        return Promise.all([Promise.resolve().then(fn), new Promise(function (r) { setTimeout(r, 600); })]).catch(function () {}).then(function () {
            if (svg) svg.classList.remove('state-spin');
            btn.disabled = false;
        });
    }

    global.spinRefresh = global.spinRefresh || spinRefresh;
    global.StateUI = { setModeChip: setModeChip, modeChipHtml: modeChipHtml, ICON: ICON, agentRows: agentRows, renderAgentState: renderAgentState, sessionSections: sessionSections, summaryHtml: summaryHtml };
})(window);
