"""Unit tests for the template HTTP layer (:mod:`routes.templates`).

These tests pin the *route-level* contract that the template engine (T2) and the
simulation runtime (T5) cannot enforce on their own:

* every template write is privileged (401 unauthenticated / 403 authenticated
  but not privileged) while reads stay open to any authenticated caller,
* ``workplace_id`` / ``primary_channel_id`` are instantiation-time inputs — never
  template defaults, never ``params`` members — and they are authZ-checked
  against the caller before anything is created,
* ``/simulate`` is per-(caller, template) rate limited (429 + ``Retry-After``)
  and its output is capped,
* ``/instantiate`` answers 201 + ``Location`` and replays a deterministic caller
  supplied id as 200 without creating a duplicate,
* ``/render`` persists nothing at all.

The simulation runtime is replaced by a fake: these tests cover the route layer's
auth/limit/cap behaviour, not the runtime internals (T5 owns those).
"""

import hashlib
import os

import pytest

import config
from app import app
from models.db import db

import routes.templates as templates_routes
from backend import agent_templates as tpl

TEMPLATE_ID = "route_support_bot"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def template_payload(**overrides):
    """Return a complete, valid template payload (overridable per test)."""
    payload = {
        "id": TEMPLATE_ID,
        "name": "Route Support Bot",
        "description": "Answers customer questions.",
        "category": "support",
        "icon": "headset",
        "parameters": [
            {"name": "company", "label": "Company", "type": "text",
             "required": True, "default": "Acme"},
            {"name": "tone", "type": "select",
             "options": ["formal", "friendly"], "default": "friendly"},
        ],
        "system_prompt": "You support {{company}} in a {{tone}} tone.",
        "defaults": {"sandbox_enabled": 1},
        "tools": [],
        "skills": [],
        "variables": [],
        "kb_files": {"notes.md": "# Notes\n"},
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    """Redirect the template store *and* agent creation into a throwaway root.

    ``agent_templates`` and ``agent_factory`` both resolve ``config.BASE_DIR``
    lazily, so patching it moves ``agent_templates/`` and ``agents/`` alike.
    """
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


class FakeSimulationRuntime:
    """Stand-in for :mod:`backend.agent_runtime.simulation_runtime`."""

    def __init__(self, result=None, error=None):
        self.result = result or {
            "simulation_id": "sim-0001",
            "response": "simulated answer",
            "tool_trace": [],
            "timeline": [],
            "outbox": [],
        }
        self.error = error
        self.calls = []

    def simulate(self, template_id, params=None, message_history=None, *,
                 overrides=None, external_user_id=None, sim_id=None,
                 orphan_ttl=None):
        self.calls.append({
            "template_id": template_id,
            "params": dict(params or {}),
            "message_history": list(message_history or []),
            "overrides": dict(overrides or {}),
            "external_user_id": external_user_id,
        })
        if self.error is not None:
            raise self.error
        return dict(self.result)


@pytest.fixture
def fake_runtime(monkeypatch):
    runtime = FakeSimulationRuntime()
    monkeypatch.setattr(
        templates_routes, "_load_simulation_runtime", lambda: runtime)
    return runtime


def login(client, user_id=None):
    with client.session_transaction() as session:
        session["authenticated"] = True
        if user_id is not None:
            session["_user_id"] = user_id


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


def create_template_via_api(client, **overrides):
    response = client.post("/api/templates", json=template_payload(**overrides))
    assert response.status_code == 201, response.get_json()
    return response


def agent_ids():
    return {agent["id"] for agent in db.get_agents()}


# ---------------------------------------------------------------------------
# AuthZ: authentication
# ---------------------------------------------------------------------------

def test_api_requires_authentication(client, repo_root):
    """Every API route answers 401 (JSON) to an anonymous caller."""
    calls = [
        ("get", "/api/templates", None),
        ("post", "/api/templates", template_payload()),
        ("get", "/api/templates/%s" % TEMPLATE_ID, None),
        ("put", "/api/templates/%s" % TEMPLATE_ID, {"name": "nope"}),
        ("delete", "/api/templates/%s" % TEMPLATE_ID, None),
        ("post", "/api/templates/%s/render" % TEMPLATE_ID, {"params": {}}),
        ("post", "/api/templates/%s/simulate" % TEMPLATE_ID, {"message": "hi"}),
        ("post", "/api/templates/%s/instantiate" % TEMPLATE_ID, {}),
    ]
    for method, path, body in calls:
        response = getattr(client, method)(path, json=body)
        assert response.status_code == 401, (method, path, response.status_code)
        assert response.get_json().get("error")


def test_page_routes_redirect_anonymous_callers(client, repo_root):
    for path in ("/templates", "/template/%s" % TEMPLATE_ID):
        response = client.get(path)
        assert response.status_code == 302, path
        assert "/login" in response.headers["Location"]


def test_page_routes_render_for_authenticated_callers(client, repo_root):
    login(client)
    create_template_via_api(client)

    gallery = client.get("/templates")
    assert gallery.status_code == 200

    editor = client.get("/template/%s" % TEMPLATE_ID)
    assert editor.status_code == 200

    # An unknown id is the editor's "create new" state, not a 404.
    assert client.get("/template/brand_new_template").status_code == 200


# ---------------------------------------------------------------------------
# AuthZ: privileged writes
# ---------------------------------------------------------------------------

def test_unprivileged_caller_cannot_write_templates(client, repo_root, monkeypatch):
    monkeypatch.setenv(templates_routes.PRIVILEGED_CALLERS_ENV, "someone_else")
    login(client, "not_privileged")

    assert client.post(
        "/api/templates", json=template_payload()).status_code == 403
    assert client.put(
        "/api/templates/%s" % TEMPLATE_ID, json={"name": "x"}).status_code == 403
    assert client.delete(
        "/api/templates/%s" % TEMPLATE_ID).status_code == 403
    # Nothing was written by the rejected create.
    assert not tpl.has_template(TEMPLATE_ID)


def test_unprivileged_caller_can_still_read(client, repo_root, monkeypatch):
    login(client)
    create_template_via_api(client)

    monkeypatch.setenv(templates_routes.PRIVILEGED_CALLERS_ENV, "someone_else")
    login(client, "not_privileged")

    listing = client.get("/api/templates")
    assert listing.status_code == 200
    assert listing.get_json()["count"] == 1

    detail = client.get("/api/templates/%s" % TEMPLATE_ID)
    assert detail.status_code == 200
    assert detail.get_json()["privileged"] is False


def test_allowlisted_caller_can_write_templates(client, repo_root, monkeypatch):
    monkeypatch.setenv(templates_routes.PRIVILEGED_CALLERS_ENV, "admin,other")
    login(client, "admin")

    response = create_template_via_api(client)
    assert response.headers["Location"] == "/api/templates/%s" % TEMPLATE_ID


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def test_template_crud_roundtrip(client, repo_root):
    login(client)

    created = create_template_via_api(client)
    assert created.headers["Location"] == "/api/templates/%s" % TEMPLATE_ID
    assert created.get_json()["template"]["id"] == TEMPLATE_ID

    assert tpl.has_template(TEMPLATE_ID)

    listing = client.get("/api/templates")
    assert listing.status_code == 200
    body = listing.get_json()
    assert [entry["id"] for entry in body["templates"]] == [TEMPLATE_ID]
    assert body["collisions"] == []
    assert body["count"] == 1

    # Duplicate create is a conflict, never a silent overwrite.
    assert client.post(
        "/api/templates", json=template_payload()).status_code == 409

    detail = client.get("/api/templates/%s" % TEMPLATE_ID)
    assert detail.status_code == 200
    report = detail.get_json()
    assert report["ok"] is True
    assert report["parameters"]["declared"] == ["company", "tone"]
    assert report["template"]["name"] == "Route Support Bot"

    updated = client.put(
        "/api/templates/%s" % TEMPLATE_ID, json={"description": "Updated."})
    assert updated.status_code == 200
    assert updated.get_json()["template"]["description"] == "Updated."
    # Shallow merge: untouched keys survive.
    assert updated.get_json()["template"]["name"] == "Route Support Bot"

    deleted = client.delete("/api/templates/%s" % TEMPLATE_ID)
    assert deleted.status_code == 200
    assert deleted.get_json() == {"id": TEMPLATE_ID, "deleted": True}
    assert not tpl.has_template(TEMPLATE_ID)

    assert client.get(
        "/api/templates/%s" % TEMPLATE_ID).status_code == 404
    assert client.delete(
        "/api/templates/%s" % TEMPLATE_ID).status_code == 404


def test_invalid_template_is_rejected(client, repo_root):
    login(client)
    response = client.post("/api/templates", json={
        "id": "bad id", "name": "Bad", "system_prompt": "hi",
    })
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_template"


def test_api_get_unknown_template_is_404(client, repo_root):
    login(client)
    response = client.get("/api/templates/does_not_exist")
    assert response.status_code == 404
    assert response.get_json()["error"] == "template_not_found"


# ---------------------------------------------------------------------------
# AuthZ: workplace/channel bindings are never template declarations
# ---------------------------------------------------------------------------

def test_create_rejects_baked_workplace_default(client, repo_root):
    login(client)
    response = client.post("/api/templates", json=template_payload(
        defaults={"sandbox_enabled": 1, "workplace_id": "wp-1"}))
    assert response.status_code == 400
    assert not tpl.has_template(TEMPLATE_ID)


def test_create_rejects_binding_parameter_name(client, repo_root):
    login(client)
    response = client.post("/api/templates", json=template_payload(
        parameters=[{"name": "primary_channel_id", "type": "text"}]))
    assert response.status_code == 400
    assert not tpl.has_template(TEMPLATE_ID)


def test_create_rejects_binding_variable_name(client, repo_root):
    login(client)
    response = client.post("/api/templates", json=template_payload(
        variables=[{"key": "workplace_id"}]))
    assert response.status_code == 400
    assert not tpl.has_template(TEMPLATE_ID)


def test_update_rejects_baked_workplace_default(client, repo_root):
    login(client)
    create_template_via_api(client)
    response = client.put("/api/templates/%s" % TEMPLATE_ID, json={
        "defaults": {"workplace_id": "wp-1"}})
    assert response.status_code == 400
    assert "workplace_id" not in (
        tpl.get_template(TEMPLATE_ID).get("defaults") or {})


# ---------------------------------------------------------------------------
# /render — preview only, never persists
# ---------------------------------------------------------------------------

def test_render_does_not_persist(client, repo_root):
    login(client)
    create_template_via_api(client)

    files_before = snapshot(repo_root)
    agents_before = agent_ids()

    response = client.post("/api/templates/%s/render" % TEMPLATE_ID,
                           json={"params": {"company": "Initech"}})
    assert response.status_code == 200
    body = response.get_json()
    assert body["persisted"] is False
    assert body["system_prompt"] == "You support Initech in a friendly tone."
    assert body["values"]["company"] == "Initech"
    assert body["values"]["tone"] == "friendly"
    assert body["spec_preview"]["name"] == "Route Support Bot"
    assert body["resolution"]["ok"] is True

    assert snapshot(repo_root) == files_before
    assert agent_ids() == agents_before


def test_render_tolerates_missing_required_parameter(client, repo_root):
    login(client)
    create_template_via_api(client, parameters=[
        {"name": "company", "type": "text", "required": True},
    ], system_prompt="You support {{company}}.")

    response = client.post("/api/templates/%s/render" % TEMPLATE_ID, json={})
    assert response.status_code == 200
    body = response.get_json()
    assert body["values"]["company"] == ""
    assert any("company" in warning for warning in body["warnings"])


def test_render_rejects_binding_in_params(client, repo_root):
    login(client)
    create_template_via_api(client)
    response = client.post("/api/templates/%s/render" % TEMPLATE_ID,
                           json={"params": {"workplace_id": "wp-1"}})
    assert response.status_code == 400
    assert response.get_json()["error"] == "binding_in_params"


# ---------------------------------------------------------------------------
# /simulate — auth + rate limit + output caps
# ---------------------------------------------------------------------------

def test_simulate_runs_through_the_runtime(client, repo_root, fake_runtime):
    login(client)
    create_template_via_api(client)

    response = client.post("/api/templates/%s/simulate" % TEMPLATE_ID, json={
        "message": "hello",
        "params": {"company": "Initech"},
    })
    assert response.status_code == 200
    body = response.get_json()
    assert body["response"] == "simulated answer"
    assert body["truncated"] is False
    assert len(fake_runtime.calls) == 1
    call = fake_runtime.calls[0]
    assert call["template_id"] == TEMPLATE_ID
    assert call["params"] == {"company": "Initech"}
    assert call["message_history"] == [{"role": "user", "content": "hello"}]
    assert call["external_user_id"] == "template-sim:admin"


def test_simulate_rejects_workplace_and_channel_bindings(
        client, repo_root, fake_runtime):
    login(client)
    create_template_via_api(client)

    response = client.post("/api/templates/%s/simulate" % TEMPLATE_ID, json={
        "message": "hello", "workplace_id": "wp-1"})
    assert response.status_code == 400
    assert response.get_json()["error"] == "binding_not_applicable"
    assert fake_runtime.calls == []


def test_simulate_output_is_capped(client, repo_root, monkeypatch):
    login(client)
    create_template_via_api(client)

    long_text = "x" * (templates_routes.MAX_SIMULATE_RESPONSE_CHARS * 3)
    runtime = FakeSimulationRuntime(result={
        "simulation_id": "sim-capped",
        "response": long_text,
        "tool_trace": [{"tool": "n"} for n in range(200)],
        "timeline": [],
        "outbox": [{"tool": "send_file"} for _ in range(200)],
    })
    monkeypatch.setattr(
        templates_routes, "_load_simulation_runtime", lambda: runtime)

    response = client.post("/api/templates/%s/simulate" % TEMPLATE_ID,
                           json={"message": "hello"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["truncated"] is True
    assert len(body["response"]) <= templates_routes.MAX_SIMULATE_RESPONSE_CHARS
    assert len(body["tool_trace"]) <= templates_routes.MAX_SIMULATE_TRACE_ITEMS
    assert len(body["outbox"]) <= templates_routes.MAX_SIMULATE_OUTBOX_ITEMS
    assert set(body["truncated_fields"]) == {"response", "tool_trace", "outbox"}


def test_simulate_is_rate_limited_per_caller_and_template(
        client, repo_root, fake_runtime, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(templates_routes, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(templates_routes, "SIMULATE_RATE_LIMIT", 3)
    monkeypatch.setattr(templates_routes, "SIMULATE_RATE_WINDOW", 60.0)
    templates_routes.reset_simulate_rate_limits()

    login(client)
    create_template_via_api(client)
    create_template_via_api(client, id="second_bot")

    for _ in range(3):
        assert client.post(
            "/api/templates/%s/simulate" % TEMPLATE_ID,
            json={"message": "hi"}).status_code == 200

    blocked = client.post("/api/templates/%s/simulate" % TEMPLATE_ID,
                          json={"message": "hi"})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.get_json()["error"] == "rate_limit_exceeded"

    # The window is per (caller, template): a different template is unaffected.
    assert client.post("/api/templates/second_bot/simulate",
                       json={"message": "hi"}).status_code == 200

    # A different caller has its own window.
    login(client, "another_admin")
    assert client.post("/api/templates/%s/simulate" % TEMPLATE_ID,
                       json={"message": "hi"}).status_code == 200

    # ...and the original window resets.
    login(client)
    clock[0] += 61.0
    assert client.post("/api/templates/%s/simulate" % TEMPLATE_ID,
                       json={"message": "hi"}).status_code == 200


def test_simulate_input_is_bounded(client, repo_root, fake_runtime):
    login(client)
    create_template_via_api(client)

    too_long = "y" * (templates_routes.MAX_SIMULATE_MESSAGE_CHARS + 1)
    response = client.post("/api/templates/%s/simulate" % TEMPLATE_ID,
                           json={"message": too_long})
    assert response.status_code == 400
    assert response.get_json()["error"] == "message_too_long"
    assert fake_runtime.calls == []


def test_simulate_reports_missing_runtime(client, repo_root, monkeypatch):
    login(client)
    create_template_via_api(client)
    monkeypatch.setattr(
        templates_routes, "_load_simulation_runtime", lambda: None)

    response = client.post("/api/templates/%s/simulate" % TEMPLATE_ID,
                           json={"message": "hi"})
    assert response.status_code == 503
    assert response.get_json()["error"] == "simulation_unavailable"


# ---------------------------------------------------------------------------
# /instantiate — 201 + Location, deterministic id replays as 200
# ---------------------------------------------------------------------------

def test_instantiate_creates_agent_and_replays_idempotently(client, repo_root):
    login(client)
    create_template_via_api(client)
    request_body = {"id": "tpl_agent_one", "params": {"company": "Initech"}}

    first = client.post("/api/templates/%s/instantiate" % TEMPLATE_ID,
                        json=request_body)
    assert first.status_code == 201
    assert first.headers["Location"] == "/api/agents/tpl_agent_one"
    assert first.get_json()["replayed"] is False
    assert first.get_json()["agent_id"] == "tpl_agent_one"

    agent = db.get_agent("tpl_agent_one")
    assert agent is not None
    assert agent["system_prompt"] == "You support Initech in a friendly tone."

    replay = client.post("/api/templates/%s/instantiate" % TEMPLATE_ID,
                         json=request_body)
    assert replay.status_code == 200
    assert replay.headers["Location"] == "/api/agents/tpl_agent_one"
    assert replay.get_json() == {
        "agent_id": "tpl_agent_one",
        "template_id": TEMPLATE_ID,
        "replayed": True,
        "bindings": {},
        "location": "/api/agents/tpl_agent_one",
    }

    # The replay created nothing: still exactly one agent with that id.
    assert [a["id"] for a in db.get_agents()
            if a["id"] == "tpl_agent_one"] == ["tpl_agent_one"]


def test_instantiate_without_id_lets_the_factory_derive_one(client, repo_root):
    login(client)
    create_template_via_api(client)

    response = client.post("/api/templates/%s/instantiate" % TEMPLATE_ID, json={})
    assert response.status_code == 201
    body = response.get_json()
    assert body["replayed"] is False
    assert body["agent_id"]
    assert db.get_agent(body["agent_id"]) is not None
    assert response.headers["Location"] == "/api/agents/%s" % body["agent_id"]


def test_instantiate_reports_unresolved_dependencies(client, repo_root):
    login(client)
    create_template_via_api(client, tools=["definitely_not_a_real_tool"])

    response = client.post("/api/templates/%s/instantiate" % TEMPLATE_ID,
                           json={"id": "tpl_missing_deps"})
    assert response.status_code in (422, 400)
    assert db.get_agent("tpl_missing_deps") is None


# ---------------------------------------------------------------------------
# /instantiate — binding authZ
# ---------------------------------------------------------------------------

def test_instantiate_rejects_binding_in_params(client, repo_root):
    login(client)
    create_template_via_api(client)

    response = client.post("/api/templates/%s/instantiate" % TEMPLATE_ID, json={
        "params": {"workplace_id": "wp-1"}})
    assert response.status_code == 400
    assert response.get_json()["error"] == "binding_in_params"


def test_instantiate_rejects_unknown_workplace(client, repo_root):
    login(client)
    create_template_via_api(client)
    before = agent_ids()

    response = client.post("/api/templates/%s/instantiate" % TEMPLATE_ID, json={
        "id": "tpl_agent_wp", "workplace_id": "no_such_workplace"})
    assert response.status_code == 400
    assert response.get_json()["error"] == "unknown_workplace"
    assert agent_ids() == before


def test_instantiate_binds_a_valid_workplace(client, repo_root):
    login(client)
    create_template_via_api(client)
    workplace_id = db.create_workplace({"name": "Local", "type": "local"})

    response = client.post("/api/templates/%s/instantiate" % TEMPLATE_ID, json={
        "id": "tpl_agent_wp", "workplace_id": workplace_id})
    assert response.status_code == 201
    assert response.get_json()["bindings"] == {"workplace_id": workplace_id}
    assert db.get_agent("tpl_agent_wp")["workplace_id"] == workplace_id


def test_instantiate_rejects_foreign_primary_channel(client, repo_root):
    login(client)
    create_template_via_api(client)
    db.create_agent({"id": "channel_owner", "name": "Owner", "system_prompt": ""})
    channel_id = db.create_channel({
        "agent_id": "channel_owner", "type": "rest", "name": "rest-1"})

    response = client.post("/api/templates/%s/instantiate" % TEMPLATE_ID, json={
        "id": "tpl_agent_chan", "primary_channel_id": channel_id})
    assert response.status_code == 403
    assert response.get_json()["error"] == "channel_not_owned"
    assert db.get_agent("tpl_agent_chan") is None


def test_instantiate_binds_a_primary_channel_owned_by_the_new_agent(
        client, repo_root):
    login(client)
    create_template_via_api(client)
    channel_id = db.create_channel({
        "agent_id": "tpl_agent_owned", "type": "rest", "name": "rest-1"})

    response = client.post("/api/templates/%s/instantiate" % TEMPLATE_ID, json={
        "id": "tpl_agent_owned", "primary_channel_id": channel_id})
    assert response.status_code == 201
    assert response.get_json()["bindings"] == {"primary_channel_id": channel_id}
    assert db.get_primary_channel_id("tpl_agent_owned") == channel_id


def test_instantiate_channel_binding_requires_deterministic_id(client, repo_root):
    login(client)
    create_template_via_api(client)
    channel_id = db.create_channel({
        "agent_id": "tpl_agent_owned", "type": "rest", "name": "rest-1"})

    response = client.post("/api/templates/%s/instantiate" % TEMPLATE_ID, json={
        "primary_channel_id": channel_id})
    assert response.status_code == 400
    assert response.get_json()["error"] == "channel_binding_requires_agent_id"


def test_unprivileged_caller_cannot_bind_workplace_or_channel(
        client, repo_root, monkeypatch):
    login(client)
    create_template_via_api(client)
    workplace_id = db.create_workplace({"name": "Local", "type": "local"})

    monkeypatch.setenv(templates_routes.PRIVILEGED_CALLERS_ENV, "someone_else")
    login(client, "not_privileged")

    response = client.post("/api/templates/%s/instantiate" % TEMPLATE_ID, json={
        "id": "tpl_agent_bound", "workplace_id": workplace_id})
    assert response.status_code == 403
    assert response.get_json()["error"] == "forbidden"
    assert db.get_agent("tpl_agent_bound") is None

    # ...but instantiating a plain, unbound agent is not itself privileged.
    plain = client.post("/api/templates/%s/instantiate" % TEMPLATE_ID,
                        json={"id": "tpl_agent_plain"})
    assert plain.status_code == 201
    assert db.get_agent("tpl_agent_plain") is not None
