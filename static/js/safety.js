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
            initSettingsNav();
        },
    };

    /* ==================== Settings nav rail ====================
     * The page shares the System Settings rail (partials/settings/nav.html).
     * Its section buttons are client-routed on /system, so here they navigate
     * back to the matching /system#<section> page. */

    function initSettingsNav() {
        document.querySelectorAll(".settings-nav button[data-section], .settings-nav button[data-group]").forEach(function (b) {
            b.addEventListener("click", function () {
                location.href = "/system#" + (b.dataset.section || b.dataset.group);
            });
        });
        // Mobile: the rail is a horizontal pill bar; bring the Safety pill into view.
        var nav = qs(".settings-nav");
        var active = nav && nav.querySelector(".settings-nav-item.active");
        if (active && nav.scrollWidth > nav.clientWidth) {
            nav.scrollLeft = active.offsetLeft - (nav.clientWidth - active.offsetWidth) / 2;
        }
    }

    /* ==================== General pane: policy + live status ====================
     * Explains the "HMADS only" vs "DMSS enabled" choice, offers the policy
     * controls (segmented policy selector, shadow/enforce mode selector, and a
     * synced enable switch, auto-saved via PUT /api/settings/decim-safety —
     * task #837), and shows the live operational state (settings + health).
     * Save feedback matches the System Settings AutoSave: a spinner while
     * saving, then a check, in the .save-status slot beside the field. */

    window.safetyGeneral = {
        _initialized: false,
        _timer: null,
        _loaded: null,          // last settings object returned by the API
        _lastHealth: null,      // last health object (reused after a save)
        _pending: { policy: null, mode: null }, // policy + mode currently selected in the UI
        _dirty: false,          // _pending differs from the loaded policy
        _saving: false,
        _queued: null,          // field ("policy" | "mode") changed while a save was in flight

        init() {
            if (this._initialized) return;
            this._initialized = true;
            var retry = qs("#sg-retry");
            if (retry) retry.addEventListener("click", function () { window.safetyGeneral.load(); });
            var refresh = qs("#sg-refresh");
            if (refresh) refresh.addEventListener("click", function () {
                RefreshButton.run(refresh, function () { return window.safetyGeneral.load(true); });
            });

            // Policy selector + enable toggle + save (task #837).
            var self = this;
            document.querySelectorAll('input[name="sg-policy"]').forEach(function (r) {
                r.addEventListener("change", function () { self._onPolicyChange(r.value); });
            });
            document.querySelectorAll('input[name="sg-mode"]').forEach(function (r) {
                r.addEventListener("change", function () { self._onModeChange(r.value); });
            });
            var toggle = qs("#sg-enabled-toggle");
            if (toggle) toggle.addEventListener("change", function () { self._onToggleChange(); });

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

        /* The mode selector only offers shadow/enforce; anything else (e.g. "off"
         * while the policy is HMADS-only) falls back to the safe default. */
        _modeFromSettings(s) {
            return (s && (s.mode === "shadow" || s.mode === "enforce")) ? s.mode : "shadow";
        },

        _payloadFromPolicy(policy) {
            if (policy === "dmss") {
                var mode = (this._pending && (this._pending.mode === "shadow" || this._pending.mode === "enforce"))
                    ? this._pending.mode : "shadow";
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
            // The mode selector only matters while DMSS is enabled; dim + disable
            // it otherwise but keep the selection so re-enabling restores it.
            this._setModeFieldEnabled(on);
            this._applyModeToControls(this._pending && this._pending.mode);
            this._previewFlow();
        },

        /* The decision-flow diagram follows the *selected* policy + mode. */
        _previewFlow() {
            var panel = qs("#safety-panel-general");
            if (!panel || !this._pending) return;
            panel.dataset.policy = this._pending.policy === "dmss" ? "dmss" : "hmads-only";
            panel.dataset.mode = this._pending.mode === "enforce" ? "enforce" : "shadow";
        },

        _setModeFieldEnabled(enabled) {
            var field = qs("#sg-mode-field");
            if (field) field.classList.toggle("is-dimmed", !enabled);
            document.querySelectorAll('input[name="sg-mode"]').forEach(function (r) {
                r.disabled = !enabled;
            });
        },

        _applyModeToControls(mode) {
            if (mode !== "shadow" && mode !== "enforce") return;
            var radio = qs("#sg-radio-" + mode);
            if (radio) radio.checked = true;
            this._previewFlow();
        },

        _markDirty() {
            var loadedPolicy = this._policyFromSettings(this._loaded);
            var policyDirty = !!(this._pending.policy && this._pending.policy !== loadedPolicy);
            // The mode only changes the payload while the DMSS policy is selected
            // (HMADS-only always sends mode="off").
            var modeDirty = !!(
                this._pending.policy === "dmss" &&
                this._pending.mode &&
                this._pending.mode !== this._modeFromSettings(this._loaded)
            );
            this._dirty = policyDirty || modeDirty;
        },

        _onPolicyChange(value) {
            this._pending.policy = value;
            this._applyPolicyToControls(value);
            this._autoSave("policy");
        },

        _onToggleChange() {
            var toggle = qs("#sg-enabled-toggle");
            var on = !!(toggle && toggle.checked);
            this._pending.policy = on ? "dmss" : "hmads-only";
            this._applyPolicyToControls(this._pending.policy);
            this._autoSave("policy");
        },

        _onModeChange(value) {
            this._pending.mode = value;
            this._applyModeToControls(value);
            this._autoSave("mode");
        },

        /* Save right after a change. A change made while a save is in flight is
         * parked and sent once that save finishes (newest selection wins). */
        _autoSave(field) {
            this._clearFieldErrors();
            this._markDirty();
            if (this._saving) { this._queued = field; return; }
            if (this._dirty) this.save(field);
        },

        /* Reflect server state into the controls. Skips the overwrite while a
         * save is in flight or queued so a background refresh doesn't clobber
         * the user's newest selection. */
        syncControls(settings) {
            this._loaded = settings || {};
            if (!this._saving && !this._queued) {
                var serverMode = this._loaded.mode;
                var keepMode = this._pending.mode === "shadow" || this._pending.mode === "enforce";
                this._pending.policy = this._policyFromSettings(this._loaded);
                // HMADS-only is stored as mode="off": remember the last DMSS mode so
                // re-enabling DMSS restores it instead of resetting to shadow.
                this._pending.mode = (serverMode === "shadow" || serverMode === "enforce" || !keepMode)
                    ? this._modeFromSettings(this._loaded) : this._pending.mode;
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
                errors.mode = "Mode must be one of: off, shadow, enforce.";
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
            this._setFieldError("#sg-err-mode", errors.mode);
            this._setFieldError("#sg-err-enabled", errors.enabled);
        },

        _clearFieldErrors() {
            this._setFieldError("#sg-err-policy", null);
            this._setFieldError("#sg-err-mode", null);
            this._setFieldError("#sg-err-enabled", null);
            ["policy", "mode"].forEach(function (field) {
                var slot = qs("#sg-status-" + field);
                if (slot && slot.classList.contains("is-error")) window.safetyGeneral._status(field, null);
            });
        },

        /* Same markup + classes as the System Settings AutoSave feedback. */
        _status(field, state) {
            var slot = qs("#sg-status-" + field);
            if (!slot) return;
            clearTimeout(slot._fadeTimer);
            slot.classList.remove("is-saving", "is-saved", "is-error");
            slot.innerHTML = "";
            if (state === "saving") {
                slot.classList.add("is-saving");
                slot.innerHTML = '<span class="spinner"></span>';
            } else if (state === "saved") {
                slot.classList.add("is-saved");
                slot.innerHTML =
                    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path class="st-check-path" d="M4 12.5l5 5L20 6.5"/></svg>';
                slot._fadeTimer = setTimeout(function () {
                    slot.classList.remove("is-saved");
                    slot.innerHTML = "";
                }, 1800);
            } else if (state === "error") {
                slot.classList.add("is-error");
                slot.innerHTML =
                    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>';
            }
        },

        save(field) {
            var self = this;
            field = field === "mode" ? "mode" : "policy";
            if (this._saving) { this._queued = field; return; }
            var errors = this._validate();
            if (!errors.ok) {
                this._showFieldErrors(errors.errors);
                this._status(field, "error");
                return;
            }

            var payload = this._payloadFromPolicy(this._pending.policy);
            this._saving = true;
            this._status(field, "saving");

            fetch("/api/settings/decim-safety", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            }).then(function (r) {
                return r.json().then(function (d) { return { ok: r.ok, data: d }; });
            }).then(function (res) {
                if (!(res.ok && res.data && res.data.success)) {
                    var err = new Error((res.data && res.data.error) || "Save failed.");
                    err.fromServer = true;
                    throw err;
                }
                self._saving = false;
                // Re-render from the authoritative server state (keeps health fresh);
                // a queued change keeps the controls on the user's newest selection.
                self.render(res.data.settings || self._loaded, self._lastHealth);
                self._status(field, "saved");
                var next = self._queued;
                self._queued = null;
                if (next) {
                    self._markDirty();
                    if (self._dirty) self.save(next);
                    else self._status(next, "saved");
                }
            }).catch(function (e) {
                self._saving = false;
                self._queued = null;
                console.error("Failed to save safety policy:", e);
                var msg = (e && e.fromServer) ? e.message : "Network error \u2014 could not reach the server.";
                // Snap back to the running policy so the controls never show an unsaved state.
                self.syncControls(self._loaded);
                var errs = {};
                errs[field] = msg;
                self._showFieldErrors(errs);
                self._status(field, "error");
                if (window.toast) toast.error("Failed to save safety policy");
                else if (window.evToast) evToast("Failed to save safety policy", "error");
            });
        },

        load(silent) {
            var loading = qs("#sg-loading");
            var error = qs("#sg-error");
            var body = qs("#sg-status");
            if (!silent && loading && !this._lastHealth) loading.hidden = false;
            if (error) error.hidden = true;
            return Promise.all([
                fetch("/api/settings/decim-safety").then(function (r) { return r.json(); }),
                fetch("/api/admin/decim-safety/health").then(function (r) { return r.json(); }),
            ]).then(function (res) {
                var settings = (res[0] && res[0].settings) || {};
                var health = res[1] || {};
                window.safetyGeneral.render(settings, health);
                RefreshButton.stamp("#sg-updated");
                return true;
            }).catch(function (e) {
                console.error("Failed to load safety status:", e);
                if (loading) loading.hidden = true;
                if (body) body.hidden = true;
                if (error) error.hidden = false;
                var posture = qs("#sg-posture");
                if (posture && !window.safetyGeneral._lastHealth) {
                    posture.dataset.state = "error";
                    window.safetyGeneral._set("#sg-posture-title", "Status unavailable");
                    window.safetyGeneral._set("#sg-posture-desc", "The running policy could not be read.");
                }
                return false;
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

            // Posture headline: the effective policy + mode, in plain words.
            var mode = settings.mode || "off";
            var state = (!dmssOn || mode === "off") ? "hmads" : (mode === "enforce" ? "enforce" : "shadow");
            var posture = qs("#sg-posture");
            if (posture) posture.dataset.state = state;
            this._set("#sg-posture-title", {
                hmads: "HMADS only",
                shadow: "DMSS in shadow mode",
                enforce: "DMSS enforcing",
            }[state]);
            this._set("#sg-posture-desc", {
                hmads: "Deterministic heuristic rules decide every command. The DMSS provider is never called.",
                shadow: "HMADS decides; model decisions are recorded and compared.",
                enforce: "The model decides when it can; HMADS is the exact fallback.",
            }[state]);
            this._set("#sg-provider", dmssOn ? (settings.provider || "—") : "— (disabled)");
            this._set("#sg-fallback",
                (health.fallback_rate_24h === null || health.fallback_rate_24h === undefined)
                    ? "—" : (Math.round(health.fallback_rate_24h * 1000) / 10) + "%");
            this._set("#sg-last", health.last_decision_at || "never");
            var errBox = qs("#sg-error");
            if (errBox) errBox.hidden = true;
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

    /* ==================== Refresh buttons ====================
     * Visible feedback for a manual refresh: spinning icon + "Refreshing…",
     * then "Updated" (or "Failed") for a moment, a short flash on the
     * refreshed region, and an "Updated hh:mm:ss" stamp beside the button.
     * The task returns a promise resolving to false on failure. */

    var RefreshButton = {
        MIN_SPIN_MS: 600,
        RESULT_MS: 1800,

        run(btn, task) {
            if (!btn || btn.dataset.state === "busy") return;
            clearTimeout(btn._rfTimer);
            var label = btn.querySelector(".rf-label");
            var started = Date.now();
            btn.dataset.state = "busy";
            btn.setAttribute("aria-busy", "true");
            if (label) label.textContent = "Refreshing\u2026";

            Promise.resolve().then(task).then(function (ok) { return ok !== false; }, function () { return false; })
                .then(function (ok) {
                    var wait = Math.max(0, RefreshButton.MIN_SPIN_MS - (Date.now() - started));
                    setTimeout(function () {
                        btn.removeAttribute("aria-busy");
                        btn.dataset.state = ok ? "done" : "failed";
                        if (label) label.textContent = ok ? "Updated" : "Failed";
                        if (ok) RefreshButton.flash(btn.dataset.target);
                        btn._rfTimer = setTimeout(function () {
                            delete btn.dataset.state;
                            if (label) label.textContent = "Refresh";
                        }, RefreshButton.RESULT_MS);
                    }, wait);
                });
        },

        flash(sel) {
            var el = sel && qs(sel);
            if (!el) return;
            el.classList.remove("rf-flash");
            void el.offsetWidth; // restart the animation
            el.classList.add("rf-flash");
            el.addEventListener("animationend", function () { el.classList.remove("rf-flash"); }, { once: true });
        },

        stamp(sel) {
            var el = qs(sel);
            if (!el) return;
            var now = new Date();
            el.textContent = "Updated " + now.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
            el.title = now.toString();
        },
    };

    /* ==================== Formatted number fields ====================
     * .dc-num wraps a text input (not type=number, which follows the page
     * locale and shows "0,9"). Values display with en-US grouping ("12,000"),
     * show raw while focused, step with the -/+ buttons or arrow keys, and
     * accept "12.000" / "0,9" as typed by an id-ID keyboard. */

    var NumField = {
        decimals(wrap) { return parseInt(wrap.dataset.decimals || "0", 10); },

        parse(raw, decimals) {
            var t = String(raw === null || raw === undefined ? "" : raw).replace(/\s/g, "");
            if (!t) return NaN;
            if (decimals > 0) {
                if (t.indexOf(".") === -1) t = t.replace(",", ".");
                t = t.replace(/,/g, "");
            } else {
                t = t.replace(/[.,]/g, "");
            }
            return /^-?\d*\.?\d+$/.test(t) ? Number(t) : NaN;
        },

        format(n, decimals) {
            if (!Number.isFinite(n)) return "";
            return n.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
        },

        human(format, n) {
            if (!Number.isFinite(n)) return "";
            if (format === "duration-ms") {
                return n < 1000 ? "under a second" : "= " + NumField.duration(n / 1000);
            }
            if (format === "duration-s") return "= " + NumField.duration(n);
            if (format === "percent") return "Requires \u2265 " + Math.round(n * 100) + "% confidence";
            return "";
        },

        duration(sec) {
            if (sec < 60) {
                var v = Math.round(sec * 10) / 10;
                return v + (v === 1 ? " second" : " seconds");
            }
            var m = Math.floor(sec / 60), r = Math.round(sec % 60);
            return m + " min" + (r ? " " + r + " s" : "");
        },

        value(input) {
            var wrap = input.closest(".dc-num");
            return NumField.parse(input.value, wrap ? NumField.decimals(wrap) : 0);
        },

        set(input, n) {
            var wrap = input.closest(".dc-num");
            input.value = NumField.format(n, wrap ? NumField.decimals(wrap) : 0);
            NumField.sync(input);
        },

        /* Refresh the human-readable hint and the confidence slider. */
        sync(input) {
            var wrap = input.closest(".dc-num");
            var n = NumField.value(input);
            var hint = document.querySelector('.dc-human[data-for="' + input.id + '"]');
            if (hint && wrap) hint.textContent = NumField.human(wrap.dataset.format, n);
            if (input.id === "dc-confidence") {
                var range = qs("#dc-confidence-range");
                if (range && Number.isFinite(n)) range.value = String(n);
            }
        },

        step(input, dir) {
            var wrap = input.closest(".dc-num");
            var min = Number(wrap.dataset.min), max = Number(wrap.dataset.max), step = Number(wrap.dataset.step) || 1;
            var n = NumField.value(input);
            if (!Number.isFinite(n)) n = min;
            // Snap to the step grid, then move one step.
            n = Math.round((Math.round(n / step) * step + dir * step) * 1e6) / 1e6;
            n = Math.min(max, Math.max(min, n));
            NumField.set(input, n);
            input.dispatchEvent(new Event("input", { bubbles: true }));
        },

        bind(root) {
            root.querySelectorAll(".dc-num").forEach(function (wrap) {
                var input = wrap.querySelector("input");
                if (!input) return;
                input.addEventListener("focus", function () {
                    var n = NumField.value(input);
                    if (Number.isFinite(n)) input.value = String(n);
                });
                input.addEventListener("blur", function () {
                    var n = NumField.value(input);
                    if (Number.isFinite(n)) NumField.set(input, n);
                });
                input.addEventListener("input", function () { NumField.sync(input); });
                input.addEventListener("keydown", function (e) {
                    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
                        e.preventDefault();
                        NumField.step(input, e.key === "ArrowUp" ? 1 : -1);
                    }
                });
                wrap.querySelectorAll(".dc-step").forEach(function (b) {
                    b.addEventListener("click", function () { NumField.step(input, Number(b.dataset.dir)); });
                });
            });
            var range = qs("#dc-confidence-range"), conf = qs("#dc-confidence");
            if (range && conf) range.addEventListener("input", function () {
                NumField.set(conf, Number(range.value));
                conf.dispatchEvent(new Event("input", { bubbles: true }));
            });
        },
    };

    /* ==================== DMSS display helpers ==================== */

    function fmtNum(n) {
        return (n === null || n === undefined || !Number.isFinite(Number(n))) ? "—" : Number(n).toLocaleString("en-US");
    }

    function fmtPct(rate) {
        return (rate === null || rate === undefined) ? "—" : (Math.round(rate * 1000) / 10) + "%";
    }

    /* "2026-10-16 09:41:12" / ISO -> "Today 09:41" | "16 Oct 09:41" (full value in title). */
    function parseWhen(s) {
        if (!s) return null;
        var d = new Date(String(s).replace(" ", "T"));
        return isNaN(d.getTime()) ? null : d;
    }
    function fmtWhen(s) {
        var d = parseWhen(s);
        if (!d) return s ? String(s) : "—";
        var time = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
        var today = new Date();
        if (d.toDateString() === today.toDateString()) return "Today " + time;
        return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" }) + " " + time;
    }

    /* snake_case API values -> readable labels; acronyms stay upper-case. */
    function humanize(v) {
        if (v === null || v === undefined || v === "") return "—";
        var s = String(v).replace(/_/g, " ").toLowerCase()
            .replace(/\b(hmads|dmss|decim)\b/g, function (m) { return m.toUpperCase(); });
        return s.charAt(0).toUpperCase() + s.slice(1);
    }

    /* Semantic tone for a decision / agreement value. */
    function toneOf(v) {
        var s = String(v || "").toLowerCase();
        if (!s) return "muted";
        if (/(block|deny|denied|unsafe|danger|critical|error|disagree)/.test(s)) return "err";
        if (/(review|approv|warn|stricter|caution|ask)/.test(s)) return "warn";
        if (/(allow|safe|agree|pass|ok)/.test(s)) return "ok";
        return "muted";
    }

    function pill(v) {
        if (v === null || v === undefined || v === "") return '<span class="dm-none">—</span>';
        return '<span class="dm-pill dm-tone-' + toneOf(v) + '">' + esc(humanize(v)) + "</span>";
    }

    function fmtMs(ms) {
        if (ms === null || ms === undefined || !Number.isFinite(Number(ms))) return "";
        ms = Number(ms);
        return ms < 1000 ? fmtNum(Math.round(ms)) + " ms" : (Math.round(ms / 100) / 10) + " s";
    }

    function sub(text) { return text ? '<span class="dm-sub">' + esc(text) + "</span>" : ""; }

    /* Final verdict in user terms: Allowed / Needs approval / Blocked. */
    var VERDICTS = {
        safe: ["Allowed", "ok"], warning: ["Allowed", "ok"],
        requires_approval: ["Needs approval", "warn"], dangerous: ["Blocked", "err"],
    };
    function verdictPill(level) {
        var v = VERDICTS[level];
        if (!v) return '<span class="dm-none">—</span>';
        return '<span class="dm-pill dm-tone-' + v[1] + '">' + v[0] + "</span>";
    }
    function decidedLabel(e) { return e.decided_by === "dmss" ? "DMSS" : "HMADS"; }

    /* HMADS rule output: verdict, matched categories, time. */
    function hmadsCell(e) {
        var cats = String(e.deterministic_categories || "").split(",").filter(Boolean).map(humanize).join(", ");
        return pill(e.deterministic_level) + sub(cats) + sub(fmtMs(e.hmads_duration_ms));
    }

    /* DMSS output: a result, a failure, or not checked at all. */
    function dmssResult(e, hideConfidence) {
        if (e.model_decision) {
            var conf = (hideConfidence || e.model_confidence == null) ? "" :
                '<span class="dm-conf">' + Math.round(Number(e.model_confidence) * 100) + "%</span>";
            return pill(e.model_decision) + conf;
        }
        if (e.fallback_reason) {
            return '<span class="dm-fallback">Failed</span><span class="dm-conf">' + esc(humanize(e.fallback_reason)) + "</span>";
        }
        return '<span class="dm-none">Not checked</span>';
    }
    function dmssChecked(e) { return !!(e.model_decision || e.fallback_reason); }
    function dmssCell(e) {
        if (!dmssChecked(e)) return dmssResult(e);
        return dmssResult(e) + sub(e.mode === "shadow" ? "shadow" : "") + sub(fmtMs(e.model_latency_ms));
    }

    function confirmDialog(opts) {
        if (typeof window.showConfirm === "function") return window.showConfirm(opts);
        return Promise.resolve(window.confirm(opts.message));
    }

    function notify(msg, type) {
        if (window.toast && toast.show) toast.show(msg, type);
        else if (window.evToast) evToast(msg, type);
    }

    /* ==================== DMSS pane: migrated dashboard ====================
     * Same data surface as the legacy /system/decim-safety page
     * (health / summary / events / telemetry-clear), restyled with the
     * shared tokens and with explicit loading / error / empty states. */

    window.safetyDmss = {
        API: "/api/admin/decim-safety",
        _initialized: false,
        _timer: null,
        _config: null,
        state: { limit: 50, offset: 0, total: 0 },

        init() {
            if (this._initialized) return;
            this._initialized = true;

            var refresh = qs("#dmss-refresh");
            if (refresh) refresh.addEventListener("click", function () {
                RefreshButton.run(refresh, function () { return window.safetyDmss.refresh(); });
            });
            var clear = qs("#dmss-clear");
            if (clear) clear.addEventListener("click", function () { window.safetyDmss.clearTelemetry(); });
            var retry = qs("#dmss-retry");
            if (retry) retry.addEventListener("click", function () { window.safetyDmss.refresh(); });
            ["#f-window", "#f-agent", "#f-tool", "#f-decided", "#f-verdict"].forEach(function (sel) {
                var el = qs(sel);
                if (el) el.addEventListener("change", function () {
                    window.safetyDmss.state.offset = 0;
                    window.safetyDmss.refresh();
                });
            });
            var form = qs("#dmss-config");
            if (form) this._bindConfig(form);

            this.refresh(); // also loads the config form
            this._timer = setInterval(function () {
                if (!document.hidden) window.safetyDmss.loadHealth();
            }, 30000);
        },

        _field(sel) { return qs(sel); },

        /* ---------- Provider configuration: per-field auto-save ----------
         * Each field PUTs only its own key (the API merges onto the stored
         * settings). Saves run one at a time so two quick edits can't race
         * each other's read-modify-write on the server. Feedback mirrors the
         * System Settings AutoSave: spinner -> check beside the field label. */
        CONFIG_FIELDS: {
            provider: { sel: "#dc-provider", key: "provider", kind: "select" },
            endpoint: { sel: "#dc-endpoint", key: "provider_endpoint", kind: "text" },
            timeout: { sel: "#dc-timeout", key: "request_timeout_ms", kind: "num" },
            confidence: { sel: "#dc-confidence", key: "minimum_confidence", kind: "num" },
            payload: { sel: "#dc-payload", key: "max_payload_chars", kind: "num" },
            failures: { sel: "#dc-failures", key: "circuit_breaker_failures", kind: "num" },
            cooldown: { sel: "#dc-cooldown", key: "circuit_breaker_cooldown_seconds", kind: "num" },
        },
        CONFIG_DEBOUNCE_MS: 800,
        _cfgTimers: {},
        _cfgPending: {},        // field -> true while a save for it is queued or in flight
        _cfgChain: null,

        _bindConfig(form) {
            var self = this;
            // Enter in a text field would submit the form: commit the field instead.
            form.addEventListener("submit", function (e) {
                e.preventDefault();
                var el = document.activeElement;
                if (el && form.contains(el) && el.blur) el.blur();
            });
            NumField.bind(form);
            Object.keys(this.CONFIG_FIELDS).forEach(function (name) {
                var f = self.CONFIG_FIELDS[name];
                var el = qs(f.sel);
                if (!el) return;
                if (f.kind === "select") {
                    el.addEventListener("change", function () { self._flushField(name); });
                } else {
                    // typing, -/+ steppers and the confidence slider all fire "input"
                    el.addEventListener("input", function () { self._queueField(name); });
                    el.addEventListener("blur", function () { self._flushField(name); });
                }
            });

            // One-click connectivity check beside the endpoint field (task #895).
            var testBtn = qs("#dc-endpoint-test");
            if (testBtn) {
                testBtn.addEventListener("click", function () { self.testEndpoint(); });
            }
            // A new URL invalidates the previous verdict.
            var endpointInput = qs("#dc-endpoint");
            if (endpointInput) {
                endpointInput.addEventListener("input", function () { self._clearEndpointTest(); });
            }
        },

        /* ---------- Endpoint connectivity test (task #895) ----------
         * POSTs the value currently in the field, so an unsaved edit is tested
         * exactly as typed. The probe is diagnostic only: production decisions,
         * telemetry and the production circuit breaker are untouched. */
        _endpointTesting: false,

        _clearEndpointTest() {
            var out = qs("#dc-endpoint-test-result");
            if (!out) return;
            out.hidden = true;
            out.className = "dc-test-result";
            out.innerHTML = "";
        },

        /* state: "pending" | "ok" | "err" */
        _renderEndpointTest(state, message, detail) {
            var out = qs("#dc-endpoint-test-result");
            if (!out) return;
            out.hidden = false;
            out.className = "dc-test-result is-" + state;
            if (state === "pending") {
                out.innerHTML = '<div class="dc-test-line"><span class="spinner"></span><span class="dc-test-msg">' +
                    esc(message) + "</span></div>";
                return;
            }
            var badge = state === "ok" ? "sf-badge-ok" : "sf-badge-err";
            var label = state === "ok" ? "Success" : "Failed";
            out.innerHTML =
                '<div class="dc-test-line"><span class="sf-badge ' + badge + '">' + label + "</span>" +
                '<span class="dc-test-msg">' + esc(message) + "</span></div>" +
                (detail ? '<div class="dc-test-meta">' + esc(detail) + "</div>" : "");
        },

        testEndpoint() {
            if (this._endpointTesting) return;
            var input = qs("#dc-endpoint");
            var btn = qs("#dc-endpoint-test");
            var value = input ? input.value.trim() : "";
            var invalid = value ? this._fieldError("endpoint", value) : "";
            if (invalid) {
                this._setConfigError("endpoint", invalid);
                this._renderEndpointTest("err", invalid);
                if (input) input.focus();
                return;
            }
            this._setConfigError("endpoint", "");

            var self = this;
            this._endpointTesting = true;
            if (btn) { btn.disabled = true; btn.classList.add("is-busy"); }
            this._renderEndpointTest("pending", "Checking the provider endpoint\u2026");

            fetch(this.API + "/endpoint/test", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                // An empty field means "test whatever is configured" (the probe
                // then reads the saved value, including any environment default).
                body: JSON.stringify(value ? { endpoint: value } : {}),
            }).then(function (r) {
                // A non-JSON body (for example the HTML 404 returned when the API
                // route is missing from the running build) must never be reported
                // as a network failure: keep the status code so the UI can explain
                // what actually happened.
                return r.text().then(function (text) {
                    var parsed = null;
                    try { parsed = text ? JSON.parse(text) : null; } catch (err) { parsed = null; }
                    return { ok: r.ok, status: r.status, data: parsed, json: parsed !== null };
                });
            }).then(function (res) {
                self._endpointTesting = false;
                if (btn) { btn.disabled = false; btn.classList.remove("is-busy"); }
                var d = res.data || {};
                if (!res.ok || !res.json) {
                    if (res.status === 429) {
                        self._renderEndpointTest("err",
                            "Rate limit exceeded \u2014 try again in " + (d.retry_after || "a few") +
                            " s (max 10 tests/min).");
                    } else if (!res.json) {
                        self._renderEndpointTest("err",
                            "Endpoint test API answered HTTP " + res.status +
                            " without JSON \u2014 Evonic may still be running an older build. Restart the service and try again.");
                    } else {
                        self._renderEndpointTest("err",
                            d.error || ("Endpoint test failed (HTTP " + res.status + ")."));
                    }
                    return;
                }
                var meta = [];
                if (d.latency_ms != null) meta.push("latency " + fmtNum(d.latency_ms) + " ms");
                if (d.decision) meta.push("decision " + d.decision);
                if (d.confidence != null) meta.push("confidence " + (Math.round(d.confidence * 100) / 100));
                if (d.model) meta.push("model " + d.model);
                if (!d.ok && d.fallback_reason) meta.push("reason " + d.fallback_reason);
                self._renderEndpointTest(d.ok ? "ok" : "err",
                    d.message || (d.ok ? "Endpoint healthy." : "Endpoint test failed."),
                    meta.join(" \u00b7 "));
            }).catch(function (e) {
                console.error("DMSS endpoint test failed:", e);
                self._endpointTesting = false;
                if (btn) { btn.disabled = false; btn.classList.remove("is-busy"); }
                self._renderEndpointTest("err",
                    "Network error \u2014 the Evonic API could not be reached, so the provider endpoint was never contacted.");
            });
        },

        _fieldValue(name) {
            var f = this.CONFIG_FIELDS[name], el = qs(f.sel);
            if (!el) return undefined;
            return f.kind === "num" ? NumField.value(el) : el.value.trim();
        },

        /* Field-level rule; returns an error message or "". */
        _fieldError(name, v) {
            function intIn(lo, hi) { return Number.isInteger(v) && v >= lo && v <= hi; }
            switch (name) {
                case "provider": return v === "systemone" ? "" : "Choose a registered provider.";
                case "endpoint": return (!v || /^https?:\/\/[^/]+/i.test(v)) ? "" : "Enter an absolute http(s) URL.";
                case "timeout": return intIn(100, 10000) ? "" : "Use 100–10,000 ms.";
                case "confidence": return (Number.isFinite(v) && v >= 0 && v <= 1) ? "" : "Use a value from 0 to 1.";
                case "payload": return intIn(1, 120000) ? "" : "Use 1–120,000 characters.";
                case "failures": return intIn(1, 20) ? "" : "Use 1–20 failures.";
                case "cooldown": return intIn(1, 3600) ? "" : "Use 1–3,600 seconds.";
            }
            return "";
        },

        _sameAsSaved(name, v) {
            var saved = (this._config || {})[this.CONFIG_FIELDS[name].key];
            if (name === "endpoint") return (v || "") === (saved || "");
            if (name === "provider") return v === saved;
            return Number.isFinite(v) && Number(saved) === v;
        },

        _queueField(name) {
            var self = this;
            clearTimeout(this._cfgTimers[name]);
            this._cfgTimers[name] = setTimeout(function () { self._flushField(name); }, this.CONFIG_DEBOUNCE_MS);
        },

        _flushField(name) {
            clearTimeout(this._cfgTimers[name]);
            delete this._cfgTimers[name];
            if (!this._config) return; // not loaded yet: nothing to compare against
            var v = this._fieldValue(name);
            var err = this._fieldError(name, v);
            if (err) {
                this._setConfigError(name, err);
                this._cfgStatus(name, "error");
                return;
            }
            this._setConfigError(name, "");
            if (this._sameAsSaved(name, v)) {
                this._cfgStatus(name, this._cfgStatusState(name) === "error" ? null : undefined);
                return;
            }
            this._saveField(name, v);
        },

        _saveField(name, value) {
            var self = this, f = this.CONFIG_FIELDS[name];
            var body = {};
            body[f.key] = value;
            this._cfgPending[name] = true;
            this._cfgStatus(name, "saving");
            var run = function () {
                return fetch("/api/settings/decim-safety", {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                }).then(function (r) {
                    return r.json().then(function (d) { return { ok: r.ok, data: d }; });
                }).then(function (res) {
                    if (!res.ok || !res.data || !res.data.success) {
                        var e = new Error((res.data && res.data.error) || "Could not save.");
                        e.fromServer = true;
                        throw e;
                    }
                    self._config = res.data.settings || self._config;
                    delete self._cfgPending[name];
                    // Echo server normalisation unless the user is editing this field again.
                    var el = qs(f.sel);
                    if (el && document.activeElement !== el && !self._cfgTimers[name]) {
                        self._applyField(name, self._config[f.key]);
                    }
                    self._cfgStatus(name, "saved");
                    self.loadHealth();
                    if (window.safetyTester) window.safetyTester._updateCount();
                }).catch(function (e) {
                    delete self._cfgPending[name];
                    console.error("Failed to save DMSS " + f.key + ":", e);
                    self._setConfigError(name, e.fromServer ? e.message : "Network error — could not reach the server.");
                    self._cfgStatus(name, "error");
                    notify("Failed to save DMSS configuration", "error");
                });
            };
            this._cfgChain = (this._cfgChain || Promise.resolve()).then(run, run);
        },

        _cfgStatusState(name) {
            var slot = qs("#dc-status-" + name);
            if (!slot) return null;
            return slot.classList.contains("is-error") ? "error" : slot.classList.contains("is-saving") ? "saving" : null;
        },

        /* state: "saving" | "saved" | "error" | null (clear) | undefined (leave as is) */
        _cfgStatus(name, state) {
            if (state === undefined) return;
            var slot = qs("#dc-status-" + name);
            if (!slot) return;
            clearTimeout(slot._fadeTimer);
            slot.classList.remove("is-saving", "is-saved", "is-error");
            slot.innerHTML = "";
            if (state === "saving") {
                slot.classList.add("is-saving");
                slot.innerHTML = '<span class="spinner"></span>';
            } else if (state === "saved") {
                slot.classList.add("is-saved");
                slot.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path class="st-check-path" d="M4 12.5l5 5L20 6.5"/></svg>';
                slot._fadeTimer = setTimeout(function () { slot.classList.remove("is-saved"); slot.innerHTML = ""; }, 1800);
            } else if (state === "error") {
                slot.classList.add("is-error");
                slot.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>';
            }
        },

        _setConfigError(field, message) {
            var el = qs("#dc-err-" + field);
            if (!el) return;
            el.hidden = !message;
            el.textContent = message || "";
            var input = qs("#dc-" + field);
            if (input) {
                if (message) input.setAttribute("aria-invalid", "true");
                else input.removeAttribute("aria-invalid");
            }
        },

        _applyField(name, v) {
            var f = this.CONFIG_FIELDS[name], el = qs(f.sel);
            if (!el) return;
            if (f.kind === "num") NumField.set(el, Number(v));
            else el.value = (v === null || v === undefined) ? "" : v;
        },

        /* Reflect server state, skipping any field the user is editing, has
         * queued/in-flight, or left with an error to fix. */
        _applyConfig(s) {
            var self = this;
            Object.keys(this.CONFIG_FIELDS).forEach(function (name) {
                var el = qs(self.CONFIG_FIELDS[name].sel);
                var busy = (el && document.activeElement === el) || self._cfgTimers[name] ||
                    self._cfgPending[name] || self._cfgStatusState(name) === "error";
                if (!busy) self._applyField(name, s[self.CONFIG_FIELDS[name].key]);
            });
        },

        loadConfig() {
            var self = this;
            return fetch("/api/settings/decim-safety").then(function (r) { if (!r.ok) throw new Error(); return r.json(); }).then(function (data) {
                self._config = data.settings || {};
                self._applyConfig(self._config);
            }).catch(function () { window.safetyDmss._setError(true, "Failed to load DMSS configuration."); return false; });
        },

        _filters() {
            var p = new URLSearchParams();
            var w = qs("#f-window");
            var v = w ? parseInt(w.value, 10) : 168;
            p.set("window_hours", String(isNaN(v) ? 168 : v));
            var a = qs("#f-agent");
            if (a && a.value) p.set("agent_id", a.value);
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

        /* Resolves to true only when every section loaded. */
        refresh() {
            // Reload the config form too; fields being edited are left alone.
            var tasks = [this.loadHealth(), this.loadAgents(), this.loadSummary(), this.loadEvents(), this.loadConfig()];
            return Promise.all(tasks).then(function (res) {
                var ok = res.every(function (r) { return r !== false; });
                if (ok) RefreshButton.stamp("#dmss-updated");
                return ok;
            });
        },

        loadHealth() {
            var card = qs("#kpi-status-card");
            function text(sel, v) { var el = qs(sel); if (el) el.textContent = v; }
            return fetch(this.API + "/health").then(function (r) {
                if (!r.ok) throw new Error("HTTP " + r.status);
                return r.json();
            }).then(function (d) {
                window.safetyDmss._setError(false);
                var mode = d.enabled ? (d.mode || "off") : "off";
                var state = mode === "enforce" ? "enforce" : (mode === "shadow" ? "shadow" : "off");
                if (card) card.dataset.state = state;
                text("#kpi-enabled", { off: "Off", shadow: "Shadow", enforce: "Enforcing" }[state]);
                text("#kpi-mode", {
                    off: "HMADS decides every command",
                    shadow: "Recorded only, HMADS decides",
                    enforce: "Model decides, HMADS fallback",
                }[state]);
                var provider = qs("#dc-provider");
                var providerOpt = provider && Array.prototype.find.call(provider.options, function (o) { return o.value === d.provider; });
                text("#kpi-provider", providerOpt ? providerOpt.textContent : (d.provider || "—"));
                var circuit = (d.circuit_state || "closed").toLowerCase();
                var cb = qs("#kpi-circuit");
                if (cb) {
                    cb.textContent = "Circuit " + circuit;
                    cb.className = "sf-badge " + (circuit === "open" ? "sf-badge-err" : (circuit === "half-open" ? "sf-badge-warn" : "sf-badge-ok"));
                }
                var latency = d.latency_ms || {};
                text("#kpi-latency", latency.p50 == null ? "—" : fmtNum(latency.p50) + " ms");
                var last = qs("#kpi-last-call");
                if (last) {
                    var parts = [];
                    if (latency.p95 != null) parts.push("p95 " + fmtNum(latency.p95) + " ms");
                    parts.push(d.last_decision_at ? "last call " + fmtWhen(d.last_decision_at) : "no calls yet");
                    last.textContent = parts.join(" · ");
                    last.title = d.last_decision_at ? "Last successful call: " + d.last_decision_at : "";
                }
                text("#kpi-fallback", fmtPct(d.fallback_rate_24h));
            }).catch(function () {
                if (card) card.dataset.state = "error";
                text("#kpi-enabled", "Unreachable");
                text("#kpi-mode", "Health check failed");
                window.safetyDmss._setError(true, "Health check failed — the Decim Safety API is unreachable.");
                return false;
            });
        },

        loadSummary() {
            var p = this._filters();
            return fetch(this.API + "/summary?" + p.toString()).then(function (r) {
                if (!r.ok) throw new Error("HTTP " + r.status);
                return r.json();
            }).then(function (d) {
                function text(sel, v) { var el = qs(sel); if (el) el.textContent = v; }
                text("#kpi-total", fmtNum(d.total_comparisons || 0));
                text("#kpi-accepted", fmtNum(d.decim_accepted || 0) + " accepted · " + fmtNum(d.fallback_count || 0) + " fallback");
                text("#kpi-unsafe",
                    (d.unsafe && d.unsafe.final != null)
                        ? (fmtNum(d.unsafe.final) + " unsafe (" + fmtPct(d.unsafe.final_rate || 0) + ")")
                        : "—");
                var verdicts = d.final_verdicts || {};
                var decided = d.decided_by || {};
                var total = d.total_comparisons || 0;
                text("#stat-total", fmtNum(total));
                text("#stat-blocked", fmtNum(verdicts.blocked || 0));
                text("#stat-approval", fmtNum(verdicts.approval || 0));
                text("#stat-decided", total
                    ? "DMSS " + fmtPct((decided.dmss || 0) / total) + " · HMADS " + fmtPct((decided.hmads || 0) / total)
                    : "—");
            }).catch(function (e) {
                console.error("DMSS summary failed:", e);
                return false;
            });
        },

        /* Agent filter options: agents with recorded events; keeps the selection. */
        loadAgents() {
            var select = qs("#f-agent");
            if (!select) return Promise.resolve(true);
            return fetch(this.API + "/agents").then(function (r) {
                if (!r.ok) throw new Error("HTTP " + r.status);
                return r.json();
            }).then(function (d) {
                var current = select.value;
                select.innerHTML = '<option value="">Any</option>' + (d.agents || []).map(function (a) {
                    return '<option value="' + esc(a.id) + '">' + esc(a.name || a.id) + "</option>";
                }).join("");
                select.value = current;
                if (select.value !== current) select.value = "";
            }).catch(function (e) {
                console.error("DMSS agents failed:", e);
                return false;
            });
        },

        loadEvents() {
            var p = this._filters();
            p.set("limit", String(this.state.limit));
            p.set("offset", String(this.state.offset));
            var decided = qs("#f-decided");
            if (decided && decided.value) p.set("decided_by", decided.value);
            var verdict = qs("#f-verdict");
            if (verdict && verdict.value) p.set("verdict", verdict.value);
            var body = qs("#tbl-events-body");
            if (!body) return Promise.resolve(true);

            return fetch(this.API + "/events?" + p.toString()).then(function (r) {
                if (!r.ok) throw new Error("HTTP " + r.status);
                return r.json();
            }).then(function (d) {
                window.safetyDmss._setError(false);
                window.safetyDmss.state.total = d.total || 0;
                var count = qs("#dmss-events-count");
                if (count) count.textContent = d.total ? fmtNum(d.total) + (d.total === 1 ? " event" : " events") : "";
                var events = d.events || [];
                var filtered = !!(verdict && verdict.value);
                if (!events.length) {
                    body.innerHTML = '<tr><td colspan="7" class="sf-empty-cell dm-events-empty">' +
                        "<strong>No events in this window</strong>" +
                        "<span>" + (filtered
                            ? "Only commands matching the Verdict filter are shown. Set it to “Any” to see every check."
                            : "Checks appear here once agents run bash or python commands.") + "</span></td></tr>";
                } else {
                    body.innerHTML = events.map(function (e, index) {
                        var agent = e.agent_name || e.agent_id;
                        var command = e.detection_command || "";
                        var note = "by " + decidedLabel(e) + (e.disposition === "execution_failed" ? " · execution failed" : "");
                        return '<tr data-event-index="' + index + '" tabindex="0" title="Show details">' +
                            '<td class="dm-when" title="' + esc(e.occurred_at) + '">' + esc(fmtWhen(e.occurred_at)) + "</td>" +
                            "<td>" + (agent ? esc(agent) : '<span class="dm-none">—</span>') + "</td>" +
                            '<td class="dm-col-cmd"><span class="dm-tool">' + esc(e.tool_type || "—") + "</span>" +
                                (command ? '<code class="dm-cmd" title="' + esc(command) + '">' + esc(command) + "</code>"
                                         : '<span class="dm-sub">Not recorded</span>') + "</td>" +
                            "<td>" + hmadsCell(e) + "</td>" +
                            "<td>" + dmssCell(e) + "</td>" +
                            "<td>" + verdictPill(e.final_level) + sub(note) + "</td>" +
                            '<td class="dm-col-go"><i data-lucide="chevron-right"></i></td>' +
                            "</tr>";
                    }).join("");
                    if (window.lucide) lucide.createIcons();
                }
                Array.prototype.forEach.call(body.querySelectorAll("tr[data-event-index]"), function (row) {
                    function open() {
                        var event = events[Number(row.dataset.eventIndex)];
                        if (!event || !window.safetyActivityModal) return;
                        window.safetyActivityModal.open(event);
                    }
                    row.addEventListener("click", open);
                    row.addEventListener("keydown", function (ev) {
                        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
                    });
                });
                window.safetyDmss._renderPager();
            }).catch(function (e) {
                console.error("DMSS events failed:", e);
                body.innerHTML = '<tr><td colspan="7" class="sf-empty-cell sf-empty-cell-err">Failed to load events.</td></tr>';
                window.safetyDmss._setError(true, "Failed to load the activity table — check the Decim Safety service and retry.");
                return false;
            });
        },

        _renderPager() {
            var el = qs("#dmss-pager");
            if (!el) return;
            var total = this.state.total;
            var limit = this.state.limit;
            var start = total ? this.state.offset + 1 : 0;
            var end = Math.min(this.state.offset + limit, total);
            el.hidden = total <= limit;
            el.innerHTML =
                '<span class="dm-pager-range">' + fmtNum(start) + "–" + fmtNum(end) + " of " + fmtNum(total) + "</span>" +
                '<button type="button" class="hm-icon-btn" id="dmss-pg-prev" aria-label="Previous page"' + (this.state.offset <= 0 ? " disabled" : "") + '><i data-lucide="chevron-left"></i></button>' +
                '<button type="button" class="hm-icon-btn" id="dmss-pg-next" aria-label="Next page"' + (end >= total ? " disabled" : "") + '><i data-lucide="chevron-right"></i></button>';
            if (window.lucide) lucide.createIcons();
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
            confirmDialog({
                title: "Clear Telemetry",
                message: "Delete all DMSS telemetry records and statistics? The provider configuration is not changed. This cannot be undone.",
                confirmText: "Clear telemetry",
                danger: true,
            }).then(function (ok) {
                if (!ok) return;
                return fetch(self.API + "/telemetry/clear", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                }).then(function (r) { return r.json(); }).then(function (d) {
                    if (!d.success) throw new Error("clear failed");
                    notify("Cleared " + fmtNum(d.deleted || 0) + " telemetry record" + (d.deleted === 1 ? "" : "s"), "success");
                    self.state.offset = 0;
                    self.refresh();
                });
            }).catch(function () {
                notify("Failed to clear telemetry", "error");
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
            // Shortcut hint matches the platform's modifier key.
            var mod = qs(".dm-kbd-hint kbd");
            if (mod && !/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)) mod.textContent = "Ctrl";
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

    /* ==================== Activity detail modal ====================
     * Renders a single DMSS event: the recorded command preview, how it was
     * detected (deterministic rules + model) and what the final decision was.
     * The row object from /events is rendered immediately, then replaced by
     * GET /events/<id> once the authoritative record arrives. */

    function setText(id, value) {
        var el = qs("#" + id);
        if (!el) return;
        el.textContent = (value === null || value === undefined || value === "") ? "\u2014" : String(value);
    }

    function setHtml(id, html) {
        var el = qs("#" + id);
        if (el) el.innerHTML = html;
    }

    /* "privilege_escalation,sandbox_escape" -> readable chips. */
    function categoryChips(value) {
        if (!value) return '<span class="dm-none">\u2014</span>';
        var chips = String(value).split(",").map(function (c) {
            var v = c.trim();
            return v ? '<span class="dm-tool">' + esc(humanize(v)) + "</span>" : "";
        });
        return chips.filter(Boolean).join(" ") || '<span class="dm-none">\u2014</span>';
    }

    function fallbackCopy(text) {
        try {
            var ta = document.createElement("textarea");
            ta.value = text;
            ta.setAttribute("readonly", "");
            ta.style.position = "fixed";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            var ok = document.execCommand("copy");
            document.body.removeChild(ta);
            return ok;
        } catch (e) {
            return false;
        }
    }

    window.safetyActivityModal = {
        _el: null,
        _lastFocus: null,
        _current: null,
        _detailSeq: 0,

        init() {
            var modal = qs("#dmss-activity-modal");
            if (!modal) return;
            this._el = modal;
            var close = qs("#dmss-modal-close");
            if (close) close.addEventListener("click", function () { window.safetyActivityModal.close(); });
            var copy = qs("#dmss-command-copy");
            if (copy) copy.addEventListener("click", function () { window.safetyActivityModal.copyCommand(); });
            modal.addEventListener("click", function (e) {
                if (e.target === modal) window.safetyActivityModal.close();
            });
            document.addEventListener("keydown", function (e) {
                var m = window.safetyActivityModal._el;
                if (!m || !m.classList.contains("active")) return;
                if (e.key === "Escape") {
                    e.preventDefault();
                    window.safetyActivityModal.close();
                    return;
                }
                if (e.key === "Tab") {
                    var f = m.querySelectorAll("button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex='-1'])");
                    if (!f.length) return;
                    var first = f[0], last = f[f.length - 1];
                    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
                    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
                }
            });
        },

        /* Legacy 4-argument calls still work; the row handler passes the event. */
        open(record, timestamp, detectionType, detectionCommand) {
            var modal = this._el || qs("#dmss-activity-modal");
            if (!modal) return;
            this._el = modal;
            var event = (record && typeof record === "object")
                ? record
                : { id: null, occurred_at: timestamp, detection_type: detectionType, detection_command: detectionCommand };
            this._current = event;
            this._lastFocus = document.activeElement;
            this._detailSeq += 1;
            this.render(event);
            this._setStatus("", false);
            this._setError("", false);
            modal.classList.add("active");
            modal.setAttribute("aria-hidden", "false");
            document.body.classList.add("dm-modal-open");
            var close = qs("#dmss-modal-close");
            if (close) close.focus();
            if (event.id) this.loadDetail(event.id);
        },

        close() {
            var modal = this._el || qs("#dmss-activity-modal");
            if (!modal) return;
            modal.classList.remove("active");
            modal.setAttribute("aria-hidden", "true");
            document.body.classList.remove("dm-modal-open");
            this._detailSeq += 1;
            if (this._lastFocus && typeof this._lastFocus.focus === "function") this._lastFocus.focus();
            this._lastFocus = null;
        },

        loadDetail(id) {
            var self = this;
            var seq = ++this._detailSeq;
            this._setStatus("Loading the full event record\u2026", true);
            fetch("/api/admin/decim-safety/events/" + encodeURIComponent(id), {
                headers: { "Accept": "application/json" },
            }).then(function (r) {
                if (!r.ok) throw new Error("HTTP " + r.status);
                return r.json();
            }).then(function (d) {
                if (seq !== self._detailSeq) return;
                self._setStatus("", false);
                if (d && d.id) { self._current = d; self.render(d); }
            }).catch(function (err) {
                if (seq !== self._detailSeq) return;
                console.error("DMSS event detail failed:", err);
                self._setStatus("", false);
                self._setError("Could not load the stored event record \u2014 showing the values from the activity list.", true);
            });
        },

        _setStatus(message, visible) {
            var el = qs("#dmss-modal-status");
            if (!el) return;
            el.textContent = message || "";
            el.hidden = !visible;
        },

        _setError(message, visible) {
            var el = qs("#dmss-modal-error");
            if (!el) return;
            el.textContent = message || "";
            el.hidden = !visible;
        },

        render(e) {
            e = e || {};

            /* --- Command --- */
            var command = e.detection_command ? String(e.detection_command) : "";
            var pre = qs("#dmss-command-text");
            var empty = qs("#dmss-command-empty");
            if (pre) { pre.textContent = command; pre.hidden = !command; }
            if (empty) empty.hidden = !!command;
            var copy = qs("#dmss-command-copy");
            if (copy) copy.disabled = !command;

            var metaEl = qs("#dmss-command-meta");
            if (metaEl) metaEl.textContent = e.command_length != null ? fmtNum(e.command_length) + " chars executed" : "";

            /* --- Summary --- */
            setText("dmss-agent", e.agent_name || e.agent_id || "Not recorded");
            setText("dmss-tool-type", e.tool_type);
            setText("dmss-occurred-at", e.occurred_at ? fmtWhen(e.occurred_at) + " \u00b7 " + e.occurred_at : "\u2014");
            setHtml("dmss-final-level", verdictPill(e.final_level) +
                (e.disposition === "execution_failed" ? ' <span class="dm-conf">execution failed</span>' : ""));
            setText("dmss-decided-by", decidedLabel(e) +
                (e.decided_by !== "dmss" && dmssChecked(e)
                    ? (e.mode === "shadow" ? " (DMSS in shadow mode)"
                        : e.model_decision ? " (stricter rule)" : " (DMSS failed)")
                    : ""));

            /* --- HMADS --- */
            setHtml("dmss-rule-verdict", pill(e.deterministic_level));
            setHtml("dmss-rule-categories", categoryChips(e.deterministic_categories));
            setText("dmss-rule-score", e.deterministic_score == null ? "\u2014" : fmtNum(e.deterministic_score));
            setText("dmss-rule-time", fmtMs(e.hmads_duration_ms) || "\u2014");

            /* --- DMSS --- */
            setHtml("dmss-model-decision", dmssResult(e, true) +
                (dmssChecked(e) && e.mode === "shadow" ? ' <span class="dm-conf">shadow</span>' : ""));
            setText("dmss-model-confidence", e.model_confidence == null ? "\u2014" : fmtPct(e.model_confidence));
            setText("dmss-model-latency", dmssChecked(e) ? (fmtMs(e.model_latency_ms) || "\u2014") : "\u2014");
            setText("dmss-model-id", e.model_id);

            /* --- Technical --- */
            setText("dmss-event-id", e.id || "Not recorded");
            setText("dmss-mode", humanize(e.mode));
            setText("dmss-policy-version", e.policy_version);
            setText("dmss-provider", e.provider_key);
        },

        copyCommand() {
            var text = this._current && this._current.detection_command;
            if (!text) return;
            var btn = qs("#dmss-command-copy");
            var done = function (ok) {
                if (btn) {
                    btn.textContent = ok ? "Copied" : "Copy failed";
                    setTimeout(function () { btn.textContent = "Copy"; }, 1600);
                }
                if (ok) notify("Command copied", "success");
            };
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text)
                    .then(function () { done(true); })
                    .catch(function () { done(fallbackCopy(text)); });
            } else {
                done(fallbackCopy(text));
            }
        },
    };

    /* ==================== Boot ==================== */

    function boot() {
        SafetyTabs.init();
        window.safetyTester.init();
        window.safetyActivityModal.init();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();
