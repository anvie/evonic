/* Safety page core — /system/safety
 * Accessible tab router (General / HMADS / DMSS) with lazy pane init,
 * General policy status (live from the settings + health APIs), and the
 * DMSS (Decim Safety) dashboard migrated from the legacy /system/decim-safety
 * page with proper loading / error / empty states.
 *
 * The HMADS pane reuses window.hmads from partials/hmads.html (loaded inline
 * by the template before this script). */

(function () {
    "use strict";

    var TABS = ["general", "hmads", "dmss"];

    function qs(sel) { return document.querySelector(sel); }
    function esc(s) {
        var d = document.createElement("div");
        d.textContent = (s === null || s === undefined) ? "" : String(s);
        return d.innerHTML;
    }

    /* ==================== Accessible tab router ====================
     * role=tablist/tab/tabpanel with roving tabindex, arrow-key navigation
     * and hash routing (#general | #hmads | #dmss). Panes initialize lazily
     * on first activation so data is only fetched when actually viewed. */

    var SafetyTabs = {
        _initialized: {},

        init() {
            var bar = qs("#safety-tablist");
            if (!bar) return;
            var tabs = Array.prototype.slice.call(bar.querySelectorAll('[role="tab"]'));

            var select = function (id, focus) {
                if (TABS.indexOf(id) === -1) id = "general";
                tabs.forEach(function (t) {
                    var on = t.dataset.tab === id;
                    t.setAttribute("aria-selected", on ? "true" : "false");
                    t.tabIndex = on ? 0 : -1;
                });
                TABS.forEach(function (tid) {
                    var p = document.getElementById("safety-panel-" + tid);
                    if (p) p.hidden = tid !== id;
                });
                if (focus) {
                    var active = null;
                    tabs.forEach(function (t) { if (t.dataset.tab === id) active = t; });
                    if (active) active.focus();
                }
                if (location.hash !== "#" + id) {
                    history.replaceState(null, "", "#" + id);
                }
                SafetyTabs.initPane(id);
            };

            SafetyTabs.initPane = function (id) {
                if (SafetyTabs._initialized[id]) return;
                SafetyTabs._initialized[id] = true;
                try {
                    if (id === "general") {
                        if (window.safetyGeneral) window.safetyGeneral.init();
                    } else if (id === "hmads") {
                        if (window.hmads) window.hmads.init();
                    } else if (id === "dmss") {
                        if (window.safetyDmss) window.safetyDmss.init();
                    }
                } catch (e) {
                    console.error("Safety pane init failed:", id, e);
                    SafetyTabs._initialized[id] = false;
                }
            };

            tabs.forEach(function (t, i) {
                t.addEventListener("click", function () { select(t.dataset.tab); });
                t.addEventListener("keydown", function (e) {
                    var next = null;
                    if (e.key === "ArrowRight") next = tabs[(i + 1) % tabs.length];
                    else if (e.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length];
                    else if (e.key === "Home") next = tabs[0];
                    else if (e.key === "End") next = tabs[tabs.length - 1];
                    if (next) {
                        e.preventDefault();
                        select(next.dataset.tab, true);
                    }
                });
            });

            window.addEventListener("hashchange", function () {
                select(location.hash.slice(1));
            });

            select(location.hash.slice(1));
        },
    };

    /* ==================== General pane: policy + live status ====================
     * Explains the "HMADS only" vs "DMSS enabled" choice, offers the policy
     * controls (segmented selector + synced enable switch, persisted via
     * PUT /api/settings/decim-safety — task #837), and shows the live
     * operational state (settings + health). */

    window.safetyGeneral = {
        _initialized: false,
        _timer: null,
        _loaded: null,          // last settings object returned by the API
        _lastHealth: null,      // last health object (reused after a save)
        _pending: { policy: null }, // policy currently selected in the UI
        _dirty: false,          // _pending differs from the loaded policy
        _saving: false,

        init() {
            if (this._initialized) return;
            this._initialized = true;
            var retry = qs("#sg-retry");
            if (retry) retry.addEventListener("click", function () { window.safetyGeneral.load(); });
            var refresh = qs("#sg-refresh");
            if (refresh) refresh.addEventListener("click", function () { window.safetyGeneral.load(); });

            // Policy selector + enable toggle + save (task #837).
            var self = this;
            document.querySelectorAll('input[name="sg-policy"]').forEach(function (r) {
                r.addEventListener("change", function () { self._onPolicyChange(r.value); });
            });
            var toggle = qs("#sg-enabled-toggle");
            if (toggle) toggle.addEventListener("change", function () { self._onToggleChange(); });
            var save = qs("#sg-save");
            if (save) save.addEventListener("click", function () { self.save(); });

            this.load();
            // Keep the status card fresh while the page is open.
            this._timer = setInterval(function () {
                if (!document.hidden) window.safetyGeneral.load(true);
            }, 30000);
        },

        /* ---------- Policy <-> settings mapping ----------
         * "HMADS only"  => enabled=false, mode="off"   (resolver never calls the provider)
         * "DMSS enabled"=> enabled=true,  mode=shadow|enforce (deterministic HMADS fallback) */
        _policyFromSettings(s) {
            return (!s || !s.enabled || s.mode === "off") ? "hmads-only" : "dmss";
        },

        _payloadFromPolicy(policy) {
            if (policy === "dmss") {
                var mode = (this._loaded && (this._loaded.mode === "shadow" || this._loaded.mode === "enforce"))
                    ? this._loaded.mode : "shadow";
                return { enabled: true, mode: mode };
            }
            return { enabled: false, mode: "off" };
        },

        _applyPolicyToControls(policy) {
            var radio = qs("#sg-radio-" + (policy === "dmss" ? "dmss" : "hmads"));
            if (radio) radio.checked = true;
            var on = policy === "dmss";
            var toggle = qs("#sg-enabled-toggle");
            if (toggle) toggle.checked = on;
            var text = qs("#sg-enabled-text");
            if (text) text.textContent = on ? "On" : "Off";
        },

        _markDirty() {
            var loadedPolicy = this._policyFromSettings(this._loaded);
            var dirty = !!(this._pending.policy && this._pending.policy !== loadedPolicy);
            this._dirty = dirty;
            var badge = qs("#sg-dirty");
            if (badge) badge.hidden = !dirty;
        },

        _onPolicyChange(value) {
            this._pending.policy = value;
            this._applyPolicyToControls(value);
            this._clearFieldErrors();
            this._markDirty();
        },

        _onToggleChange() {
            var toggle = qs("#sg-enabled-toggle");
            var on = !!(toggle && toggle.checked);
            var policy = on ? "dmss" : "hmads-only";
            this._pending.policy = policy;
            var radio = qs("#sg-radio-" + (on ? "dmss" : "hmads"));
            if (radio) radio.checked = true;
            var text = qs("#sg-enabled-text");
            if (text) text.textContent = on ? "On" : "Off";
            this._clearFieldErrors();
            this._markDirty();
        },

        /* Reflect server state into the controls. Skips the overwrite while the
         * user has unsaved edits so a background refresh doesn't clobber them. */
        syncControls(settings) {
            this._loaded = settings || {};
            if (!this._dirty) {
                this._pending.policy = this._policyFromSettings(this._loaded);
                this._applyPolicyToControls(this._pending.policy);
            }
            this._markDirty();
        },

        /* ---------- Field-level validation + persistence ---------- */
        _validate() {
            var errors = {};
            var policy = this._pending ? this._pending.policy : null;
            if (policy !== "hmads-only" && policy !== "dmss") {
                errors.policy = "Choose a policy: HMADS only or DMSS enabled.";
                return { ok: false, errors: errors };
            }
            var payload = this._payloadFromPolicy(policy);
            if (typeof payload.enabled !== "boolean") errors.enabled = "Enabled must be a boolean.";
            if (["off", "shadow", "enforce"].indexOf(payload.mode) === -1) {
                errors.enabled = "Mode must be one of: off, shadow, enforce.";
            }
            return { ok: Object.keys(errors).length === 0, errors: errors };
        },

        _setFieldError(sel, msg) {
            var el = qs(sel);
            if (!el) return;
            if (msg) { el.textContent = msg; el.hidden = false; }
            else { el.hidden = true; }
        },

        _showFieldErrors(errors) {
            this._setFieldError("#sg-err-policy", errors.policy);
            this._setFieldError("#sg-err-enabled", errors.enabled || errors.mode);
        },

        _clearFieldErrors() {
            this._setFieldError("#sg-err-policy", null);
            this._setFieldError("#sg-err-enabled", null);
        },

        save() {
            var self = this;
            if (this._saving) return;
            var errors = this._validate();
            if (!errors.ok) { this._showFieldErrors(errors.errors); return; }
            this._clearFieldErrors();

            var payload = this._payloadFromPolicy(this._pending.policy);
            this._saving = true;
            var saveBtn = qs("#sg-save");
            var status = qs("#sg-save-status");
            var errBox = qs("#sg-save-error");
            if (saveBtn) saveBtn.disabled = true;
            if (status) status.textContent = "Saving\u2026";
            if (errBox) errBox.hidden = true;

            fetch("/api/settings/decim-safety", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            }).then(function (r) {
                return r.json().then(function (d) { return { ok: r.ok, data: d }; });
            }).then(function (res) {
                self._saving = false;
                if (saveBtn) saveBtn.disabled = false;
                if (status) status.textContent = "";
                if (res.ok && res.data && res.data.success) {
                    if (window.toast) toast.success("Safety policy saved");
                    else if (window.evToast) evToast("Safety policy saved", "success");
                    // Re-render from the authoritative server state (keeps health fresh).
                    self.render((res.data && res.data.settings) || self._loaded, self._lastHealth);
                } else {
                    var msg = (res.data && res.data.error) || "Save failed.";
                    if (errBox) {
                        var em = qs("#sg-save-error-msg");
                        if (em) em.textContent = msg;
                        errBox.hidden = false;
                    }
                    if (window.toast) toast.error(msg);
                    else if (window.evToast) evToast(msg, "error");
                }
            }).catch(function (e) {
                self._saving = false;
                if (saveBtn) saveBtn.disabled = false;
                if (status) status.textContent = "";
                console.error("Failed to save safety policy:", e);
                if (errBox) {
                    var em2 = qs("#sg-save-error-msg");
                    if (em2) em2.textContent = "Network error \u2014 could not reach the server.";
                    errBox.hidden = false;
                }
                if (window.toast) toast.error("Failed to save safety policy");
                else if (window.evToast) evToast("Failed to save safety policy", "error");
            });
        },

        load(silent) {
            var loading = qs("#sg-loading");
            var error = qs("#sg-error");
            var body = qs("#sg-status");
            if (!silent && loading) loading.hidden = false;
            if (error) error.hidden = true;

            Promise.all([
                fetch("/api/settings/decim-safety").then(function (r) { return r.json(); }),
                fetch("/api/admin/decim-safety/health").then(function (r) { return r.json(); }),
            ]).then(function (res) {
                var settings = (res[0] && res[0].settings) || {};
                var health = res[1] || {};
                window.safetyGeneral.render(settings, health);
            }).catch(function (e) {
                console.error("Failed to load safety status:", e);
                if (loading) loading.hidden = true;
                if (body) body.hidden = true;
                if (error) error.hidden = false;
            });
        },

        render(settings, health) {
            var loading = qs("#sg-loading");
            var body = qs("#sg-status");
            if (loading) loading.hidden = true;
            if (body) body.hidden = false;
            this._lastHealth = health || {};

            var dmssOn = !!settings.enabled;

            // Highlight the active policy card (badge on the HMADS card, pill on the DMSS card).
            document.querySelectorAll(".sf-policy").forEach(function (card) {
                var policy = card.dataset.policy;
                var active = (policy === "dmss") === dmssOn;
                card.classList.toggle("is-active", active);
                var badge = card.querySelector(".sf-policy-badge");
                if (badge) {
                    badge.textContent = active ? "Active" : "Inactive";
                    badge.className = "sf-policy-badge " + (active ? "sf-badge sf-badge-accent" : "sf-badge sf-badge-muted");
                }
                var pill = card.querySelector(".sf-policy-active-pill");
                if (pill) pill.hidden = !active;
            });

            // Mode label: off / shadow / enforce (with the human meaning).
            var mode = settings.mode || "off";
            var modeText = mode === "off" ? "off" : mode;
            this._set("#sg-mode", modeText);
            this._set("#sg-enabled", dmssOn ? "On" : "Off");
            this._set("#sg-provider", dmssOn ? (settings.provider || "—") : "— (disabled)");
            this._set("#sg-fallback",
                (health.fallback_rate_24h === null || health.fallback_rate_24h === undefined)
                    ? "—" : (Math.round(health.fallback_rate_24h * 1000) / 10) + "%");
            this._set("#sg-last", health.last_decision_at || "never");
            this._set("#sg-timeout", (settings.request_timeout_ms != null ? settings.request_timeout_ms + " ms" : "—"));
            this._set("#sg-confidence", (settings.minimum_confidence != null ? String(settings.minimum_confidence) : "—"));
            this._set("#sg-breaker",
                (settings.circuit_breaker_failures != null && settings.circuit_breaker_cooldown_seconds != null)
                    ? (settings.circuit_breaker_failures + " fails / " + settings.circuit_breaker_cooldown_seconds + " s cooldown")
                    : "—");
            this._set("#sg-retention", (settings.retention_days != null ? settings.retention_days + " days" : "—"));

            // Circuit badge color.
            var cb = qs("#sg-circuit-badge");
            if (cb) {
                var state = (health.circuit_state || "closed").toLowerCase();
                cb.className = "sf-badge " + (state === "open" ? "sf-badge-err" : (state === "half-open" ? "sf-badge-warn" : "sf-badge-ok"));
                cb.textContent = state;
            }

            // Keep the policy controls in sync with the server state.
            this.syncControls(settings);
        },

        _set(sel, value) {
            var el = qs(sel);
            if (el) el.textContent = value;
        },
    };

    /* ==================== DMSS pane: migrated dashboard ====================
     * Same data surface as the legacy /system/decim-safety page
     * (health / summary / events / telemetry-clear), restyled with the
     * shared tokens and with explicit loading / error / empty states. */

    window.safetyDmss = {
        API: "/api/admin/decim-safety",
        _initialized: false,
        _timer: null,
        _config: null,
        _configDirty: false,
        _savingConfig: false,
        state: { limit: 50, offset: 0, total: 0 },

        init() {
            if (this._initialized) return;
            this._initialized = true;

            var refresh = qs("#dmss-refresh");
            if (refresh) refresh.addEventListener("click", function () { window.safetyDmss.refresh(); });
            var clear = qs("#dmss-clear");
            if (clear) clear.addEventListener("click", function () { window.safetyDmss.clearTelemetry(); });
            var retry = qs("#dmss-retry");
            if (retry) retry.addEventListener("click", function () { window.safetyDmss.refresh(); });
            ["#f-window", "#f-mode", "#f-tool", "#f-unsafe"].forEach(function (sel) {
                var el = qs(sel);
                if (el) el.addEventListener("change", function () {
                    window.safetyDmss.state.offset = 0;
                    window.safetyDmss.refresh();
                });
            });
            var form = qs("#dmss-config");
            if (form) form.addEventListener("submit", function (e) {
                e.preventDefault(); window.safetyDmss.saveConfig();
            });
            ["#dc-provider", "#dc-endpoint", "#dc-timeout", "#dc-confidence", "#dc-payload", "#dc-failures", "#dc-cooldown"].forEach(function (sel) {
                var input = qs(sel);
                if (input) input.addEventListener("input", function () { window.safetyDmss._markConfigDirty(); });
            });

            this.loadConfig();
            this.refresh();
            this._timer = setInterval(function () {
                if (!document.hidden) window.safetyDmss.loadHealth();
            }, 30000);
        },

        _field(sel) { return qs(sel); },

        _configPayload() {
            function value(sel) { var el = qs(sel); return el ? el.value.trim() : ""; }
            return {
                provider: value("#dc-provider"),
                provider_endpoint: value("#dc-endpoint"),
                request_timeout_ms: Number(value("#dc-timeout")),
                minimum_confidence: Number(value("#dc-confidence")),
                max_payload_chars: Number(value("#dc-payload")),
                circuit_breaker_failures: Number(value("#dc-failures")),
                circuit_breaker_cooldown_seconds: Number(value("#dc-cooldown")),
            };
        },

        _setConfigError(field, message) {
            var el = qs("#dc-err-" + field);
            if (!el) return;
            el.hidden = !message;
            el.textContent = message || "";
        },

        _clearConfigErrors() {
            ["provider", "endpoint", "timeout", "confidence", "payload", "failures", "cooldown"].forEach(function (name) {
                window.safetyDmss._setConfigError(name, "");
            });
            var box = qs("#dc-error"); if (box) box.hidden = true;
        },

        _validateConfig(payload) {
            var ok = true;
            this._clearConfigErrors();
            function error(name, message) { window.safetyDmss._setConfigError(name, message); ok = false; }
            if (payload.provider !== "systemone") error("provider", "Choose a registered provider.");
            // An existing masked secret is valid; a newly entered endpoint must be HTTPS/HTTP.
            if (payload.provider_endpoint && payload.provider_endpoint.indexOf("…") < 0 && payload.provider_endpoint.charAt(0) !== "•" && !/^https?:\/\/[^/]+/i.test(payload.provider_endpoint)) error("endpoint", "Enter an absolute http(s) URL.");
            if (!Number.isInteger(payload.request_timeout_ms) || payload.request_timeout_ms < 100 || payload.request_timeout_ms > 10000) error("timeout", "Use 100–10,000 ms.");
            if (!Number.isFinite(payload.minimum_confidence) || payload.minimum_confidence < 0 || payload.minimum_confidence > 1) error("confidence", "Use a value from 0 to 1.");
            if (!Number.isInteger(payload.max_payload_chars) || payload.max_payload_chars < 1 || payload.max_payload_chars > 120000) error("payload", "Use 1–120,000 characters.");
            if (!Number.isInteger(payload.circuit_breaker_failures) || payload.circuit_breaker_failures < 1 || payload.circuit_breaker_failures > 20) error("failures", "Use 1–20 failures.");
            if (!Number.isInteger(payload.circuit_breaker_cooldown_seconds) || payload.circuit_breaker_cooldown_seconds < 1 || payload.circuit_breaker_cooldown_seconds > 3600) error("cooldown", "Use 1–3,600 seconds.");
            return ok;
        },

        _markConfigDirty() {
            this._configDirty = true;
            var badge = qs("#dmss-config-dirty"); if (badge) badge.hidden = false;
        },

        loadConfig() {
            var self = this;
            fetch("/api/settings/decim-safety").then(function (r) { if (!r.ok) throw new Error(); return r.json(); }).then(function (data) {
                var s = data.settings || {};
                self._config = s;
                if (self._configDirty) return;
                var values = { "#dc-provider": s.provider, "#dc-endpoint": s.provider_endpoint || "", "#dc-timeout": s.request_timeout_ms, "#dc-confidence": s.minimum_confidence, "#dc-payload": s.max_payload_chars, "#dc-failures": s.circuit_breaker_failures, "#dc-cooldown": s.circuit_breaker_cooldown_seconds };
                Object.keys(values).forEach(function (sel) { var el = qs(sel); if (el) el.value = values[sel]; });
                var badge = qs("#dmss-config-dirty"); if (badge) badge.hidden = true;
            }).catch(function () { window.safetyDmss._setError(true, "Failed to load DMSS configuration."); });
        },

        saveConfig() {
            if (this._savingConfig) return;
            var self = this, payload = this._configPayload();
            if (!this._validateConfig(payload)) return;
            this._savingConfig = true;
            var btn = qs("#dc-save"), status = qs("#dc-status"); if (btn) btn.disabled = true; if (status) status.textContent = "Saving…";
            fetch("/api/settings/decim-safety", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); }).then(function (res) {
                self._savingConfig = false; if (btn) btn.disabled = false; if (status) status.textContent = "";
                if (!res.ok || !res.data.success) throw new Error((res.data && res.data.error) || "Could not save configuration.");
                self._configDirty = false; self._config = res.data.settings || {}; self.loadConfig(); self.loadHealth();
                if (window.evToast) evToast("DMSS configuration saved", "success");
            }).catch(function (err) { self._savingConfig = false; if (btn) btn.disabled = false; if (status) status.textContent = ""; var box = qs("#dc-error"), msg = qs("#dc-error-msg"); if (msg) msg.textContent = err.message; if (box) box.hidden = false; if (window.evToast) evToast("Failed to save DMSS configuration", "error"); });
        },

        _filters() {
            var p = new URLSearchParams();
            var w = qs("#f-window");
            var v = w ? parseInt(w.value, 10) : 168;
            p.set("window_hours", String(isNaN(v) ? 168 : v));
            var m = qs("#f-mode");
            if (m && m.value) p.set("mode", m.value);
            var t = qs("#f-tool");
            if (t && t.value) p.set("tool_type", t.value);
            return p;
        },

        _setError(show, message) {
            var banner = qs("#dmss-error");
            if (!banner) return;
            banner.hidden = !show;
            if (show) {
                var msg = qs("#dmss-error-msg");
                if (msg) msg.textContent = message || "Failed to load DMSS data.";
            }
        },

        refresh() {
            this.loadHealth();
            this.loadSummary();
            this.loadEvents();
        },

        loadHealth() {
            var badge = qs("#dmss-state-badge");
            function text(sel, v) { var el = qs(sel); if (el) el.textContent = v; }
            fetch(this.API + "/health").then(function (r) {
                if (!r.ok) throw new Error("HTTP " + r.status);
                return r.json();
            }).then(function (d) {
                window.safetyDmss._setError(false);
                text("#kpi-enabled", d.enabled ? "On" : "Off");
                text("#kpi-mode", d.enabled ? (d.mode || "off") : "off");
                text("#kpi-provider", d.provider || "—");
                text("#kpi-circuit", "circuit " + (d.circuit_state || "closed"));
                var latency = d.latency_ms || {};
                text("#kpi-latency", latency.p50 == null ? "—" : (latency.p50 + " ms p50"));
                text("#kpi-last-call", "last successful call " + (d.last_decision_at || "—"));
                text("#kpi-fallback",
                    (d.fallback_rate_24h === null || d.fallback_rate_24h === undefined)
                        ? "—" : (Math.round(d.fallback_rate_24h * 1000) / 10) + "%");
                if (badge) {
                    var label = d.enabled ? d.mode : "disabled";
                    badge.textContent = label;
                    badge.className = "sf-badge " +
                        (d.enabled && d.mode === "enforce" ? "sf-badge-err" :
                         d.enabled ? "sf-badge-warn" : "sf-badge-muted");
                }
            }).catch(function () {
                if (badge) {
                    badge.textContent = "unreachable";
                    badge.className = "sf-badge sf-badge-err";
                }
                window.safetyDmss._setError(true, "Health check failed — the Decim Safety API is unreachable.");
            });
        },

        loadSummary() {
            var p = this._filters();
            fetch(this.API + "/summary?" + p.toString()).then(function (r) {
                if (!r.ok) throw new Error("HTTP " + r.status);
                return r.json();
            }).then(function (d) {
                function text(sel, v) { var el = qs(sel); if (el) el.textContent = v; }
                text("#kpi-total", d.total_comparisons != null ? d.total_comparisons : 0);
                text("#kpi-accepted", (d.decim_accepted != null ? d.decim_accepted : 0) + " accepted · " + (d.fallback_count != null ? d.fallback_count : 0) + " fallback");
                text("#kpi-unsafe",
                    (d.unsafe && d.unsafe.final != null)
                        ? (d.unsafe.final + " unsafe · " + Math.round((d.unsafe.final_rate || 0) * 1000) / 10 + "%")
                        : "—");
                window.safetyDmss._renderTable("#tbl-decisions", d.decision_distribution || {});
                window.safetyDmss._renderTable("#tbl-agreement", d.agreement_matrix || {});
            }).catch(function (e) {
                console.error("DMSS summary failed:", e);
            });
        },

        _renderTable(sel, obj) {
            var el = qs(sel);
            if (!el) return;
            var rows = Object.keys(obj).map(function (k) {
                return "<tr><td>" + esc(k) + "</td><td>" + esc(obj[k]) + "</td></tr>";
            }).join("");
            el.innerHTML = rows || '<tr><td colspan="2" class="sf-empty-cell">no data in this window</td></tr>';
        },

        loadEvents() {
            var p = this._filters();
            p.set("limit", String(this.state.limit));
            p.set("offset", String(this.state.offset));
            var unsafe = qs("#f-unsafe");
            if (unsafe && unsafe.checked) p.set("unsafe_only", "1");
            var body = qs("#tbl-events-body");
            if (!body) return;

            fetch(this.API + "/events?" + p.toString()).then(function (r) {
                if (!r.ok) throw new Error("HTTP " + r.status);
                return r.json();
            }).then(function (d) {
                window.safetyDmss._setError(false);
                window.safetyDmss.state.total = d.total || 0;
                var events = d.events || [];
                if (!events.length) {
                    body.innerHTML = '<tr><td colspan="11" class="sf-empty-cell">No events in this window. Commands flagged while "Unsafe only" is on appear here.</td></tr>';
                } else {
                    body.innerHTML = events.map(function (e) {
                        return "<tr>" +
                            "<td>" + esc(e.occurred_at) + "</td>" +
                            "<td>" + esc(e.mode) + "</td>" +
                            "<td>" + esc(e.tool_type) + "</td>" +
                            '<td class="strong">' + esc(e.final_level) + "</td>" +
                            "<td>" + esc(e.decision_source) + "</td>" +
                            "<td>" + esc(e.model_decision || "—") + "</td>" +
                            "<td>" + (e.model_confidence == null ? "—" : esc(e.model_confidence)) + "</td>" +
                            "<td>" + esc(e.agreement || "—") + "</td>" +
                            "<td>" + esc(e.fallback_reason || "—") + "</td>" +
                            "<td>" + esc(e.disposition || "—") + "</td>" +
                            '<td class="mono">' + esc(e.command_fingerprint || "—") + " (" + esc(e.command_length) + ")</td>" +
                            "</tr>";
                    }).join("");
                }
                window.safetyDmss._renderPager();
            }).catch(function (e) {
                console.error("DMSS events failed:", e);
                body.innerHTML = '<tr><td colspan="11" class="sf-empty-cell sf-empty-cell-err">Failed to load events.</td></tr>';
                window.safetyDmss._setError(true, "Failed to load the activity table — check the Decim Safety service and retry.");
            });
        },

        _renderPager() {
            var el = qs("#dmss-pager");
            if (!el) return;
            var total = this.state.total;
            var limit = this.state.limit;
            var start = total ? this.state.offset + 1 : 0;
            var end = Math.min(this.state.offset + limit, total);
            el.innerHTML =
                "<span>Showing " + start + "–" + end + " of " + total + "</span>" +
                '<button type="button" class="sf-btn sf-btn-ghost sf-btn-sm" id="dmss-pg-prev"' + (this.state.offset <= 0 ? " disabled" : "") + ">Prev</button>" +
                '<button type="button" class="sf-btn sf-btn-ghost sf-btn-sm" id="dmss-pg-next"' + (end >= total ? " disabled" : "") + ">Next</button>";
            var prev = qs("#dmss-pg-prev");
            var next = qs("#dmss-pg-next");
            if (prev) prev.addEventListener("click", function () {
                window.safetyDmss.state.offset = Math.max(0, window.safetyDmss.state.offset - limit);
                window.safetyDmss.loadEvents();
            });
            if (next) next.addEventListener("click", function () {
                if (end < total) {
                    window.safetyDmss.state.offset += limit;
                    window.safetyDmss.loadEvents();
                }
            });
        },

        clearTelemetry() {
            var self = this;
            if (!confirm("Clear all Decim Safety telemetry records and statistics? This does not change Decim configuration.")) return;
            fetch(this.API + "/telemetry/clear", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
            }).then(function (r) { return r.json(); }).then(function (d) {
                alert(d.success ? ("Cleared " + d.deleted + " record(s).") : "Clear failed.");
                self.state.offset = 0;
                self.refresh();
            }).catch(function () {
                alert("Clear failed.");
            });
        },
    };

    /* ==================== Diagnostic tester (task #839) ====================
     * Bounded probe of the DMSS provider: POST /api/admin/decim-safety/test.
     * The payload is data, not code — the server never executes it. Results
     * render inline (decision + confidence + reason + timing) with explicit
     * "diagnostic only" semantics. */

    // Ready-to-submit sample payloads (task #843): each chip also selects the
    // matching tool type, so the pasted payload is valid for the probe as-is.
    var TESTER_SAMPLES = {
        "bash-safe": { tool: "bash", payload: 'echo "hello world"' },
        "bash-risky": { tool: "bash", payload: "sudo rm -rf ./build" },
        "py-safe": { tool: "python", payload: 'print("hello world")' },
        "py-risky": { tool: "python", payload: 'import subprocess\nsubprocess.run("rm -rf ./dist", shell=True)' },
    };

    window.safetyTester = {
        _running: false,

        init() {
            var run = qs("#dmss-tester-run");
            if (!run) return;
            var payload = qs("#dmss-tester-payload");
            if (payload) {
                payload.addEventListener("input", function () { window.safetyTester._updateCount(); });
                // Ctrl/Cmd+Enter runs the probe without leaving the textarea.
                payload.addEventListener("keydown", function (e) {
                    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
                        e.preventDefault();
                        window.safetyTester.run();
                    }
                });
            }
            run.addEventListener("click", function () { window.safetyTester.run(); });
            // Sample chips: populate the payload (and matching tool type) so the
            // probe is ready to submit without typing anything.
            var toolEl = qs("#dmss-tester-tool");
            Array.prototype.forEach.call(
                document.querySelectorAll("#dmss-tester-samples .sf-tester-sample"),
                function (btn) {
                    btn.addEventListener("click", function () {
                        var s = TESTER_SAMPLES[btn.dataset.sample];
                        if (!s || !payload) return;
                        if (toolEl) toolEl.value = s.tool;
                        payload.value = s.payload;
                        window.safetyTester._updateCount();
                        payload.focus();
                    });
                }
            );
            this._updateCount();
        },

        _updateCount() {
            var payload = qs("#dmss-tester-payload");
            var count = qs("#dmss-tester-count");
            if (!payload || !count) return;
            var len = payload.value.length;
            var max = (window.safetyDmss && window.safetyDmss._config &&
                       window.safetyDmss._config.max_payload_chars) || null;
            count.textContent = len.toLocaleString() +
                (max ? " / " + Number(max).toLocaleString() : "") + " chars";
            count.classList.toggle("is-over", max != null && len > Number(max));
        },

        run() {
            if (this._running) return;
            var payloadEl = qs("#dmss-tester-payload");
            var toolEl = qs("#dmss-tester-tool");
            var out = qs("#dmss-tester-out");
            var run = qs("#dmss-tester-run");
            var payload = payloadEl ? payloadEl.value : "";
            if (!payload.trim()) {
                this._renderError(out, "Paste a sample payload first.");
                return;
            }
            this._running = true;
            if (run) run.disabled = true;
            this._renderPending(out);

            fetch("/api/admin/decim-safety/test", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ payload: payload, tool_type: toolEl ? toolEl.value : "bash" }),
            }).then(function (r) {
                return r.json().then(function (d) { return { ok: r.ok, status: r.status, data: d }; });
            }).then(function (res) {
                window.safetyTester._running = false;
                if (run) run.disabled = false;
                if (!res.ok) {
                    if (res.status === 429) {
                        var secs = (res.data && res.data.retry_after) || "a moment";
                        window.safetyTester._renderError(out,
                            "Rate limit exceeded — try again in " + secs + " s (max 10 tests/min).");
                    } else {
                        window.safetyTester._renderError(out,
                            (res.data && res.data.error) || "Test failed (HTTP " + res.status + ").");
                    }
                    return;
                }
                window.safetyTester._renderResult(out, res.data);
            }).catch(function (e) {
                console.error("DMSS diagnostic test failed:", e);
                window.safetyTester._running = false;
                if (run) run.disabled = false;
                window.safetyTester._renderError(out, "Network error — could not reach the server.");
            });
        },

        _renderPending(out) {
            if (!out) return;
            out.hidden = false;
            out.className = "sf-tester-out";
            out.innerHTML = '<div class="sf-tester-pending"><div class="spinner"></div><span>Calling DMSS provider…</span></div>';
        },

        _renderError(out, message) {
            if (!out) return;
            out.hidden = false;
            out.className = "sf-tester-out sf-tester-out-err";
            out.innerHTML =
                '<div class="sf-tester-line"><span class="sf-tester-verdict sf-tester-verdict-none">no decision</span>' +
                '<span class="sf-tester-msg">' + esc(message) + "</span></div>";
        },

        _renderResult(out, d) {
            if (!out) return;
            out.hidden = false;
            var decision = d.decision; // "allow" | "review" | "block" | null
            var cls = decision === "allow" ? "sf-tester-verdict-allow"
                : decision === "review" ? "sf-tester-verdict-review"
                : decision === "block" ? "sf-tester-verdict-block"
                : "sf-tester-verdict-none";
            var label = decision ? decision.toUpperCase() : "NO DECISION";

            var rows = "";
            function row(key, value) {
                if (value === null || value === undefined || value === "") return;
                rows += '<div class="sf-tester-kv"><span class="sf-tester-k">' + key +
                    '</span><span class="sf-tester-v">' + esc(value) + "</span></div>";
            }
            row("confidence", d.confidence == null ? "—" : (Math.round(d.confidence * 1000) / 10) + "%");
            row("latency", d.latency_ms == null ? "—" : d.latency_ms + " ms");
            row("provider", d.provider || "—");
            row("model", d.model || "—");
            row("payload", (d.payload_chars != null ? d.payload_chars : 0) + " chars · " + (d.tool_type || "bash"));

            var notes = [];
            if (d.fallback_reason === "low_confidence") {
                notes.push("Production would fall back to HMADS (confidence below the configured minimum).");
            } else if (d.fallback_reason) {
                notes.push("Fallback reason: " + d.fallback_reason + ".");
            }
            if (d.dmss_active === false) {
                notes.push("DMSS is currently disabled — production decides with HMADS only; this probe is diagnostic.");
            }

            out.className = "sf-tester-out";
            out.innerHTML =
                '<div class="sf-tester-line"><span class="sf-tester-verdict ' + cls + '">' + label + "</span>" +
                '<span class="sf-tester-msg">' + esc(d.reason || "") + "</span></div>" +
                (rows ? '<div class="sf-tester-rows">' + rows + "</div>" : "") +
                (notes.length
                    ? '<div class="sf-tester-notes">' + notes.map(function (n) {
                        return "<span>• " + esc(n) + "</span>";
                    }).join("") + "</div>"
                    : "");
        },
    };

    /* ==================== Boot ==================== */

    function boot() {
        SafetyTabs.init();
        window.safetyTester.init();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();
