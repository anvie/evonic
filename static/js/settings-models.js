/* Models pane: provider-grouped model management.
 * Extracted from settings.html — uses ModelsCache so the
 * General pane's model selects stay in sync via 'models:changed'. */

window.settingsModels = {
    models: [],
    providers: [],
    searchQuery: "",
    statusFilter: "all",
    _pings: {},   // last ping result per model id, kept across re-renders
    _currentTestModelId: null,
    _fetchProviderId: null,
    _authProviderId: null,
    _authKind: null,
    _authPollTimer: null,

    async init() {
        if (!this._menuHandlersBound) {
            this._menuHandlersBound = true;
            document.addEventListener("click", () => this.closeMenus());
            document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") this.closeMenus(); });
        }
        await this.load();
    },

    async load() {
        try {
            const [modelsData, providersData] = await Promise.all([
                ModelsCache.get(),
                apiGet("/api/providers").then((d) => d.providers || []),
            ]);
            this.models = modelsData;
            this.providers = providersData;
            this.render();
            this._populateProviderSelect();
        } catch (error) {
            console.error("Failed to load models:", error);
        }
    },

    async reload() {
        ModelsCache.invalidate();
        await this.load();
    },

    _populateProviderSelect() {
        const sel = document.getElementById("model-provider");
        if (!sel) return;
        sel.innerHTML = this.providers
            .map((p) => `<option value="${p.id}">${p.name}</option>`)
            .join("");
    },

    render() {
        const modelsList = document.getElementById("models-list");
        const q = this.searchQuery.toLowerCase().trim();

        const byStatus = (m) =>
            this.statusFilter === "on" ? Boolean(m.enabled)
            : this.statusFilter === "think" ? Boolean(m.thinking)
            : true;
        const filtered = this.models.filter(
            (m) =>
                byStatus(m) &&
                (!q ||
                    (m.name || "").toLowerCase().includes(q) ||
                    (m.provider || "").toLowerCase().includes(q) ||
                    (m.model_name || "").toLowerCase().includes(q)),
        );

        // Group models by provider, and ensure ALL providers appear (even with 0 models)
        const provMap = {};
        for (const p of this.providers) provMap[p.id] = p;
        const groups = {};
        for (const p of this.providers) {
            if (this.statusFilter !== "all") continue;   // a status filter lists only providers that have matching models
            const matchesSearch = !q || (p.name || "").toLowerCase().includes(q) || p.id.toLowerCase().includes(q);
            if (matchesSearch) groups[p.id] = [];
        }
        for (const m of filtered) {
            const pid = m.provider || "unknown";
            if (!groups[pid]) groups[pid] = [];
            groups[pid].push(m);
        }

        if (Object.keys(groups).length === 0) {
            modelsList.innerHTML =
                '<p class="text-gray-500 text-center py-4">No models or providers match your search.</p>';
            return;
        }

        const sortedProviders = Object.keys(groups).sort((a, b) => {
            const na = (provMap[a]?.name || a).toLowerCase();
            const nb = (provMap[b]?.name || b).toLowerCase();
            return na.localeCompare(nb);
        });

        modelsList.innerHTML = sortedProviders
            .map((pid) => this._renderProvider(pid, provMap[pid] || { id: pid, name: pid }, groups[pid]))
            .join("");
    },

    // ---- Provider block + model cards (see .mcd-* in settings.css) ----
    _ICON: {
        star: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2.5l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 17.4 6.1 20.5l1.2-6.5L2.5 9.4l6.6-.9z"/></svg>',
        dots: '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>',
        bolt: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 3 4 14h7l-1 7 9-11h-7z"/></svg>',
        edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
        copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
        trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2m-9 0 1 14h8l1-14"/></svg>',
        brain: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a4 4 0 0 0-4 4 3 3 0 0 0-2 5 3 3 0 0 0 2 5 4 4 0 0 0 4 4zM12 3a4 4 0 0 1 4 4 3 3 0 0 1 2 5 3 3 0 0 1-2 5 4 4 0 0 1-4 4z"/></svg>',
        check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
        eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
        plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
        refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 15-6.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-15 6.7L3 16M3 21v-5h5"/></svg>',
        play: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 4l14 8-14 8z"/></svg>',
        key: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="15" r="4"/><path d="m11 12 9-9m-3 3 3 3"/></svg>',
    },

    _fmtTokens(n) {
        n = Number(n) || 0;
        if (n <= 0) return "";
        if (n >= 1000000) return (n / 1000000).toFixed(n % 1000000 ? 1 : 0) + "M";
        if (n >= 1000) return Math.round(n / 1000) + "K";
        return String(n);
    },

    // Claude CLI models use an effort level (mirrors backend resolve_effort); others show their token budget.
    _thinkLabel(model) {
        const budget = Number(model.thinking_budget) || 0;
        if (model.api_format === "claude_cli") {
            const lvl = budget <= 0 ? "medium" : budget <= 2048 ? "low" : budget <= 8192 ? "medium"
                : budget <= 24576 ? "high" : budget <= 65536 ? "xhigh" : "max";
            return "Thinking · " + lvl;
        }
        return budget > 0 ? "Thinking · " + this._fmtTokens(budget) : "Thinking";
    },

    _isDecision(model) {
        return model.type === "decision";
    },

    _renderProvider(pid, prov, models) {
        const I = this._ICON;
        const e = (v) => this._escapeHtml(v);
        const apiFormat = prov.api_format;
        const hasCredentialSetup = ["codex", "anthropic", "claude_cli"].includes(apiFormat);
        const connected = Boolean(prov.credential_configured);
        const kind = prov.type === "local" ? "Local" : "Remote";

        let status = "";
        let primary = "";
        if (hasCredentialSetup) {
            status = connected
                ? '<span class="mcd-pill ok"><span class="mcd-dot"></span>Ready</span>'
                : '<span class="mcd-pill warn"><span class="mcd-dot"></span>Setup needed</span>';
            if (!connected) {
                primary = `<button type="button" class="mcd-btn primary" onclick="settingsModels.openCredentialSetup('${pid}')">${I.key}Set up</button>`;
            } else {
                primary = `<button type="button" class="mcd-btn" onclick="settingsModels.fetchModels('${pid}')" title="Discover models from this provider">${I.refresh}Fetch models</button>`;
            }
        } else {
            primary = `<button type="button" class="mcd-btn" onclick="settingsModels.fetchModels('${pid}')" title="Discover models from the provider API">${I.refresh}Fetch models</button>`;
        }
        const addModel = `<button type="button" class="mcd-btn" onclick="settingsModels.addModelForProvider('${pid}')" title="Add a custom model">${I.plus}Model</button>`;
        const menu =
            (hasCredentialSetup
                ? `<button role="menuitem" type="button" onclick="settingsModels.openCredentialSetup('${pid}')">${I.key}${connected ? "Manage login" : "Set up"}</button>`
                : `<button role="menuitem" type="button" onclick="settingsModels.testProvider('${pid}')">${I.bolt}Test connection</button>`) +
            `<button role="menuitem" type="button" onclick="settingsModels.editProvider('${pid}')">${I.edit}Edit provider</button>` +
            `<hr><button role="menuitem" type="button" class="danger" onclick="settingsModels.deleteProvider('${pid}')">${I.trash}Delete provider</button>`;

        const cards = models.length
            ? models.map((m) => this._renderModelCard(m, prov)).join("")
            : `<div class="mcd-empty">No models yet. Use <strong>Fetch models</strong> to discover what this provider offers, or add one by hand.</div>`;

        return `
        <section class="mcd-prov">
            <div class="mcd-prov-head">
                <div class="mcd-prov-id">
                    ${this._logo({ provider: pid, provider_name: prov.name }, 22, "mcd-logo mcd-logo-sm")}
                    <div class="mcd-prov-text">
                        <div class="mcd-prov-name">${e(prov.name)}</div>
                        <div class="mcd-prov-meta">${kind} · ${models.length} model${models.length !== 1 ? "s" : ""}</div>
                    </div>
                    ${status}
                </div>
                <div class="mcd-prov-actions">
                    ${primary}${addModel}
                    <div class="mcd-menuwrap">
                        <button type="button" class="mcd-ib" aria-label="Provider actions" aria-haspopup="menu" aria-expanded="false" onclick="settingsModels.toggleMenu(event, this)">${I.dots}</button>
                        <div class="mcd-menu" role="menu">${menu}</div>
                    </div>
                </div>
            </div>
            <div class="mcd-grid">${cards}</div>
        </section>`;
    },

    // Brand mark (ai-logos.js); the provider's display name helps when the model name says nothing.
    _logo(m, size, wrapClass) {
        if (typeof aiLogoHtml !== "function") return "";
        const prov = this.providers.find((p) => p.id === m.provider);
        const html = aiLogoHtml({ ...m, provider_name: m.provider_name || (prov && prov.name) || "" }, size);
        return wrapClass ? `<span class="${wrapClass}">${html}</span>` : html;
    },

    _renderModelCard(model, prov) {
        const I = this._ICON;
        const e = (v) => this._escapeHtml(v);
        const id = model.id;
        const name = model.name || model.model_name || id;
        const decision = this._isDecision(model);
        const ctx = this._fmtTokens(model.context_window);
        const shortcode = model.shortcode != null ? model.shortcode : "?";

        let facts = "";
        if (model.thinking) facts += `<span class="mcd-chip think">${I.brain}${e(this._thinkLabel(model))}</span>`;
        if (model.vision_supported) facts += `<span class="mcd-chip">${I.eye}Vision</span>`;
        if (ctx) facts += `<span class="mcd-chip">${ctx} context</span>`;
        if (decision) facts += `<span class="mcd-chip ready">${I.check}Decision</span>`;

        const state = model.enabled
            ? (model.is_default ? "Default · enabled" : "Enabled")
            : "Off";
        const search = [name, model.model_name, prov && prov.name].join(" ").toLowerCase();

        return `
        <article class="mcd-card${model.is_default ? " is-default" : ""}${model.enabled ? "" : " is-off"}" tabindex="0" aria-label="Edit ${e(name)}" onclick="settingsModels.onCardClick(event, '${id}')" onkeydown="settingsModels.onCardKey(event, '${id}')" data-on="${model.enabled ? 1 : 0}" data-think="${model.thinking ? 1 : 0}" data-search="${e(search)}">
            <div class="mcd-head">
                <span class="mcd-logo">${this._logo(model, 26)}${model.is_default ? `<span class="mcd-star" title="Default model">${I.star}</span>` : ""}</span>
                <div class="mcd-title">
                    <div class="mcd-name" title="${e(name)}">${e(name)}</div>
                    <div class="mcd-id"><span title="${e(model.model_name)}">${e(model.model_name)}</span>
                        <button type="button" class="mcd-copy" aria-label="Copy model id" data-id="${e(model.model_name)}" onclick="settingsModels.copyId(this)">${I.copy}</button></div>
                </div>
                <div class="mcd-acts">
                    ${decision ? "" : `<button type="button" class="mcd-ib mcd-test" aria-label="Ping ${e(name)}" title="Ping: send a short prompt to check this model answers" onclick="settingsModels.pingModel('${id}', this)">${I.bolt}</button>`}
                    <div class="mcd-menuwrap">
                        <button type="button" class="mcd-ib" aria-label="More actions" aria-haspopup="menu" aria-expanded="false" onclick="settingsModels.toggleMenu(event, this)">${I.dots}</button>
                        <div class="mcd-menu" role="menu">
                            ${model.is_default || decision ? "" : `<button role="menuitem" type="button" onclick="settingsModels.setDefault('${id}')">${I.star}Set as default</button>`}
                            <button role="menuitem" type="button" onclick="settingsModels.edit('${id}')">${I.edit}Edit</button>
                            <button role="menuitem" type="button" onclick="settingsModels.clone('${id}')">${I.copy}Clone</button>
                            ${decision ? `<button role="menuitem" type="button" onclick="decisionModels.openPlayground({model: '${id}'})">${I.play}Open playground</button>` : ""}
                            <hr><button role="menuitem" type="button" class="danger" onclick="settingsModels.remove('${id}')">${I.trash}Delete</button>
                        </div>
                    </div>
                </div>
            </div>
            ${facts ? `<div class="mcd-facts">${facts}</div>` : ""}
            <div class="mcd-foot">
                <span class="mcd-state"><span class="mcd-dot${model.enabled ? "" : " off"}"></span>${state}</span>
                <span class="mcd-ping" aria-live="polite">${this._pingHtml(this._pings[id])}</span>
                <span class="mcd-sc" title="Use /model ${shortcode} in chat">#${shortcode}</span>
            </div>
        </article>`;
    },

    // A click anywhere on the card edits the model; its own controls keep their actions.
    onCardClick(event, modelId) {
        if (event.target.closest("button, a, input, .mcd-menu")) return;
        this.edit(modelId);
    },

    onCardKey(event, modelId) {
        if (event.target !== event.currentTarget || (event.key !== "Enter" && event.key !== " ")) return;
        event.preventDefault();
        this.edit(modelId);
    },

    // ---- ping: one short prompt through the model itself ----
    _pingHtml(r) {
        if (!r) return "";
        if (r.busy) return '<span class="mcd-pingres busy">Pinging…</span>';
        const e = (v) => this._escapeHtml(v);
        if (r.success) {
            const tip = r.reply ? `Replied: ${r.reply}` : (r.note || "Model answered");
            return `<span class="mcd-pingres ok" title="${e(tip)}">${this._ICON.check}Reachable · ${r.latency_ms} ms</span>`;
        }
        const msg = String(r.error || "No answer");
        const short = msg.length > 42 ? msg.slice(0, 41) + "…" : msg;
        return `<span class="mcd-pingres bad" title="${e(msg)}">Unreachable · ${e(short)}</span>`;
    },

    async pingModel(modelId, btn) {
        const card = btn.closest(".mcd-card");
        const slot = card && card.querySelector(".mcd-ping");
        if (btn.classList.contains("is-busy")) return;
        const show = (r) => { this._pings[modelId] = r; if (slot) slot.innerHTML = this._pingHtml(r); };
        btn.classList.add("is-busy");
        btn.disabled = true;
        show({ busy: true });
        try {
            const res = await fetch("/api/models/" + encodeURIComponent(modelId) + "/ping", { method: "POST" });
            show(await res.json());
        } catch (error) {
            show({ success: false, error: error.message });
        } finally {
            btn.classList.remove("is-busy");
            btn.disabled = false;
        }
    },

    // ---- popup menus / small helpers ----
    toggleMenu(ev, btn) {
        ev.stopPropagation();
        const menu = btn.parentElement.querySelector(".mcd-menu");
        const open = !menu.classList.contains("open");
        this.closeMenus();
        menu.classList.toggle("open", open);
        btn.setAttribute("aria-expanded", String(open));
    },

    closeMenus() {
        document.querySelectorAll(".mcd-menu.open").forEach((m) => {
            m.classList.remove("open");
            const b = m.parentElement.querySelector("button[aria-expanded]");
            if (b) b.setAttribute("aria-expanded", "false");
        });
    },

    copyId(btn) {
        try {
            navigator.clipboard.writeText(btn.dataset.id || "").catch(() => {});
        } catch (err) { /* clipboard unavailable */ }
        const prev = btn.innerHTML;
        btn.innerHTML = this._ICON.check;
        setTimeout(() => { btn.innerHTML = prev; }, 1200);
    },

    setStatusFilter(f) {
        this.statusFilter = f;
        document.querySelectorAll("#model-status-filter button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.f === f)));
        this.render();
    },

    filter() {
        this.searchQuery = document.getElementById("model-search-input").value;
        this.render();
    },

    /* ---- Provider CRUD ---- */

    showAddProviderModal() {
        document.getElementById("provider-modal-title").textContent = "Add Provider";
        document.getElementById("provider-form").reset();
        document.getElementById("provider-edit-id").value = "";
        document.getElementById("provider-id").disabled = false;
        openModal("provider-modal");
    },

    editProvider(providerId) {
        const prov = this.providers.find((p) => p.id === providerId);
        if (!prov) return;
        document.getElementById("provider-modal-title").textContent = "Edit Provider";
        document.getElementById("provider-edit-id").value = prov.id;
        document.getElementById("provider-id").value = prov.id;
        document.getElementById("provider-id").disabled = true;
        document.getElementById("provider-name").value = prov.name || "";
        document.getElementById("provider-type").value = prov.type || "remote";
        document.getElementById("provider-base-url").value = prov.endpoint || prov.base_url || "";
        document.getElementById("provider-api-key").value = "";
        document.getElementById("provider-api-format").value = prov.api_format || "openai";
        openModal("provider-modal");
    },

    _adapterKeyForFormat(format) {
        return {
            openai: "openai.chat_completions.v1",
            ollama: "ollama.chat.v1",
            anthropic: "anthropic.messages.v1",
            codex: "codex.responses.v1",
            systemone: "systemone.questions.v1",
        }[format] || "openai.chat_completions.v1";
    },

    async saveProvider(event) {
        event.preventDefault();
        const editId = document.getElementById("provider-edit-id").value;
        const apiFormat = document.getElementById("provider-api-format").value;
        const data = {
            id: document.getElementById("provider-id").value,
            name: document.getElementById("provider-name").value,
            type: document.getElementById("provider-type").value,
            endpoint: document.getElementById("provider-base-url").value,
            api_key: document.getElementById("provider-api-key").value,
            api_format: apiFormat,
            adapter_key: this._adapterKeyForFormat(apiFormat),
        };

        try {
            let result;
            if (editId) {
                result = await apiPut("/api/providers/" + encodeURIComponent(editId), data);
            } else {
                result = await apiPost("/api/providers", data);
            }
            if (result.success) {
                closeModal("provider-modal");
                await this.reload();
                if (window.toast) toast.show("Provider saved", "success");
            } else {
                if (window.toast) toast.show("Error: " + (result.error || "Failed"), "error");
            }
        } catch (error) {
            if (window.toast) toast.show("Failed to save provider: " + error.message, "error");
        }
    },

    async deleteProvider(providerId) {
        const prov = this.providers.find((p) => p.id === providerId);
        const label = (prov && prov.name) || providerId;
        let impact;
        try {
            impact = await apiGet("/api/providers/" + encodeURIComponent(providerId) + "/delete-impact");
        } catch (error) {
            if (window.toast) toast.show("Failed: " + error.message, "error");
            return;
        }
        const n = (impact.models || []).length;
        const cascade = n > 0;

        if (impact.is_default) {
            const def = impact.models.find((m) => m.is_default);
            if (window.showConfirm) {
                await showConfirm({
                    title: "Cannot delete provider",
                    message: `"${def ? def.name : "A model"}" in ${label} is the global default model.\nSet another model as default, then delete this provider.`,
                    confirmText: "OK",
                    cancelText: "Close",
                    danger: false,
                });
            }
            return;
        }

        let message = `Delete "${label}"?`;
        if (cascade) {
            const names = impact.models.map((m) => "• " + m.name);
            const shown = names.slice(0, 8).concat(names.length > 8 ? [`… and ${names.length - 8} more`] : []);
            message = `Delete "${label}" and its ${n} model${n !== 1 ? "s" : ""}?\n${shown.join("\n")}`;
            if ((impact.agents || []).length) {
                const agents = impact.agents.map((a) => a.name);
                const list = agents.slice(0, 5).join(", ") + (agents.length > 5 ? ` and ${agents.length - 5} more` : "");
                message += `\n\nAgents using them will fall back to the default model: ${list}.`;
            }
            if ((impact.profiles || []).length) {
                message += `\nDecision profiles still pointing at them: ${impact.profiles.join(", ")}.`;
            }
        }
        if (!(await showConfirm({ title: "Delete Provider", message, confirmText: cascade ? `Delete provider and ${n} model${n !== 1 ? "s" : ""}` : "Delete" }))) return;

        try {
            const result = await apiDelete("/api/providers/" + encodeURIComponent(providerId) + (cascade ? "?cascade=1" : ""));
            if (result.success) {
                if (window.toast && cascade) toast.show(`Deleted ${label} and ${result.models_removed} model${result.models_removed !== 1 ? "s" : ""}`, "success");
                await this.reload();
            } else {
                if (window.toast) toast.show("Error: " + (result.error || "Failed"), "error");
            }
        } catch (error) {
            if (window.toast) toast.show("Failed: " + error.message, "error");
        }
    },

    async testProvider(providerId) {
        if (window.toast) toast.show("Testing provider connection…", "info", 2000);
        try {
            const result = await apiPost(
                "/api/providers/" + encodeURIComponent(providerId) + "/test",
                {},
            );
            if (result.success) {
                if (window.toast) toast.success(result.message || "Connected!", 3000);
            } else {
                if (window.toast) toast.error("Failed: " + (result.error || "Unknown error"), 5000);
            }
        } catch (error) {
            if (window.toast) toast.error("Connection error: " + error.message, 5000);
        }
    },

    /* ---- Fetch Models from Provider ---- */

    async fetchModels(providerId) {
        this._fetchProviderId = providerId;
        const prov = this.providers.find((p) => p.id === providerId);
        const content = document.getElementById("fetch-models-content");
        document.getElementById("fetch-models-title").textContent =
            "Available Models — " + (prov ? prov.name : providerId);
        content.innerHTML =
            '<div class="text-center py-8"><div class="spinner" style="width:32px;height:32px;border-width:3px;"></div><p class="mt-4 text-gray-500">Fetching models from provider…</p></div>';
        openModal("fetch-models-modal");

        try {
            const result = await apiPost(
                "/api/providers/" + encodeURIComponent(providerId) + "/fetch-models",
                {},
            );
            if (!result.success) {
                content.innerHTML =
                    '<p class="text-red-500 text-center py-4">Failed: ' +
                    this._escapeHtml(result.error || "Unknown error") +
                    "</p>";
                return;
            }
            if (!result.models || result.models.length === 0) {
                content.innerHTML =
                    '<p class="text-gray-500 text-center py-4">No models found.</p>';
                return;
            }

            const searchHtml =
                '<input type="text" id="fetch-model-search" placeholder="Filter models…" oninput="settingsModels._filterFetchedModels()" class="w-full border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm mb-3 focus:outline-none focus:ring-2 focus:ring-indigo-300 dark:bg-gray-700 dark:text-gray-100" />';

            const listHtml = result.models
                .map(
                    (m) =>
                        `<label class="fetch-model-item flex items-center gap-3 px-3 py-2 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700/50 cursor-pointer" data-model-id="${this._escapeHtml(m.id)}">
                        <input type="checkbox" class="fetch-model-cb rounded border-gray-300 text-indigo-600 focus:ring-indigo-500" value="${this._escapeHtml(m.id)}" ${m.already_added ? "disabled checked" : ""} />
                        <span class="text-sm text-gray-800 dark:text-gray-200 truncate">${this._escapeHtml(m.id)}</span>
                        ${m.already_added ? '<span class="text-[10px] text-gray-400 ml-auto">added</span>' : ""}
                    </label>`,
                )
                .join("");

            content.innerHTML = searchHtml + '<div class="space-y-1 max-h-[50vh] overflow-y-auto">' + listHtml + "</div>";
        } catch (error) {
            content.innerHTML =
                '<p class="text-red-500 text-center py-4">Error: ' +
                this._escapeHtml(error.message) +
                "</p>";
        }
    },

    _filterFetchedModels() {
        const q = (document.getElementById("fetch-model-search")?.value || "").toLowerCase();
        document.querySelectorAll(".fetch-model-item").forEach((el) => {
            el.style.display = el.dataset.modelId.toLowerCase().includes(q) ? "" : "none";
        });
    },

    async addSelectedModels() {
        const cbs = document.querySelectorAll(".fetch-model-cb:checked:not(:disabled)");
        if (cbs.length === 0) {
            if (window.toast) toast.show("No models selected", "info");
            return;
        }
        let added = 0;
        for (const cb of cbs) {
            try {
                const result = await apiPost(
                    "/api/providers/" + encodeURIComponent(this._fetchProviderId) + "/add-model",
                    { model_name: cb.value },
                );
                if (result.success) added++;
            } catch (e) {
                console.error("Failed to add model:", cb.value, e);
            }
        }
        closeModal("fetch-models-modal");
        await this.reload();
        if (window.toast) toast.show(`Added ${added} model(s)`, "success");
    },

    /* ---- Model CRUD ---- */

    showAddModelModal() {
        document.getElementById("modal-title").textContent = "Add Model";
        document.getElementById("model-form").reset();
        document.getElementById("model-id").value = "";
        this._populateProviderSelect();
        this.toggleFields();
        openModal("model-modal");
    },

    addModelForProvider(providerId) {
        const prov = this.providers.find((p) => p.id === providerId);
        if (!prov) return;

        document.getElementById("modal-title").textContent = "Add Model — " + (prov.name || providerId);
        document.getElementById("model-form").reset();
        document.getElementById("model-id").value = "";
        this._populateProviderSelect();
        document.getElementById("model-provider").value = providerId;
        // SystemOne only serves structured decisions.
        document.getElementById("model-type").value =
            prov.adapter_key === "systemone.questions.v1" ? "decision" : "llm";
        this.toggleFields();
        openModal("model-modal");
    },

    edit(modelId) {
        const model = this.models.find((m) => m.id === modelId);
        if (!model) return;

        document.getElementById("modal-title").textContent = "Edit Model";
        document.getElementById("model-id").value = model.id;
        document.getElementById("model-name").value = model.name || "";
        document.getElementById("model-type").value = this._isDecision(model) ? "decision" : "llm";
        this._populateProviderSelect();
        document.getElementById("model-provider").value = model.provider || "";
        document.getElementById("model-base-url").value = model.legacy_override_provenance ? "Legacy override retained (read-only)" : "Inherited from provider";
        document.getElementById("model-name-param").value = model.model_name || "";
        document.getElementById("model-max-tokens").value = model.max_tokens || 32768;
        document.getElementById("model-context-window").value = model.context_window || 0;
        document.getElementById("model-timeout").value = model.timeout || 60;
        document.getElementById("model-max-concurrent").value =
            model.model_max_concurrent != null ? model.model_max_concurrent : 1;
        document.getElementById("model-temperature").value =
            model.temperature != null ? model.temperature : "";
        document.getElementById("model-thinking").checked = !!model.thinking;
        document.getElementById("model-thinking-budget").value =
            model.thinking_budget || 0;
        document.getElementById("model-enabled").checked = !!model.enabled;
        document.getElementById("model-is-default").checked = !!model.is_default;
        document.getElementById("model-vision-supported").checked =
            !!model.vision_supported;
        this.toggleFields();
        openModal("model-modal");
    },

    toggleFields() {
        // Decision models have no chat settings (tokens, sampling, thinking, vision, default).
        const decision = document.getElementById("model-type").value === "decision";
        document.querySelectorAll("#model-form [data-llm-only]").forEach((el) => {
            el.classList.toggle("hidden", decision);
        });
    },

    async save(event) {
        event.preventDefault();

        const modelId = document.getElementById("model-id").value;
        // Capabilities are derived server-side from the model type.
        const modelData = {
            name: document.getElementById("model-name").value,
            type: document.getElementById("model-type").value,
            provider: document.getElementById("model-provider").value,
            model_name: document.getElementById("model-name-param").value,
            max_tokens:
                parseInt(document.getElementById("model-max-tokens").value) || 32768,
            context_window:
                parseInt(document.getElementById("model-context-window").value) || 0,
            timeout: parseInt(document.getElementById("model-timeout").value) || 60,
            model_max_concurrent:
                parseInt(document.getElementById("model-max-concurrent").value) || 0,
            temperature:
                document.getElementById("model-temperature").value !== ""
                    ? parseFloat(document.getElementById("model-temperature").value)
                    : null,
            thinking: document.getElementById("model-thinking").checked ? 1 : 0,
            thinking_budget:
                parseInt(document.getElementById("model-thinking-budget").value) || 0,
            enabled: document.getElementById("model-enabled").checked ? 1 : 0,
            is_default: document.getElementById("model-is-default").checked ? 1 : 0,
            vision_supported: document.getElementById("model-vision-supported").checked
                ? 1
                : 0,
        };

        try {
            let result;
            if (modelId) {
                result = await apiPut(
                    "/api/models/" + encodeURIComponent(modelId),
                    modelData,
                );
            } else {
                result = await apiPost("/api/models", modelData);
            }

            if (result.success) {
                closeModal("model-modal");
                await this.reload();
                if (window.toast) toast.show("Model saved", "success");
            } else {
                if (window.toast)
                    toast.show("Error: " + (result.error || "Failed to save model"), "error");
            }
        } catch (error) {
            console.error("Failed to save model:", error);
            if (window.toast) toast.show("Failed to save model: " + error.message, "error");
        }
    },

    async setDefault(modelId) {
        try {
            const result = await apiPost(
                "/api/models/" + encodeURIComponent(modelId) + "/set-default",
                {},
            );
            if (result.success) {
                await this.reload();
            } else {
                if (window.toast)
                    toast.show("Error: " + (result.error || "Failed to set default"), "error");
            }
        } catch (error) {
            console.error("Failed to set default:", error);
        }
    },

    async remove(modelId) {
        if (
            !(await showConfirm({
                title: "Delete Model",
                message: "Delete this model? This cannot be undone.",
                confirmText: "Delete",
            }))
        )
            return;

        try {
            const result = await apiDelete("/api/models/" + encodeURIComponent(modelId));
            if (result.success) {
                await this.reload();
            } else {
                if (window.toast)
                    toast.show("Error: " + (result.error || "Failed to delete model"), "error");
            }
        } catch (error) {
            console.error("Failed to delete model:", error);
        }
    },

    async clone(modelId) {
        try {
            const result = await apiPost(
                "/api/models/" + encodeURIComponent(modelId) + "/clone",
                {},
            );
            if (result.success) {
                await this.reload();
                this.edit(result.model_id);
            } else {
                if (window.toast)
                    toast.show("Error: " + (result.error || "Failed to clone model"), "error");
            }
        } catch (error) {
            console.error("Failed to clone model:", error);
            if (window.toast) toast.show("Failed to clone model: " + error.message, "error");
        }
    },

    /* ---- Connection test ---- */

    _parseTestError(rawError) {
        if (!rawError) return { message: "Unknown error", detail: "" };
        const jsonMatch = rawError.match(
            /\{[^}]*"error"\s*:\s*(?:"([^"]+)"|\{"message"\s*:\s*"([^"]+)"\})/,
        );
        if (jsonMatch) {
            return { message: jsonMatch[1] || jsonMatch[2], detail: rawError };
        }
        const httpMatch = rawError.match(/^HTTP\s+(\d+):\s*(.*)/);
        if (httpMatch) {
            return { message: httpMatch[2] || rawError, detail: rawError };
        }
        return { message: rawError, detail: "" };
    },

    _getTestTroubleshootingTips(statusCode, errorMsg) {
        const msg = (errorMsg || "").toLowerCase();
        const tips = [];
        if (statusCode === 401) {
            tips.push("Your API key is missing or invalid — check the provider or model settings");
            tips.push("Some providers require you to generate an API key from their dashboard first");
        } else if (statusCode === 403) {
            tips.push("Access denied — your API key may not have permission for this endpoint");
        } else if (statusCode === 404) {
            tips.push("The API endpoint was not found — verify the Base URL is correct");
        } else if (statusCode === 429) {
            tips.push("Rate limited — wait and try again");
        } else if (statusCode && statusCode >= 500) {
            tips.push("The provider's server returned an error — usually temporary");
        }
        if (msg.includes("connection") || msg.includes("timeout") || msg.includes("network")) {
            tips.push("Check that the Base URL is reachable from this server");
        }
        return tips;
    },

    async testConnection(modelId) {
        const testStatus = document.getElementById("connection-test-status");
        const footer = document.getElementById("connection-test-footer");
        const title = document.getElementById("connection-test-title");
        const header = document.getElementById("connection-test-header");

        this._currentTestModelId = modelId;
        const testBtn = document.querySelector(
            `button[onclick*="testConnection('${modelId}')"]`,
        );
        if (testBtn) {
            testBtn.disabled = true;
            testBtn.classList.add("opacity-50", "cursor-not-allowed");
        }

        if (header) {
            header.className =
                "flex justify-between items-center p-5 border-b border-gray-200 dark:border-gray-600";
        }
        title.textContent = "Testing Connection…";
        title.className = "m-0 text-gray-800 dark:text-gray-100";
        openModal("connection-test-modal");
        testStatus.innerHTML =
            '<div class="text-center py-8">' +
            '<div class="spinner" style="width:32px;height:32px;border-width:3px;"></div>' +
            '<p class="mt-4 text-gray-600 dark:text-gray-400 font-medium">Testing connection…</p>' +
            "</div>";
        footer.classList.add("hidden");

        if (window.toast) toast.show("Testing model connection…", "info", 2000);

        try {
            const response = await fetch(
                "/api/models/" + encodeURIComponent(modelId) + "/test",
                { method: "POST" },
            );
            const result = await response.json();

            if (result.success) {
                if (window.toast) toast.success("Connected successfully!", 3000);
                if (header) {
                    header.className =
                        "flex justify-between items-center p-5 border-b border-green-200 dark:border-green-700 bg-green-50 dark:bg-green-900/20";
                }
                title.textContent = "Connection Successful";
                title.className = "m-0 text-green-700 dark:text-green-400";
                testStatus.innerHTML =
                    '<div class="p-4 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-700 rounded-lg">' +
                    '<div class="flex items-center gap-2 mb-3">' +
                    '<svg class="w-6 h-6 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>' +
                    '<span class="text-green-800 dark:text-green-300 font-semibold text-base">Model is reachable</span></div>' +
                    '<div class="text-green-700 dark:text-green-400 text-sm"><strong>Endpoint:</strong> ' +
                    this._escapeHtml(result.message) + "</div>" +
                    '<div class="text-green-600 dark:text-green-500 text-sm mt-1"><strong>Available models:</strong> ' +
                    result.available_models + "</div></div>";
            } else {
                const parsed = this._parseTestError(result.error);
                const statusCode = result.status_code;
                const tips = this._getTestTroubleshootingTips(statusCode, parsed.message);

                if (window.toast) toast.error("Connection failed: " + parsed.message, 5000);
                if (header) {
                    header.className =
                        "flex justify-between items-center p-5 border-b border-red-200 dark:border-red-700 bg-red-50 dark:bg-red-900/20";
                }
                title.textContent = "Connection Failed";
                title.className = "m-0 text-red-700 dark:text-red-400";

                let html =
                    '<div class="p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-700 rounded-lg">' +
                    '<div class="flex items-start gap-2 mb-2">' +
                    '<svg class="w-6 h-6 text-red-500 mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>' +
                    '<span class="text-red-800 dark:text-red-300 font-semibold text-base">' +
                    this._escapeHtml(parsed.message) + "</span></div>";
                if (tips.length > 0) {
                    html += '<div class="test-tips"><strong>Troubleshooting:</strong><ul>';
                    tips.forEach((tip) => { html += "<li>" + this._escapeHtml(tip) + "</li>"; });
                    html += "</ul></div>";
                }
                if (parsed.detail && parsed.detail !== parsed.message) {
                    html +=
                        '<details class="test-error-detail"><summary class="cursor-pointer text-gray-500">Show raw error</summary>' +
                        '<code class="block mt-1 p-2 bg-gray-100 dark:bg-gray-700 rounded text-xs">' +
                        this._escapeHtml(parsed.detail) + "</code></details>";
                }
                html += "</div>";
                testStatus.innerHTML = html;
            }
        } catch (error) {
            if (header) {
                header.className =
                    "flex justify-between items-center p-5 border-b border-red-200 dark:border-red-700 bg-red-50 dark:bg-red-900/20";
            }
            title.textContent = "Connection Failed";
            title.className = "m-0 text-red-700 dark:text-red-400";
            testStatus.innerHTML =
                '<div class="p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-700 rounded-lg">' +
                '<span class="text-red-800 dark:text-red-300 font-semibold">' +
                this._escapeHtml(error.message || "Network error") + "</span></div>";
        } finally {
            if (this._currentTestModelId === modelId && testBtn) {
                testBtn.disabled = false;
                testBtn.classList.remove("opacity-50", "cursor-not-allowed");
            }
        }
        footer.classList.remove("hidden");
    },

    _escapeHtml(str) {
        const div = document.createElement("div");
        div.textContent = str;
        // innerHTML leaves quotes alone; cards put these values inside attributes.
        return div.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    },

    /* ---- Provider credential setup ---- */

    _authBase() {
        return `/api/providers/${encodeURIComponent(this._authProviderId)}/auth/${this._authKind}`;
    },

    _credentialSourceLabel(source) {
        return ({
            api_key: "Anthropic API key",
            codex_cli_import: "Imported Codex CLI login",
            evonic_oauth: "Evonic-managed OAuth",
        })[source] || "Not configured";
    },

    async openCredentialSetup(providerId) {
        const provider = this.providers.find((item) => item.id === providerId);
        if (!provider) return;
        this.closeCredentialSetup();
        this._authProviderId = providerId;
        this._authKind = provider.api_format === "codex" ? "codex" : "claude";
        document.getElementById("provider-auth-title").textContent = `Set up ${provider.name}`;
        document.getElementById("provider-auth-content").innerHTML =
            '<div class="text-center py-10"><div class="spinner mx-auto" style="width:28px;height:28px;border-width:2px;"></div><p class="mt-3 text-sm text-gray-600 dark:text-gray-300">Checking available credentials…</p></div>';
        openModal("provider-auth-modal");
        try {
            const status = await apiGet(this._authBase() + "/status");
            if (status.api_format === "claude_cli") this._renderClaudeCliSetup(status);
            else if (status.api_format === "anthropic") this._renderAnthropicKeySetup(status);
            else this._renderCredentialChoices(status);
        } catch (error) {
            this._showAuthError(error.message || "Could not check credentials.");
        }
    },

    _statusHeader(connected, title, detail) {
        return `
            <div class="flex items-start justify-between gap-4 pb-5 border-b border-gray-200 dark:border-gray-700">
                <div>
                    <p class="font-semibold text-gray-900 dark:text-gray-100">${this._escapeHtml(title)}</p>
                    <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">${this._escapeHtml(detail)}</p>
                </div>
                <span class="inline-flex items-center gap-2 text-sm font-medium ${connected ? "text-emerald-700 dark:text-emerald-300" : "text-amber-700 dark:text-amber-300"}">
                    <span class="w-2 h-2 rounded-full ${connected ? "bg-emerald-500" : "bg-amber-500"}"></span>${connected ? "Ready" : "Setup needed"}
                </span>
            </div>`;
    },

    _renderClaudeCliSetup(status) {
        const cli = status.cli || {};
        const detail = status.connected
            ? `Claude Code CLI ${cli.version || ""} signed in (${cli.auth_method || "unknown"}).`
            : (cli.error || "Claude Code CLI is not signed in.");
        document.getElementById("provider-auth-content").innerHTML = `
            ${this._statusHeader(status.connected, status.connected ? "Connected" : "Sign in with the Claude Code CLI", detail)}
            <div class="py-5 space-y-3 text-sm text-gray-600 dark:text-gray-300">
                <p>Evonic runs the official <code>claude -p</code> command and never reads or stores your Claude login. Sign in once in a terminal:</p>
                <pre class="rounded-lg bg-gray-100 dark:bg-gray-900 px-3 py-2 text-gray-900 dark:text-gray-100 select-all">claude auth login</pre>
                <p>Claude's built-in tools are turned off for these calls; Evonic's own tools and safety checks stay in charge.</p>
            </div>
            <button type="button" onclick="settingsModels.authRecheckClaudeCli()" class="px-3 py-2 text-sm font-medium rounded-lg border border-gray-300 dark:border-gray-600 text-gray-800 dark:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-indigo-400">Check again</button>`;
    },

    async authRecheckClaudeCli() {
        try {
            this._renderClaudeCliSetup(await apiGet(this._authBase() + "/status?refresh=1"));
            await this.load();
        } catch (error) {
            this._showAuthError(error.message || "Could not check the Claude Code CLI.");
        }
    },

    _renderAnthropicKeySetup(status) {
        document.getElementById("provider-auth-content").innerHTML = `
            ${this._statusHeader(status.connected, status.connected ? "Connected" : "Add an API key", status.connected ? "Anthropic API key saved." : "Pay-per-token access through the Anthropic API.")}
            <div class="py-5"><label for="provider-auth-secret" class="font-medium text-gray-900 dark:text-gray-100">Anthropic API key</label>
                <p class="mt-1 mb-3 text-sm text-gray-600 dark:text-gray-300">For a Claude Pro/Max subscription, use the <strong>Claude Code (CLI)</strong> provider instead.</p>
                <div class="flex flex-col sm:flex-row gap-2"><input id="provider-auth-secret" type="password" autocomplete="new-password" data-1p-ignore="true" data-lpignore="true" placeholder="sk-ant-api…" class="flex-1 border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-indigo-400"><button type="button" onclick="settingsModels.authSaveSecret()" class="px-3 py-2 text-sm font-medium rounded-lg border border-gray-300 dark:border-gray-600 text-gray-800 dark:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-indigo-400">Save key</button></div></div>
            ${status.connected ? '<div class="pt-5 border-t border-gray-200 dark:border-gray-700"><button type="button" onclick="settingsModels.authDisconnect()" class="text-sm font-medium text-red-600 dark:text-red-400 hover:underline focus:outline-none focus:ring-2 focus:ring-red-400 rounded">Remove API key</button></div>' : ""}`;
    },

    _renderCredentialChoices(status) {
        const source = this._credentialSourceLabel(status.credential_source);
        const existingLabel = "Import Codex CLI login";
        const existingHelp = "Imports the current CLI credential snapshot. A separate login is safer if Codex CLI is used often.";
        document.getElementById("provider-auth-content").innerHTML = `
            <div class="flex items-start justify-between gap-4 pb-5 border-b border-gray-200 dark:border-gray-700">
                <div>
                    <p class="font-semibold text-gray-900 dark:text-gray-100">${status.connected ? "Connected" : "Choose how to connect"}</p>
                    <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">${status.connected ? this._escapeHtml(source) : "Evonic uses only the credential source you choose."}</p>
                </div>
                <span class="inline-flex items-center gap-2 text-sm font-medium ${status.connected ? "text-emerald-700 dark:text-emerald-300" : "text-amber-700 dark:text-amber-300"}">
                    <span class="w-2 h-2 rounded-full ${status.connected ? "bg-emerald-500" : "bg-amber-500"}"></span>${status.connected ? "Ready" : "Setup needed"}
                </span>
            </div>
            <div class="divide-y divide-gray-200 dark:divide-gray-700">
                <div class="py-5 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                    <div class="max-w-md"><p class="font-medium text-gray-900 dark:text-gray-100">${existingLabel}</p><p class="mt-1 text-sm text-gray-600 dark:text-gray-300">${existingHelp}</p></div>
                    <button type="button" onclick="settingsModels.authUseExisting()" ${status.existing_available ? "" : "disabled"} class="px-3 py-2 text-sm font-medium rounded-lg border border-gray-300 dark:border-gray-600 text-gray-800 dark:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-indigo-400 disabled:opacity-50 disabled:cursor-not-allowed">${status.existing_available ? "Use existing" : "Not detected"}</button>
                </div>
                <div class="py-5 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                    <div class="max-w-md"><p class="font-medium text-gray-900 dark:text-gray-100">Sign in with another account</p><p class="mt-1 text-sm text-gray-600 dark:text-gray-300">Use OpenAI's device-code flow without changing your Codex CLI login.</p></div>
                    <button type="button" onclick="settingsModels.authStartNew()" class="px-3 py-2 text-sm font-medium text-white bg-indigo-600 rounded-lg hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-400 focus:ring-offset-2">Start sign-in</button>
                </div>
            </div>
            ${status.connected ? '<div class="pt-5 border-t border-gray-200 dark:border-gray-700"><button type="button" onclick="settingsModels.authDisconnect()" class="text-sm font-medium text-red-600 dark:text-red-400 hover:underline focus:outline-none focus:ring-2 focus:ring-red-400 rounded">Disconnect this provider</button></div>' : ""}`;
    },

    async authUseExisting() {
        try {
            const result = await apiPost(this._authBase() + "/use-existing", {});
            if (result.error) return this._showAuthError(result.error);
            await this._finishAuth("Existing credentials connected.");
        } catch (error) {
            this._showAuthError(error.message || "Could not use existing credentials.");
        }
    },

    async authStartNew() {
        const providerId = this._authProviderId;
        try {
            if (this._authKind === "codex") {
                const result = await apiPost(this._authBase() + "/device", {});
                if (result.error) return this._showAuthError(result.error);
                document.getElementById("provider-auth-content").innerHTML = `
                    <div class="py-3 text-center">
                        <p class="text-sm text-gray-600 dark:text-gray-300">Open the OpenAI sign-in page and enter this one-time code.</p>
                        <div class="my-6"><code class="text-3xl font-semibold tracking-wider text-gray-900 dark:text-white select-all">${this._escapeHtml(result.user_code)}</code></div>
                        <a href="${result.verification_url}" target="_blank" rel="noopener" class="inline-flex px-4 py-2 text-sm font-medium text-white bg-indigo-600 rounded-lg hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-400">Open OpenAI sign-in</a>
                        <div class="mt-6 flex items-center justify-center gap-3 text-sm text-gray-600 dark:text-gray-300"><div class="spinner" style="width:20px;height:20px;border-width:2px;"></div><span id="provider-auth-progress">Waiting for approval…</span></div>
                    </div>`;
                window.open(result.verification_url, "_blank", "noopener");
                this._pollCodexDevice(providerId, Math.max(3, result.interval || 5));
            }
        } catch (error) {
            this._showAuthError(error.message || "Could not start sign-in.");
        }
    },

    _pollCodexDevice(providerId, interval) {
        clearTimeout(this._authPollTimer);
        this._authPollTimer = setTimeout(async () => {
            if (this._authProviderId !== providerId) return;
            try {
                const result = await apiPost(this._authBase() + "/device/poll", {});
                if (result.status === "complete") return this._finishAuth("OpenAI Codex connected.");
                if (["error", "expired"].includes(result.status)) return this._showAuthError(result.error || "Sign-in failed.");
            } catch (error) {
                const progress = document.getElementById("provider-auth-progress");
                if (progress) progress.textContent = "Connection interrupted; retrying…";
            }
            this._pollCodexDevice(providerId, interval);
        }, interval * 1000);
    },

    async authSaveSecret() {
        const secret = document.getElementById("provider-auth-secret")?.value.trim();
        if (!secret) return this._showAuthError("Enter an API key first.");
        try {
            const result = await apiPost(this._authBase() + "/secret", { secret });
            if (result.error) return this._showAuthError(result.error);
            await this._finishAuth("Anthropic API key saved.");
        } catch (error) {
            this._showAuthError(error.message || "Could not save the credential.");
        }
    },

    async authDisconnect() {
        const provider = this.providers.find((item) => item.id === this._authProviderId);
        if (!(await showConfirm({
            title: "Disconnect provider",
            message: `Remove Evonic's active credential for ${provider?.name || "this provider"}? External CLI credentials are not deleted.`,
            confirmText: "Disconnect",
        }))) return;
        try {
            const result = await apiPost(this._authBase() + "/disconnect", {});
            if (result.error) return this._showAuthError(result.error);
            await this._finishAuth("Provider disconnected.");
        } catch (error) {
            this._showAuthError(error.message || "Could not disconnect the provider.");
        }
    },

    _showAuthError(message) {
        let error = document.getElementById("provider-auth-error");
        if (!error) {
            error = document.createElement("div");
            error.id = "provider-auth-error";
            error.setAttribute("role", "alert");
            error.className = "mb-4 p-3 rounded-lg bg-red-50 dark:bg-red-950/40 text-sm text-red-800 dark:text-red-200";
            document.getElementById("provider-auth-content")?.prepend(error);
        }
        error.textContent = message;
    },

    async _finishAuth(message) {
        this.closeCredentialSetup();
        await this.reload();
        if (window.toast) toast.success(message, 3000);
    },

    closeCredentialSetup() {
        clearTimeout(this._authPollTimer);
        this._authPollTimer = null;
        closeModal("provider-auth-modal");
        this._authProviderId = null;
        this._authKind = null;
    },

    closeTestModal() {
        closeModal("connection-test-modal");
        if (this._currentTestModelId) {
            const testBtn = document.querySelector(
                `button[onclick*="testConnection('${this._currentTestModelId}')"]`,
            );
            if (testBtn) {
                testBtn.disabled = false;
                testBtn.classList.remove("opacity-50", "cursor-not-allowed");
            }
            this._currentTestModelId = null;
        }
    },
};
