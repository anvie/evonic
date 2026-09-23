"""End-to-end tests for the template editor UI (task #30).

``templates/edit_template.html`` is the create/edit surface for agent templates
plus the "Test it" simulation panel.  These tests pin the *page* contract and
the wiring it relies on:

* ``GET /template/<id>`` renders the editor for an EXISTING id (edit mode) and
  for an UNKNOWN id (the "create new" state), embedding the resolved template
  as the ``#tpl-bootstrap`` JSON blob;
* every editor surface the task calls for is present (identity, parameters,
  prompt + unknown-placeholder validation, defaults grouped Basic/Advanced with
  the four security-sensitive keys flagged, tools, skills, variables, kb files);
* the "Test it" panel is wired to ``POST /api/templates/<id>/render`` and
  ``POST /api/templates/<id>/simulate`` and renders the simulated outbox;
* a create -> edit round-trip through the API is reflected in a re-opened page;
* driving ``/simulate`` through the REAL simulation runtime (with only the LLM
  turn stubbed) captures the simulated outbox and leaves NO persistent side
  effects, and ``/render`` never writes anything.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import config
from app import app
from models.db import db

import routes.templates as templates_routes
from backend import agent_templates as tpl
from backend.agent_runtime import llm_tool_executor as _itx
from backend.agent_runtime import simulation_runtime as simrt

TEMPLATE_ID = "editor_roundtrip_bot"

BOOTSTRAP_RE = re.compile(
    r'<script id="tpl-bootstrap" type="application/json">(.*?)</script>',
    re.S,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def template_payload(**overrides):
    """Return a complete, valid template payload (overridable per test)."""
    payload = {
        "id": TEMPLATE_ID,
        "name": "Editor Round-trip Bot",
        "description": "Exercises the editor page.",
        "category": "support",
        "icon": "headset",
        "parameters": [
            {"name": "company", "label": "Company", "type": "text",
             "required": True, "default": "Acme"},
            {"name": "tone", "type": "select", "options": ["formal", "friendly"],
             "default": "friendly"},
        ],
        "system_prompt": "You support {{company}} in a {{tone}} tone.",
        "defaults": {"sandbox_enabled": 1},
        # A tool that actually resolves, so the resolver report is clean.  The
        # outbound tools the simulation intercepts (create_schedule,
        # send_agent_message) are driven through the real chokepoint by the
        # stubbed LLM turn in the simulate test, independent of declaration.
        "tools": ["read_file"],
        "skills": [],
        "variables": [{"key": "API_KEY", "is_secret": True, "required": True}],
        "kb_files": {"notes.md": "# Notes\n"},
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    """Redirect the template store *and* agent creation into a throwaway root."""
    root = tmp_path / "repo"
    (root / "agent_templates").mkdir(parents=True, exist_ok=True)
    (root / "skillsets").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "BASE_DIR", str(root), raising=False)
    monkeypatch.delenv(templates_routes.PRIVILEGED_CALLERS_ENV, raising=False)
    templates_routes.reset_simulate_rate_limits()
    return str(root)


@pytest.fixture
def client():
    with app.test_client() as test_client:
        yield test_client


def login(client, user_id=None):
    with client.session_transaction() as session:
        session["authenticated"] = True
        if user_id is not None:
            session["_user_id"] = user_id


def create_template_via_api(client, **overrides):
    response = client.post("/api/templates", json=template_payload(**overrides))
    assert response.status_code == 201, response.get_json()
    return response


def bootstrap_of(body):
    match = BOOTSTRAP_RE.search(body)
    assert match is not None, "editor page must embed the #tpl-bootstrap JSON blob"
    return json.loads(match.group(1))


def snapshot(root):
    """Hash every file under *root* so a test can prove nothing was written."""
    hashes = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as handle:
                hashes[os.path.relpath(full, root)] = hashlib.sha256(
                    handle.read()).hexdigest()
    return hashes


def _schedule_count():
    with db._connect() as conn:
        try:
            return conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------

EDITOR_SURFACE_IDS = (
    # identity
    "tpl-field-id", "tpl-field-name", "tpl-field-description",
    "tpl-field-category", "tpl-field-icon",
    # parameters + prompt
    "tpl-params-list", "tpl-param-add", "tpl-field-prompt",
    "tpl-ph-chips", "tpl-ph-warning",
    # defaults (grouped) + security notice
    "tpl-defaults-basic", "tpl-defaults-advanced",
    # tools / skills / variables / kb
    "tpl-tools-input", "tpl-skills-input", "tpl-vars-list", "tpl-var-add",
    "tpl-kb-list", "tpl-kb-add",
    # "Test it" panel
    "tpl-sim-params", "tpl-sim-message", "tpl-sim-send", "tpl-sim-gate",
    "tpl-conv", "tpl-outbox", "tpl-activity", "tpl-render-btn",
    # actions
    "tpl-save-btn", "tpl-delete-btn",
)


def test_editor_page_renders_every_surface_in_edit_mode(client, repo_root):
    login(client)
    create_template_via_api(client)

    response = client.get("/template/%s" % TEMPLATE_ID)
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    for element_id in EDITOR_SURFACE_IDS:
        assert 'id="%s"' % element_id in body, element_id

    # The simulation/render endpoints the panel calls.
    assert "/render" in body
    assert "/simulate" in body
    # The four security-sensitive default keys are flagged force-safe.
    for key in ("sandbox_enabled", "run_as_user", "messaging_acl",
                "messaging_acl_mode"):
        assert key in body, key
    assert "SECURITY" in body


def test_editor_page_renders_create_mode_for_unknown_id(client, repo_root):
    login(client)
    response = client.get("/template/brand_new_template")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'id="tpl-editor"' in body

    boot = bootstrap_of(body)
    assert boot["template_id"] == "brand_new_template"
    # An unknown id is the create surface: no resolved report is embedded.
    assert boot["report"] is None


def test_editor_bootstrap_carries_the_resolved_report(client, repo_root):
    """The page embeds exactly what ``resolve_template`` returns (round-trip)."""
    login(client)
    create_template_via_api(client)

    body = client.get("/template/%s" % TEMPLATE_ID).get_data(as_text=True)
    boot = bootstrap_of(body)
    report = boot["report"]

    assert report["template"]["id"] == TEMPLATE_ID
    assert report["template"]["name"] == "Editor Round-trip Bot"
    assert report["template"]["system_prompt"].startswith("You support")
    assert report["template"]["defaults"]["sandbox_enabled"] == 1
    assert [p["name"] for p in report["template"]["parameters"]] == [
        "company", "tone"]
    # The resolution report the panel's "resolved-config" summary consumes.
    assert report["tools"]["declared"] == ["read_file"]
    assert report["tools"]["missing"] == []
    assert report["ok"] is True

    # Byte-for-byte agreement with the engine's own view.
    assert report["template"] == tpl.get_template(TEMPLATE_ID)


def test_create_then_edit_round_trip_is_visible_in_the_editor(client, repo_root):
    """POST (create) then PUT (edit) both land, and the editor shows the edit."""
    login(client)
    create_template_via_api(client)

    updated = {
        "name": "Edited Bot",
        "system_prompt": "You support {{company}} (edited).",
        "defaults": {"sandbox_enabled": 1, "run_as_user": "svc"},
        "parameters": [
            {"name": "company", "type": "text", "required": True,
             "default": "Globex"},
        ],
    }
    put = client.put("/api/templates/%s" % TEMPLATE_ID, json=updated)
    assert put.status_code == 200, put.get_json()

    boot = bootstrap_of(client.get("/template/%s" % TEMPLATE_ID).get_data(as_text=True))
    template = boot["report"]["template"]
    assert template["name"] == "Edited Bot"
    assert template["system_prompt"] == "You support {{company}} (edited)."
    assert template["defaults"]["run_as_user"] == "svc"
    assert [p["name"] for p in template["parameters"]] == ["company"]


# ---------------------------------------------------------------------------
# Render: preview only, never persists
# ---------------------------------------------------------------------------

def test_render_route_persists_nothing(client, repo_root):
    login(client)
    create_template_via_api(client)
    before = snapshot(repo_root)

    response = client.post("/api/templates/%s/render" % TEMPLATE_ID,
                           json={"params": {"company": "Initech", "tone": "formal"}})
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["persisted"] is False
    assert payload["system_prompt"] == "You support Initech in a formal tone."
    assert payload["resolution"]["ok"] is True
    # No agent row, no template mutation, no file writes.
    assert snapshot(repo_root) == before
    assert db.get_agent(TEMPLATE_ID) is None


# ---------------------------------------------------------------------------
# Simulate: real runtime, simulated outbox, NO persistent side effects
# ---------------------------------------------------------------------------

def test_simulate_route_captures_outbox_without_side_effects(
        client, repo_root, monkeypatch):
    """End-to-end through the REAL simulation runtime + tool chokepoint.

    Only the LLM turn is stubbed: the stub drives the runtime's real
    ``_execute_tool_core`` so the task-#25 outbound interceptor must engage.
    The ephemeral agent's outbound calls are captured in the simulated outbox
    the editor renders, and teardown must leave no rows and no files behind.
    """
    login(client)
    create_template_via_api(client)
    before = snapshot(repo_root)
    schedules_before = _schedule_count()

    from backend.agent_runtime import llm_call as _call
    from backend.subagent_manager import subagent_manager

    executed = []

    def real_exec(fn_name, args):
        executed.append((fn_name, dict(args)))
        return {"success": True}

    def builtin_exec(fn_name, args):
        return None

    def fake_handle_message(agent_id, external_user_id, message, session_id=None,
                            skip_buffer=False):
        agent = subagent_manager.get(agent_id)
        assert agent and agent.get("is_simulation")
        ctx = dict(agent)
        ctx.update({"_db_agent_id": agent_id,
                    "session_id": session_id or agent_id,
                    "user_id": external_user_id})
        r1 = _call._execute_tool_core(
            "create_schedule",
            {"name": "sim reminder", "trigger_type": "date",
             "trigger_config": {"run_date": "2999-01-01T00:00:00"},
             "action_type": "static_message", "action_config": {}},
            builtin_exec, real_exec, ctx)
        r2 = _call._execute_tool_core(
            "send_agent_message",
            {"target_agent_id": "other", "message": "hi"},
            builtin_exec, real_exec, ctx)
        return {"response": "simulated reply",
                "tool_trace": [{"tool": "create_schedule", "result": r1},
                               {"tool": "send_agent_message", "result": r2}],
                "timeline": []}

    import backend.agent_runtime as ar_pkg
    monkeypatch.setattr(ar_pkg.agent_runtime, "handle_message", fake_handle_message)

    response = client.post(
        "/api/templates/%s/simulate" % TEMPLATE_ID,
        json={"params": {"company": "Initech"},
              "message": "please schedule a reminder and ping the team"},
    )
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()

    # The turn ran through the real (throwaway) runtime.
    assert payload.get("error") is not True, payload
    assert payload["response"] == "simulated reply"

    # ... and the outbound effects were intercepted, never executed.
    tools = [entry["tool"] for entry in payload["outbox"]]
    assert tools == ["create_schedule", "send_agent_message"]
    assert all(entry["simulated"] for entry in payload["outbox"])
    assert all(entry.get("summary") for entry in payload["outbox"])
    assert executed == []
    assert _schedule_count() == schedules_before

    sim_id = payload["simulation_id"]

    # No agent row, no residual simulation tree, no captured outbox left over.
    assert db.get_agent(sim_id) is None
    assert not os.path.isdir(simrt.sim_root_for(sim_id))
    assert _itx.get_simulated_outbox(sim_id) == []

    # The template store is untouched: render/simulate are pure reads.
    assert snapshot(repo_root) == before


# ---------------------------------------------------------------------------
# 6. Typing in a repeater field must not lose focus (task #30 follow-up)
#
# Robin reported that the caret disappeared after every keystroke in a field of
# the editor.  Cause: the parameters list is re-rendered on each keystroke in a
# parameter Name/Options field, and a plain ``renderParameters()`` replaced the
# inputs the user was typing in.  The re-render is now focus-preserving.
# ---------------------------------------------------------------------------

PAGE_PATH = Path(__file__).resolve().parents[1] / "templates" / "edit_template.html"

NODE = shutil.which("node") or shutil.which("nodejs")


def _extract_js_function(source, name):
    """Return the balanced-brace body of ``function <name>(...)`` in ``source``."""
    start = source.index("function " + name + "(")
    brace = source.index("{", start)
    depth = 0
    for pos in range(brace, len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                return source[start:pos + 1]
    raise AssertionError("unbalanced braces while extracting " + name)


def test_repeater_list_inputs_use_the_focus_preserving_render(repo_root):
    """The focus-preserving helpers exist and are what the list inputs call."""
    source = PAGE_PATH.read_text(encoding="utf-8")

    for name in ("datasetKey", "captureListFocus", "restoreListFocus",
                 "rerenderParameters", "rerenderVariables"):
        assert "function " + name + "(" in source, name

    # A parameter Name/Options (and a variable's is_secret) edit re-renders the
    # list through the focus-preserving wrapper instead of a bare render.
    on_param = _extract_js_function(source, "onParamEdit")
    assert "rerenderParameters()" in on_param
    # No *bare* renderParameters() call: the wrapper must be used every time,
    # otherwise the field the user is typing in is thrown away again.
    assert not re.search(r"(?<![A-Za-z_])renderParameters\(\)", on_param)

    on_var = _extract_js_function(source, "onVarEdit")
    assert "rerenderVariables()" in on_var
    assert not re.search(r"(?<![A-Za-z_])renderVariables\(\)", on_var)


# A tiny DOM: re-rendering replaces the input nodes (that is what killed focus);
# the wrapper must hand focus + caret back to the equivalent fresh node.
FOCUS_HARNESS_JS = r"""
let ACTIVE = null;
function makeInput(p, f, val) {
  const el = {
    dataset: { p: String(p), f: f },
    value: val,
    selectionStart: 0,
    selectionEnd: 0,
    focus: function () { ACTIVE = el; },
    setSelectionRange: function (a, b) { el.selectionStart = a; el.selectionEnd = b; },
  };
  return el;
}
const OLD = makeInput(0, "name", "ab");
let NEW = null;
const container = {
  contains: function (el) { return el === OLD || el === NEW; },
  querySelectorAll: function () { return NEW ? [NEW] : []; },
};
function $(id) { return container; }
function renderParameters() { NEW = makeInput(0, "name", "ab"); }
const document = { get activeElement() { return ACTIVE; } };

__FNS__

// The user focuses the parameter Name input and the caret sits after "ab".
ACTIVE = OLD;
OLD.selectionStart = 2;
OLD.selectionEnd = 2;

// A keystroke re-renders the list.
rerenderParameters();

if (ACTIVE !== NEW) { console.error("focus was lost across the re-render"); process.exit(1); }
if (ACTIVE.selectionStart !== 2) { console.error("caret was lost: " + ACTIVE.selectionStart); process.exit(1); }
if (ACTIVE.dataset.f !== "name" || ACTIVE.dataset.p !== "0") { console.error("wrong field restored"); process.exit(1); }
console.log("focus+caret preserved");
"""


@pytest.mark.skipif(NODE is None, reason="node is required for the DOM harness")
def test_focus_and_caret_survive_a_repeater_re_render(repo_root, tmp_path):
    """Drive the real helpers against a tiny DOM: focus + caret must survive."""
    source = PAGE_PATH.read_text(encoding="utf-8")
    functions = "\n\n".join(
        _extract_js_function(source, name)
        for name in ("datasetKey", "captureListFocus", "restoreListFocus", "rerenderParameters"))
    script = tmp_path / "focus_harness.js"
    script.write_text(FOCUS_HARNESS_JS.replace("__FNS__", functions), encoding="utf-8")

    result = subprocess.run([NODE, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "focus+caret preserved" in result.stdout
