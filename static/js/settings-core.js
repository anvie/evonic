/* Settings console core: router, auto-save engine, search, shared caches.
 * Loaded on /system before settings-general/tools/models.js. */

/* ==================== API helpers ==================== */

async function apiGet(url) {
    const response = await fetch(url);
    return response.json();
}

async function apiPost(url, data) {
    const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
    });
    return response.json();
}

async function apiPut(url, data) {
    const response = await fetch(url, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
    });
    return response.json();
}

async function apiDelete(url) {
    const response = await fetch(url, { method: "DELETE" });
    return response.json();
}

/* ==================== Modal helpers (used by pane markup onclicks) ==================== */

function openModal(modalId) {
    document.getElementById(modalId).classList.add("active");
}

function closeModal(modalId) {
    document.getElementById(modalId).classList.remove("active");
}

/* ==================== Shared models cache ====================
 * One /api/models fetch shared by every pane. Models CRUD calls
 * invalidate() which re-fetches and broadcasts 'models:changed'. */

const ModelsCache = {
    _promise: null,
    get() {
        if (!this._promise) {
            this._promise = apiGet("/api/models").then((d) => d.models || []);
        }
        return this._promise;
    },
    invalidate() {
        this._promise = null;
        document.dispatchEvent(new CustomEvent("models:changed"));
    },
};

/* ==================== AutoSave engine ====================
 * Declarative bindings via data attributes on the control:
 *   data-setting-key       server key (also routing key)
 *   data-setting-type      int | bool | string   (default string)
 *   data-setting-endpoint  "put:/api/..." — dedicated endpoint instead of batch
 * Composite endpoints register a handler; destructive toggles register a guard.
 * Feedback renders into the row's .save-status slot. */

const AutoSave = {
    DEBOUNCE_MS: 800,
    _timers: new Map(),
    _inflight: new Map(),
    _pending: new Map(),
    _handlers: new Map(),
    _guards: new Map(),

    registerHandler(key, fn) {
        this._handlers.set(key, fn);
    },

    registerGuard(key, fn) {
        this._guards.set(key, fn);
    },

    bind(root) {
        root.querySelectorAll("[data-setting-key]").forEach((el) => {
            if (el._autoSaveBound) return;
            el._autoSaveBound = true;
            const key = el.dataset.settingKey;

            if (el.type === "checkbox") {
                el.addEventListener("change", async () => {
                    const guard = this._guards.get(key);
                    if (guard) {
                        const ok = await guard(el.checked);
                        if (!ok) {
                            el.checked = !el.checked;
                            return;
                        }
                    }
                    this.save(key, el.checked, el);
                });
            } else if (el.tagName === "SELECT") {
                el.addEventListener("change", () => this.save(key, el.value, el));
            } else {
                // number/text inputs: debounce while typing, flush on blur/Enter
                el.addEventListener("input", () => this._queue(key, el));
                el.addEventListener("keydown", (e) => {
                    if (e.key === "Enter") el.blur();
                });
                el.addEventListener("blur", () => this._flush(key, el));
            }
        });
    },

    _queue(key, el) {
        clearTimeout(this._timers.get(key));
        this._timers.set(
            key,
            setTimeout(() => this._flush(key, el), this.DEBOUNCE_MS),
        );
    },

    _flush(key, el) {
        clearTimeout(this._timers.get(key));
        this._timers.delete(key);
        let value = el.value;
        const type = el.dataset.settingType || "";
        if (type === "int") {
            if (value === "" || isNaN(parseInt(value, 10))) return; // incomplete input
            value = parseInt(value, 10);
        } else if (type === "float") {
            if (value === "" || !Number.isFinite(Number(value))) return; // incomplete input
            value = Number(value);
        }
        if (el._lastSaved === value) return; // nothing changed
        this.save(key, value, el);
    },

    save(key, value, el) {
        // Coalesce: if a request for this key is in flight, park the newest value.
        if (this._inflight.has(key)) {
            this._pending.set(key, { value, el });
            return;
        }
        this._status(el, "saving");
        const p = this._send(key, value, el)
            .then((serverValue) => {
                el._lastSaved = serverValue !== undefined ? serverValue : value;
                // Echo server-side clamping back into the control
                if (
                    serverValue !== undefined &&
                    serverValue !== null &&
                    el.tagName === "INPUT" &&
                    el.type !== "checkbox" &&
                    String(el.value) !== String(serverValue)
                ) {
                    el.value = serverValue;
                }
                this._status(el, "saved");
            })
            .catch((err) => {
                this._status(el, "error", err.message || "Save failed");
                if (window.toast) toast.show(key + ": " + (err.message || "save failed"), "error");
            })
            .finally(() => {
                this._inflight.delete(key);
                const next = this._pending.get(key);
                if (next) {
                    this._pending.delete(key);
                    this.save(key, next.value, next.el);
                }
            });
        this._inflight.set(key, p);
    },

    async _send(key, value, el) {
        const handler = this._handlers.get(key);
        if (handler) {
            return handler(value);
        }
        const endpoint = el && el.dataset.settingEndpoint;
        if (endpoint) {
            const [method, url] = endpoint.split(/:(.+)/);
            const body =
                (el.dataset.settingType || "") === "bool" || el.type === "checkbox"
                    ? { enabled: value }
                    : { value };
            const res = method === "post" ? await apiPost(url, body) : await apiPut(url, body);
            if (res && res.success === false) {
                throw new Error(res.error || "Save failed");
            }
            return undefined;
        }
        // Default: single-key batch save
        const res = await apiPost("/api/settings/batch", { settings: { [key]: value } });
        if (!res || res.success === false) {
            throw new Error((res && res.error) || "Save failed");
        }
        if (res.errors && res.errors.length) {
            const mine = res.errors.find((e) => e.startsWith(key));
            throw new Error(mine ? mine.slice(key.length + 1).trim() : res.errors.join(", "));
        }
        return res.results ? res.results[key] : undefined;
    },

    /* ---- per-row status rendering ---- */
    _statusEl(el) {
        const row = el && el.closest ? el.closest(".setting-row") : null;
        return row ? row.querySelector(".save-status") : null;
    },

    _status(el, state, msg) {
        const slot = this._statusEl(el);
        if (!slot) return;
        clearTimeout(slot._fadeTimer);
        slot.classList.remove("is-saving", "is-saved", "is-error");
        const row = el.closest(".setting-row");
        const oldErr = row && row.querySelector(".setting-row-error");
        if (oldErr) oldErr.remove();

        if (state === "saving") {
            slot.classList.add("is-saving");
            slot.innerHTML = '<span class="spinner"></span>';
        } else if (state === "saved") {
            slot.classList.add("is-saved");
            slot.innerHTML =
                '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path class="st-check-path" d="M4 12.5l5 5L20 6.5"/></svg>';
            slot._fadeTimer = setTimeout(() => {
                slot.classList.remove("is-saved");
                slot.innerHTML = "";
            }, 1800);
        } else if (state === "error") {
            slot.classList.add("is-error");
            slot.innerHTML =
                '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>';
            if (row && msg) {
                const div = document.createElement("div");
                div.className = "setting-row-error";
                div.textContent = msg;
                row.querySelector(".setting-row-text").appendChild(div);
            }
        }
    },
};

/* ==================== Section router ====================
 * Top-level panes (#section-<id>) plus grouped sub-pages: General is a nav group whose
 * sections are separate pages (#general-<x>) rendered inside their shared pane.
 * Sub-page markup carries class="subpage" data-page="<page id>". */

const SettingsNav = {
    sections: {
        general: { init: () => window.settingsGeneral && settingsGeneral.init() },
        tools: { init: () => window.settingsTools && settingsTools.init() },
        models: { init: () => window.settingsModels && settingsModels.init() },
        users: { init: () => window.usersTab && usersTab.init() },
        shared_channel: { init: () => window.sharedChannel && sharedChannel.init() },
        logs: { init: () => window.logViewer && logViewer.init() },
    },
    /* Nav groups: key -> {label, default page}. The key is also the pane id. */
    groups: {
        general: { label: "General", default: "general-appearance" },
    },
    /* Sub-pages: page id -> {pane, group, label} */
    subpages: {
        "general-appearance": { pane: "general", group: "general", label: "Appearance" },
        "general-models-routing": { pane: "general", group: "general", label: "Models & Routing" },
        "general-agent-behavior": { pane: "general", group: "general", label: "Agent Behavior" },
        "general-reliability": { pane: "general", group: "general", label: "Reliability & Concurrency" },
        "general-whatsapp-safe-delivery": { pane: "general", group: "general", label: "WhatsApp Safe Delivery" },
        "general-privacy": { pane: "general", group: "general", label: "Privacy & Access" },
    },
    DEFAULT_PAGE: "general-appearance",
    current: null,
    _last: {},
    _initialized: new Set(),

    /** Resolve any id/hash to a known page id. A group key ("general", "shared_channel") opens its default page. */
    resolve(id) {
        if (this.subpages[id] || (this.sections[id] && !this.groups[id])) return id;
        if (this.groups[id]) return this.groups[id].default;
        return this.DEFAULT_PAGE;
    },

    activate(id, opts) {
        opts = opts || {};
        // #836: HMADS rules moved to the consolidated Safety page —
        // legacy callers (showTab('hmads'), deep links) are redirected there.
        if (id === "hmads") {
            window.location.replace("/system/safety#hmads");
            return;
        }
        const page = this.resolve(id);
        const sub = this.subpages[page];
        const pane = sub ? sub.pane : page;
        this.current = page;
        if (sub) this._last[sub.group] = page;
        if (typeof SettingsSearch !== "undefined") SettingsSearch.clearMarks();

        document.querySelectorAll(".settings-pane").forEach((p) => {
            p.classList.toggle("active", p.id === "section-" + pane);
        });
        document.querySelectorAll(".subpage").forEach((el) => {
            el.classList.toggle("active", el.dataset.page === page);
        });
        document.querySelectorAll(".settings-nav-item[data-section]").forEach((btn) => {
            const active = btn.dataset.section === page;
            btn.classList.toggle("active", active);
            if (active) btn.setAttribute("aria-current", "page");
            else btn.removeAttribute("aria-current");
            // Keep the active pill visible on mobile — but not on initial load,
            // where it would scroll the search field out of view.
            if (active && !opts.fromHash && window.matchMedia("(max-width: 1023px)").matches) {
                btn.scrollIntoView({ inline: "center", block: "nearest", behavior: "smooth" });
            }
        });
        this._syncGroups(sub ? sub.group : null);
        if (!opts.fromHash) {
            history.replaceState(null, "", "#" + page);
        }
        if (!this._initialized.has(pane)) {
            this._initialized.add(pane);
            try {
                this.sections[pane].init();
            } catch (e) {
                console.error("Section init failed:", pane, e);
                this._initialized.delete(pane);
            }
        }
    },

    /** Mark which nav group holds the current page; the group opens whenever it does. */
    _syncGroups(activeGroup) {
        Object.keys(this.groups).forEach((key) => {
            const el = document.getElementById("nav-group-" + key);
            if (!el) return;
            const has = key === activeGroup;
            if (has) el.classList.add("open");
            el.classList.toggle("has-active", has);
            const parent = el.querySelector(".settings-nav-parent");
            if (parent) {
                parent.setAttribute("aria-expanded", el.classList.contains("open") ? "true" : "false");
                parent.classList.toggle("active", has);
            }
        });
    },

    toggleGroup(key) {
        const el = document.getElementById("nav-group-" + key);
        if (!el) return;
        if (!el.classList.contains("has-active")) {
            // Coming from elsewhere: open the group on the page the user last had in it
            el.classList.add("open");
            this.activate(this._last[key] || this.groups[key].default);
            return;
        }
        el.classList.toggle("open");
        this._syncGroups(key);
    },

    route() {
        const h = location.hash.slice(1);
        // #836: the old #hmads hash now lives on the consolidated Safety page
        if (h === "hmads") {
            window.location.replace("/system/safety#hmads");
            return;
        }
        if (h.startsWith("tools-")) {
            const toolId = decodeURIComponent(h.slice(6));
            this.activate("tools", { fromHash: true });
            if (window.settingsTools && settingsTools.whenLoaded) {
                settingsTools.whenLoaded.then(() => settingsTools.openEditor(toolId));
            }
            return;
        }
        this.activate(h, { fromHash: true });
    },

    /** Human label for a page id. */
    label(page) {
        if (this.subpages[page]) return this.subpages[page].label;
        const btn = document.querySelector('.settings-nav-item[data-section="' + page + '"]');
        return btn ? btn.textContent.trim() : page;
    },

    /** Breadcrumb for a page: ["General", "Appearance"] or ["Tools"]. */
    path(page) {
        const sub = this.subpages[page];
        return sub ? [this.groups[sub.group].label, sub.label] : [this.label(page)];
    },
};

// Legacy alias — old code/pages may still call showTab('...')
window.showTab = function (name) {
    SettingsNav.activate(name);
};

/* ==================== Global settings search ====================
 * Searches every page: setting rows and group headings (General), headings / labels / table headers
 * of the other panes, and the page names themselves. Results are ranked, keyboard-navigable and
 * highlight the matched words; jumping to one opens its page, scrolls to it and marks the hit
 * (outline + <mark> on the matched words) until the next search / click / page change. */

const SettingsSearch = {
    _input: null,
    _results: null,
    _items: [],
    _sel: -1,
    _marked: [],
    _markTimer: null,

    init() {
        this._input = document.getElementById("settings-search-input");
        this._results = document.getElementById("settings-search-results");
        if (!this._input) return;

        this._input.addEventListener("input", () => this._onQuery(this._input.value));
        this._input.addEventListener("keydown", (e) => {
            if (e.key === "Escape") {
                this._input.value = "";
                this._onQuery("");
                this.clearMarks();
            } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                if (!this._items.length) return;
                e.preventDefault();
                const n = this._items.length;
                this._select((this._sel + (e.key === "ArrowDown" ? 1 : -1) + n) % n);
            } else if (e.key === "Enter") {
                const m = this._items[this._sel >= 0 ? this._sel : 0];
                if (m) {
                    e.preventDefault();
                    this._jumpTo(m);
                }
            }
        });
        this._input.addEventListener("blur", () => {
            // Let a click on a result land before hiding
            setTimeout(() => this._results.classList.add("hidden"), 150);
        });
        this._input.addEventListener("focus", () => {
            if (this._input.value.trim()) this._onQuery(this._input.value);
        });
    },

    /* ---- index (rebuilt per query: panes load lazily and the DOM is small) ---- */
    _clean(t) {
        return (t || "").replace(/\s+/g, " ").trim();
    },

    _buildIndex() {
        const index = [];
        const seen = new Set();
        const add = (page, title, desc, el, extra, isPage) => {
            title = this._clean(title);
            if (!title || title.length > 120) return;
            const key = page + "|" + title.toLowerCase() + "|" + (desc || "").slice(0, 40);
            if (seen.has(key)) return;
            seen.add(key);
            index.push({
                page: page,
                title: title,
                desc: this._clean(desc),
                extra: (extra || "").toLowerCase(),
                el: el,
                isPage: !!isPage,
                path: isPage && SettingsNav.subpages[page] ? [SettingsNav.groups[SettingsNav.subpages[page].group].label] : isPage ? ["Settings"] : SettingsNav.path(page),
            });
        };

        // Page names (every nav destination)
        document.querySelectorAll(".settings-nav-item[data-section]").forEach((btn) => {
            add(btn.dataset.section, btn.textContent, "", null, "page", true);
        });

        // Content: setting rows, sub-group heads, headings, labels, table headers — attributed to their page.
        const scan = (root, page) => {
            root.querySelectorAll("h1,h2,h3,h4,label,th,legend,.setting-row,.setting-subgroup-head,[data-search]").forEach((el) => {
                if (el.closest(".modal, [role=dialog]")) return;
                if (el.matches(".setting-row")) {
                    const t = el.querySelector(".setting-row-title");
                    const d = el.querySelector(".setting-row-desc");
                    add(page, t ? t.textContent : el.dataset.search, d ? d.textContent : "", el, el.dataset.search);
                } else if (!el.closest(".setting-row")) {
                    add(page, el.textContent, "", el, el.dataset.search);
                }
            });
        };
        document.querySelectorAll(".settings-pane").forEach((pane) => {
            const id = pane.id.replace(/^section-/, "");
            const subs = pane.querySelectorAll(".subpage");
            if (subs.length) subs.forEach((el) => scan(el, el.dataset.page));
            else scan(pane, id);
        });
        return index;
    },

    _terms(q) {
        return q.toLowerCase().split(/\s+/).filter(Boolean);
    },

    _score(item, terms) {
        const title = item.title.toLowerCase();
        const desc = item.desc.toLowerCase();
        let score = 0;
        for (const t of terms) {
            if (title.startsWith(t)) score += 4;
            else if (title.includes(t)) score += 3;
            else if (item.extra.includes(t)) score += 2;
            else if (desc.includes(t)) score += 1;
            else return 0; // every term must match somewhere
        }
        if (item.isPage) score += 2;
        return score;
    },

    _esc(t) {
        return t.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    },

    /** HTML-escape text and wrap every query term in <mark>. */
    _highlight(text, terms) {
        if (!terms.length) return this._esc(text);
        const re = new RegExp("(" + terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "gi");
        return text.split(re).map((part, i) => (i % 2 ? "<mark>" + this._esc(part) + "</mark>" : this._esc(part))).join("");
    },

    _snippet(desc, terms) {
        if (!desc) return "";
        const low = desc.toLowerCase();
        let at = -1;
        for (const t of terms) {
            const i = low.indexOf(t);
            if (i >= 0 && (at < 0 || i < at)) at = i;
        }
        if (at < 0) return desc.length > 110 ? desc.slice(0, 110) + "…" : desc;
        const start = Math.max(0, at - 30);
        const end = Math.min(desc.length, start + 120);
        return (start > 0 ? "…" : "") + desc.slice(start, end) + (end < desc.length ? "…" : "");
    },

    _onQuery(q) {
        const terms = this._terms(q);
        const navItems = document.querySelectorAll(".settings-nav-item[data-section]");
        navItems.forEach((b) => {
            b.classList.remove("search-dim");
            const badge = b.querySelector(".nav-count");
            if (badge) badge.remove();
        });
        document.querySelectorAll(".settings-nav-parent .nav-count").forEach((b) => b.remove());
        document.querySelectorAll(".settings-nav-group").forEach((g) => g.classList.remove("search-open"));

        if (!terms.length) {
            this._results.classList.add("hidden");
            this._results.innerHTML = "";
            this._items = [];
            this._sel = -1;
            return;
        }
        this.clearMarks();

        const matches = this._buildIndex()
            .map((item) => ({ item, score: this._score(item, terms) }))
            .filter((m) => m.score > 0)
            .sort((a, b) => b.score - a.score)
            .map((m) => m.item);

        // Markers on the nav: dim pages without hits, count hits on the rest (General's children roll up)
        const counts = {};
        matches.forEach((m) => { if (!m.isPage) counts[m.page] = (counts[m.page] || 0) + 1; });
        navItems.forEach((b) => {
            const page = b.dataset.section;
            const n = counts[page] || 0;
            const byName = b.textContent.trim().toLowerCase().includes(terms[0]);
            b.classList.toggle("search-dim", !n && !byName);
            if (n) {
                const badge = document.createElement("span");
                badge.className = "nav-count";
                badge.textContent = n;
                b.appendChild(badge);
            }
        });
        Object.keys(SettingsNav.groups).forEach((key) => {
            const parent = document.querySelector('#nav-group-' + key + " .settings-nav-parent");
            let total = 0;
            Object.keys(SettingsNav.subpages).forEach((pg) => {
                if (SettingsNav.subpages[pg].group === key) total += counts[pg] || 0;
            });
            if (!parent || !total) return;
            const badge = document.createElement("span");
            badge.className = "nav-count";
            badge.textContent = total;
            parent.insertBefore(badge, parent.querySelector(".nav-chev"));
            document.getElementById("nav-group-" + key)?.classList.add("search-open");
        });

        this._items = matches.slice(0, 12);
        this._sel = this._items.length ? 0 : -1;
        if (!this._items.length) {
            this._results.innerHTML = '<div class="settings-search-empty">No settings match “' + this._esc(q.trim()) + "”</div>";
        } else {
            this._results.innerHTML = "";
            this._items.forEach((m, i) => {
                const btn = document.createElement("button");
                btn.type = "button";
                btn.className = "settings-search-result" + (i === 0 ? " selected" : "");
                btn.setAttribute("role", "option");
                const snippet = this._snippet(m.desc, terms);
                btn.innerHTML =
                    '<div class="sr-path">' + m.path.map((p) => this._esc(p)).join(" › ") + (m.isPage ? " · page" : "") + "</div>" +
                    '<div class="sr-title">' + this._highlight(m.title, terms) + "</div>" +
                    (snippet ? '<div class="sr-desc">' + this._highlight(snippet, terms) + "</div>" : "");
                btn.addEventListener("mousedown", (e) => e.preventDefault()); // keep input focus until click lands
                btn.addEventListener("click", () => this._jumpTo(m));
                btn.addEventListener("mousemove", () => this._select(i));
                this._results.appendChild(btn);
            });
            if (matches.length > this._items.length) {
                const more = document.createElement("div");
                more.className = "settings-search-empty";
                more.textContent = "+" + (matches.length - this._items.length) + " more — keep typing to narrow down";
                this._results.appendChild(more);
            }
        }
        this._results.classList.remove("hidden");
    },

    _select(i) {
        this._sel = i;
        const btns = this._results.querySelectorAll(".settings-search-result");
        btns.forEach((b, j) => b.classList.toggle("selected", j === i));
        if (btns[i]) btns[i].scrollIntoView({ block: "nearest" });
    },

    /* ---- markers in the page ---- */
    clearMarks() {
        clearTimeout(this._markTimer);
        this._marked.forEach((n) => {
            if (n.nodeName === "MARK" && n.parentNode) {
                const parent = n.parentNode;
                parent.replaceChild(document.createTextNode(n.textContent), n);
                parent.normalize();
            } else if (n.classList) {
                n.classList.remove("search-found");
            }
        });
        this._marked = [];
        document.removeEventListener("pointerdown", this._onAway, true);
    },

    _markText(root, terms) {
        const re = new RegExp("(" + terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "gi");
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
            acceptNode: (n) =>
                n.nodeValue.trim() && !n.parentElement.closest("mark, script, style, select, textarea, option, .save-status")
                    ? NodeFilter.FILTER_ACCEPT
                    : NodeFilter.FILTER_REJECT,
        });
        const nodes = [];
        while (walker.nextNode()) nodes.push(walker.currentNode);
        nodes.forEach((node) => {
            if (!re.test(node.nodeValue)) return;
            re.lastIndex = 0;
            const frag = document.createDocumentFragment();
            node.nodeValue.split(re).forEach((part, i) => {
                if (i % 2) {
                    const mk = document.createElement("mark");
                    mk.className = "search-mark";
                    mk.textContent = part;
                    frag.appendChild(mk);
                    this._marked.push(mk);
                } else if (part) {
                    frag.appendChild(document.createTextNode(part));
                }
            });
            node.parentNode.replaceChild(frag, node);
        });
    },

    _onAway: function () {
        SettingsSearch.clearMarks();
    },

    _jumpTo(match) {
        const terms = this._terms(this._input.value);
        this._results.classList.add("hidden");
        this._input.value = "";
        document.querySelectorAll(".settings-nav-item").forEach((b) => b.classList.remove("search-dim"));
        document.querySelectorAll(".nav-count").forEach((b) => b.remove());
        document.querySelectorAll(".settings-nav-group").forEach((g) => g.classList.remove("search-open"));
        this._input.blur();
        SettingsNav.activate(match.page);
        requestAnimationFrame(() => {
            const el = match.el;
            if (!el) return;
            el.scrollIntoView({ block: "center", behavior: "smooth" });
            el.classList.remove("row-highlight");
            void el.offsetWidth; // restart the flash animation
            el.classList.add("row-highlight");
            el.classList.add("search-found");
            this._marked.push(el);
            this._markText(el, terms);
            // Marks stay until the user clicks elsewhere, navigates, searches again, or ~15s pass
            setTimeout(() => document.addEventListener("pointerdown", this._onAway, true), 400);
            this._markTimer = setTimeout(() => this.clearMarks(), 15000);
        });
    },
};

/* ==================== Boot ==================== */

document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".settings-nav-item[data-section]").forEach((btn) => {
        btn.addEventListener("click", () => SettingsNav.activate(btn.dataset.section));
    });
    document.querySelectorAll(".settings-nav-parent").forEach((parent) => {
        parent.addEventListener("click", () => SettingsNav.toggleGroup(parent.dataset.group));
    });
    SettingsSearch.init();
    SettingsNav.route();
});

window.addEventListener("hashchange", () => {
    const h = location.hash.slice(1);
    if (h === SettingsNav.current) return; // our own history.replaceState / activate already handled it
    SettingsNav.route();
});
