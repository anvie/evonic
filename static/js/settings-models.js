/* Models pane: provider-grouped model management.
 * Extracted from settings.html — uses ModelsCache so the
 * General pane's model selects stay in sync via 'models:changed'. */

window.settingsModels = {
    models: [],
    providers: [],
    searchQuery: "",
    _currentTestModelId: null,
    _fetchProviderId: null,
    _authProviderId: null,
    _authKind: null,
    _authPollTimer: null,

    async init() {
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

        const filtered = q
            ? this.models.filter(
                  (m) =>
                      (m.name || "").toLowerCase().includes(q) ||
                      (m.provider || "").toLowerCase().includes(q) ||
                      (m.model_name || "").toLowerCase().includes(q),
              )
            : this.models;

        // Group models by provider, and ensure ALL providers appear (even with 0 models)
        const provMap = {};
        for (const p of this.providers) provMap[p.id] = p;
        const groups = {};
        for (const p of this.providers) {
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
            .map((pid) => {
                const prov = provMap[pid] || { id: pid, name: pid };
                const models = groups[pid];
                const typeBadge =
                    prov.type === "local"
                        ? '<span class="inline-block px-1.5 py-0.5 rounded text-[10px] font-medium bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300">local</span>'
                        : '<span class="inline-block px-1.5 py-0.5 rounded text-[10px] font-medium bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300">remote</span>';

                const modelCards = models.length > 0
                    ? models.map((model) => this._renderModelCard(model)).join("")
                    : `<div class="col-span-full text-center py-4 text-sm text-gray-400 dark:text-gray-500">No models yet — click <strong>Fetch Models</strong> to discover available models from this provider.</div>`;

                const hasCredentialSetup = ["codex", "anthropic"].includes(prov.api_format);
                const connected = Boolean(prov.credential_configured);

                let actionButtons;
                const addModelBtn = `<button class="px-2 py-1 text-xs font-medium text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-950/50 rounded hover:bg-emerald-100 dark:hover:bg-emerald-900/50 transition-colors" onclick="settingsModels.addModelForProvider('${pid}')" title="Add a custom model">+ Model</button>`;
                if (hasCredentialSetup) {
                    const status = connected
                        ? '<span class="inline-flex items-center gap-1.5 px-2 py-1 text-xs font-medium text-emerald-700 dark:text-emerald-300"><span class="w-2 h-2 rounded-full bg-emerald-500"></span>Ready</span>'
                        : '<span class="inline-flex items-center gap-1.5 px-2 py-1 text-xs font-medium text-amber-700 dark:text-amber-300"><span class="w-2 h-2 rounded-full bg-amber-500"></span>Setup needed</span>';
                    actionButtons = `${status}` +
                        `<button class="px-2 py-1 text-xs font-medium text-indigo-700 dark:text-indigo-300 bg-indigo-50 dark:bg-indigo-950/50 rounded hover:bg-indigo-100 dark:hover:bg-indigo-900/50 focus:outline-none focus:ring-2 focus:ring-indigo-400 transition-colors" onclick="settingsModels.openCredentialSetup('${pid}')">${connected ? "Manage login" : "Set up"}</button>` +
                        (connected ? `<button class="px-2 py-1 text-xs font-medium text-indigo-600 dark:text-indigo-400 bg-indigo-50 dark:bg-indigo-950/50 rounded hover:bg-indigo-100 dark:hover:bg-indigo-900/50 transition-colors" onclick="settingsModels.fetchModels('${pid}')" title="Discover models">Fetch Models</button>` : "") +
                        addModelBtn +
                        `<button class="px-2 py-1 text-xs font-medium text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-700 rounded hover:bg-gray-100 dark:hover:bg-gray-600 transition-colors" onclick="settingsModels.editProvider('${pid}')" title="Edit provider settings">Edit</button>` +
                        `<button class="px-2 py-1 text-xs font-medium text-red-500 dark:text-red-400 bg-red-50 dark:bg-red-950/50 rounded hover:bg-red-100 dark:hover:bg-red-900/50 transition-colors" onclick="settingsModels.deleteProvider('${pid}')" title="Delete provider">Del</button>`;
                } else {
                    actionButtons =
                        `<button class="px-2 py-1 text-xs font-medium text-indigo-600 dark:text-indigo-400 bg-indigo-50 dark:bg-indigo-950/50 rounded hover:bg-indigo-100 dark:hover:bg-indigo-900/50 transition-colors" onclick="settingsModels.fetchModels('${pid}')" title="Discover models from provider API">Fetch Models</button>` +
                        addModelBtn +
                        `<button class="px-2 py-1 text-xs font-medium text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-700 rounded hover:bg-gray-100 dark:hover:bg-gray-600 transition-colors" onclick="settingsModels.testProvider('${pid}')" title="Test provider connection">Test</button>` +
                        `<button class="px-2 py-1 text-xs font-medium text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-700 rounded hover:bg-gray-100 dark:hover:bg-gray-600 transition-colors" onclick="settingsModels.editProvider('${pid}')" title="Edit provider settings">Edit</button>` +
                        `<button class="px-2 py-1 text-xs font-medium text-red-500 dark:text-red-400 bg-red-50 dark:bg-red-950/50 rounded hover:bg-red-100 dark:hover:bg-red-900/50 transition-colors" onclick="settingsModels.deleteProvider('${pid}')" title="Delete provider">Del</button>`;
                }

                return `
                    <div class="provider-group">
                        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-2 px-1">
                            <div class="flex items-center gap-2">
                                <h3 class="text-sm font-semibold text-gray-700 dark:text-gray-300">${this._escapeHtml(prov.name)}</h3>
                                ${typeBadge}
                                <span class="text-xs text-gray-400">${models.length} model${models.length !== 1 ? "s" : ""}</span>
                            </div>
                            <div class="flex flex-wrap items-center gap-1.5">
                                ${actionButtons}
                            </div>
                        </div>
                        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                            ${modelCards}
                        </div>
                    </div>`;
            })
            .join('<hr class="my-6 border-gray-200 dark:border-gray-700">');
    },

    _renderModelCard(model) {
        const typeColors =
            model.type === "remote"
                ? "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300"
                : "bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300";
        const enabledColors = model.enabled
            ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300"
            : "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300";
        const defaultBorder = model.is_default
            ? "border-indigo-400 dark:border-indigo-500 ring-1 ring-indigo-200 dark:ring-indigo-800"
            : "border-gray-200 dark:border-gray-700";
        const shortcode = model.shortcode != null ? model.shortcode : "?";

        return `
        <div class="model-card bg-white dark:bg-gray-800 rounded-lg border ${defaultBorder} p-3 hover:shadow-sm transition-shadow flex flex-col gap-2">
            <div class="flex items-start justify-between gap-2">
                <div class="flex items-center gap-2 min-w-0">
                    <span class="inline-flex items-center justify-center w-6 h-6 rounded-full bg-gray-100 dark:bg-gray-700 text-[11px] font-bold text-gray-600 dark:text-gray-300 flex-shrink-0">${shortcode}</span>
                    <h4 class="font-semibold text-sm text-gray-900 dark:text-gray-100 truncate min-w-0">${model.name}</h4>
                </div>
                <div class="flex flex-row items-center gap-1.5 shrink-0">
                    ${!model.is_default ? `<button class="p-1 rounded border border-amber-400 dark:border-amber-500 text-amber-500 dark:text-amber-400 bg-transparent cursor-pointer hover:bg-amber-50 dark:hover:bg-amber-950/50 transition-colors" onclick="settingsModels.setDefault('${model.id}')" title="Set Default"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z"/></svg></button>` : ""}
                    <button class="p-1 rounded border border-gray-300 dark:border-gray-600 bg-transparent cursor-pointer text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors" onclick="settingsModels.testConnection('${model.id}')" title="Test"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg></button>
                    <button class="p-1 rounded border border-indigo-400 dark:border-indigo-500 text-indigo-500 dark:text-indigo-400 bg-transparent cursor-pointer hover:bg-indigo-50 dark:hover:bg-indigo-950/50 transition-colors" onclick="settingsModels.edit('${model.id}')" title="Edit"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg></button>
                    <button class="p-1 rounded border border-emerald-400 dark:border-emerald-500 text-emerald-500 dark:text-emerald-400 bg-transparent cursor-pointer hover:bg-emerald-50 dark:hover:bg-emerald-950/50 transition-colors" onclick="settingsModels.clone('${model.id}')" title="Clone"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" stroke-width="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" stroke-width="2" stroke-linecap="round"/></svg></button>
                </div>
            </div>

            <div class="flex flex-wrap items-center gap-1.5">
                ${model.is_default ? '<span class="inline-block bg-indigo-600 text-white px-1.5 py-0.5 rounded text-[10px] font-semibold leading-none">Default</span>' : ""}
                <span class="inline-block px-1.5 py-0.5 rounded text-[11px] font-medium ${typeColors}">${model.type}</span>
                <span class="inline-block px-1.5 py-0.5 rounded text-[11px] font-medium ${enabledColors}">${model.enabled ? "On" : "Off"}</span>
                ${model.thinking ? '<span class="inline-block px-1.5 py-0.5 rounded text-[11px] font-medium bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-300">Thinking</span>' : ""}
            </div>

            <div class="flex items-end justify-between gap-2">
                <div class="text-[11px] text-gray-500 dark:text-gray-400 truncate min-w-0">
                    ${model.model_name}
                </div>
                <button class="p-1 rounded border border-red-400 dark:border-red-500 text-red-400 dark:text-red-400 bg-transparent cursor-pointer hover:bg-red-50 dark:hover:bg-red-950/50 transition-colors shrink-0" onclick="settingsModels.remove('${model.id}')" title="Delete"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg></button>
            </div>
        </div>`;
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
        document.getElementById("provider-base-url").value = prov.base_url || "";
        document.getElementById("provider-api-key").value = "";
        document.getElementById("provider-api-format").value = prov.api_format || "openai";
        openModal("provider-modal");
    },

    async saveProvider(event) {
        event.preventDefault();
        const editId = document.getElementById("provider-edit-id").value;
        const data = {
            id: document.getElementById("provider-id").value,
            name: document.getElementById("provider-name").value,
            type: document.getElementById("provider-type").value,
            base_url: document.getElementById("provider-base-url").value,
            api_key: document.getElementById("provider-api-key").value,
            api_format: document.getElementById("provider-api-format").value,
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
        if (
            !(await showConfirm({
                title: "Delete Provider",
                message: "Delete this provider? Its models must be removed first.",
                confirmText: "Delete",
            }))
        )
            return;
        try {
            const result = await apiDelete("/api/providers/" + encodeURIComponent(providerId));
            if (result.success) {
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
        document.getElementById("model-type").value = prov.type || "remote";
        if (prov.base_url) document.getElementById("model-base-url").value = prov.base_url;
        if (prov.api_format) document.getElementById("model-api-format").value = prov.api_format;
        this.toggleFields();
        openModal("model-modal");
    },

    edit(modelId) {
        const model = this.models.find((m) => m.id === modelId);
        if (!model) return;

        document.getElementById("modal-title").textContent = "Edit Model";
        document.getElementById("model-id").value = model.id;
        document.getElementById("model-name").value = model.name || "";
        document.getElementById("model-type").value = model.type || "remote";
        this._populateProviderSelect();
        document.getElementById("model-provider").value = model.provider || "";
        document.getElementById("model-base-url").value = model.base_url || "";
        document.getElementById("model-api-key").value = model.api_key || "";
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
        document.getElementById("model-api-format").value = model.api_format || "openai";

        this.toggleFields();
        openModal("model-modal");
    },

    toggleFields() {
        const type = document.getElementById("model-type").value;
        const provider = document.getElementById("model-provider").value;
        const apiKeyGroup = document.getElementById("api-key-group");
        if (type === "local" && (provider === "ollama" || provider === "llama.cpp")) {
            apiKeyGroup.style.display = "none";
        } else {
            apiKeyGroup.style.display = "block";
        }
        const idHint = document.getElementById("model-id-hint");
        if (idHint) {
            idHint.style.display = type === "remote" ? "block" : "none";
        }
    },

    async save(event) {
        event.preventDefault();

        const modelId = document.getElementById("model-id").value;
        const modelData = {
            name: document.getElementById("model-name").value,
            type: document.getElementById("model-type").value,
            provider: document.getElementById("model-provider").value,
            base_url: document.getElementById("model-base-url").value,
            api_key: document.getElementById("model-api-key").value,
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
            api_format: document.getElementById("model-api-format").value,
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
        return div.innerHTML;
    },

    /* ---- Provider credential setup ---- */

    _authBase() {
        return `/api/providers/${encodeURIComponent(this._authProviderId)}/auth/${this._authKind}`;
    },

    _credentialSourceLabel(source) {
        return ({
            api_key: "Anthropic API key",
            setup_token: "Claude setup-token",
            claude_code: "Claude Code credential store",
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
            this._renderCredentialChoices(status);
        } catch (error) {
            this._showAuthError(error.message || "Could not check credentials.");
        }
    },

    _renderCredentialChoices(status) {
        const isCodex = this._authKind === "codex";
        const source = this._credentialSourceLabel(status.credential_source);
        const existingLabel = isCodex ? "Import Codex CLI login" : "Use Claude Code login";
        const existingHelp = isCodex
            ? "Imports the current CLI credential snapshot. A separate login is safer if Codex CLI is used often."
            : "Links the freshest credential from macOS Keychain or ~/.claude/.credentials.json.";
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
                    <div class="max-w-md"><p class="font-medium text-gray-900 dark:text-gray-100">Sign in with another account</p><p class="mt-1 text-sm text-gray-600 dark:text-gray-300">${isCodex ? "Use OpenAI's device-code flow without changing your Codex CLI login." : "Authorize a Claude Pro or Max account and paste the returned code."}</p></div>
                    <button type="button" onclick="settingsModels.authStartNew()" class="px-3 py-2 text-sm font-medium text-white bg-indigo-600 rounded-lg hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-400 focus:ring-offset-2">Start sign-in</button>
                </div>
                ${isCodex ? "" : `<div class="py-5"><label for="provider-auth-secret" class="font-medium text-gray-900 dark:text-gray-100">API key or setup-token</label><p class="mt-1 mb-3 text-sm text-gray-600 dark:text-gray-300">Use a pay-per-token API key, or paste a token created by <code>claude setup-token</code>.</p><div class="flex flex-col sm:flex-row gap-2"><input id="provider-auth-secret" type="password" autocomplete="new-password" data-1p-ignore="true" data-lpignore="true" placeholder="sk-ant-…" class="flex-1 border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-indigo-400"><button type="button" onclick="settingsModels.authSaveSecret()" class="px-3 py-2 text-sm font-medium rounded-lg border border-gray-300 dark:border-gray-600 text-gray-800 dark:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-indigo-400">Save credential</button></div></div>`}
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
            } else {
                const result = await apiPost(this._authBase() + "/oauth", {});
                if (result.error) return this._showAuthError(result.error);
                document.getElementById("provider-auth-content").innerHTML = `
                    <div class="py-2">
                        <p class="text-sm text-gray-600 dark:text-gray-300">Authorize Evonic in the Claude page. Copy the full code shown after approval, including the part after <code>#</code>.</p>
                        <a href="${result.auth_url}" target="_blank" rel="noopener" class="inline-flex mt-4 px-4 py-2 text-sm font-medium text-white bg-indigo-600 rounded-lg hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-400">Open Claude authorization</a>
                        <label for="provider-auth-code" class="block mt-6 mb-2 text-sm font-medium text-gray-900 dark:text-gray-100">Authorization code</label>
                        <textarea id="provider-auth-code" rows="3" placeholder="code#state" class="w-full border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-indigo-400"></textarea>
                        <button type="button" onclick="settingsModels.authCompleteClaude()" class="mt-3 px-4 py-2 text-sm font-medium text-white bg-indigo-600 rounded-lg hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-400">Complete sign-in</button>
                    </div>`;
                window.open(result.auth_url, "_blank", "noopener");
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

    async authCompleteClaude() {
        const code = document.getElementById("provider-auth-code")?.value.trim();
        if (!code) return this._showAuthError("Paste the authorization code first.");
        try {
            const result = await apiPost(this._authBase() + "/oauth/complete", { code });
            if (result.error) return this._showAuthError(result.error);
            await this._finishAuth("Claude connected.");
        } catch (error) {
            this._showAuthError(error.message || "Could not complete sign-in.");
        }
    },

    async authSaveSecret() {
        const secret = document.getElementById("provider-auth-secret")?.value.trim();
        if (!secret) return this._showAuthError("Enter an API key or setup-token first.");
        try {
            const result = await apiPost(this._authBase() + "/secret", { secret });
            if (result.error) return this._showAuthError(result.error);
            await this._finishAuth("Anthropic credential saved.");
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
