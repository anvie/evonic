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

/* ==================== Section router ==================== */

const SettingsNav = {
    sections: {
        general: { init: () => window.settingsGeneral && settingsGeneral.init() },
        tools: { init: () => window.settingsTools && settingsTools.init() },
        models: { init: () => window.settingsModels && settingsModels.init() },
        hmads: { init: () => window.hmads && hmads.init() },
        users: { init: () => window.usersTab && usersTab.init() },
        shared_channel: { init: () => window.sharedChannel && sharedChannel.init() },
        logs: { init: () => window.logViewer && logViewer.init() },
    },
    _initialized: new Set(),

    activate(id, opts) {
        opts = opts || {};
        const section = this.sections[id] ? id : "general";
        document.querySelectorAll(".settings-pane").forEach((p) => {
            p.classList.toggle("active", p.id === "section-" + section);
        });
        document.querySelectorAll(".settings-nav-item").forEach((btn) => {
            btn.classList.toggle("active", btn.dataset.section === section);
            // Keep the active pill visible on mobile — but not on initial load,
            // where it would scroll the search field out of view.
            if (
                btn.dataset.section === section &&
                !opts.fromHash &&
                window.matchMedia("(max-width: 1023px)").matches
            ) {
                btn.scrollIntoView({ inline: "center", block: "nearest", behavior: "smooth" });
            }
        });
        if (!opts.fromHash) {
            history.replaceState(null, "", "#" + section);
        }
        if (!this._initialized.has(section)) {
            this._initialized.add(section);
            try {
                this.sections[section].init();
            } catch (e) {
                console.error("Section init failed:", section, e);
                this._initialized.delete(section);
            }
        }
    },

    route() {
        const h = location.hash.slice(1);
        if (h.startsWith("tools-")) {
            const toolId = decodeURIComponent(h.slice(6));
            this.activate("tools", { fromHash: true });
            if (window.settingsTools && settingsTools.whenLoaded) {
                settingsTools.whenLoaded.then(() => settingsTools.openEditor(toolId));
            }
            return;
        }
        this.activate(this.sections[h] ? h : "general", { fromHash: true });
    },
};

// Legacy alias — old code/pages may still call showTab('...')
window.showTab = function (name) {
    SettingsNav.activate(name);
};

/* ==================== Settings search ==================== */

const SettingsSearch = {
    _index: null,
    _input: null,
    _results: null,

    init() {
        this._input = document.getElementById("settings-search-input");
        this._results = document.getElementById("settings-search-results");
        if (!this._input) return;

        this._input.addEventListener("input", () => this._onQuery(this._input.value));
        this._input.addEventListener("keydown", (e) => {
            if (e.key === "Escape") {
                this._input.value = "";
                this._onQuery("");
                this._input.blur();
            } else if (e.key === "Enter") {
                const first = this._results.querySelector(".settings-search-result");
                if (first) first.click();
            }
        });
        this._input.addEventListener("blur", () => {
            // Let a click on a result land before hiding
            setTimeout(() => this._results.classList.add("hidden"), 150);
        });
        this._input.addEventListener("focus", () => {
            if (this._input.value.trim()) this._onQuery(this._input.value);
        });

        // "/" focuses search (unless typing in a field)
        document.addEventListener("keydown", (e) => {
            if (
                e.key === "/" &&
                !e.metaKey &&
                !e.ctrlKey &&
                !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) &&
                !document.activeElement.isContentEditable
            ) {
                e.preventDefault();
                this._input.focus();
            }
        });
    },

    _buildIndex() {
        const index = [];
        document.querySelectorAll(".settings-pane").forEach((pane) => {
            const section = pane.id.replace(/^section-/, "");
            const navBtn = document.querySelector(
                '.settings-nav-item[data-section="' + section + '"]',
            );
            const sectionLabel = navBtn ? navBtn.textContent.trim() : section;
            pane.querySelectorAll(".setting-row[data-search]").forEach((row) => {
                index.push({
                    section: section,
                    sectionLabel: sectionLabel,
                    title: row.querySelector(".setting-row-title")
                        ? row.querySelector(".setting-row-title").textContent.trim()
                        : row.dataset.search,
                    haystack: (row.dataset.search || "").toLowerCase(),
                    row: row,
                });
            });
        });
        return index;
    },

    _onQuery(q) {
        q = q.trim().toLowerCase();
        const navItems = document.querySelectorAll(".settings-nav-item");
        if (!q) {
            this._results.classList.add("hidden");
            this._results.innerHTML = "";
            navItems.forEach((b) => b.classList.remove("search-dim"));
            return;
        }
        if (!this._index) this._index = this._buildIndex();
        const matches = this._index.filter((item) => item.haystack.includes(q));

        // Dim nav items whose section has no match (and doesn't match by name)
        const matchedSections = new Set(matches.map((m) => m.section));
        navItems.forEach((b) => {
            const byName = b.textContent.trim().toLowerCase().includes(q);
            b.classList.toggle(
                "search-dim",
                !byName && !matchedSections.has(b.dataset.section),
            );
        });

        if (matches.length === 0) {
            this._results.innerHTML =
                '<div class="settings-search-empty">No settings match your search</div>';
        } else {
            this._results.innerHTML = "";
            matches.slice(0, 8).forEach((m) => {
                const btn = document.createElement("button");
                btn.type = "button";
                btn.className = "settings-search-result";
                btn.innerHTML =
                    '<div class="sr-path"></div><div class="sr-title"></div>';
                btn.querySelector(".sr-path").textContent = m.sectionLabel;
                btn.querySelector(".sr-title").textContent = m.title;
                btn.addEventListener("click", () => this._jumpTo(m));
                this._results.appendChild(btn);
            });
        }
        this._results.classList.remove("hidden");
    },

    _jumpTo(match) {
        this._results.classList.add("hidden");
        this._input.value = "";
        document
            .querySelectorAll(".settings-nav-item")
            .forEach((b) => b.classList.remove("search-dim"));
        SettingsNav.activate(match.section);
        requestAnimationFrame(() => {
            match.row.scrollIntoView({ block: "center", behavior: "smooth" });
            match.row.classList.remove("row-highlight");
            void match.row.offsetWidth; // restart the flash animation
            match.row.classList.add("row-highlight");
        });
    },
};

/* ==================== Boot ==================== */

document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".settings-nav-item").forEach((btn) => {
        btn.addEventListener("click", () => SettingsNav.activate(btn.dataset.section));
    });
    SettingsSearch.init();
    SettingsNav.route();
});

window.addEventListener("hashchange", () => SettingsNav.route());
