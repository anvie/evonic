/* evModelPicker: the "Switch model" dialog, for any page (Agent State, plugins).
 *
 *   evModelPicker.open({
 *     title: 'Switch model', subtitle: '...',
 *     models: [...],            // optional: GET /api/models/choices when left out
 *     selected: 'model-id',     // highlighted first (default: current)
 *     current: 'model-id',      // what is in use now; Apply is off while it is selected
 *     tags: function (m) { return [{text: 'primary', tone: 'green'}]; },   // tones: green, amber, indigo, red, gray
 *     applyLabel: 'Use this model', onApply: function (id) { return promise; },
 *     clearLabel: 'Use agent default', clearEnabled: true, onClear: function () { return promise; },
 *   });
 *
 * onApply/onClear may return a promise; the dialog closes when it resolves to anything but false, and stays open
 * (buttons enabled again) when it rejects or resolves to false. A model is shown with its provider, id, context window,
 * output limit, thinking, vision and temperature; aiLogoHtml (ai-logos.js) draws its logo when the page has it.
 */
(function () {
    var ID = 'model-switch-modal';
    var TONES = {
        green: 'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300',
        amber: 'bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-300',
        indigo: 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900 dark:text-indigo-300',
        red: 'bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-300',
        gray: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300'
    };
    var st = null;      // {opts, models, selected, query, busy}

    function esc(s) {
        return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
        });
    }

    function logo(m, size) {
        if (typeof aiLogoHtml === 'function') return aiLogoHtml(m, size);
        var ch = (m.name || m.provider_name || m.provider || '?').charAt(0).toUpperCase();
        return '<span aria-hidden="true" style="width:' + size + 'px;height:' + size + 'px;flex:none;display:inline-flex;align-items:center;justify-content:center;border-radius:9999px;font-size:' + Math.round(size * 0.45) + 'px;font-weight:600" class="bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-300">' + esc(ch) + '</span>';
    }

    function close() {
        var el = document.getElementById(ID);
        if (el) el.remove();
        document.removeEventListener('keydown', onKey);
        st = null;
    }

    function onKey(e) { if (e.key === 'Escape') close(); }

    function tagsHtml(m) {
        var list = (st.opts.tags && st.opts.tags(m)) || [];
        return list.map(function (t) {
            return ' <span class="text-[10px] px-1.5 py-0.5 rounded ' + (TONES[t.tone] || TONES.gray) + '">' + esc(t.text) + '</span>';
        }).join('');
    }

    function detail(m) {
        var rows = [
            ['Provider', m.provider_name || m.provider],
            ['Model ID', m.model_name],
            ['Context window', m.context_window ? Number(m.context_window).toLocaleString() + ' tokens' : '—'],
            ['Max output', m.max_tokens ? Number(m.max_tokens).toLocaleString() + ' tokens' : '—'],
            ['Thinking', m.thinking ? ('On' + (m.thinking_budget ? ' (budget ' + Number(m.thinking_budget).toLocaleString() + ')' : '')) : 'Off'],
            ['Vision', m.vision_supported ? 'Yes' : 'No'],
            ['Temperature', m.temperature === null || m.temperature === undefined ? 'default' : m.temperature]
        ];
        return rows.map(function (r) {
            return '<div class="flex justify-between gap-4 py-1.5 border-b border-gray-100 dark:border-gray-700 text-sm">' +
                '<span class="text-gray-500 dark:text-gray-400">' + esc(r[0]) + '</span>' +
                '<span class="text-gray-800 dark:text-gray-100 font-medium text-right break-all">' + esc(String(r[1])) + '</span></div>';
        }).join('');
    }

    // Models grouped by provider, filtered by the search box (name, model id, provider).
    function listHtml() {
        var q = (st.query || '').trim().toLowerCase();
        var groups = {}, order = [];
        st.models.forEach(function (m) {
            var prov = m.provider_name || m.provider || 'Other';
            if (q && (prov + ' ' + m.name + ' ' + m.model_name).toLowerCase().indexOf(q) === -1) return;
            if (!groups[prov]) { groups[prov] = []; order.push(prov); }
            groups[prov].push(m);
        });
        if (!order.length) return '<p class="text-sm text-gray-400 px-2 py-3">No models match.</p>';
        order.sort(function (a, b) { return a.localeCompare(b); });
        return order.map(function (prov) {
            var items = groups[prov].map(function (m) {
                var active = m.id === st.selected;
                return '<button type="button" data-model-id="' + esc(m.id) + '" class="w-full text-left px-3 py-2 rounded-lg text-sm cursor-pointer flex items-center gap-3 ' +
                    (active ? 'bg-indigo-50 dark:bg-indigo-900/40 text-indigo-700 dark:text-indigo-200' : 'hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-200') + '">' +
                    logo(m, 26) +
                    '<span class="min-w-0 flex-1"><span class="block font-medium truncate">' + esc(m.name) + tagsHtml(m) + '</span>' +
                    '<span class="block text-xs opacity-70 truncate">' + esc(m.model_name) + '</span></span></button>';
            }).join('');
            return '<div class="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500 px-3 pt-2 pb-1">' + logo({ provider: groups[prov][0].provider, provider_name: prov }, 14) + '<span>' + esc(prov) + '</span>' +
                ' <span class="font-normal">· ' + groups[prov].length + '</span></div>' + items;
        }).join('');
    }

    function bindList(panel) {
        panel.querySelectorAll('[data-model-id]').forEach(function (b) {
            b.addEventListener('click', function () { st.selected = b.getAttribute('data-model-id'); render(); });
        });
    }

    function run(fn, arg) {
        if (!fn || st.busy) return;
        st.busy = true;
        render();
        Promise.resolve().then(function () { return fn(arg); }).then(function (ok) {
            if (ok === false) { if (st) { st.busy = false; render(); } return; }
            close();
        }, function (e) {
            console.error('[evModelPicker]', e);
            if (st) { st.busy = false; render(); }
        });
    }

    function render() {
        var o = st.opts;
        var old = document.getElementById(ID);
        // Re-rendering replaces the DOM; remember the list scroll so a click doesn't jump to the top.
        var oldList = old && old.querySelector('[data-list]');
        var listScroll = oldList ? oldList.scrollTop : 0;
        if (old) old.remove();

        var overlay = document.createElement('div');
        overlay.id = ID;
        overlay.setAttribute('style', 'position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;padding:16px;background:rgba(0,0,0,0.55);');
        overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });

        var sel = null;
        st.models.forEach(function (m) { if (m.id === st.selected) sel = m; });
        var canApply = sel && sel.id !== o.current && !st.busy;
        var canClear = o.clearLabel && o.clearEnabled !== false && !st.busy;

        var panel = document.createElement('div');
        panel.className = 'bg-white dark:bg-gray-800 shadow-xl rounded-xl';
        panel.setAttribute('style', 'width:100%;max-width:720px;max-height:min(560px,calc(100vh - 32px));display:flex;flex-direction:column;overflow:hidden;');
        panel.innerHTML =
            '<div class="flex items-center justify-between px-5 py-3 border-b border-gray-100 dark:border-gray-700">' +
            '  <div><h3 class="text-base font-semibold text-gray-800 dark:text-gray-100">' + esc(o.title || 'Switch model') + '</h3>' +
            (o.subtitle ? '  <p class="text-xs text-gray-500 dark:text-gray-400">' + esc(o.subtitle) + '</p>' : '') + '</div>' +
            '  <button type="button" data-close class="inline-flex items-center justify-center w-7 h-7 rounded-full bg-gray-100 hover:bg-gray-200 dark:bg-gray-700 dark:hover:bg-gray-600 text-gray-500 dark:text-gray-300 cursor-pointer" title="Close">×</button>' +
            '</div>' +
            '<div style="display:flex;flex:1;min-height:0;flex-wrap:wrap;overflow-y:auto;">' +
            '  <div style="flex:1 1 260px;display:flex;flex-direction:column;min-height:0;">' +
            '    <div style="padding:10px 10px 0;">' + evSearchBox('data-search aria-label="Search models"', st.query, 'Search models…') + '</div>' +
            '    <div data-list style="max-height:330px;overflow-y:auto;padding:10px;">' + listHtml() + '</div>' +
            '  </div>' +
            '  <div style="flex:1 1 260px;padding:14px 18px;" class="sm:border-l border-gray-100 dark:border-gray-700">' +
            (sel ? '<div class="flex items-center gap-3 mb-2 text-gray-800 dark:text-gray-100">' + logo(sel, 36) + '<div class="min-w-0"><div class="text-sm font-semibold truncate">' + esc(sel.name) + '</div><div class="text-xs text-gray-500 dark:text-gray-400 truncate">' + esc(sel.provider_name || sel.provider) + '</div></div></div>' + detail(sel) : '<p class="text-sm text-gray-400">' + (st.models.length ? 'Pick a model.' : 'No models available.') + '</p>') +
            '  </div>' +
            '</div>' +
            '<div class="flex items-center justify-between gap-2 px-5 py-3 border-t border-gray-100 dark:border-gray-700">' +
            (o.clearLabel ? '  <button type="button" data-clear class="text-xs underline cursor-pointer text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"' + (canClear ? '' : ' disabled style="opacity:.4;cursor:default"') + '>' + esc(o.clearLabel) + '</button>' : '<span></span>') +
            '  <div class="flex gap-2">' +
            '    <button type="button" data-close class="px-3 py-1.5 text-sm rounded-lg bg-gray-100 hover:bg-gray-200 dark:bg-gray-700 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-200 cursor-pointer">Cancel</button>' +
            '    <button type="button" data-apply class="px-3 py-1.5 text-sm rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white cursor-pointer"' + (canApply ? '' : ' disabled style="opacity:.5;cursor:default"') + '>' + esc(st.busy ? 'Saving…' : (o.applyLabel || 'Use this model')) + '</button>' +
            '  </div>' +
            '</div>';

        panel.querySelectorAll('[data-close]').forEach(function (b) { b.addEventListener('click', close); });
        bindList(panel);
        var search = panel.querySelector('[data-search]');
        search.addEventListener('input', function () {
            st.query = search.value;
            panel.querySelector('[data-list]').innerHTML = listHtml();
            bindList(panel);
        });
        var applyBtn = panel.querySelector('[data-apply]');
        applyBtn.addEventListener('click', function () { if (!applyBtn.disabled) run(o.onApply, st.selected); });
        var clearBtn = panel.querySelector('[data-clear]');
        if (clearBtn) clearBtn.addEventListener('click', function () { if (!clearBtn.disabled) run(o.onClear); });

        overlay.appendChild(panel);
        document.body.appendChild(overlay);
        var newList = panel.querySelector('[data-list]');
        if (newList) newList.scrollTop = listScroll;
        focusSearch(overlay, search);
    }

    // Focus the search input every time the dialog becomes visible so the user can start typing immediately. Focus is
    // attempted synchronously and retried on the next frame, because some browsers (notably Safari/iOS) only honour
    // focus() once the node has been laid out.
    function focusSearch(overlay, search) {
        function apply() {
            // The dialog was closed or re-rendered meanwhile — don't touch the DOM.
            if (!overlay.isConnected || overlay !== document.getElementById(ID)) return;
            var active = document.activeElement;
            // Never steal focus from something the user already focused inside the dialog.
            if (active && active !== document.body && active !== search && overlay.contains(active)) return;
            try { search.focus({ preventScroll: true }); } catch (e) { search.focus(); }
            if (document.activeElement !== search) return;
            try {
                var end = search.value.length;
                search.setSelectionRange(end, end);
            } catch (e) { /* selection range unsupported for this input type */ }
        }
        apply();
        if (document.activeElement !== search) {
            if (window.requestAnimationFrame) window.requestAnimationFrame(apply);
            else setTimeout(apply, 0);
        }
    }

    function show(opts, models) {
        close();
        st = { opts: opts, models: models || [], query: '', busy: false,
               selected: opts.selected || opts.current || ((models || [])[0] || {}).id };
        render();
        document.addEventListener('keydown', onKey);
    }

    window.evModelPicker = {
        open: function (opts) {
            opts = opts || {};
            if (opts.models) { show(opts, opts.models); return; }
            fetch('/api/models/choices')
                .then(function (r) { return r.json(); })
                .then(function (data) { show(opts, data.models || []); })
                .catch(function (e) { console.error('[evModelPicker] model list error:', e); });
        },
        close: close
    };
})();

/* Shared search box (model switcher, workplace picker): icon, clear button, focus ring. */
(function () {
    if (document.getElementById('ev-sbox-style')) return;
    var st = document.createElement('style');
    st.id = 'ev-sbox-style';
    st.textContent =
        '.ev-sbox{position:relative;display:flex;align-items:center}' +
        '.ev-sbox>svg{position:absolute;left:.8rem;width:1rem;height:1rem;color:#94a3b8;pointer-events:none;transition:color .15s}' +
        '.ev-sbox input[type=search]{width:100%;height:2.5rem;padding:0 2.25rem 0 2.45rem;font-size:.875rem;border-radius:.75rem;border:1px solid rgb(148 163 184 / .35);background:rgb(148 163 184 / .1);color:#1f2937;outline:none;-webkit-appearance:none;appearance:none;transition:border-color .15s,box-shadow .15s,background-color .15s}' +
        'html.dark .ev-sbox input[type=search]{color:#e5e7eb;background:rgb(15 23 42 / .55);border-color:rgb(148 163 184 / .25)}' +
        '.ev-sbox input[type=search]::placeholder{color:#94a3b8}' +
        '.ev-sbox input[type=search]::-webkit-search-cancel-button,.ev-sbox input[type=search]::-webkit-search-decoration{-webkit-appearance:none;display:none}' +
        '.ev-sbox input[type=search]:hover{border-color:rgb(148 163 184 / .6)}' +
        '.ev-sbox input[type=search]:focus{border-color:#6366f1;background:transparent;box-shadow:0 0 0 3px rgb(99 102 241 / .22)}' +
        '.ev-sbox:focus-within>svg{color:#6366f1}' +
        '.ev-sbox-x{position:absolute;right:.45rem;width:1.5rem;height:1.5rem;display:inline-flex;align-items:center;justify-content:center;border:0;border-radius:9999px;background:transparent;color:#94a3b8;cursor:pointer}' +
        '.ev-sbox-x:hover{background:rgb(148 163 184 / .22);color:#475569}' +
        'html.dark .ev-sbox-x:hover{color:#e2e8f0}' +
        '.ev-sbox-x svg{width:.8rem;height:.8rem}' +
        '.ev-sbox input:placeholder-shown~.ev-sbox-x{display:none}';
    document.head.appendChild(st);
})();

function evSearchBox(attrs, value, placeholder) {
    var v = String(value || '').replace(/[&<>"']/g, function (c) {
        return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
    return '<div class="ev-sbox">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>' +
        '<input type="search" ' + attrs + ' placeholder="' + placeholder + '" value="' + v + '" autocomplete="off" spellcheck="false">' +
        '<button type="button" class="ev-sbox-x" tabindex="-1" aria-label="Clear search" onclick="var i=this.previousElementSibling;i.value=\'\';i.dispatchEvent(new Event(\'input\',{bubbles:true}));i.focus()">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>';
}
