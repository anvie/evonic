"""E2E / wiring tests for the Templates tab on ``/agents`` (task #29).

The tab is client-side JS over the template HTTP layer (T6), so these tests pin
two things:

* the wiring the browser relies on — the ``/agents`` page ships a Templates tab
  whose JS entry points call ``GET /api/templates``, ``POST /render`` and
  ``POST /instantiate``;
* the endpoint contract the tab exercises end-to-end — a template parameter that
  is *required* but has a default renders correctly when its field is left
  EMPTY, and create-from-template produces a real agent.
"""

import os

import pytest

import config
from app import app
from models.db import db

import routes.templates as templates_routes
from backend import agent_templates as tpl

TEMPLATE_ID = "tab_support_bot"


def template_payload(**overrides):
    """A complete, valid template: a required param WITH a default is the key
    case (leaving its field empty must still render using the default)."""
    payload = {
        "id": TEMPLATE_ID,
        "name": "Tab Support Bot",
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


def make_template(repo_root, **overrides):
    tpl.create_template(template_payload(**overrides), base_dir=repo_root)


# ---------------------------------------------------------------------------
# 1. The /agents page ships the Templates tab + its JS wiring
# ---------------------------------------------------------------------------

def test_agents_page_ships_templates_tab(client, repo_root):
    login(client)
    response = client.get("/agents")
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    # Tab button + panel.
    assert 'id="tabBtnTemplates"' in html
    assert 'id="tab-templates"' in html
    assert "switchTab('templates')" in html
    # Grid + empty state.
    assert 'id="templates-grid"' in html
    assert 'id="templates-empty"' in html
    # Preview + create-from-template modals.
    assert 'id="template-preview-modal"' in html
    assert 'id="template-create-modal"' in html
    assert 'id="tpl-prompt-preview"' in html
    assert 'id="tpl-param-fields"' in html
    # JS entry points + the exact endpoints the tab calls.
    for fn in ("loadTemplates", "renderTemplates", "openTemplatePreview",
               "openTemplateCreate", "submitTemplateCreate",
               "renderTemplatePreview", "templateEditorHref"):
        assert "function " + fn in html, fn
    assert "'/api/templates'" in html
    assert "'/render'" in html
    assert "'/instantiate'" in html
    # 'Edit template' links to the T9 editor page.
    assert "'/template/'" in html


# ---------------------------------------------------------------------------
# 2. GET /api/templates feeds the grid
# ---------------------------------------------------------------------------

def test_list_endpoint_feeds_the_grid(client, repo_root):
    make_template(repo_root)
    login(client)

    response = client.get("/api/templates")
    assert response.status_code == 200
    data = response.get_json()
    by_id = {entry["id"]: entry for entry in data["templates"]}
    assert TEMPLATE_ID in by_id
    entry = by_id[TEMPLATE_ID]
    # The fields templateCard() renders.
    for key in ("name", "description", "category", "icon", "parameter_count"):
        assert key in entry
    assert entry["category"] == "support"
    assert entry["parameter_count"] == 2


# ---------------------------------------------------------------------------
# 3. Required param WITH a default renders when its field is left EMPTY
# ---------------------------------------------------------------------------

def test_required_param_default_renders_when_field_empty(client, repo_root):
    make_template(repo_root)
    login(client)

    # The UI leaves an untouched/required field out of `params`.
    response = client.post(
        "/api/templates/%s/render" % TEMPLATE_ID, json={"params": {}})
    assert response.status_code == 200, response.get_json()
    data = response.get_json()

    assert data["system_prompt"] == "You support Acme in a friendly tone."
    assert data["values"]["company"] == "Acme"
    assert data["values"]["tone"] == "friendly"
    assert data["persisted"] is False

    # An explicit value wins over the default.
    override = client.post(
        "/api/templates/%s/render" % TEMPLATE_ID,
        json={"params": {"company": "Globex", "tone": "formal"}})
    assert override.status_code == 200
    assert override.get_json()["system_prompt"] == \
        "You support Globex in a formal tone."


# ---------------------------------------------------------------------------
# 4. Create-from-template produces the agent (and replay is idempotent)
# ---------------------------------------------------------------------------

def test_create_from_template_produces_agent(client, repo_root):
    make_template(repo_root)
    login(client)

    # The tab sends the id as the deterministic id and the name via `overrides`
    # (the route reads overrides, not a top-level `name`).
    response = client.post(
        "/api/templates/%s/instantiate" % TEMPLATE_ID,
        json={"id": "acme_bot", "overrides": {"name": "Acme Bot"}, "params": {}})
    assert response.status_code == 201, response.get_json()
    data = response.get_json()
    assert data["agent_id"] == "acme_bot"
    assert data["replayed"] is False
    assert response.headers["Location"] == "/api/agents/acme_bot"

    # The agent really exists, with the rendered (default-filled) prompt.
    agent = db.get_agent("acme_bot")
    assert agent is not None
    assert agent["name"] == "Acme Bot"
    prompt_path = os.path.join(repo_root, "agents", "acme_bot", "SYSTEM.md")
    assert os.path.isfile(prompt_path)
    with open(prompt_path, encoding="utf-8") as handle:
        assert "You support Acme in a friendly tone." in handle.read()

    # Replaying the same deterministic id creates nothing new (200 replay).
    replay = client.post(
        "/api/templates/%s/instantiate" % TEMPLATE_ID,
        json={"id": "acme_bot", "overrides": {"name": "Acme Bot"}, "params": {}})
    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True


# ---------------------------------------------------------------------------
# 5. The Templates tab header ships a "New template" action (task #29 follow-up)
# ---------------------------------------------------------------------------

def test_templates_tab_ships_new_template_action_next_to_refresh(client, repo_root):
    """Robin asked for a "create new template" action beside the Refresh button."""
    login(client)
    html = client.get("/agents").get_data(as_text=True)

    # The action exists and points at the editor's create surface.
    assert 'id="template-new-btn"' in html
    assert 'href="/template/new"' in html
    assert "New template" in html

    # It sits in the Templates panel header, AFTER the Refresh button.
    header = html.split('id="tab-templates"', 1)[1].split('id="templates-grid"', 1)[0]
    assert "loadTemplates(true)" in header          # the Refresh button
    assert "template-new-btn" in header
    assert header.index("loadTemplates(true)") < header.index("template-new-btn")


def test_new_template_link_opens_the_editor_in_create_mode(client, repo_root):
    """``/template/new`` is an unknown id, so the editor renders its create mode
    (empty form) instead of prefilling the sentinel as the template id."""
    login(client)
    response = client.get("/template/new")
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert 'id="tpl-editor"' in html
    # Bootstrap carries the URL id but no saved template => create mode.
    assert '"template_id": "new"' in html
    assert '"report": null' in html
    # The editor must ignore the "new" sentinel when seeding the id field.
    assert 'BOOT.template_id !== "new"' in html
