/* ModelChain — reusable "main model + ordered fallbacks" card + picker.
 *
 * Self-contained: builds its own markup inside the root element and needs only
 * css/model-chain.css and ui-utils' shared tokens. Server-rendered pages emit
 * the root with the `model_chain` macro (templates/partials/model_chain.html);
 * JS-rendered pages create a root and pass the same fields as options.
 *
 *   const chain = new ModelChain(root, {
 *       title: "Explorer model", description: "…",
 *       fallbacks: 5,                          // fallback slot count
 *       mainLabel: "Main",                     // name of the first slot
 *       eligible: (model) => model.enabled,    // pickable models
 *       ineligibleLabel: "No vision",          // hint for enabled-but-rejected models
 *       requireMain: true,                     // false: saving nothing is allowed
 *       mainEmptyText: "Not set",              // text for an empty main slot
 *       inherit: {                             // optional "use the global chain" mode:
 *           label: "Global default model",     //   adds a source combobox (global / custom);
 *           note: "Uses the default model and fallbacks from System Settings.",
 *           href: "/system#general-models-routing",   // custom opens the picker
 *       },
 *       selectClass: "…",                      // extra classes for that combobox
 *       onSave: async ({ mainId, fallbackIds }) => ({ mainId, fallbackIds }),
 *   });
 *   chain.setModels(models);
 *   chain.setValue({ mainId, fallbackIds });   // mainId "" = inherit (if enabled)
 *   chain.setInherited({ mainId, fallbackIds }); // the chain inherit mode shows
 *
 * The card shows the main model on top and the fallbacks as pills below it
 * (only filled slots are shown). A pill can be removed in place (saved right
 * away); everything else goes through Manage, which opens a modal: search and
 * every model on the left, the selection in order on the right (first = main,
 * rest = fallbacks; reorder with the arrows or by dragging). onSave persists
 * the chain and returns what was stored, or throws with a message (shown in the
 * modal, or next to the Manage button for an inline removal).
 */
(function () {
    "use strict";

    const ICONS = {
        up: '<path d="m18 15-6-6-6 6"/>',
        down: '<path d="m6 9 6 6 6-6"/>',
        x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
        plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
        check: '<path d="M20 6 9 17l-5-5"/>',
        pencil: '<path d="M21.17 6.81a1 1 0 0 0-3.99-3.99L3.84 16.17a2 2 0 0 0-.5.83l-1.32 4.35a.5.5 0 0 0 .62.62l4.35-1.32a2 2 0 0 0 .83-.5z"/>',
        search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
        grip: '<circle cx="9" cy="6" r="1"/><circle cx="9" cy="12" r="1"/><circle cx="9" cy="18" r="1"/>'
            + '<circle cx="15" cy="6" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="15" cy="18" r="1"/>',
    };

    function icon(name) {
        return '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" '
            + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            + ICONS[name] + "</svg>";
    }

    function esc(value) {
        const div = document.createElement("div");
        div.textContent = value == null ? "" : String(value);
        return div.innerHTML;
    }

    let uid = 0;

    class ModelChain {
        constructor(root, options) {
            if (!root) throw new Error("ModelChain: root element is required");
            const opts = options || {};
            const data = root.dataset;
            this.root = root;
            this.uid = root.id || "model-chain-" + (++uid);
            this.title = opts.title || data.title || "Models";
            this.description = opts.description || data.description || "";
            this.fallbacks = Number(opts.fallbacks || data.fallbacks || 5);
            this.mainLabel = opts.mainLabel || data.mainLabel || "Main";
            this.mainEmptyText = opts.mainEmptyText || "Not set";
            this.requireMain = opts.requireMain !== false;
            this.eligible = opts.eligible || ((model) => !!model.enabled);
            // Hint for an enabled model that eligible() still rejects.
            this.ineligibleLabel = opts.ineligibleLabel || "Not supported";
            this.onSave = opts.onSave || (async (value) => value);
            this.inherit = opts.inherit || null;
            this.selectClass = opts.selectClass || "";

            this.models = [];
            this.value = { mainId: "", fallbackIds: [] };
            this.inherited = { mainId: "", fallbackIds: [] };
            this.loaded = false;
            this._busy = false;
            this.draft = [];
            this.query = "";

            this._build();
            this._bind();
        }

        roleLabel(index) {
            return index === 0 ? this.mainLabel : "Fallback " + index;
        }

        get capacity() {
            return this.fallbacks + 1;
        }

        get inheriting() {
            return !!this.inherit && !this.value.mainId;
        }

        /* ---------- public API ---------- */

        setModels(models) {
            this.models = (Array.isArray(models) ? models : []).filter((m) => m.type !== "decision");
            this.renderChain();
            if (this.isOpen()) this.renderPicker();
        }

        setValue(value) {
            const v = value || {};
            const mainId = v.mainId || "";
            const fallbackIds = (v.fallbackIds || []).filter(Boolean).slice(0, this.fallbacks);
            // Inherit mode follows the global chain whole: no own fallbacks.
            this.value = { mainId, fallbackIds: this.inherit && !mainId ? [] : fallbackIds };
            this.loaded = true;
            this.renderChain();
        }

        setInherited(value) {
            const v = value || {};
            this.inherited = {
                mainId: v.mainId || "",
                fallbackIds: (v.fallbackIds || []).filter(Boolean).slice(0, this.fallbacks),
            };
            this.renderChain();
            if (this.isOpen()) this.renderPicker();
        }

        getValue() {
            return { mainId: this.value.mainId, fallbackIds: this.value.fallbackIds.slice() };
        }

        open() {
            if (!this.loaded) return;
            // Leaving the global chain starts from it, so it is easy to tweak.
            const start = this.inheriting
                ? [this.inherited.mainId, ...this.inherited.fallbackIds]
                    .filter((id) => id && this.models.some((m) => m.id === id && this.eligible(m)))
                : [this.value.mainId, ...this.value.fallbackIds];
            this.draft = start.filter(Boolean).slice(0, this.capacity);
            this.query = "";
            this.search.value = "";
            this._setStatus(this.modalStatus, "", "");
            this.renderPicker();
            this._lastFocus = document.activeElement;
            this.modal.classList.add("is-open");
            document.documentElement.classList.add("mc-modal-open");
            setTimeout(() => this.search.focus(), 0);
        }

        close() {
            this.modal.classList.remove("is-open");
            document.documentElement.classList.remove("mc-modal-open");
            this._syncSource();  // cancelling "Custom…" puts the combobox back
            if (this._lastFocus && this._lastFocus.focus) this._lastFocus.focus();
        }

        isOpen() {
            return this.modal.classList.contains("is-open");
        }

        /* ---------- markup ---------- */

        _build() {
            const id = esc(this.uid);
            this.root.classList.add("model-chain");
            this.root.innerHTML = `
                <div class="mc-head">
                    <div class="mc-head-text">
                        <div class="mc-title setting-row-title">${esc(this.title)}</div>
                        ${this.description ? `<div class="mc-desc setting-row-desc">${esc(this.description)}</div>` : ""}
                    </div>
                </div>
                ${this.inherit ? `
                <div class="mc-source">
                    <select class="mc-source-select ${this.selectClass ? esc(this.selectClass) : "mc-source-default"}" data-mc-source
                            aria-label="${esc(this.title)} source">
                        <option value="inherit">${esc(this.inherit.label || "Global default")}</option>
                        <option value="custom">Custom models…</option>
                    </select>
                </div>` : ""}
                <div class="mc-card" data-mc-card aria-label="${esc(this.title)}">
                    <div class="mc-hero"><span class="mc-unset">Loading models…</span></div>
                </div>
                <p class="mc-inherit-note" data-mc-note hidden></p>`;

            const modal = document.createElement("div");
            modal.className = "mc-modal";
            modal.id = this.uid + "-picker";
            modal.setAttribute("role", "dialog");
            modal.setAttribute("aria-modal", "true");
            modal.setAttribute("aria-labelledby", this.uid + "-picker-title");
            modal.innerHTML = `
                <div class="mc-dialog">
                    <div class="mc-dialog-head">
                        <div>
                            <h3 id="${id}-picker-title">${esc(this.title)}</h3>
                            <p>Pick up to ${this.capacity} models. The first is the ${esc(this.mainLabel.toLowerCase())}
                               model; the rest are fallbacks, tried in order.</p>
                        </div>
                        <button type="button" class="mc-close" data-mc-close aria-label="Close">&times;</button>
                    </div>
                    <div class="mc-dialog-body">
                        <section class="mc-pane mc-pane-all" aria-label="All models">
                            <label class="mc-search">
                                ${icon("search")}
                                <input type="search" data-mc-search placeholder="Search models or providers"
                                       aria-label="Search models" autocomplete="off" spellcheck="false">
                            </label>
                            <div class="mc-list" data-mc-list role="list"></div>
                        </section>
                        <section class="mc-pane mc-pane-selected" aria-label="Selected models">
                            <div class="mc-pane-head">
                                <span>Selected models</span>
                                <span class="mc-count" data-mc-count></span>
                            </div>
                            <ol class="mc-selected" data-mc-selected></ol>
                        </section>
                    </div>
                    <div class="mc-dialog-foot">
                        <span class="mc-status" data-mc-modal-status aria-live="polite"></span>
                        <button type="button" class="mc-btn" data-mc-close>Cancel</button>
                        <button type="button" class="mc-btn mc-btn-primary" data-mc-save>${icon("check")}Save</button>
                    </div>
                </div>`;
            // The modal lives in <body> so no ancestor can clip or offset it.
            document.body.appendChild(modal);
            this.modal = modal;

            const $ = (sel) => this.root.querySelector(sel);
            const m = (sel) => modal.querySelector(sel);
            this.status = document.createElement("span");
            this.status.className = "mc-status";
            this.status.setAttribute("aria-live", "polite");
            this.note = $("[data-mc-note]");
            this.btnEdit = document.createElement("button");
            this.btnEdit.type = "button";
            this.btnEdit.className = "mc-btn mc-btn-sm";
            this.btnEdit.disabled = true;
            this.btnEdit.setAttribute("aria-haspopup", "dialog");
            this.btnEdit.setAttribute("aria-controls", this.uid + "-picker");
            this.btnEdit.setAttribute("aria-label", "Manage " + this.title);
            this.btnEdit.innerHTML = icon("pencil") + "Manage";
            this.source = $("[data-mc-source]");
            this.card = $("[data-mc-card]");
            this.search = m("[data-mc-search]");
            this.list = m("[data-mc-list]");
            this.selected = m("[data-mc-selected]");
            this.count = m("[data-mc-count]");
            this.modalStatus = m("[data-mc-modal-status]");
            this.btnSave = m("[data-mc-save]");
        }

        /* ---------- chain card ---------- */

        _byId() {
            return new Map(this.models.map((m) => [m.id, m]));
        }

        _flag(model) {
            const flag = !model ? "deleted" : (model.enabled ? "" : "disabled");
            return flag ? `<span class="mc-flag">${flag}</span>` : "";
        }

        renderChain() {
            const byId = this._byId();
            const inheriting = this.inheriting;
            const { mainId, fallbackIds } = this.value;

            // Hero: the main model (or the empty state) with Manage on its right.
            const main = byId.get(mainId);
            let hero;
            if (!this.loaded) {
                hero = '<span class="mc-unset">Loading models…</span>';
            } else if (!mainId) {
                hero = `<span class="mc-unset">${esc(this.mainEmptyText)}</span>`;
            } else {
                hero = `<span class="mc-badge">${esc(this.mainLabel)}</span>
                    <span class="mc-hero-text">
                        <span class="mc-model${main ? "" : " is-missing"}" title="${esc(main ? main.name : mainId)}">${esc(main ? main.name : mainId)}</span>
                        ${main && main.provider ? `<span class="mc-provider">${esc(main.provider)}</span>` : ""}
                        ${this._flag(main)}
                    </span>`;
            }
            this.card.classList.toggle("is-empty", !mainId);
            this.card.innerHTML = `<div class="mc-hero${mainId ? "" : " is-empty"}">${hero}<span class="mc-hero-actions" data-mc-actions></span></div>`
                + (mainId ? this._renderStrip(byId, fallbackIds) : "");
            // The status and Manage elements are moved back in on every render so
            // their listeners stay bound.
            this.card.querySelector("[data-mc-actions]").append(this.status, this.btnEdit);
            this.btnEdit.innerHTML = icon("pencil") + (mainId || !this.loaded ? "Manage" : "Choose model");

            // On the global chain the card is hidden; a summary line names
            // the inherited models and links to where they are set.
            this.card.hidden = inheriting;
            this.note.hidden = !inheriting;
            if (inheriting) {
                const inhMain = byId.get(this.inherited.mainId);
                const n = this.inherited.fallbackIds.length;
                const summary = this.inherited.mainId
                    ? ` Currently <b>${esc(inhMain ? inhMain.name : this.inherited.mainId)}</b>`
                        + (n ? ` with ${n} fallback${n === 1 ? "" : "s"}.` : " with no fallbacks.")
                    : "";
                this.note.innerHTML = esc(this.inherit.note || "") + summary
                    + (this.inherit.href ? ` <a href="${esc(this.inherit.href)}">Change in System Settings</a>` : "");
            }
            this._syncSource();
            this.btnEdit.disabled = !this.loaded || this._busy;
        }

        _renderStrip(byId, fallbackIds) {
            const pills = fallbackIds.map((id, i) => {
                const m = byId.get(id);
                const name = m ? m.name : id;
                const tip = m && m.provider ? `${name} · ${m.provider}` : name;
                return `<span class="mc-pill${m ? "" : " is-missing"}" data-index="${i}" title="${esc(tip)}">
                        <button type="button" class="mc-pill-n" data-act="remove" data-index="${i}"
                                aria-label="Remove ${esc(name)}" title="Remove">
                            <span class="mc-pill-num">${i + 1}</span><span class="mc-pill-rm">${icon("x")}</span>
                        </button>
                        <span class="mc-pill-name">${esc(name)}</span>
                        ${m && m.provider ? `<span class="mc-pill-prov">${esc(m.provider)}</span>` : ""}
                        ${this._flag(m)}
                    </span>`;
            }).join("");
            const none = fallbackIds.length ? "" : '<span class="mc-none">No fallbacks</span>';
            return `<div class="mc-strip"><span class="mc-strip-label">↳ If it fails <span class="mc-count-inline">${fallbackIds.length}/${this.fallbacks}</span></span>${none}${pills}</div>`;
        }

        /* Inline removal persists right away. */
        async _commit(ids) {
            if (this._busy) return;
            const [mainId = "", ...fallbackIds] = ids;
            this._busy = true;
            this._setStatus(this.status, "saving", "Saving…");
            this.renderChain();
            try {
                const stored = (await this.onSave({ mainId, fallbackIds })) || { mainId, fallbackIds };
                this._busy = false;
                this.setValue(stored);
                this._setStatus(this.status, "saved", "Saved");
            } catch (err) {
                this._busy = false;
                this.renderChain();
                this._setStatus(this.status, "error", (err && err.message) || "Save failed");
            }
        }

        _syncSource() {
            if (!this.source) return;
            this.source.value = this.inheriting ? "inherit" : "custom";
            this.source.disabled = !this.loaded;
        }

        async _useInherited() {
            this.source.disabled = true;
            this._setStatus(this.status, "saving", "Saving…");
            try {
                const stored = (await this.onSave({ mainId: "", fallbackIds: [] })) || { mainId: "", fallbackIds: [] };
                this.setValue(stored);
                this._setStatus(this.status, "saved", "Saved");
            } catch (err) {
                this._syncSource();
                this._setStatus(this.status, "error", (err && err.message) || "Save failed");
            } finally {
                this.source.disabled = false;
            }
        }

        /* ---------- picker ---------- */

        renderPicker() {
            this._renderList();
            this._renderSelected();
        }

        _matches(model) {
            const q = this.query.trim().toLowerCase();
            if (!q) return true;
            const hay = [model.name, model.model_name, model.provider].join(" ").toLowerCase();
            return q.split(/\s+/).every((part) => hay.includes(part));
        }

        _renderList() {
            const full = this.draft.length >= this.capacity;
            const groups = new Map();
            this.models.filter((m) => this._matches(m)).forEach((m) => {
                const key = m.provider || "Other";
                if (!groups.has(key)) groups.set(key, []);
                groups.get(key).push(m);
            });

            if (!this.models.length) {
                this.list.innerHTML = '<p class="mc-list-empty">No models configured — add one in the Models section.</p>';
                return;
            }
            if (!groups.size) {
                this.list.innerHTML = `<p class="mc-list-empty">No models match “${esc(this.query)}”.</p>`;
                return;
            }

            let html = "";
            groups.forEach((models, provider) => {
                html += `<div class="mc-group" role="presentation">${esc(provider)}</div>`;
                models.forEach((m) => {
                    const pos = this.draft.indexOf(m.id);
                    const picked = pos !== -1;
                    const eligible = this.eligible(m);
                    const disabled = !picked && (!eligible || full);
                    const hint = picked ? this.roleLabel(pos)
                        : (!eligible ? (m.enabled ? this.ineligibleLabel : "Disabled")
                            : (full ? "Chain full" : ""));
                    html += `<button type="button" class="mc-item${picked ? " is-picked" : ""}"
                                role="listitem" data-id="${esc(m.id)}" ${disabled ? "disabled" : ""}
                                aria-pressed="${picked}">
                        <span class="mc-item-check">${picked ? icon("check") : icon("plus")}</span>
                        <span class="mc-item-text">
                            <span class="mc-item-name">${esc(m.name)}</span>
                            <span class="mc-item-sub">${esc(m.model_name || m.id)}</span>
                        </span>
                        ${hint ? `<span class="mc-item-hint">${esc(hint)}</span>` : ""}
                    </button>`;
                });
            });
            this.list.innerHTML = html;
        }

        _renderSelected() {
            const byId = this._byId();
            const ids = this.draft;
            this.count.textContent = `${ids.length} / ${this.capacity}`;
            if (!ids.length) {
                this.selected.innerHTML = '<li class="mc-selected-empty">Pick a model on the left. '
                    + `The first one you pick becomes the ${esc(this.mainLabel.toLowerCase())} model.`
                    + (this.requireMain || this.inherit ? "" : ` Leave it empty for: ${esc(this.mainEmptyText)}.`)
                    + "</li>";
                return;
            }
            const last = ids.length - 1;
            const missing = ids.filter((id) => !byId.has(id)).length;
            const notice = missing
                ? `<li class="mc-sel-notice" role="note">${missing === 1 ? "1 model no longer exists" : `${missing} models no longer exist`}. Remove ${missing === 1 ? "it" : "them"} before saving.</li>`
                : "";
            this.selected.innerHTML = notice + ids.map((id, i) => {
                const m = byId.get(id);
                const name = m ? m.name : id;
                let sub;
                if (!m) sub = '<span class="mc-flag">Deleted model</span>';
                else sub = esc(m.model_name || m.id) + (m.enabled ? "" : ' · <span class="mc-flag">Disabled</span>');
                const actions = `
                    <span class="mc-sel-actions">
                        <button type="button" class="mc-icon-btn" data-move="-1" data-index="${i}"
                                ${i === 0 ? "disabled" : ""} aria-label="Move ${esc(name)} up">${icon("up")}</button>
                        <button type="button" class="mc-icon-btn" data-move="1" data-index="${i}"
                                ${i === last ? "disabled" : ""} aria-label="Move ${esc(name)} down">${icon("down")}</button>
                        <button type="button" class="mc-icon-btn is-remove" data-remove="${i}"
                                aria-label="Remove ${esc(name)}">${icon("x")}</button>
                    </span>`;
                return `<li class="mc-sel${i === 0 ? " is-main" : ""}${m ? "" : " is-missing"}" draggable="true" data-index="${i}">
                    <span class="mc-grip" aria-hidden="true">${icon("grip")}</span>
                    <span class="mc-role">${esc(this.roleLabel(i))}</span>
                    <span class="mc-sel-text">
                        <span class="mc-item-name">${esc(name)}</span>
                        <span class="mc-item-sub">${sub}</span>
                    </span>
                    ${actions}
                </li>`;
            }).join("");
        }

        _toggle(id) {
            const pos = this.draft.indexOf(id);
            if (pos !== -1) this.draft.splice(pos, 1);
            else if (this.draft.length < this.capacity) this.draft.push(id);
            this._setStatus(this.modalStatus, "", "");
            this.renderPicker();
        }

        _move(from, to) {
            if (to < 0 || to >= this.draft.length || from === to) return;
            const [id] = this.draft.splice(from, 1);
            this.draft.splice(to, 0, id);
            this._renderSelected();
            this._renderList();
        }

        async save() {
            const [mainId = "", ...fallbackIds] = this.draft;
            // With an inherit option, "nothing" is chosen in the combobox,
            // so the custom picker needs at least a main model.
            if (!mainId && (this.requireMain || this.inherit)) {
                this._setStatus(this.modalStatus, "error",
                    `Pick at least a ${this.mainLabel.toLowerCase()} model`);
                return;
            }
            const known = new Set(this.models.map((m) => m.id));
            if ([mainId, ...fallbackIds].some((id) => id && !known.has(id))) {
                this._setStatus(this.modalStatus, "error", "Remove the deleted models first");
                return;
            }
            this.btnSave.disabled = true;
            this._setStatus(this.modalStatus, "saving", "Saving…");
            try {
                const stored = (await this.onSave({ mainId, fallbackIds })) || { mainId, fallbackIds };
                this.setValue(stored);
                this.close();
                this._setStatus(this.status, "saved", "Saved");
            } catch (err) {
                this._setStatus(this.modalStatus, "error", (err && err.message) || "Save failed");
            } finally {
                this.btnSave.disabled = false;
            }
        }

        /* ---------- events ---------- */

        _bind() {
            this.btnEdit.addEventListener("click", () => this.open());

            // Fallback pills: remove. Adding and reordering live in the Manage picker.
            this.card.addEventListener("click", (e) => {
                const btn = e.target.closest("[data-act]");
                if (!btn || btn.disabled || this._busy) return;
                const ids = [this.value.mainId, ...this.value.fallbackIds];
                const i = Number(btn.dataset.index) + 1;  // slot in ids (0 = main)
                if (btn.dataset.act === "remove") { ids.splice(i, 1); this._commit(ids); }
            });
            this.btnSave.addEventListener("click", () => this.save());
            this.modal.querySelectorAll("[data-mc-close]").forEach((b) =>
                b.addEventListener("click", () => this.close()));
            if (this.source) {
                this.source.addEventListener("change", () => {
                    if (this.source.value === "custom") {
                        if (this.inheriting) this.open();
                    } else if (!this.inheriting) {
                        this._useInherited();
                    }
                });
            }

            // Backdrop click closes only when the press also started there.
            let pressedOnBackdrop = false;
            this.modal.addEventListener("mousedown", (e) => { pressedOnBackdrop = e.target === this.modal; });
            this.modal.addEventListener("click", (e) => {
                if (e.target === this.modal && pressedOnBackdrop) this.close();
            });
            this.modal.addEventListener("keydown", (e) => {
                if (e.key === "Escape") { e.preventDefault(); this.close(); }
            });

            this.search.addEventListener("input", () => {
                this.query = this.search.value;
                this._renderList();
            });
            this.search.addEventListener("keydown", (e) => {
                if (e.key !== "Enter") return;
                e.preventDefault();
                const first = this.list.querySelector(".mc-item:not([disabled]):not(.is-picked)");
                if (first) this._toggle(first.dataset.id);
            });

            this.list.addEventListener("click", (e) => {
                const item = e.target.closest(".mc-item");
                if (item && !item.disabled) this._toggle(item.dataset.id);
            });

            this.selected.addEventListener("click", (e) => {
                const move = e.target.closest("[data-move]");
                if (move && !move.disabled) {
                    const from = Number(move.dataset.index);
                    const to = from + Number(move.dataset.move);
                    this._move(from, to);
                    const again = this.selected.querySelector(
                        `[data-move="${move.dataset.move}"][data-index="${to}"]:not([disabled])`);
                    (again || this.selected.querySelector(`[data-index="${to}"] .mc-icon-btn`))?.focus();
                    return;
                }
                const remove = e.target.closest("[data-remove]");
                if (remove) {
                    this.draft.splice(Number(remove.dataset.remove), 1);
                    this.renderPicker();
                }
            });

            // Drag to reorder the selection.
            let dragFrom = null;
            this.selected.addEventListener("dragstart", (e) => {
                const li = e.target.closest(".mc-sel");
                if (!li) return;
                dragFrom = Number(li.dataset.index);
                li.classList.add("is-dragging");
                e.dataTransfer.effectAllowed = "move";
                e.dataTransfer.setData("text/plain", String(dragFrom));
            });
            this.selected.addEventListener("dragover", (e) => {
                if (dragFrom === null) return;
                e.preventDefault();
                const li = e.target.closest(".mc-sel");
                this.selected.querySelectorAll(".is-drop").forEach((el) => el.classList.remove("is-drop"));
                if (li) li.classList.add("is-drop");
            });
            this.selected.addEventListener("drop", (e) => {
                if (dragFrom === null) return;
                e.preventDefault();
                const li = e.target.closest(".mc-sel");
                const to = li ? Number(li.dataset.index) : this.draft.length - 1;
                this._move(dragFrom, to);
            });
            this.selected.addEventListener("dragend", () => {
                dragFrom = null;
                this.selected.querySelectorAll(".is-dragging, .is-drop")
                    .forEach((el) => el.classList.remove("is-dragging", "is-drop"));
            });
        }

        _setStatus(el, state, message) {
            if (!el) return;
            clearTimeout(el._mcTimer);
            el.className = "mc-status" + (state ? " is-" + state : "");
            el.textContent = message || "";
            if (state === "saved") el._mcTimer = setTimeout(() => this._setStatus(el, "", ""), 2000);
        }
    }

    window.ModelChain = ModelChain;
})();
