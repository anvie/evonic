/* Models pane: provider-grouped model management.
 * Extracted from settings.html — uses ModelsCache so the
 * General pane's model selects stay in sync via 'models:changed'. */

window.settingsModels = {
    models: [],
    providers: [],
    searchQuery: "",
    _currentTestModelId: null,
    _fetchProviderId: null,
    _codexStatus: null,

    async init() {
        await this.load();
        this._checkCodexStatus();
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

                const isCodex = prov.api_format === "codex" || prov.auth_type === "oauth";
                const codexConnected = this._codexStatus && this._codexStatus.connected && this._codexStatus.provider_id === pid;

                let actionButtons;
                const addModelBtn = `<button class="px-2 py-1 text-xs font-medium text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-950/50 rounded hover:bg-emerald-100 dark:hover:bg-emerald-900/50 transition-colors" onclick="settingsModels.addModelForProvider('${pid}')" title="Add a custom model">+ Model</button>`;
                if (isCodex) {
                    const statusDot = codexConnected
                        ? '<span class="inline-block w-2 h-2 rounded-full bg-green-500 mr-1" title="Connected"></span>'
                        : '<span class="inline-block w-2 h-2 rounded-full bg-gray-400 mr-1" title="Not connected"></span>';
                    const connectBtn = codexConnected
                        ? `<button class="px-2 py-1 text-xs font-medium text-red-500 dark:text-red-400 bg-red-50 dark:bg-red-950/50 rounded hover:bg-red-100 dark:hover:bg-red-900/50 transition-colors" onclick="settingsModels.codexDisconnect()" title="Disconnect OAuth">Disconnect</button>`
                        : `<button class="px-2 py-1 text-xs font-medium text-green-600 dark:text-green-400 bg-green-50 dark:bg-green-950/50 rounded hover:bg-green-100 dark:hover:bg-green-900/50 transition-colors" onclick="settingsModels.codexConnect('${pid}')" title="Connect via OAuth">Connect</button>`;
                    actionButtons = `${statusDot}${connectBtn}` +
                        (codexConnected ? `<button class="px-2 py-1 text-xs font-medium text-indigo-600 dark:text-indigo-400 bg-indigo-50 dark:bg-indigo-950/50 rounded hover:bg-indigo-100 dark:hover:bg-indigo-900/50 transition-colors" onclick="settingsModels.fetchModels('${pid}')" title="Discover models">Fetch Models</button>` : "") +
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
                        <div class="flex items-center justify-between mb-2 px-1">
                            <div class="flex items-center gap-2">
                                <h3 class="text-sm font-semibold text-gray-700 dark:text-gray-300">${this._escapeHtml(prov.name)}</h3>
                                ${typeBadge}
                                <span class="text-xs text-gray-400">${models.length} model${models.length !== 1 ? "s" : ""}</span>
                            </div>
                            <div class="flex items-center gap-1.5">
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
        document.getElementById("model-pdf-supported").checked =
            !!model.pdf_supported;
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
            pdf_supported: document.getElementById("model-pdf-supported").checked
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

    /* ---- Codex OAuth (PKCE flow) ---- */

    async _checkCodexStatus() {
        try {
            this._codexStatus = await apiGet("/api/provider/codex/status");
            this.render();
        } catch (e) {
            this._codexStatus = null;
        }
    },

    async codexConnect(providerId) {
        try {
            const result = await apiPost("/api/provider/codex/connect", {});
            if (result.error) {
                if (window.toast) toast.error(result.error, 5000);
                return;
            }
            window.open(result.auth_url, "_blank");
            this._showAuthWaitingModal();
        } catch (e) {
            if (window.toast) toast.error("Failed: " + e.message, 5000);
        }
    },

    _showAuthWaitingModal() {
        const testStatus = document.getElementById("connection-test-status");
        const footer = document.getElementById("connection-test-footer");
        const title = document.getElementById("connection-test-title");
        const header = document.getElementById("connection-test-header");

        if (header) header.className = "flex justify-between items-center p-5 border-b border-indigo-200 dark:border-indigo-700 bg-indigo-50 dark:bg-indigo-900/20";
        title.textContent = "Connect to OpenAI Codex";
        title.className = "m-0 text-indigo-700 dark:text-indigo-400";
        testStatus.innerHTML =
            '<div class="text-center py-4">' +
            '<p class="text-sm text-gray-600 dark:text-gray-400 mb-4">Complete the login in the OpenAI page that just opened.</p>' +
            '<p class="text-xs text-gray-400 mb-3">Waiting for authorization…</p>' +
            '<div class="spinner mx-auto" style="width:24px;height:24px;border-width:2px;"></div>' +
            '<p id="codex-poll-status" class="text-xs text-gray-400 mt-3"></p>' +
            '<hr class="my-4 border-gray-200 dark:border-gray-700">' +
            '<p class="text-xs text-gray-500 dark:text-gray-400 mb-2">Trouble auto-redirecting?</p>' +
            '<p class="text-xs text-gray-400 mb-2">Paste the full callback URL from your browser address bar below:</p>' +
            '<textarea id="codex-callback-url" rows="2" class="w-full text-xs p-2 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-200" placeholder="http://localhost:1455/auth/callback?code=...&state=..."></textarea>' +
            '<button onclick="settingsModels.pasteCallback()" class="mt-2 px-3 py-1 text-xs font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded transition-colors">Submit</button>' +
            '</div>';
        footer.classList.add("hidden");
        openModal("connection-test-modal");

        this._pollAuthCallback();
    },

    _pollAuthCallback() {
        const deadline = Date.now() + 300_000;

        const poll = setInterval(async () => {
            if (Date.now() > deadline) {
                clearInterval(poll);
                const el = document.getElementById("codex-poll-status");
                if (el) el.textContent = "Timed out. Close and try again.";
                return;
            }
            try {
                const result = await apiPost("/api/provider/codex/poll", {});
                if (result.status === "complete") {
                    clearInterval(poll);
                    closeModal("connection-test-modal");
                    await this._checkCodexStatus();
                    await this.reload();
                    if (window.toast) toast.success("Connected to Codex!", 3000);
                } else if (result.status === "error" || result.status === "expired") {
                    clearInterval(poll);
                    const el = document.getElementById("codex-poll-status");
                    if (el) el.textContent = result.error || "Authorization failed.";
                }
            } catch (e) { /* ignore, retry next tick */ }
        }, 3000);

        this._codexPollTimer = poll;
    },

    async codexDisconnect() {
        if (!(await showConfirm({
            title: "Disconnect Codex",
            message: "This will remove the stored OAuth tokens. You’ll need to reconnect to use Codex models.",
            confirmText: "Disconnect",
        }))) return;

        try {
            const result = await apiPost("/api/provider/codex/disconnect", {});
            if (result.success) {
                this._codexStatus = null;
                await this._checkCodexStatus();
                this.render();
                if (window.toast) toast.show("Codex disconnected", "success");
            } else {
                if (window.toast) toast.error(result.error || "Failed", 5000);
            }
        } catch (e) {
            if (window.toast) toast.error("Failed: " + e.message, 5000);
        }
    },

    async pasteCallback() {
        const textarea = document.getElementById("codex-callback-url");
        const statusEl = document.getElementById("codex-poll-status");
        const url = textarea ? textarea.value.trim() : "";

        if (!url) {
            if (window.toast) toast.error("Please paste the callback URL first.", 3000);
            return;
        }

        if (!url.includes("code=") || !url.includes("state=")) {
            if (window.toast) toast.error("URL must contain 'code' and 'state' parameters. Paste the full URL.", 5000);
            return;
        }

        if (statusEl) statusEl.textContent = "Processing callback URL…";

        try {
            const result = await apiPost("/api/provider/codex/callback", { url });
            if (result.success) {
                if (statusEl) statusEl.textContent = "Connected!";
                closeModal("connection-test-modal");
                await this._checkCodexStatus();
                await this.reload();
                if (window.toast) toast.success("Connected to Codex!", 3000);
            } else {
                if (statusEl) statusEl.textContent = result.error || "Failed to process callback.";
                if (window.toast) toast.error(result.error || "Failed", 5000);
            }
        } catch (e) {
            if (statusEl) statusEl.textContent = "Error: " + e.message;
            if (window.toast) toast.error("Failed: " + e.message, 5000);
        }
    },

    closeTestModal() {
        closeModal("connection-test-modal");
        if (this._codexPollTimer) {
            clearInterval(this._codexPollTimer);
            this._codexPollTimer = null;
        }
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
