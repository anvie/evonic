/* Run with: node unit_tests/test_settings_models_reasoning.js */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {execFileSync} = require("node:child_process");

const elements = new Map();
function element(id) {
    if (!elements.has(id)) elements.set(id, {
        id, value: "", style: {}, disabled: false, options: [],
        replaceChildren(...options) { this.options = options; },
        add(option) { this.options.push(option); },
        setCustomValidity(message) { this.validationMessage = message; },
    });
    return elements.get(id);
}
const template = fs.readFileSync("templates/partials/settings/models.html", "utf8");
assert.ok(!template.includes('reasoning-effort-refresh'));
assert.ok(!template.includes('model-advanced-settings'));
for (const id of ["reasoning-effort-group", "model-reasoning-effort-manual"]) {
    const tag = template.match(new RegExp(`<[^>]*id="${id}"[^>]*>`))[0];
    assert.ok(!/class="[^"]*\bhidden\b/.test(tag));
}
const context = {
    window: {}, document: {getElementById: element}, URLSearchParams, URL,
    Option: function (text, value) { this.text = text; this.value = value; },
    apiGet: () => { throw new Error("Effort selection must not call an API"); },
};
vm.runInNewContext(fs.readFileSync("static/js/settings-models.js", "utf8"), context);
const settings = context.window.settingsModels;
settings.reasoningCatalog = JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c",
    "import json; from backend.reasoning_capabilities import REASONING_CATALOG; print(json.dumps(REASONING_CATALOG))"], {encoding: "utf8"}));
settings.providers = [
    {id: "codex", api_format: "codex", base_url: "https://chatgpt.com/backend-api/codex"},
    {id: "cavoti", api_format: "openai", base_url: "https://cavoti.com/v1"},
    {id: "gemini", api_format: "openai", base_url: "https://generativelanguage.googleapis.com/v1beta/openai"},
];
element("model-provider").value = "codex";
element("model-name-param").value = "gpt-6-astra";
element("model-api-format").value = "codex";
settings.resetReasoningEffort("max");
settings.updateReasoningOptions();
const select = element("model-reasoning-effort"), manual = element("model-reasoning-effort-manual");
assert.equal(select.value, "max");
assert.equal(select.options.length, 6);
assert.ok(!select.options.some(o => o.value === "ultra"));
assert.equal(select.disabled, false);
settings.resetReasoningEffort("ultra");
settings.updateReasoningOptions();
assert.ok(select.validationMessage);
assert.equal(select.value, "ultra"); // Preserve stale saved values; never silently map to max.
element("model-provider").value = "cavoti";
settings.changeModelProvider();
assert.equal(settings._reasoningEffort, null);
assert.equal(select.disabled, true);
assert.equal(manual.disabled, false);
manual.value = "custom_level";
settings.changeReasoningEffort();
assert.equal(settings._reasoningEffort, "custom_level");
settings.updateReasoningOptions();
assert.equal(manual.value, "custom_level");
manual.value = "";
settings.changeReasoningEffort();
assert.equal(settings._reasoningEffort, null);
element("model-provider").value = "gemini";
settings.changeModelProvider();
assert.equal(manual.disabled, true);
assert.ok(element("reasoning-effort-hint").textContent.includes("not available"));
element("model-base-url").value = "not a URL";
settings.updateReasoningOptions();
assert.ok(element("reasoning-effort-hint").textContent.includes("valid provider URL"));
for (const entry of Object.values(settings.reasoningCatalog)) {
    element("model-base-url").value = "https://" + entry.host;
    element("model-api-format").value = entry.api_format;
    for (const [model, caps] of Object.entries(entry.models)) {
        element("model-name-param").value = model;
        settings.resetReasoningEffort();
        settings.updateReasoningOptions();
        assert.deepEqual(select.options.map(option => option.value), ["", ...caps.efforts]);
        assert.equal(manual.disabled, true);
    }
    element("model-name-param").value = "__proto__";
    settings.updateReasoningOptions();
    assert.equal(manual.disabled, false);
}
console.log("Static reasoning controls passed without API calls");
