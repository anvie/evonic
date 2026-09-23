"""Unit tests for the legacy ``/api/skillsets*`` compatibility surface.

``routes/skills.py`` serves the *pre-template* skillset API on top of
:mod:`backend.agent_templates` (the module that owns both template roots) and
:mod:`backend.agent_factory` (the only creation path).  Its response shape must
not change, because ``templates/skills.html`` (list + resolve preview),
``templates/agents.html`` (the "Create from Skillset" dropdown) and
``templates/edit_skillset.html`` consume it verbatim.

These tests pin:

* field-for-field parity of the adapters against the legacy reference
  implementation :mod:`backend.skillsets` (real repo skillsets *and* synthetic
  edge-case fixtures),
* the legacy skillset-apply semantics end to end: merged prompt/tools/skills/KB,
  each skillset skill enabled globally *and* assigned to the new agent, the
  default KB files and the workspace/artifacts layout,
* the absence of the old time-of-check/time-of-use pre-check: a conflicting or
  concurrent apply with the same id leaves exactly one agent behind,
* that ``POST /api/agents`` also creates through the factory (no inline
  creation, no pre-check) while keeping its legacy error envelope.
"""

import ast
import json
import os
import threading

import pytest

import config
from app import app
from models.db import db

import routes.skills as skills_routes
from backend import agent_factory
from backend import agent_templates as tpl
from backend import skillsets as legacy_skillsets

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Keys every legacy skillset file in this repo carries.
LEGACY_SKILLSET_KEYS = {
    "id", "name", "description", "system_prompt", "model", "tools", "skills",
    "kb_files",
}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    """Redirect the template store *and* agent creation into a throwaway root.

    ``agent_templates`` and ``agent_factory`` both resolve ``config.BASE_DIR``
    lazily, so patching it moves ``skillsets/``, ``agents/`` and
    ``shared/agents/`` alike.  ``defaults/`` is seeded so the factory's default
    knowledge-base copy can be asserted.
    """
    root = tmp_path / "repo"
    (root / "agent_templates").mkdir(parents=True, exist_ok=True)
    (root / "skillsets").mkdir(parents=True, exist_ok=True)
    defaults = root / "defaults"
    defaults.mkdir(parents=True, exist_ok=True)
    for _target_name, source_name in agent_factory.DEFAULT_KB_FILES:
        (defaults / source_name).write_text(
            "default kb %s" % source_name, encoding="utf-8")
    monkeypatch.setattr(config, "BASE_DIR", str(root), raising=False)
    return str(root)


@pytest.fixture
def client():
    with app.test_client() as test_client:
        yield test_client


def login(client, user_id="admin"):
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["_user_id"] = user_id


def write_skillset(root, filename, payload):
    """Write a legacy skillset file (raw text when *payload* is a string)."""
    path = os.path.join(root, "skillsets", filename)
    with open(path, "w", encoding="utf-8") as handle:
        if isinstance(payload, str):
            handle.write(payload)
        else:
            json.dump(payload, handle, indent=2)
    return path


def skillset_payload(**overrides):
    payload = {
        "id": "gamma",
        "name": "Gamma",
        "description": "Gamma blueprint",
        "system_prompt": "You are Gamma.",
        "model": "",
        "tools": ["bash", "read_file"],
        "skills": ["scheduler", "kanban"],
        "kb_files": {"notes.md": "# Notes\n"},
    }
    payload.update(overrides)
    return payload


def point_legacy_module_at(root, monkeypatch):
    """Make :mod:`backend.skillsets` read the same synthetic root."""
    monkeypatch.setattr(
        legacy_skillsets, "SKILLCSETS_DIR", os.path.join(root, "skillsets"))


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class SkillEnableRecorder:
    """Stand-in for ``skills_manager`` recording the legacy global enables."""

    def __init__(self):
        self.calls = []

    def set_skill_enabled(self, skill_id, enabled):
        self.calls.append((skill_id, enabled))
        return {"id": skill_id, "enabled": bool(enabled)}


def agents_with_id(agent_id):
    return [entry["id"] for entry in db.get_agents() if entry["id"] == agent_id]


# ---------------------------------------------------------------------------
# Adapter parity against the legacy reference implementation
# ---------------------------------------------------------------------------

def test_list_alias_matches_the_legacy_module_field_for_field():
    adapted = [
        skills_routes._legacy_skillset_summary(payload)
        for payload in tpl.legacy_skillsets()
    ]
    assert adapted, "the repo ships skillsets; parity must not be vacuous"
    assert adapted == legacy_skillsets.list_skillsets()
    for entry in adapted:
        assert set(entry) == {
            "id", "name", "description", "tools_count", "skills_count"}


def test_get_alias_matches_the_legacy_module_for_every_repo_skillset():
    skillsets = legacy_skillsets.list_skillsets()
    assert skillsets
    for entry in skillsets:
        raw = tpl.get_legacy_skillset(entry["id"])
        assert raw == legacy_skillsets.get_skillset(entry["id"])
        assert set(raw) == LEGACY_SKILLSET_KEYS


def test_resolve_alias_keeps_the_legacy_field_shape():
    for entry in legacy_skillsets.list_skillsets():
        resolved = tpl.resolve_legacy_skillset(entry["id"])
        raw = legacy_skillsets.get_skillset(entry["id"])
        legacy_resolved = legacy_skillsets.resolve_skillset(entry["id"])

        # Same field surface as the legacy resolver ...
        assert set(resolved) == set(legacy_resolved)
        assert set(resolved) == set(raw) | {"resolved_tools", "unresolved_tools"}
        # ... and resolved/unresolved stay an order-preserving partition of the
        # declared tool names.
        declared = [name for name in raw["tools"] if isinstance(name, str) and name]
        assert resolved["resolved_tools"] + resolved["unresolved_tools"] == declared

        # The template layer checks availability against the full assignable tool
        # surface (registered defs *and* builtin tool ids), so it may resolve a
        # superset of what the legacy resolver resolved; it must never resolve
        # less.  Field parity, not membership parity, is the compatibility
        # contract.
        assert set(resolved["resolved_tools"]) >= set(legacy_resolved["resolved_tools"])


def test_list_route_returns_the_legacy_shape(client, repo_root, monkeypatch):
    login(client)
    write_skillset(repo_root, "alpha.json", skillset_payload(
        id="alpha", name="Alpha", description="first",
        tools=["bash", "nope"], skills=["scheduler"], kb_files={}))
    write_skillset(repo_root, "broken.json", "{not json at all")
    point_legacy_module_at(repo_root, monkeypatch)

    response = client.get("/api/skillsets")
    assert response.status_code == 200
    body = response.get_json()
    assert body["skillsets"] == legacy_skillsets.list_skillsets()
    assert body["skillsets"] == [{
        "id": "alpha",
        "name": "Alpha",
        "description": "first",
        "tools_count": 2,
        "skills_count": 1,
    }]


def test_non_object_legacy_file_is_skipped_not_fatal(client, repo_root, monkeypatch):
    """Documented divergence: a non-object JSON file is skipped, not a 500."""
    login(client)
    write_skillset(repo_root, "not_an_object.json", [1, 2, 3])
    write_skillset(repo_root, "alpha.json", skillset_payload(id="alpha"))
    point_legacy_module_at(repo_root, monkeypatch)

    response = client.get("/api/skillsets")
    assert response.status_code == 200
    assert [entry["id"] for entry in response.get_json()["skillsets"]] == ["alpha"]

    # The legacy loader (the reference for the shape, not for robustness) trips
    # on the very same file - which is why the alias cannot delegate 1:1.
    with pytest.raises(AttributeError):
        legacy_skillsets.list_skillsets()


def test_get_route_returns_the_raw_legacy_payload(client, repo_root, monkeypatch):
    login(client)
    payload = skillset_payload(id="beta")
    write_skillset(repo_root, "beta.json", payload)
    point_legacy_module_at(repo_root, monkeypatch)

    response = client.get("/api/skillsets/beta")
    assert response.status_code == 200
    assert response.get_json() == payload
    assert response.get_json() == legacy_skillsets.get_skillset("beta")


def test_resolve_route_matches_the_legacy_shape(client, repo_root, monkeypatch):
    login(client)
    write_skillset(repo_root, "beta.json", skillset_payload(id="beta"))
    point_legacy_module_at(repo_root, monkeypatch)

    response = client.get("/api/skillsets/beta/resolve")
    assert response.status_code == 200
    body = response.get_json()
    raw = legacy_skillsets.get_skillset("beta")
    assert set(body) == set(raw) | {"resolved_tools", "unresolved_tools"}
    assert body["system_prompt"] == raw["system_prompt"]
    assert body["name"] == raw["name"]


def test_unknown_skillset_is_404_on_every_read(client, repo_root, monkeypatch):
    login(client)
    point_legacy_module_at(repo_root, monkeypatch)

    assert client.get("/api/skillsets/nope").status_code == 404
    assert client.get("/api/skillsets/nope/resolve").status_code == 404
    assert client.get("/api/skillsets/nope").get_json() == {
        "error": "Skillset not found"}


def test_build_legacy_skillset_spec_matches_the_legacy_merge(repo_root, monkeypatch):
    write_skillset(repo_root, "gamma.json", skillset_payload())
    point_legacy_module_at(repo_root, monkeypatch)

    agent_data = {
        "id": "over_agent",
        "name": "Override Name",
        "description": "Override description",
        "system_prompt": "Overridden prompt.",
        "tools": ["calculator"],
        "skills": ["obscura"],
        "kb_files": {"override.md": "O"},
        "model": "model-x",
    }
    spec = tpl.build_legacy_skillset_spec(
        "gamma", agent_data, base_dir=repo_root)
    merged = legacy_skillsets.apply_skillset("gamma", agent_data)

    assert spec["id"] == merged["id"] == "over_agent"
    assert spec["name"] == merged["name"] == "Override Name"
    assert spec["description"] == merged["description"]
    assert spec["system_prompt"] == merged["system_prompt"] == "Overridden prompt."
    assert spec["tools"] == merged["tools"] == ["calculator"]
    assert spec["skills"] == merged["skills"] == ["obscura"]
    # ``kb_files`` (legacy key) -> ``knowledge_base`` (factory shape); the legacy
    # ``model`` key -> ``model_id``, mirroring agent_templates._adapt_legacy.
    assert spec["knowledge_base"] == [{"path": "override.md", "content": "O"}]
    assert spec["model_id"] == merged["model"] == "model-x"

    # Skillset values win when the request body does not override them.
    fallback = tpl.build_legacy_skillset_spec(
        "gamma", {"id": "plain_agent"}, base_dir=repo_root)
    assert fallback["name"] == "Gamma"
    assert fallback["system_prompt"] == "You are Gamma."
    assert fallback["tools"] == ["bash", "read_file"]
    assert fallback["skills"] == ["scheduler", "kanban"]
    assert fallback["knowledge_base"] == [{"path": "notes.md", "content": "# Notes\n"}]
    assert "model_id" not in fallback  # the repo skillsets declare model ''


def test_build_legacy_skillset_spec_rejects_unknown_skillset(repo_root):
    with pytest.raises(tpl.TemplateNotFoundError):
        tpl.build_legacy_skillset_spec("ghost", {"id": "x"}, base_dir=repo_root)


# ---------------------------------------------------------------------------
# Apply: legacy semantics, end to end, through the factory
# ---------------------------------------------------------------------------

def test_apply_creates_an_agent_end_to_end(client, repo_root, monkeypatch):
    login(client)
    recorder = SkillEnableRecorder()
    monkeypatch.setattr(skills_routes, "skills_manager", recorder)
    write_skillset(repo_root, "gamma.json", skillset_payload())
    point_legacy_module_at(repo_root, monkeypatch)

    response = client.post("/api/skillsets/gamma/apply", json={
        "id": "gamma_agent", "name": "Gamma Agent", "description": "from skillset"})

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "agent_id": "gamma_agent",
        "message": "Agent 'Gamma Agent' created from skillset 'gamma'.",
    }

    agent = db.get_agent("gamma_agent")
    assert agent is not None
    assert agent["name"] == "Gamma Agent"
    assert agent["description"] == "from skillset"

    agent_dir = os.path.join(repo_root, "agents", "gamma_agent")
    assert read_text(os.path.join(agent_dir, "SYSTEM.md")) == "You are Gamma."
    assert read_text(os.path.join(agent_dir, "kb", "notes.md")) == "# Notes\n"
    # The factory still copies the defaults/*.md knowledge base.
    for target_name, _source_name in agent_factory.DEFAULT_KB_FILES:
        assert os.path.isfile(os.path.join(agent_dir, "kb", target_name))

    # Tools: the skillset's own tools plus the managed artifact/vision sets.
    tools = set(db.get_agent_tools("gamma_agent"))
    assert {"bash", "read_file"} <= tools
    assert agent_factory.ARTIFACT_TOOLS <= tools
    assert agent_factory.VISION_TOOLS <= tools

    # Skills: assigned to the agent (factory -> db.set_agent_skills) and enabled
    # globally, in skillset order (legacy -> skills_manager.set_skill_enabled).
    assert sorted(db.get_agent_skills("gamma_agent")) == ["kanban", "scheduler"]
    assert recorder.calls == [("scheduler", True), ("kanban", True)]

    # Workspace + artifacts directory layout.
    workspace = os.path.join(repo_root, "shared", "agents", "gamma_agent")
    assert os.path.isdir(os.path.join(workspace, "artifacts"))
    assert agent["workspace"] == workspace


def test_apply_keeps_the_legacy_error_envelope(client, repo_root, monkeypatch):
    login(client)
    point_legacy_module_at(repo_root, monkeypatch)
    write_skillset(repo_root, "gamma.json", skillset_payload())

    missing_id = client.post(
        "/api/skillsets/gamma/apply", json={"name": "No id"})
    assert missing_id.status_code == 400
    assert missing_id.get_json() == {"error": "Agent ID is required."}

    unknown = client.post(
        "/api/skillsets/ghost/apply", json={"id": "ghost_agent"})
    assert unknown.status_code == 404
    assert unknown.get_json() == {"error": "Skillset 'ghost' not found."}

    invalid = client.post(
        "/api/skillsets/gamma/apply", json={"id": "Not Slug"})
    assert invalid.status_code == 400
    assert "id" in invalid.get_json()["error"].lower()


def test_apply_duplicate_id_is_rejected_by_the_primary_key(client, repo_root, monkeypatch):
    login(client)
    monkeypatch.setattr(
        skills_routes, "skills_manager", SkillEnableRecorder())
    write_skillset(repo_root, "gamma.json", skillset_payload())
    point_legacy_module_at(repo_root, monkeypatch)

    body = {"id": "dup_agent", "name": "Dup"}
    first = client.post("/api/skillsets/gamma/apply", json=body)
    assert first.status_code == 200

    second = client.post("/api/skillsets/gamma/apply", json=body)
    assert second.status_code == 409
    assert second.get_json() == {"error": "Agent ID 'dup_agent' already exists."}
    assert agents_with_id("dup_agent") == ["dup_agent"]


def test_concurrent_apply_creates_exactly_one_agent(client, repo_root, monkeypatch):
    """No time-of-check/time-of-use window: the PRIMARY KEY decides."""
    login(client)
    monkeypatch.setattr(
        skills_routes, "skills_manager", SkillEnableRecorder())
    write_skillset(repo_root, "gamma.json", skillset_payload())
    point_legacy_module_at(repo_root, monkeypatch)

    barrier = threading.Barrier(2)
    outcomes = {}
    errors = []

    def worker(name):
        try:
            with app.test_client() as worker_client:
                login(worker_client)
                barrier.wait(timeout=10)
                response = worker_client.post(
                    "/api/skillsets/gamma/apply",
                    json={"id": "race_agent", "name": "Race"})
                outcomes[name] = (response.status_code, response.get_json())
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertions
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(name,)) for name in ("a", "b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert sorted(status for status, _body in outcomes.values()) == [200, 409]
    assert agents_with_id("race_agent") == ["race_agent"]
    # The winner created a fully configured agent.
    assert db.get_agent_skills("race_agent")
    assert os.path.isfile(
        os.path.join(repo_root, "agents", "race_agent", "SYSTEM.md"))


# ---------------------------------------------------------------------------
# POST /api/agents — factory-backed creation, legacy envelope
# ---------------------------------------------------------------------------

def create_agent_body(**overrides):
    body = {"id": "route_agent", "name": "Route Agent", "description": "d"}
    body.update(overrides)
    return body


def test_create_agent_route_delegates_to_the_factory(client, repo_root):
    login(client)

    response = client.post("/api/agents", json=create_agent_body())
    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["agent"]["id"] == "route_agent"
    assert body["agent"]["name"] == "Route Agent"
    assert body["agent"]["system_prompt"] == ""
    assert "workspace" not in body["agent"]  # sanitized, as before

    agent_dir = os.path.join(repo_root, "agents", "route_agent")
    for target_name, _source_name in agent_factory.DEFAULT_KB_FILES:
        assert os.path.isfile(os.path.join(agent_dir, "kb", target_name))

    tools = set(db.get_agent_tools("route_agent"))
    assert agent_factory.ARTIFACT_TOOLS <= tools
    assert agent_factory.VISION_TOOLS <= tools
    assert db.get_agent("route_agent")["workspace"] == os.path.join(
        repo_root, "shared", "agents", "route_agent")


def test_create_agent_route_duplicate_is_rejected_without_a_pre_check(client, repo_root):
    login(client)
    assert client.post("/api/agents", json=create_agent_body()).status_code == 200

    duplicate = client.post("/api/agents", json=create_agent_body(name="Other"))
    assert duplicate.status_code == 400
    assert duplicate.get_json() == {"error": "Agent ID already exists."}
    assert agents_with_id("route_agent") == ["route_agent"]


def test_create_agent_route_keeps_legacy_validation_messages(client, repo_root):
    login(client)

    bad_id = client.post("/api/agents", json=create_agent_body(id="Bad-ID"))
    assert bad_id.status_code == 400
    assert bad_id.get_json()["error"].startswith("Invalid ID.")

    reserved = client.post("/api/agents", json=create_agent_body(id="parent_sub_1"))
    assert reserved.status_code == 400
    assert "sub-agent pattern" in reserved.get_json()["error"]

    long_name = client.post("/api/agents", json=create_agent_body(name="x" * 201))
    assert long_name.status_code == 400
    assert long_name.get_json() == {"error": "Name too long (max 200 characters)."}

    not_an_object = client.post("/api/agents", json=[1, 2])
    assert not_an_object.status_code == 400

    # The factory rejects fields it does not model (workspace is
    # server-controlled) instead of silently ignoring them.
    unsupported = client.post(
        "/api/agents", json=create_agent_body(workspace="/tmp/elsewhere"))
    assert unsupported.status_code == 400
    assert "workspace" in unsupported.get_json()["error"]


def test_create_agent_route_binds_workplace_and_primary_channel(client, repo_root):
    login(client)
    db.create_workplace({"id": "wp_route", "name": "Route WP", "type": "local"})
    db.create_channel({"id": "chan_route", "agent_id": "bound_agent",
                       "name": "Route channel", "type": "webchat"})

    response = client.post("/api/agents", json=create_agent_body(
        id="bound_agent", workplace_id="wp_route", primary_channel_id="chan_route"))
    assert response.status_code == 200

    agent = db.get_agent("bound_agent")
    assert agent["workplace_id"] == "wp_route"
    assert agent["primary_channel_id"] == "chan_route"
    # Bindings never leak into the stored spec: the workspace stays the
    # factory-controlled shared/agents/<id>.
    assert agent["workspace"] == os.path.join(
        repo_root, "shared", "agents", "bound_agent")


def test_create_agent_route_has_no_inline_creation_left():
    """Source-level guard: the route must not grow its own creation path back."""
    path = os.path.join(REPO_ROOT, "routes", "agents.py")
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "api_create_agent"
    )
    body = ast.get_source_segment(source, function)

    assert "agent_factory.create_agent(" in body
    assert "db.create_agent(" not in body
    assert "db.get_agent(agent_id)" not in body  # no pre-check / TOCTOU window
    assert "shutil" not in body  # no default-KB copying here any more
