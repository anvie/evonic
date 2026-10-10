/* General pane: loads values, populates model selects, wires theme +
 * composite/guarded saves. Everything else auto-saves via AutoSave. */

window.settingsGeneral = {
    _bound: false,
    _systemSchemeListener: null,

    async init() {
        const pane = document.getElementById("section-general");
        if (!pane) return;
        pane.dataset.loading = "1";

        // Theme reflects local preference instantly, no fetch needed
        this._updateThemeButtons(EvTheme.get().mode);
        this._renderPresets();

        try {
            const [general, models, defaultModel, classifier, cmpModel] = await Promise.all([
                apiGet("/api/settings/general"),
                ModelsCache.get(),
                apiGet("/api/settings/default-model").catch(() => null),
                apiGet("/api/settings/task-classifier").catch(() => null),
                apiGet("/api/settings/cmp-model").catch(() => null),
            ]);
            if (!general || general.error) {
                throw new Error((general && general.error) || "Failed to load settings");
            }
            this._fill(general);
            this._populateModelSelects(models, {
                defaultModelId: defaultModel && defaultModel.model ? defaultModel.model.id : "",
                defaultModelFallbackId: general.default_model_fallback_id || "",
                visionModelId: general.vision_model_id || "",
                visionFallbackModelId: general.vision_fallback_model_id || "",
                visionFallbackModel2Id: general.vision_fallback_model_2_id || "",
                kbOrganizerModelId: general.kb_organizer_model_id || "",
                classifierModelId: classifier ? classifier.model_id || "" : "",
                cmpModelId: cmpModel ? cmpModel.model_id || "" : "",
            });
            const clsToggle = document.getElementById("task-classifier-toggle");
            if (clsToggle && classifier) clsToggle.checked = !!classifier.enabled;
        } catch (e) {
            console.error("settingsGeneral.init failed:", e);
            if (window.toast) toast.show("Failed to load settings: " + e.message, "error");
        } finally {
            delete pane.dataset.loading;
        }

        if (!this._bound) {
            this._bound = true;
            this._registerSpecialSaves();
            AutoSave.bind(pane);
            // Repopulate model selects when the Models pane changes anything
            document.addEventListener("models:changed", async () => {
                const models = await ModelsCache.get();
                this._populateModelSelects(models, this._currentSelectValues());
            });
        }
    },

    _fill(s) {
        const set = (id, v) => {
            const el = document.getElementById(id);
            if (el) {
                el.value = v;
                el._lastSaved = v;
            }
        };
        const check = (id, v) => {
            const el = document.getElementById(id);
            if (el) el.checked = !!v;
        };
        set("agent-timeout-retries-input", s.agent_timeout_retries);
        set("llm-max-retries-input", s.llm_max_retries);
        set("max-concurrent-per-agent-input", s.max_concurrent_llm_per_agent);
        set("max-concurrent-per-model-input", s.max_concurrent_llm_per_model);
        set("max-concurrent-llm-global-input", s.max_concurrent_llm_global);
        set("agent-queue-workers-input", s.agent_queue_workers);
        set("max-tool-iterations-input", s.max_tool_iterations);
        set("max-goal-nudges-input", s.max_goal_nudges);
        set("agent-sidebar-limit-input", s.agent_sidebar_limit);
        set("kb-organizer-nightly-time-input", s.kb_organizer_nightly_time);
        check("public-history-toggle", s.public_history);
        check("long-running-guard-toggle", s.long_running_guard_enabled);
        check("root-fs-scan-guard-toggle", s.root_fs_scan_guard_enabled);
        check("message-wrapper-toggle", s.message_wrapper_enabled);
        check("whatsapp-safe-delivery-toggle", s.whatsapp_safe_delivery_enabled);
        check("whatsapp-natural-formatting-toggle", s.whatsapp_natural_formatting_enabled);
        set("whatsapp-pool-window-input", s.whatsapp_pool_window_seconds);
        set("whatsapp-min-send-interval-input", s.whatsapp_min_send_interval_seconds);
        set("whatsapp-typing-speed-input", s.whatsapp_typing_chars_per_second);
        set("whatsapp-max-typing-delay-input", s.whatsapp_max_typing_delay_seconds);
        set("whatsapp-outbound-limit-input", s.whatsapp_max_outbound_per_minute);
    },

    _currentSelectValues() {
        const val = (id) => {
            const el = document.getElementById(id);
            return el ? el.value : "";
        };
        return {
            defaultModelId: val("default-model-select"),
            defaultModelFallbackId: val("default-model-fallback-select"),
            visionModelId: val("vision-model-select"),
            visionFallbackModelId: val("vision-fallback-model-select"),
            visionFallbackModel2Id: val("vision-fallback-model-2-select"),
            kbOrganizerModelId: val("kb-organizer-model-select"),
            classifierModelId: val("task-classifier-model-select"),
            cmpModelId: val("cmp-model-select"),
        };
    },

    _populateModelSelects(models, current) {
        const fill = (id, list, selectedId, emptyLabel) => {
            const el = document.getElementById(id);
            if (!el) return;
            if (list.length === 0) {
                el.innerHTML =
                    '<option value="">No models configured — add one in the Models section</option>';
                return;
            }
            ui.populateModelSelect(el, list, {
                selectedValue: selectedId || "",
                emptyLabel: emptyLabel,
            });
        };

        const enabled = models.filter((m) => m.enabled);
        fill("default-model-select", models, current.defaultModelId, "— Select model —");
        fill(
            "default-model-fallback-select",
            enabled,
            current.defaultModelFallbackId,
            "None / disabled",
        );
        fill(
            "vision-model-select",
            enabled.filter((m) => m.vision_supported),
            current.visionModelId,
            "Auto-detect (first vision-capable model)",
        );
        const visionModels = enabled.filter((m) => m.vision_supported);
        fill(
            "vision-fallback-model-select",
            visionModels,
            current.visionFallbackModelId,
            "No fallback",
        );
        fill(
            "vision-fallback-model-2-select",
            visionModels,
            current.visionFallbackModel2Id,
            "No fallback",
        );
        fill(
            "kb-organizer-model-select",
            enabled,
            current.kbOrganizerModelId,
            "Use agent’s default model",
        );
        fill(
            "task-classifier-model-select",
            models,
            current.classifierModelId,
            "Use default model",
        );
        fill(
            "cmp-model-select",
            models,
            current.cmpModelId,
            "Use Task Classifier model",
        );
    },

    _registerSpecialSaves() {
        // Task classifier: composite PUT — always send both fields together
        const saveClassifier = async () => {
            const toggle = document.getElementById("task-classifier-toggle");
            const select = document.getElementById("task-classifier-model-select");
            const res = await apiPut("/api/settings/task-classifier", {
                enabled: toggle ? toggle.checked : true,
                model_id: select ? select.value : "",
            });
            if (!res || !res.success) {
                throw new Error((res && res.error) || "Save failed");
            }
        };
        AutoSave.registerHandler("task_classifier_enabled", saveClassifier);
        AutoSave.registerHandler("task_classifier_model_id", saveClassifier);

        // CMP path-detection model
        AutoSave.registerHandler("cmp_model_id", async () => {
            const select = document.getElementById("cmp-model-select");
            const res = await apiPut("/api/settings/cmp-model", {
                model_id: select ? select.value : "",
            });
            if (!res || !res.success) {
                throw new Error((res && res.error) || "Save failed");
            }
        });

        // Public history: guarded by an explicit confirm modal when enabling
        AutoSave.registerGuard("public_history", (enabling) => {
            if (!enabling) return Promise.resolve(true);
            return new Promise((resolve) => {
                const modal = document.getElementById("public-history-confirm-modal");
                const confirmBtn = document.getElementById("public-history-confirm-btn");
                const cancelBtn = document.getElementById("public-history-cancel-btn");
                const done = (ok) => {
                    modal.classList.remove("active");
                    confirmBtn.onclick = cancelBtn.onclick = null;
                    resolve(ok);
                };
                confirmBtn.onclick = () => done(true);
                cancelBtn.onclick = () => done(false);
                modal.classList.add("active");
            });
        });
    },

    /* ---- Theme ---- */

    setTheme(theme) {
        EvTheme.set({ mode: theme });
        this._updateThemeButtons(theme);
        this._renderPresets();
        const seg = document.getElementById("theme-segmented");
        AutoSave.save("theme", theme, seg);
    },

    /** Pick a preset. Choosing one from the family that is not showing switches the mode so the change is visible
     *  (unless the mode is "system", which keeps following the OS). */
    setPreset(id) {
        const preset = EvTheme.PRESETS.find((p) => p.id === id);
        if (!preset) return;
        const opts = {};
        opts[preset.mode] = id;
        const mode = EvTheme.get().mode;
        if (mode !== "system" && mode !== preset.mode) opts.mode = preset.mode;
        EvTheme.set(opts);
        this._updateThemeButtons(EvTheme.get().mode);
        this._renderPresets();
    },

    _renderPresets() {
        const st = EvTheme.get();
        ["dark", "light"].forEach((family) => {
            const grid = document.getElementById("theme-presets-" + family);
            if (!grid) return;
            grid.innerHTML = EvTheme.PRESETS.filter((p) => p.mode === family)
                .map((p) => {
                    const sw = p.swatch;
                    const selected = st[family] === p.id;
                    const active = selected && st.isDark === (family === "dark");
                    return (
                        '<button type="button" class="theme-card' + (selected ? " selected" : "") + (active ? " active" : "") + '"' +
                        ' role="radio" aria-checked="' + selected + '" data-id="' + p.id + '"' +
                        ' onclick="settingsGeneral.setPreset(\'' + p.id + '\')" title="' + p.name + (selected && !active ? " (used in " + family + " mode)" : "") + '">' +
                        '<span class="theme-swatch" style="background:' + sw.bg + ';border-color:' + sw.border + '">' +
                        '<span class="theme-swatch-bar" style="background:' + sw.surface + ';border-color:' + sw.border + '">' +
                        '<i style="background:' + sw.text + '"></i><i style="background:' + sw.text + ';opacity:.45"></i></span>' +
                        '<span class="theme-swatch-dot" style="background:' + sw.accent + '"></span>' +
                        "</span>" +
                        '<span class="theme-card-name">' + p.name + "</span>" +
                        "</button>"
                    );
                })
                .join("");
        });
    },

    _updateThemeButtons(theme) {
        const seg = document.getElementById("theme-segmented");
        if (!seg) return;
        const buttons = Array.from(seg.querySelectorAll(".segmented-btn"));
        let index = 0;
        buttons.forEach((btn, i) => {
            const active = btn.dataset.value === theme;
            btn.classList.toggle("active", active);
            if (active) index = i;
        });
        const thumb = seg.querySelector(".segmented-thumb");
        if (thumb) thumb.style.transform = "translateX(" + index * 100 + "%)";
    },
};
