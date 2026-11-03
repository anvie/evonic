/* Decision Profiles admin pane. Keeps Playground selection in URL state only; it never invokes a model. */
window.decisionModels = {
    profiles: [],
    models: [],

    async init() {
        await this.load();
        this.renderPlayground();
        this.bindPlayground();
        this.bindProfiles();
        this.bindActivity();
        this.loadActivity();
        this.loadRecordInput();
    },

    async load() {
        const status = document.getElementById("decision-profiles-status");
        try {
            const [profilesData, models] = await Promise.all([
                apiGet("/api/admin/decision-profiles"),
                ModelsCache.get(),
            ]);
            this.profiles = profilesData.profiles || [];
            this.models = models || [];
            this.render();
            this.populateActivityOptions();
        } catch (error) {
            status.textContent = "Could not load Decision Profiles. Refresh and try again.";
            status.className = "rounded border border-red-300 bg-red-50 dark:bg-red-950/30 dark:border-red-800 p-4 text-sm text-red-800 dark:text-red-200";
        }
    },

    compatibleModels() {
        return this.models.filter((model) => model.enabled && this._supportsDecision(model));
    },

    _supportsDecision(model) {
        return model.type === "decision";
    },

    PROFILE_PAGE_SIZE: 10,
    profileQuery: "",
    profilePage: 0,

    filteredProfiles() {
        const q = this.profileQuery.trim().toLowerCase();
        if (!q) return this.profiles;
        return this.profiles.filter((p) => [p.display_name, p.profile_key, p.description, p.primary_model_id, p.fallback_model_id]
            .some((value) => String(value || "").toLowerCase().includes(q)));
    },

    bindProfiles() {
        const search = document.getElementById("decision-profiles-search");
        if (!search || search.dataset.bound) return; search.dataset.bound = "1";
        search.addEventListener("input", () => { this.profileQuery = search.value; this.profilePage = 0; this.render(); });
    },

    profilesPage(delta) { this.profilePage += delta; this.render(); },

    render() {
        const status = document.getElementById("decision-profiles-status");
        const list = document.getElementById("decision-profiles-list");
        const pager = document.getElementById("decision-profiles-pager");
        if (!this.profiles.length) {
            status.textContent = "No Decision Profiles yet. Create one to configure a reusable structured decision.";
            status.className = "rounded border border-dashed border-gray-300 dark:border-gray-600 p-5 text-sm text-gray-600 dark:text-gray-300";
            list.innerHTML = ""; pager.classList.add("hidden");
            return;
        }
        const matches = this.filteredProfiles();
        const pages = Math.max(1, Math.ceil(matches.length / this.PROFILE_PAGE_SIZE));
        this.profilePage = Math.max(0, Math.min(this.profilePage, pages - 1));
        const start = this.profilePage * this.PROFILE_PAGE_SIZE, shown = matches.slice(start, start + this.PROFILE_PAGE_SIZE);
        if (!matches.length) {
            status.textContent = "No profiles match your search.";
            status.className = "rounded border border-dashed border-gray-300 dark:border-gray-600 p-5 text-sm text-gray-600 dark:text-gray-300";
        } else { status.textContent = ""; status.className = "hidden"; }
        list.innerHTML = shown.map((profile) => this.card(profile)).join("");
        pager.classList.toggle("hidden", pages <= 1);
        document.getElementById("decision-profiles-range").textContent = matches.length ? `${start + 1}–${start + shown.length} of ${matches.length}` : "";
        document.getElementById("decision-profiles-prev").disabled = this.profilePage === 0;
        document.getElementById("decision-profiles-next").disabled = this.profilePage >= pages - 1;
    },

    card(profile) {
        const state = profile.enabled ? "Enabled" : "Disabled";
        const stateClass = profile.enabled ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300" : "bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-300";
        const key = this.escape(profile.profile_key);
        return `<article class="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2 space-y-1.5 cursor-pointer hover:shadow-md hover:border-indigo-300 dark:hover:border-indigo-700 transition" onclick="decisionModels.openPlayground({profile: '${key}'})">
          <div class="flex items-center justify-between gap-2"><div class="min-w-0"><h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">${this.escape(profile.display_name)}</h3><p class="text-xs text-gray-500 dark:text-gray-400 font-mono truncate">${key}</p></div><div class="flex items-center gap-2 flex-shrink-0" onclick="event.stopPropagation()"><span class="rounded px-1.5 py-0.5 text-xs font-medium ${stateClass}">${this.escape(profile.mode)}</span>${mkToggle({ checked: !!profile.enabled, onChange: `decisionModels.toggle('${key}')`, extraClass: 'flex-shrink-0' })}<button type="button" onclick="decisionModels.edit('${key}')" class="text-gray-400 hover:text-indigo-600 p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 transition" title="Edit"><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg></button>${!this.isBuiltin(profile.profile_key) ? `<button type="button" onclick="decisionModels.remove('${key}')" class="text-gray-400 hover:text-red-600 p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 transition" title="Delete"><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg></button>` : ""}</div></div>
          <p class="text-xs text-gray-600 dark:text-gray-300 line-clamp-2">${this.escape(profile.description || "No description provided.")}</p>
        </article>`;
    },

    isBuiltin(key) { return ["safety.command", "agent.task_complexity", "agent.path_boundary", "agent.goal_completion"].includes(key); },
    escape(value) { const el = document.createElement("div"); el.textContent = value == null ? "" : String(value); return el.innerHTML; },

    // Registry ids are frozen at creation time: `generate_model_id` runs once and
    // `update_model` never recomputes it, so a renamed model keeps its original
    // `provider/model_name` id. Label with the live provider and upstream model
    // name instead - that is the part of the row the endpoint actually receives.
    modelLabel(model) {
        const name = model.name || model.model_name || model.id || "";
        const upstream = [model.provider, model.model_name].filter(Boolean).join(" · ");
        return upstream ? `${name} (${upstream})` : String(name);
    },

    modelOptions(selected, includeEmpty) {
        const compatible = this.compatibleModels();
        const options = compatible.map((model) => `<option value="${this.escape(model.id)}" ${model.id === selected ? "selected" : ""}>${this.escape(this.modelLabel(model))}</option>`);
        return (includeEmpty ? '<option value="">No fallback</option>' : '<option value="" disabled selected>Select a compatible model</option>') + options.join("");
    },

    openCreate() {
        document.getElementById("decision-profile-form").reset();
        document.getElementById("decision-profile-modal-title").textContent = "New Decision Profile";
        document.getElementById("decision-profile-revision").value = "";
        document.getElementById("decision-profile-key").disabled = false;
        document.getElementById("decision-profile-primary").innerHTML = this.modelOptions(null, false);
        document.getElementById("decision-profile-fallback").innerHTML = this.modelOptions(null, true);
        document.getElementById("decision-profile-schema").value = JSON.stringify({version: "decision-schema/v1", fields: [{key: "approved", kind: "boolean"}]}, null, 2);
        this.clearError();
        openModal("decision-profile-modal");
    },

    edit(key) {
        const profile = this.profiles.find((item) => item.profile_key === key);
        if (!profile) return;
        document.getElementById("decision-profile-modal-title").textContent = "Edit Decision Profile";
        document.getElementById("decision-profile-revision").value = profile.revision;
        const fields = {"decision-profile-key":"profile_key", "decision-profile-name":"display_name", "decision-profile-description":"description", "decision-profile-mode":"mode", "decision-profile-policy":"policy_version"};
        Object.entries(fields).forEach(([id, field]) => document.getElementById(id).value = profile[field] || "");
        document.getElementById("decision-profile-key").disabled = true;
        document.getElementById("decision-profile-enabled").checked = profile.enabled;
        document.getElementById("decision-profile-primary").innerHTML = this.modelOptions(profile.primary_model_id, false);
        document.getElementById("decision-profile-fallback").innerHTML = this.modelOptions(profile.fallback_model_id, true);
        document.getElementById("decision-profile-timeout").value = profile.timeout_ms;
        document.getElementById("decision-profile-retries").value = profile.retry_limit;
        document.getElementById("decision-profile-confidence").value = profile.minimum_confidence;
        document.getElementById("decision-profile-schema").value = JSON.stringify(profile.schema, null, 2);
        this.clearError();
        openModal("decision-profile-modal");
    },

    payload() {
        return {profile_key: document.getElementById("decision-profile-key").value.trim(), display_name: document.getElementById("decision-profile-name").value.trim(), description: document.getElementById("decision-profile-description").value.trim(), enabled: document.getElementById("decision-profile-enabled").checked, mode: document.getElementById("decision-profile-mode").value, schema_version: "decision-schema/v1", policy_version: document.getElementById("decision-profile-policy").value.trim(), schema: JSON.parse(document.getElementById("decision-profile-schema").value), primary_model_id: document.getElementById("decision-profile-primary").value, fallback_model_id: document.getElementById("decision-profile-fallback").value || null, timeout_ms: Number(document.getElementById("decision-profile-timeout").value), retry_limit: Number(document.getElementById("decision-profile-retries").value), minimum_confidence: Number(document.getElementById("decision-profile-confidence").value)};
    },

    async save(event) {
        event.preventDefault();
        let data;
        try { data = this.payload(); } catch (error) { this.showError("schema: must be valid JSON."); return; }
        const revision = document.getElementById("decision-profile-revision").value;
        try {
            const response = revision ? await apiPut("/api/admin/decision-profiles/" + encodeURIComponent(data.profile_key), {...data, revision: Number(revision)}) : await apiPost("/api/admin/decision-profiles", data);
            if (!response.success) {
                const message = (response.error && response.error.path ? response.error.path + ": " : "") + ((response.error && response.error.message) || "Could not save profile.");
                if (window.toast) toast.show(message, "error");
                return;
            }
            closeModal("decision-profile-modal"); await this.load(); if (window.toast) toast.show("Decision Profile saved", "success");
        } catch (error) { if (window.toast) toast.show("Could not save profile. Check the values and try again.", "error"); }
    },

    showError(message) { const el = document.getElementById("decision-profile-error"); el.textContent = message; el.classList.remove("hidden"); },
    clearError() { document.getElementById("decision-profile-error").classList.add("hidden"); },

    async toggle(key) { const profile = this.profiles.find((item) => item.profile_key === key); if (!profile) return; const data = {...profile, enabled: !profile.enabled, mode: !profile.enabled && profile.mode === "off" ? "shadow" : profile.mode, revision: profile.revision}; const response = await apiPut("/api/admin/decision-profiles/" + encodeURIComponent(key), data); if (!response.success) { if (window.toast) toast.show((response.error && response.error.message) || "Could not update profile", "error"); return; } await this.load(); },
    async remove(key) { if (!(await showConfirm({title: "Delete Decision Profile", message: "Delete this application profile? This cannot be undone.", confirmText: "Delete"}))) return; const response = await apiDelete("/api/admin/decision-profiles/" + encodeURIComponent(key)); if (!response.success) { if (window.toast) toast.show((response.error && response.error.message) || "Could not delete profile", "error"); return; } await this.load(); },
    openPlayground(context) { const params = new URLSearchParams(context); history.replaceState(null, "", "#decision-playground?" + params.toString()); SettingsNav.activate("decision-playground"); this.renderPlayground(context); },
    playgroundDefaultSchema() { return {version: "decision-schema/v1", fields: [{key: "priority", kind: "choice", description: "Operational priority", choices: ["HIGH", "LOW"]}, {key: "requires_review", kind: "boolean", description: "Whether a human should review this incident"}]}; },
    playgroundSchema() { return JSON.parse(document.getElementById("playground-schema").value); },
    playgroundSetSchema(schema) { document.getElementById("playground-schema").value = JSON.stringify(schema, null, 2); this.renderSchemaBuilder(schema); this.playgroundStateSave(); },
    // The selection survives a refresh: URL query (deep links) wins, then the last saved state.
    playgroundStateLoad() { try { return JSON.parse(localStorage.getItem("evonic.decisionPlayground") || "{}") || {}; } catch (_) { return {}; } },
    playgroundStateSave() {
        const mode = document.getElementById("playground-mode"); if (!mode || !this._playgroundReady) return;
        const state = this.playgroundStateLoad(); state.mode = mode.value;
        state.profile = document.getElementById("playground-profile").value; state.model = document.getElementById("playground-model").value;
        state.input = document.getElementById("playground-input").value;
        state.inputFor = state.profile;   // which profile this input text belongs to
        if (mode.value === "direct") { try { state.schema = this.playgroundSchema(); } catch (_) {} }
        try { localStorage.setItem("evonic.decisionPlayground", JSON.stringify(state)); } catch (_) {}
    },
    renderPlayground(context) {
        const box = document.getElementById("decision-playground-context"); if (!box) return;
        const query = context || Object.fromEntries(new URLSearchParams(location.hash.split("?", 2)[1] || ""));
        const profileSelect = document.getElementById("playground-profile"), modelSelect = document.getElementById("playground-model");
        if (!profileSelect || !modelSelect) return;
        const saved = this.playgroundStateLoad(), deepLinked = !!(query.profile || query.model);
        const has = (select, value) => value && Array.from(select.options).some(o => o.value === value);
        profileSelect.innerHTML = this.profiles.map(p => `<option value="${this.escape(p.profile_key)}">${this.escape(p.display_name)} (${this.escape(p.profile_key)})</option>`).join("");
        modelSelect.innerHTML = this.compatibleModels().map(m => `<option value="${this.escape(m.id)}">${this.escape(this.modelLabel(m))}</option>`).join("");
        const pick = deepLinked ? query : saved;
        let mode = query.model ? "direct" : query.profile ? "profile" : (saved.mode === "direct" ? "direct" : "profile");
        if (has(profileSelect, pick.profile)) profileSelect.value = pick.profile;
        if (has(modelSelect, pick.model)) modelSelect.value = pick.model;
        // Keep the user's own edits for the profile they were made for; otherwise the profile's sample is used (see playgroundProfileChanged).
        const keepInput = !deepLinked && typeof saved.input === "string" && saved.input && (!saved.inputFor || saved.inputFor === profileSelect.value);
        if (keepInput) document.getElementById("playground-input").value = saved.input;
        this._playgroundKeepInput = keepInput;
        if (!document.getElementById("playground-schema").value) {
            this.playgroundSetSchema(mode === "direct" && saved.schema && Array.isArray(saved.schema.fields) ? saved.schema : this.playgroundDefaultSchema());
        }
        this.setPlaygroundMode(mode);
        this._playgroundReady = true;
        if (deepLinked && location.hash.startsWith("#decision-playground?")) history.replaceState(null, "", "#decision-playground");  // consumed; later picks must win on refresh
        this.playgroundStateSave();
        box.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg><span>Inference only: no shell, tool, webhook or configuration change can run from here. Inputs and provider responses are not retained.</span>`;
    },
    bindPlayground() {
        const mode = document.getElementById("playground-mode"), schema = document.getElementById("playground-schema"), profile = document.getElementById("playground-profile");
        if (!mode || mode.dataset.bound) return; mode.dataset.bound = "1";
        document.querySelectorAll(".dp-seg [data-mode]").forEach(btn => btn.addEventListener("click", () => { this.setPlaygroundMode(btn.dataset.mode); this.playgroundStateSave(); }));
        profile.addEventListener("change", () => { this.playgroundProfileChanged(); this.playgroundStateSave(); });
        document.getElementById("playground-model").addEventListener("change", () => this.playgroundStateSave());
        document.getElementById("playground-input").addEventListener("input", () => this.playgroundStateSave());
        document.getElementById("playground-input").addEventListener("keydown", e => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); this.playgroundRun(); } });
        schema.addEventListener("input", () => { try { this.renderSchemaBuilder(this.playgroundSchema()); this.playgroundError(""); this.playgroundStateSave(); } catch (_) { this.playgroundError("schema: must be valid JSON before it can be run."); } });
    },
    setPlaygroundMode(mode) {
        const direct = mode === "direct";
        document.getElementById("playground-mode").value = direct ? "direct" : "profile";
        document.querySelectorAll(".dp-seg [data-mode]").forEach(btn => btn.setAttribute("aria-pressed", String(btn.dataset.mode === (direct ? "direct" : "profile"))));
        document.getElementById("playground-profile-wrap").classList.toggle("hidden", direct); document.getElementById("playground-model-wrap").classList.toggle("hidden", !direct);
        if (!direct) this.playgroundProfileChanged();
        else { const saved = this.playgroundStateLoad(); if (saved.schema && Array.isArray(saved.schema.fields)) this.playgroundSetSchema(saved.schema); else { try { this.renderSchemaBuilder(this.playgroundSchema()); } catch (_) {} } }
    },
    playgroundModeChanged() { this.setPlaygroundMode(document.getElementById("playground-mode").value); },
    playgroundProfileChanged() {
        const profile = this.profiles.find(p => p.profile_key === document.getElementById("playground-profile").value);
        const meta = document.getElementById("playground-profile-meta");
        if (meta) meta.textContent = profile ? [profile.description, profile.primary_model_id ? "Model: " + profile.primary_model_id : ""].filter(Boolean).join(" · ") : "";
        if (profile) this.playgroundSetSchema(profile.schema);
        // A profile that ships a sample input prefills the (still editable) test input with it.
        const keep = this._playgroundKeepInput; this._playgroundKeepInput = false;
        if (profile && profile.sample_input && !keep) this.playgroundUseSample();
        const sampleBtn = document.getElementById("playground-use-sample"); if (sampleBtn) sampleBtn.classList.toggle("hidden", !(profile && profile.sample_input));
    },
    playgroundUseSample() {
        const profile = this.profiles.find(p => p.profile_key === document.getElementById("playground-profile").value);
        if (!profile || !profile.sample_input) return;
        document.getElementById("playground-input").value = JSON.stringify(profile.sample_input, null, 2); this.playgroundStateSave();
    },
    renderSchemaBuilder(schema) {
        const root = document.getElementById("playground-schema-builder"); if (!root || !schema || !Array.isArray(schema.fields)) return;
        const readonly = document.getElementById("playground-mode").value !== "direct", lock = readonly ? "disabled" : "";
        document.getElementById("playground-schema-actions").classList.toggle("hidden", readonly);
        document.getElementById("playground-schema-mode").textContent = readonly ? "Read-only · defined by the saved profile" : "Editable · applies to this test only";
        document.getElementById("playground-schema-json").classList.toggle("hidden", readonly);
        if (readonly) {
            const kinds = {choice: "Choice", boolean: "Yes / No", score: "Score"};
            root.innerHTML = schema.fields.map(field => {
                const detail = field.kind === "choice" ? `<div class="dp-opts">${(field.choices || []).map(c => `<span class="dp-opt">${this.escape(c)}</span>`).join("")}</div>`
                    : field.kind === "score" ? `<div class="dp-opts"><span class="dp-opt">${this.escape(field.minimum == null ? 0 : field.minimum)} → ${this.escape(field.maximum == null ? 1 : field.maximum)}</span></div>`
                    : `<div class="dp-opts"><span class="dp-opt">true</span><span class="dp-opt">false</span></div>`;
                return `<div class="dp-ro"><div class="dp-ro-top"><code class="dp-ro-key">${this.escape(field.key || "")}</code><span class="dp-chip">${this.escape(kinds[field.kind] || field.kind)}</span>${field.nullable ? '<span class="dp-chip">can be null</span>' : ""}</div>${field.description ? `<p class="dp-ro-desc">${this.escape(field.description)}</p>` : ""}${detail}</div>`;
            }).join("");
            return;
        }
        const opt = (field, value, label) => `<option value="${value}" ${field.kind === value ? "selected" : ""}>${label}</option>`;
        root.innerHTML = schema.fields.map((field, index) => `<div class="dp-field">
            <div class="dp-c3"><label class="dp-label">Key</label><input data-field="key" data-index="${index}" value="${this.escape(field.key || "")}" class="dp-input" ${lock}></div>
            <div class="dp-c3"><label class="dp-label">Type</label><select data-field="kind" data-index="${index}" class="dp-select" ${lock}>${opt(field, "choice", "Choice")}${opt(field, "boolean", "Boolean / null")}${opt(field, "score", "Score")}</select></div>
            <div class="dp-c6"><label class="dp-label">Description</label><input data-field="description" data-index="${index}" value="${this.escape(field.description || "")}" class="dp-input" ${lock}></div>
            <div class="dp-c6 ${field.kind === "choice" ? "" : "hidden"}"><label class="dp-label">Choices (comma-separated)</label><input data-field="choices" data-index="${index}" value="${this.escape((field.choices || []).join(", "))}" class="dp-input" ${lock}></div>
            <div class="dp-c6 ${field.kind === "score" ? "" : "hidden"}"><label class="dp-label">Range</label><div style="display:flex;gap:.5rem"><input type="number" step="any" aria-label="Minimum score" data-field="minimum" data-index="${index}" value="${field.minimum == null ? 0 : field.minimum}" class="dp-input" ${lock}><input type="number" step="any" aria-label="Maximum score" data-field="maximum" data-index="${index}" value="${field.maximum == null ? 1 : field.maximum}" class="dp-input" ${lock}></div></div>
            <div class="dp-c6 dp-field-foot"><label><input type="checkbox" data-field="nullable" data-index="${index}" ${field.nullable ? "checked" : ""} ${lock}> Allow null</label>${readonly ? "" : `<button type="button" class="dp-link" data-remove="${index}">Remove</button>`}</div></div>`).join("");
        root.querySelectorAll("input, select").forEach(el => el.addEventListener("change", event => this.playgroundFieldChanged(event))); root.querySelectorAll("[data-remove]").forEach(el => el.addEventListener("click", () => { const s = this.playgroundSchema(); s.fields.splice(Number(el.dataset.remove), 1); this.playgroundSetSchema(s); }));
    },
    playgroundFieldChanged(event) { const el = event.target, schema = this.playgroundSchema(), field = schema.fields[Number(el.dataset.index)], name = el.dataset.field; if (!field || !name) return; if (name === "nullable") field[name] = el.checked; else if (name === "choices") field.choices = el.value.split(",").map(v => v.trim()).filter(Boolean); else if (name === "minimum" || name === "maximum") field[name] = Number(el.value); else field[name] = el.value; if (name === "kind") { delete field.choices; delete field.minimum; delete field.maximum; if (field.kind === "choice") field.choices = ["YES", "NO"]; if (field.kind === "score") { field.minimum = 0; field.maximum = 1; } } this.playgroundSetSchema(schema); },
    playgroundAddField() { const schema = this.playgroundSchema(); schema.fields.push({key: `field_${schema.fields.length + 1}`, kind: "boolean", description: ""}); this.playgroundSetSchema(schema); },
    playgroundPreset() { this.playgroundSetSchema(this.playgroundDefaultSchema()); document.getElementById("playground-input").value = JSON.stringify({incident: "Payment-service outage affecting checkout", impact: "Customers cannot complete purchases"}, null, 2); },
    playgroundReset() { this.playgroundSetSchema(this.playgroundDefaultSchema()); document.getElementById("playground-input").value = "{}"; document.getElementById("playground-result").innerHTML = `<div class="dp-empty">No test has run yet.<br>Results appear here. Inputs and raw provider responses are not retained.</div>`; this.playgroundError(""); },
    playgroundError(message) { const el = document.getElementById("playground-error"); if (!message) { el.textContent = ""; el.classList.add("hidden"); return; } el.textContent = message; el.classList.remove("hidden"); },
    async playgroundRun() { let schema, input; try { schema = this.playgroundSchema(); input = JSON.parse(document.getElementById("playground-input").value); if (!input || Array.isArray(input) || typeof input !== "object") throw new Error("input_context must be a JSON object"); } catch (error) { this.playgroundError(error.message || "Schema and input must be valid JSON objects."); return; } const direct = document.getElementById("playground-mode").value === "direct"; const payload = {mode: direct ? "direct" : "profile", input_context: input}; if (direct) Object.assign(payload, {model_id: document.getElementById("playground-model").value, schema}); else payload.profile_key = document.getElementById("playground-profile").value; const button = document.getElementById("playground-run"); button.disabled = true; button.textContent = "Running…"; this.playgroundError(""); try { const response = await apiPost("/api/admin/decision-playground/run", payload); if (!response.success) { this.playgroundError((response.error && ((response.error.path ? response.error.path + ": " : "") + response.error.message)) || "Test could not run."); return; } this.renderPlaygroundResult(response.result); } catch (_) { this.playgroundError("Test could not run. Refresh and try again."); } finally { button.disabled = false; button.textContent = "Run safe test"; } },
    renderPlaygroundResult(result) {
        const root = document.getElementById("playground-result"), error = result.error;
        this.lastPlaygroundResult = result;
        if (error) { root.innerHTML = `<div class="dp-banner bad"><strong>${this.escape(error.code)}</strong><span>${this.escape(error.message)}${error.retryable ? " You can try again." : ""}</span></div>`; return; }
        const pct = n => Math.round(n * 1000) / 10 + "%";
        const fields = (result.fields || []).map(field => {
            const cands = (field.candidates || []).slice().sort((x, y) => y.probability - x.probability);
            const conf = field.confidence == null ? "" : `<div class="dp-conf"><span>Confidence</span><div class="dp-bar"><i style="width:${Math.max(0, Math.min(1, field.confidence)) * 100}%"></i></div><b>${this.escape(Math.round(field.confidence * 100) + "%")}</b></div>`;
            const bars = cands.length ? `<div class="dp-cands">${cands.map((c, i) => `<div class="dp-cand ${i === 0 ? "top" : ""}"><b title="${this.escape(String(c.value))}">${this.escape(String(c.value))}</b><div class="dp-bar"><i style="width:${Math.max(0, Math.min(1, c.probability)) * 100}%"></i></div><span>${this.escape(pct(c.probability))}</span></div>`).join("")}</div>` : "";
            const logits = field.provider_logits && field.provider_logits.length ? "" : `<p class="dp-meta">Provider logits unavailable (not supplied by adapter).</p>`;
            return `<div class="dp-out"><div class="dp-out-top"><span class="dp-out-key">${this.escape(field.key)}</span><span class="dp-val">${this.escape(JSON.stringify(field.value))}</span></div>${conf}${bars}${logits}</div>`;
        }).join("");
        const chips = [result.provider && result.provider.model_id, result.timing && result.timing.total_ms != null ? result.timing.total_ms + " ms" : "", result.attempts ? result.attempts + (result.attempts === 1 ? " attempt" : " attempts") : "", result.fallback && result.fallback.used ? "fallback used" : ""].filter(Boolean).map(c => `<span class="dp-chip">${this.escape(c)}</span>`).join("");
        const details = {profile: result.profile, provider: result.provider, timing: result.timing, attempts: result.attempts, fallback: result.fallback, warnings: result.warnings, candidate_scores: result.display && result.display.candidate_scores, provider_logits: result.display && result.display.provider_logits};
        root.innerHTML = `<div class="dp-banner ok">✓ Decision completed</div><div class="dp-chips">${chips}</div>${fields}<details class="dp-details"><summary>Execution details</summary><pre>${this.escape(JSON.stringify(details, null, 2))}</pre></details><details class="dp-details"><summary>Normalized JSON</summary><pre>${this.escape(JSON.stringify(result, null, 2))}</pre></details>`;
    },
    async playgroundCopy() { if (!this.lastPlaygroundResult) return; try { await navigator.clipboard.writeText(JSON.stringify(this.lastPlaygroundResult, null, 2)); if (window.toast) toast.show("Normalized result copied", "success"); } catch (_) { this.playgroundError("Could not copy the result."); } },

    bindActivity() {
        const form = document.getElementById("decision-activity-filters");
        if (!form || form.dataset.bound) return;
        form.dataset.bound = "1";
        form.addEventListener("submit", (event) => { event.preventDefault(); this.activityCursor = null; this.loadActivity(); });
    },
    activityFilters() {
        const form = document.getElementById("decision-activity-filters");
        const values = new FormData(form), params = new URLSearchParams({limit: "25"});
        for (const [key, value] of values.entries()) {
            if (!value) continue;
            params.set(key, (key === "from" || key === "to") ? String(Date.parse(value) / 1000) : value);
        }
        return params;
    },
    populateActivityOptions() {
        const profile = document.getElementById("decision-activity-profile"), model = document.getElementById("decision-activity-model");
        if (profile) profile.innerHTML = '<option value="">All profiles</option>' + this.profiles.map((item) => `<option value="${this.escape(item.profile_key)}">${this.escape(item.display_name)} (${this.escape(item.profile_key)})</option>`).join("");
        if (model) model.innerHTML = '<option value="">All models</option>' + this.models.map((item) => `<option value="${this.escape(item.id)}">${this.escape(this.modelLabel(item))}</option>`).join("");
    },
    activityStatus(message, kind = "neutral") {
        const status = document.getElementById("decision-activity-status");
        if (!status) return;
        status.textContent = message;
        status.className = kind === "error" ? "rounded border border-red-300 bg-red-50 dark:bg-red-950/30 dark:border-red-800 p-3 text-sm text-red-800 dark:text-red-200" : "text-sm text-gray-500 dark:text-gray-400";
    },
    async loadActivity(append = false) {
        const list = document.getElementById("decision-activity-list"), more = document.getElementById("decision-activity-more");
        if (!list) return;
        if (!append) { this.activityCursor = null; this.activityEvents = []; list.innerHTML = ""; this.activityStatus("Loading activity…"); }
        const params = this.activityFilters(); if (append && this.activityCursor) params.set("cursor", this.activityCursor);
        try {
            const response = await apiGet("/api/admin/decision-activity?" + params.toString());
            const events = response.events || [];
            this.activityEvents = append ? this.activityEvents.concat(events) : events;
            this.activityCursor = response.next_cursor || null;
            more.classList.toggle("hidden", !this.activityCursor);
            if (!this.activityEvents.length) this.activityStatus("No matching activity. Events may not have been recorded yet, may have expired under retention policy, or may have been redacted.");
            else { this.activityStatus(`${this.activityEvents.length} event${this.activityEvents.length === 1 ? "" : "s"} shown.`); list.innerHTML = this.activityEvents.map((event) => this.activityRow(event)).join(""); }
        } catch (_) { more.classList.add("hidden"); this.activityStatus("Could not load Decision Activity. Refresh and try again.", "error"); }
    },
    activityRow(event) {
        const badge = event.result_class === "error" ? "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300" : event.fallback_used ? "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200" : "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300";
        const state = event.result_class === "error" ? `Error: ${event.error_code || "unknown"}` : event.fallback_used ? "Fallback used" : "Success";
        const cell = "px-3 py-2 align-top";
        return `<tr tabindex="0" class="border-t border-gray-200 dark:border-gray-700 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/40 focus:outline-none focus:ring-2 focus:ring-indigo-500" onclick="decisionModels.openActivity('${this.escape(event.id)}')" onkeydown="if (event.key === 'Enter') decisionModels.openActivity('${this.escape(event.id)}')"><td class="${cell} whitespace-nowrap text-gray-500 dark:text-gray-400">${this.escape(this.activityDate(event.created_at))}</td><td class="${cell} font-medium text-gray-900 dark:text-gray-100">${this.escape(event.profile_key)} <span class="text-xs font-normal text-gray-500">${this.escape(event.profile_version)}</span></td><td class="${cell}">${this.escape(event.source)}</td><td class="${cell} text-gray-500 dark:text-gray-400">${this.escape(event.model_id || "unavailable")}</td><td class="${cell}"><span class="rounded px-2 py-0.5 text-xs font-medium ${badge}">${this.escape(state)}</span></td><td class="${cell} text-right whitespace-nowrap text-gray-500 dark:text-gray-400">${event.latency_ms == null ? "unavailable" : this.escape(event.latency_ms + " ms")}</td></tr>`;
    },
    activityDate(value) { return value ? new Date(Number(value) * 1000).toLocaleString() : "time unavailable"; },
    clearActivityFilters() { const form = document.getElementById("decision-activity-filters"); form.reset(); this.loadActivity(); },
    async clearActivity() {
        if (!(await showConfirm({title: "Clear Decision Activity", message: "Permanently delete all recorded decision activity? This cannot be undone.", confirmText: "Clear"}))) return;
        const response = await apiDelete("/api/admin/decision-activity");
        if (!response.success) { if (window.toast) toast.show("Could not clear activity", "error"); return; }
        if (window.toast) toast.show("Decision activity cleared", "success");
        this.loadActivity();
    },
    async openActivity(id) {
        const root = document.getElementById("decision-activity-detail"); root.textContent = "Loading event…"; openModal("decision-activity-modal");
        try { const response = await apiGet("/api/admin/decision-activity/" + encodeURIComponent(id)); this.renderActivityDetail(response.event); }
        catch (_) { root.innerHTML = '<p class="text-red-700 dark:text-red-300">This event is unavailable. It may have expired or been redacted under retention policy.</p>'; }
    },
    activityInputNote(event) {
        const why = {off: "input recording was off when this ran", withheld: "the input contained credential-like text, so it was withheld", too_large: "the input was larger than 16 KiB", invalid: "the input could not be stored"};
        return event.input_status && why[event.input_status] ? `Not recorded: ${why[event.input_status]}.` : "Not recorded: this event predates input recording.";
    },
    renderActivityDetail(event) {
        const root = document.getElementById("decision-activity-detail"), summary = event.outcome_summary || {};
        this._activityDetail = event;
        const rows = [["Source", event.source], ["Profile", event.profile_key], ["Profile version", event.profile_version], ["Model", event.model_id], ["Adapter", event.adapter_key], ["Result", event.result_class], ["Typed error", event.error_code], ["Confidence", event.confidence == null ? "unavailable" : Math.round(event.confidence * 100) + "%"], ["Latency", event.latency_ms == null ? "unavailable" : event.latency_ms + " ms"], ["Attempts", event.attempts], ["Fallback", event.fallback_used ? `used${event.fallback_reason ? ": " + event.fallback_reason : ""}` : "not used"], ["Correlation ID", event.correlation_id || "unavailable"], ["Recorded", this.activityDate(event.created_at)]];
        const profile = this.profiles.find((item) => item.profile_key === event.profile_key);
        const hasInput = event.input_context != null;
        const canReproduce = hasInput && !!profile;
        const changed = profile && profile.policy_version !== event.profile_version;
        const section = "mt-4";
        const heading = (text) => `<h4 class="font-medium text-gray-900 dark:text-gray-100">${text}</h4>`;
        const input = hasInput
            ? `<pre class="mt-2 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-100 dark:bg-gray-900 p-3 text-xs max-h-64">${this.escape(JSON.stringify(event.input_context, null, 2))}</pre>`
            : `<p class="mt-2 text-sm text-gray-500 dark:text-gray-400">${this.escape(this.activityInputNote(event))}</p>`;
        const keys = Object.keys(summary);
        const conf = event.confidence == null ? "" : Math.max(0, Math.min(1, event.confidence));
        const chips = [event.model_id, event.latency_ms != null ? event.latency_ms + " ms" : "", event.attempts ? event.attempts + (event.attempts === 1 ? " attempt" : " attempts") : "", event.fallback_used ? "fallback used" : ""].filter(Boolean).map((c) => `<span class="dp-chip">${this.escape(c)}</span>`).join("");
        const cards = keys.map((key) => {
            const value = summary[key], isScore = typeof value === "number" && value >= 0 && value <= 1;
            const bar = isScore ? `<div class="dp-conf"><span>Score</span><div class="dp-bar"><i style="width:${value * 100}%"></i></div><b>${this.escape(Math.round(value * 1000) / 10 + "%")}</b></div>` : "";
            return `<div class="dp-out"><div class="dp-out-top"><span class="dp-out-key">${this.escape(key)}</span><span class="dp-val">${this.escape(JSON.stringify(value))}</span></div>${bar}</div>`;
        }).join("");
        const meter = conf === "" ? "" : `<div class="dp-out"><div class="dp-out-top"><span class="dp-out-key">Overall confidence</span><span class="dp-val">${this.escape(Math.round(conf * 100) + "%")}</span></div><div class="dp-conf"><span>Lowest field confidence</span><div class="dp-bar"><i style="width:${conf * 100}%"></i></div></div></div>`;
        const output = event.error_code
            ? `<div class="dp-banner bad mt-2"><strong>${this.escape(event.error_code)}</strong><span>No answer was produced.</span></div>${chips ? `<div class="dp-chips">${chips}</div>` : ""}`
            : keys.length
                ? `<div class="dp-banner ok mt-2">✓ Decision completed</div><div class="dp-chips">${chips}</div>${cards}${meter}<p class="dp-meta">Per-option probabilities are not recorded in Activity. Run it in the Playground to see them.</p>`
                : `<p class="mt-2 text-sm text-gray-500 dark:text-gray-400">No answer fields were returned.</p>`;
        const action = canReproduce
            ? `<button type="button" class="btn-primary text-sm" onclick="decisionModels.rerunActivity('${this.escape(event.id)}')">Test in Playground</button>`
            : `<button type="button" class="btn-secondary text-sm" ${profile ? "" : "disabled"} onclick="closeModal('decision-activity-modal'); decisionModels.openPlayground({profile: '${this.escape(event.profile_key)}'})">Open profile in Playground</button>`;
        const note = canReproduce
            ? `Fills the Playground with exactly this input${changed ? `. The profile has changed since this event (${this.escape(event.profile_version)} to ${this.escape(profile.policy_version)}), so the result may differ` : ""}. It does not run until you choose Run safe test.`
            : !profile ? "This profile no longer exists as a saved profile, so the event cannot be reproduced." : "No input was recorded for this event, so it cannot be reproduced exactly.";
        root.innerHTML = `<div class="dp"><div class="flex flex-wrap items-center justify-between gap-3"><p class="text-xs text-gray-500 dark:text-gray-400 flex-1 min-w-[12rem]">${note}</p>${action}</div>
            <div class="${section}">${heading("Input")}${input}</div>
            <div class="${section}">${heading("Output")}${output}</div>
            <dl class="mt-6 grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3 text-sm">${rows.map(([key, value]) => `<div><dt class="text-gray-500 dark:text-gray-400">${this.escape(key)}</dt><dd class="font-medium text-gray-900 dark:text-gray-100 break-all">${this.escape(value == null ? "unavailable" : value)}</dd></div>`).join("")}</dl></div>`;
    },
    async rerunActivity(id) {
        const cached = this._activityDetail && this._activityDetail.id === id ? this._activityDetail : null;
        const event = cached || await apiGet("/api/admin/decision-activity/" + encodeURIComponent(id)).then((response) => response.event).catch(() => null);
        const profile = event && this.profiles.find((item) => item.profile_key === event.profile_key);
        if (!event || event.input_context == null || !profile) return;
        closeModal("decision-activity-modal");
        this.openPlayground({profile: event.profile_key});
        document.getElementById("playground-input").value = JSON.stringify(event.input_context, null, 2);
        this.playgroundStateSave();
        document.getElementById("decision-playground-context").textContent = "Input copied from a Decision Activity event. Review it, then choose Run safe test; it has not run.";
        document.getElementById("playground-run").focus();
    },
    async loadRecordInput() {
        const box = document.getElementById("decision-activity-record-input"); if (!box) return;
        try { const response = await apiGet("/api/admin/decision-activity/settings"); if (typeof response.record_input === "boolean") box.checked = response.record_input; } catch (_) { /* leave the default (on) */ }
    },
    async setRecordInput(enabled) {
        const box = document.getElementById("decision-activity-record-input");
        const response = await apiPut("/api/admin/decision-activity/settings", {record_input: !!enabled}).catch(() => ({success: false}));
        if (!response.success) { if (box) box.checked = !enabled; if (window.toast) toast.show("Could not change input recording", "error"); return; }
        if (window.toast) toast.show(enabled ? "Inputs will be recorded" : "Inputs will no longer be recorded", "success");
    },
};
