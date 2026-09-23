"""End-to-end verification for the shipped example templates (task #31).

``agent_templates/`` now ships three example blueprints that are meant to be
copied, edited and instantiated.  Nothing else in the suite would notice if one
of them regressed, so this module pins the whole user-visible path:

* every shipped example validates against the canonical schema, loads from the
  repository root, and resolves its declared tools/skills on a clean machine;
* the examples collectively exercise the documented feature set (required
  parameter *with* a default, select, boolean, number, a non-trivial
  ``defaults`` set, tools, skills, a plain and a **secret** variable, KB files);
* instantiating an example through the programmatic API
  (``create_agent_from_template``) and through the UI endpoint
  (``POST /api/templates/<id>/instantiate``) yields the **same agent spec**
  (row fields, system prompt, KB files, tools, skills, variables);
* a simulation run leaves **no DB rows** and **no files** under ``BASE_DIR``,
  and never writes a secret value into a log record, the template file, or the
  run's result.

Everything happens on throwaway roots and the test DB from ``conftest``; the
repository's real ``agent_templates/`` is only ever copied, never written.
"""

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from app import app
from models.db import db

from backend import agent_factory as factory
from backend import agent_templates as tpl
from backend.agent_runtime import agent_runtime as runtime_singleton
from backend.agent_runtime import llm_tool_executor as _itx
from backend.agent_runtime import simulation_runtime as simrt

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES_DIR = os.path.join(REPO_ROOT, "agent_templates")

#: The examples this task ships.  Parametrised tests iterate exactly these.
EXAMPLE_IDS = ("support_triage_bot", "data_analyst", "hello_world_showcase")

#: Distinctive plaintext that must never reach a log record, the template file,
#: or the simulation result.
SECRET_NEEDLE = "needle-do-not-log-3f9c17ab"

#: Instantiate-time parameters that exercise every parameter type.
PARAMS = {
    "support_triage_bot": {
        "company": "Globex",
        "tone": "professional",
        "escalate_on_refund": False,
        "refund_limit_usd": 500,
    },
    "data_analyst": {
        "report_scope": "quarterly ARR",
        "currency": "EUR",
        "include_charts": False,
        "lookback_days": 90,
    },
    "hello_world_showcase": {
        "agent_name": "Ada",
        "greeting_style": "playful",
        "emoji_enabled": False,
    },
}

#: Values supplied for each example's declared variables (one is secret).
VARIABLES = {
    "support_triage_bot": {"CRM_API_TOKEN": SECRET_NEEDLE},
    "data_analyst": {"WAREHOUSE_DSN": SECRET_NEEDLE},
    "hello_world_showcase": {"DEMO_API_KEY": SECRET_NEEDLE},
}

#: ``defaults`` keys that only the advanced section of the agent editor shows.
ADVANCED_DEFAULTS = frozenset({
    "sandbox_enabled",
    "bash_exec_enabled",
    "disable_turn_prefetch",
    "enable_cmp",
    "memory_engine",
    "kb_organizer_mode",
    "tool_compression_enabled",
    "message_wrapper_enabled",
    "attachment_max_size_mb",
    "summarize_threshold",
    "summarize_tail",
    "outbound_buffer_seconds",
    "message_buffer_seconds",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def payload(template_id):
    """Raw JSON of a shipped example (as an editor would see it on disk)."""
    return json.loads(read_text(os.path.join(EXAMPLES_DIR, template_id + ".json")))


def shipped_ids():
    return sorted(
        name[: -len(".json")]
        for name in os.listdir(EXAMPLES_DIR)
        if name.endswith(".json")
    )


def snapshot(root):
    """Content hash of every file under *root*, so writes can be detected."""
    hashes = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as handle:
                hashes[os.path.relpath(full, root)] = hashlib.sha256(
                    handle.read()).hexdigest()
    return hashes


def make_examples_root(tmp_path, name):
    """A throwaway repo root with copies of the examples + skill manifests.

    Only the manifests are created (``skills/<id>/skill.json``) because that is
    all the template layer's dependency report reads; it keeps the fixture
    hermetic and fast while still proving the declared skills resolve.
    """
    root = tmp_path / name
    (root / "agent_templates").mkdir(parents=True, exist_ok=True)
    (root / "skillsets").mkdir(parents=True, exist_ok=True)

    skill_ids = set()
    for template_id in shipped_ids():
        source = os.path.join(EXAMPLES_DIR, template_id + ".json")
        shutil.copy(source, root / "agent_templates" / (template_id + ".json"))
        skill_ids.update(payload(template_id).get("skills") or [])

    for skill_id in sorted(skill_ids):
        target = root / "skills" / skill_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "skill.json").write_text(
            json.dumps({"id": skill_id, "name": skill_id}), encoding="utf-8")
    return str(root)


def login(client, user_id="admin"):
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["_user_id"] = user_id


def agent_ids():
    return {agent["id"] for agent in db.get_agents()}


def session_index_count(agent_id):
    conn = sqlite3.connect(db.db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM session_index WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def spec_view(agent_id, base_dir):
    """The complete, comparable spec of a created agent."""
    agent = db.get_agent(agent_id)
    assert agent is not None, agent_id

    fields = {}
    for key in sorted(factory.SPEC_FIELD_ALLOWLIST):
        assert key in agent, "agent row is missing column %r" % key
        fields[key] = agent[key]

    agent_dir = factory.agent_dir_for(base_dir, agent_id)
    kb_dir = os.path.join(agent_dir, "kb")
    kb_files = {}
    for dirpath, _dirnames, filenames in os.walk(kb_dir):
        for name in filenames:
            full = os.path.join(dirpath, name)
            kb_files[os.path.relpath(full, kb_dir)] = read_text(full)

    return {
        "fields": fields,
        "system_prompt": read_text(os.path.join(agent_dir, "SYSTEM.md")),
        "kb_files": kb_files,
        "tools": sorted(db.get_agent_tools(agent_id)),
        "skills": sorted(db.get_agent_skills(agent_id)),
        "variables": sorted(
            (row["key"], row["value"], int(bool(row["is_secret"])))
            for row in db.get_agent_variables(agent_id)
        ),
    }


@pytest.fixture
def client():
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture
def examples_root(tmp_path):
    """A throwaway repo root holding copies of the shipped examples."""
    return make_examples_root(tmp_path, "examples")


# ---------------------------------------------------------------------------
# 1. The shipped examples are valid, loadable and dependency-clean
# ---------------------------------------------------------------------------

def test_shipped_examples_are_the_expected_three():
    assert set(shipped_ids()) >= set(EXAMPLE_IDS)


@pytest.mark.parametrize("template_id", EXAMPLE_IDS)
def test_example_is_a_valid_canonical_template(template_id):
    raw = payload(template_id)
    canonical = tpl.validate_template(raw)

    # The file on disk *is* the canonical form: no editor round-trip needed.
    assert canonical == tpl.validate_template(canonical)
    assert canonical["id"] == template_id
    assert canonical["schema_version"] == tpl.TEMPLATE_SCHEMA_VERSION
    assert canonical["system_prompt"].strip()
    assert canonical["kb_files"]


@pytest.mark.parametrize("template_id", EXAMPLE_IDS)
def test_example_is_listed_and_readable_via_the_ui_payload(client, monkeypatch,
                                                           examples_root,
                                                           template_id):
    # The UI reads config.BASE_DIR lazily, so this is all it takes to serve the
    # throwaway root: the payload below is proven to come from disk, not memory.
    monkeypatch.setattr(config, "BASE_DIR", examples_root, raising=False)
    login(client)
    response = client.get("/api/templates/%s" % template_id)
    assert response.status_code == 200, response.get_json()

    body = response.get_json()
    assert body["ok"] is True, body["errors"]
    assert body["tools"]["missing"] == []
    assert body["skills"]["missing"] == []
    assert body["deps_checked"] == {"tools": True, "skills": True}

    # The editor payload is exactly what is on disk (round-trip, no mutation).
    served = dict(body["template"])
    served.pop("_meta", None)
    assert served == tpl.validate_template(payload(template_id))


def test_examples_cover_every_documented_template_feature():
    payloads = [payload(template_id) for template_id in EXAMPLE_IDS]

    # a required parameter that still carries a default
    assert any(
        param.get("required") and param.get("default") not in (None, "")
        for p in payloads for param in p["parameters"]
    )
    # select + boolean + number parameters, with option lists where required
    types = {param["type"] for p in payloads for param in p["parameters"]}
    assert {"text", "select", "boolean", "number"} <= types
    for p in payloads:
        for param in p["parameters"]:
            if param["type"] == "select":
                assert param["options"]

    # a non-trivial defaults set, mixing basic and advanced agent settings
    for p in payloads:
        defaults = set(p["defaults"])
        assert "enabled" in defaults
        assert defaults & ADVANCED_DEFAULTS
        assert len(defaults) >= 5

    # tools, skills, variables (at least one secret, no secret defaults), KB
    for p in payloads:
        assert p["tools"], p["id"]
        assert p["skills"], p["id"]
        assert p["variables"], p["id"]
        assert p["kb_files"], p["id"]
    all_variables = [v for p in payloads for v in p["variables"]]
    assert any(v.get("is_secret") for v in all_variables)
    assert any(not v.get("is_secret") for v in all_variables)
    for variable in all_variables:
        if variable.get("is_secret"):
            assert variable.get("default") is None

    # every secret variable is actually declared to the agent (not inlined)
    for p in payloads:
        prompt = p["system_prompt"]
        for variable in p["variables"]:
            if variable.get("is_secret"):
                assert "{{" + variable["key"] + "}}" not in prompt


# ---------------------------------------------------------------------------
# 2. UI instantiation == programmatic instantiation (spec round-trip)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template_id", EXAMPLE_IDS)
def test_ui_and_programmatic_instantiation_agree(client, tmp_path, monkeypatch,
                                                 template_id):
    programmatic_root = make_examples_root(tmp_path, "programmatic")
    ui_root = make_examples_root(tmp_path, "ui")
    # The UI routes resolve config.BASE_DIR lazily, so this is all it takes to
    # point template lookup *and* agent creation at the throwaway root.
    monkeypatch.setattr(config, "BASE_DIR", ui_root, raising=False)

    params = dict(PARAMS[template_id])
    variables = dict(VARIABLES[template_id])

    before = agent_ids()

    # --- via the programmatic API -----------------------------------------
    programmatic_id = tpl.create_agent_from_template(
        template_id, params, {"variables": variables},
        db=db, base_dir=programmatic_root,
    )
    assert programmatic_id in agent_ids() - before

    # --- via the UI endpoint ----------------------------------------------
    login(client)
    ui_id = "ui_" + template_id
    response = client.post(
        "/api/templates/%s/instantiate" % template_id,
        json={"params": params, "overrides": {"variables": variables},
              "id": ui_id},
    )
    assert response.status_code == 201, response.get_json()
    assert response.get_json()["agent_id"] == ui_id

    # Replaying the same request is idempotent (200, no new agent).
    replay = client.post(
        "/api/templates/%s/instantiate" % template_id,
        json={"params": params, "overrides": {"variables": variables},
              "id": ui_id},
    )
    assert replay.status_code == 200, replay.get_json()
    assert replay.get_json()["replayed"] is True

    # --- the two agents are spec-identical --------------------------------
    from_ui = spec_view(ui_id, ui_root)
    from_api = spec_view(programmatic_id, programmatic_root)
    assert from_ui == from_api

    # Rendering really used the caller's parameters, not the template defaults.
    preview = tpl.preview_template(template_id, params,
                                   base_dir=programmatic_root)
    assert from_api["system_prompt"] == preview["system_prompt"]
    for key, value in params.items():
        if isinstance(value, str):
            assert value in from_api["system_prompt"]

    # The secret reached the agent as a secret row and nowhere else.
    secrets = {key: value for key, value, is_secret in from_api["variables"]
               if is_secret}
    assert variables == secrets

    # ... and the template files on disk were never rewritten.
    assert read_text(os.path.join(ui_root, "agent_templates",
                                  template_id + ".json")) == read_text(
        os.path.join(EXAMPLES_DIR, template_id + ".json"))


# ---------------------------------------------------------------------------
# 3. Simulation: full fidelity, zero footprint, no leaked secrets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template_id", EXAMPLE_IDS)
def test_simulation_leaves_no_rows_no_files_and_no_secret_in_logs(
        tmp_path, monkeypatch, caplog, template_id):
    root = make_examples_root(tmp_path, "simulation")
    monkeypatch.setattr(config, "BASE_DIR", root, raising=False)

    before_files = snapshot(root)
    before_agents = agent_ids()

    # Replace only the model loop: the simulation session, the ephemeral
    # registry, the workspace materialisation and teardown all stay real.
    seen = []

    def fake_handle_message(agent_id, external_user_id, message, session_id=None,
                            skip_buffer=False, **_kwargs):
        # A real turn materialises a session row for the ephemeral agent id --
        # the one durable effect a run has, which teardown must undo.
        sid = db.get_or_create_session(
            agent_id, external_user_id, None, db_agent_id=None)
        seen.append({"agent_id": agent_id, "session_id": sid,
                     "message": message})
        return {"response": "simulated answer", "tool_trace": [], "timeline": []}

    monkeypatch.setattr(runtime_singleton, "handle_message", fake_handle_message)
    caplog.set_level(logging.DEBUG)

    result = simrt.simulate(
        template_id,
        params=dict(PARAMS[template_id]),
        message_history=[
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ],
        overrides={"variables": dict(VARIABLES[template_id])},
    )

    sim_id = result["simulation_id"]
    try:
        # The run really happened, against the ephemeral id.
        assert result["response"] == "simulated answer"
        assert not result.get("error"), result.get("error_message")
        assert seen and seen[0]["agent_id"] == sim_id
        assert sim_id.startswith("sim-%s-" % template_id)

        # NO durable row, NO session row, NO variable row for the sim id.
        assert db.get_agent(sim_id) is None
        assert agent_ids() == before_agents
        assert session_index_count(sim_id) == 0
        assert db.get_agent_variables(sim_id) == []

        # NO files under BASE_DIR (and the /tmp sim tree is gone again).
        assert not os.path.isdir(simrt.sim_root_for(sim_id))
        assert snapshot(root) == before_files

        # The secret never leaks into the result, the template, or any log.
        assert SECRET_NEEDLE not in json.dumps(result)
        assert SECRET_NEEDLE not in caplog.text
        assert SECRET_NEEDLE not in read_text(
            os.path.join(EXAMPLES_DIR, template_id + ".json"))
    finally:
        simrt.teardown(sim_id)


@pytest.mark.parametrize("template_id", EXAMPLE_IDS)
def test_simulation_materialises_the_whole_template(tmp_path, monkeypatch,
                                                    template_id):
    root = make_examples_root(tmp_path, "materialise")
    monkeypatch.setattr(config, "BASE_DIR", root, raising=False)
    before_files = snapshot(root)

    template = tpl.get_template(template_id)
    params = dict(PARAMS[template_id])
    variables = dict(VARIABLES[template_id])
    preview = tpl.preview_template(template_id, params, base_dir=root)
    sim_id = simrt.new_simulation_id(template_id)

    session = simrt.SimulationSession(
        template_id, params=params, overrides={"variables": variables},
        sim_id=sim_id)
    try:
        session.open()
        agent = session.agent
        sim_root = simrt.sim_root_for(sim_id)

        # Fidelity: the template's tools, skills and variables are all present.
        assert set(template["tools"]) <= set(agent["_simulation_tool_ids"])
        assert set(template["skills"]) <= set(agent["_simulation_skill_ids"])
        for key, value in variables.items():
            assert agent["_simulation_variables"][key] == value

        # Fidelity: prompt + KB are materialised under the throwaway root.
        assert read_text(os.path.join(sim_root, "agents", sim_id,
                                      "SYSTEM.md")) == preview["system_prompt"]
        kb_dir = os.path.join(sim_root, "agents", sim_id, "kb")
        for rel, content in template["kb_files"].items():
            assert read_text(os.path.join(kb_dir, rel)) == content

        # Containment: the whole tree lives under /tmp, never under BASE_DIR.
        assert os.path.realpath(sim_root).startswith(
            os.path.realpath(simrt.SIM_ROOT_BASE))
        assert not os.path.exists(
            os.path.join(root, "agents", sim_id))

        # Outbound effects are intercepted at the real chokepoint.
        from backend.agent_runtime.llm_call import _execute_tool_core

        executed = []
        sid = session._ensure_session()
        ctx = dict(agent)
        ctx.update({"_db_agent_id": sim_id, "session_id": sid,
                    "user_id": "__simulation__"})
        outcome = _execute_tool_core(
            "send_notification", {"message": "sim only"},
            lambda fn, args: None,
            lambda fn, args: executed.append((fn, dict(args))) or {"success": True},
            ctx,
        )
        assert outcome.get("simulated") is True
        assert executed == []
        assert [entry["tool"] for entry in _itx.get_simulated_outbox(sim_id)] \
            == ["send_notification"]
    finally:
        session.close()

    # Guaranteed teardown even while a turn was materialised.
    assert db.get_agent(sim_id) is None
    assert session_index_count(sim_id) == 0
    assert not os.path.isdir(simrt.sim_root_for(sim_id))
    # Nothing under BASE_DIR was created, touched or rewritten.
    assert snapshot(root) == before_files
