"""Registration through the template engine, with Evonic stubbed.

Evonic does not need to be installed: ``config``, ``models.db``, ``backend``
(``agent_templates`` / ``agent_factory`` / ``plugin_manager``)
are replaced with the smallest faithful surface the plugin uses, then the
plugin is imported as the ``tiyasancloud`` package. The fake template engine
renders ``agent_templates/<id>.json`` the way the real one does, so the tests
assert on the behaviour the plugin adds on top of it, not on a lock-step mock.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import types
from typing import Any, Dict, List, Optional

import pytest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_ROOT = os.path.dirname(PLUGIN_DIR)

# --------------------------------------------------------------------- stubs
#: Live plugin config the fake ``backend.plugin_manager`` hands back.
PLUGIN_CONFIG: Dict[str, Any] = {}


class _Manager:
    def get_plugin_config(self, plugin_id: str) -> Dict[str, Any]:
        return dict(PLUGIN_CONFIG)


class TemplateError(ValueError):
    pass


class TemplateValidationError(TemplateError):
    pass


class TemplateRenderError(TemplateValidationError):
    pass


class TemplateNotFoundError(TemplateError):
    pass


class TemplateResolveError(TemplateError):
    def __init__(self, message: str, report: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.report = report or {}


class AgentAlreadyExistsError(Exception):
    pass


class SpecValidationError(Exception):
    pass


class FakeDB:
    """The slice of ``models.db`` the plugin touches."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.agents: Dict[str, Dict[str, Any]] = {}
        self.tools: Dict[str, List[str]] = {}
        self.skills: Dict[str, List[str]] = {}
        self.workplaces: Dict[str, Dict[str, Any]] = {}
        self.connectors: Dict[str, Dict[str, Any]] = {}
        self._seq = 0

    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        row = self.agents.get(agent_id)
        return dict(row) if row else None

    def create_agent(self, row: Dict[str, Any]) -> str:
        self.agents[row["id"]] = dict(row)
        return row["id"]

    def update_agent(self, agent_id: str, data: Dict[str, Any]) -> bool:
        if agent_id not in self.agents:
            return False
        self.agents[agent_id].update(data)
        return True

    def get_agent_tools(self, agent_id: str) -> List[str]:
        return list(self.tools.get(agent_id, []))

    def set_agent_tools(self, agent_id: str, tools: Any) -> None:
        self.tools[agent_id] = list(tools or [])

    def get_agent_skills(self, agent_id: str) -> List[str]:
        return list(self.skills.get(agent_id, []))

    def set_agent_skills(self, agent_id: str, skills: Any) -> None:
        self.skills[agent_id] = list(skills or [])

    def set_agent_variables_bulk(self, agent_id: str, variables: Any) -> None:
        self.variables = list(variables or [])

    def delete_agent(self, agent_id: str) -> bool:
        return self.agents.pop(agent_id, None) is not None

    def create_workplace(self, data: Dict[str, Any]) -> str:
        self._seq += 1
        workplace_id = f"wp_{self._seq}"
        self.workplaces[workplace_id] = {**data, "id": workplace_id, "status": "connecting"}
        return workplace_id

    def create_connector(self, data: Dict[str, Any]) -> str:
        self.connectors[data["workplace_id"]] = dict(data)
        return data["id"]

    def get_connector_by_workplace(self, workplace_id: str) -> Optional[Dict[str, Any]]:
        return self.connectors.get(workplace_id)

    def get_workplace(self, workplace_id: str) -> Optional[Dict[str, Any]]:
        return self.workplaces.get(workplace_id)


DB = FakeDB()


class Engine:
    """Records every ``create_agent_from_template`` call and fakes its effects."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.raise_exc: Optional[BaseException] = None

    def create_agent_from_template(
        self,
        template_id: str,
        params: Optional[Dict[str, Any]] = None,
        overrides: Optional[Dict[str, Any]] = None,
        *,
        db: Any = None,
        base_dir: Optional[str] = None,
        if_exists: Optional[str] = None,
        **_kwargs: Any,
    ) -> str:
        params = params or {}
        overrides = dict(overrides or {})
        self.calls.append({
            "template_id": template_id, "params": params, "overrides": overrides,
            "db": db, "base_dir": base_dir, "if_exists": if_exists,
        })
        if self.raise_exc is not None:
            raise self.raise_exc

        root = base_dir or BASE_DIR
        path = os.path.join(root, "agent_templates", f"{template_id}.json")
        if not os.path.isfile(path):
            raise TemplateNotFoundError(f"Template '{template_id}' was not found.")
        with open(path, encoding="utf-8") as handle:
            template = json.load(handle)

        declared = {p["name"]: p for p in template.get("parameters", [])}
        unknown = sorted(set(params) - set(declared))
        if unknown:
            raise TemplateValidationError(
                "Undeclared parameter(s): %s." % ", ".join(unknown)
            )
        values = {
            name: params.get(name, decl.get("default", ""))
            for name, decl in declared.items()
        }
        rendered = re.sub(
            r"\{\{\s*(\w+)\s*\}\}",
            lambda match: str(values.get(match.group(1), "")),
            template.get("system_prompt", ""),
        )

        agent_id = overrides.get("id") or template["id"]
        if if_exists == "error" and agent_id in DB.agents:
            raise AgentAlreadyExistsError(agent_id)
        DB.create_agent({
            "id": agent_id,
            "name": overrides.get("name") or template.get("name") or agent_id,
            "description": overrides.get("description", ""),
            "system_prompt": rendered,
            "workspace": os.path.join(root, "shared", "agents", agent_id),
            "workplace_id": None,
            "model_id": OverrideValue(overrides, "model_id"),
            "enabled": OverrideValue(overrides, "enabled"),
            "sandbox_enabled": OverrideValue(overrides, "sandbox_enabled"),
            "bash_exec_enabled": OverrideValue(overrides, "bash_exec_enabled"),
            "agent_messaging_enabled": OverrideValue(overrides, "agent_messaging_enabled"),
            "builtin_tools_enabled": OverrideValue(overrides, "builtin_tools_enabled"),
        })
        DB.set_agent_tools(agent_id, overrides.get("tools") or template.get("tools") or [])
        if template.get("skills"):
            DB.set_agent_skills(agent_id, template["skills"])
        agent_dir = os.path.join(root, "agents", agent_id)
        os.makedirs(agent_dir, exist_ok=True)
        with open(os.path.join(agent_dir, "SYSTEM.md"), "w", encoding="utf-8") as handle:
            handle.write(rendered)
        return agent_id


class OverrideValue:
    """A sentinel that reports whether an override supplied the field."""

    def __init__(self, overrides: Dict[str, Any], key: str) -> None:
        self._present = key in overrides
        self._value = overrides.get(key)

    def __eq__(self, other: Any) -> bool:
        return self._value == other

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return repr(self._value)


ENGINE = Engine()

#: Mutable module handles so tests can tweak them.
BASE_DIR = ""
CONFIG = types.ModuleType("config")


def _install_stubs() -> None:
    global BASE_DIR
    tmp = tempfile.mkdtemp(prefix="tiyasancloud-test-")
    BASE_DIR = os.path.join(tmp, "base")
    os.makedirs(BASE_DIR, exist_ok=True)
    os.environ["TIYASANCLOUD_PLUGIN_DB"] = os.path.join(tmp, "plugin.db")

    CONFIG.BASE_DIR = BASE_DIR
    sys.modules["config"] = CONFIG

    db_module = types.ModuleType("models.db")
    db_module.db = DB
    models_pkg = types.ModuleType("models")
    models_pkg.db = db_module
    sys.modules["models"] = models_pkg
    sys.modules["models.db"] = db_module

    templates_mod = types.ModuleType("backend.agent_templates")
    for name in ("TemplateError", "TemplateValidationError", "TemplateRenderError",
                 "TemplateNotFoundError", "TemplateResolveError"):
        setattr(templates_mod, name, globals()[name])
    templates_mod.create_agent_from_template = ENGINE.create_agent_from_template
    templates_mod.get_template = lambda template_id, base_dir=None: {"id": template_id}
    templates_mod.render_system_prompt = lambda template, params=None, **kw: ""

    factory_mod = types.ModuleType("backend.agent_factory")
    factory_mod.AgentAlreadyExistsError = AgentAlreadyExistsError
    factory_mod.SpecValidationError = SpecValidationError

    manager_mod = types.ModuleType("backend.plugin_manager")
    manager_mod.plugin_manager = _Manager()

    backend_pkg = types.ModuleType("backend")
    backend_pkg.agent_templates = templates_mod
    backend_pkg.agent_factory = factory_mod
    backend_pkg.plugin_manager = manager_mod
    sys.modules["backend"] = backend_pkg
    sys.modules["backend.agent_templates"] = templates_mod
    sys.modules["backend.agent_factory"] = factory_mod
    sys.modules["backend.plugin_manager"] = manager_mod

    if PLUGINS_ROOT not in sys.path:
        sys.path.insert(0, PLUGINS_ROOT)


_install_stubs()

import tiyasancloud.agents as agents  # noqa: E402
import tiyasancloud.common as common  # noqa: E402
import tiyasancloud.store as store  # noqa: E402


# ------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _clean():
    """Fresh fake Evonic state, template root and plugin DB for every test."""
    DB.reset()
    ENGINE.reset()
    PLUGIN_CONFIG.clear()
    shutil_rmtree_files(os.path.join(BASE_DIR, "agent_templates"))
    shutil_rmtree_files(os.path.join(BASE_DIR, "agents"))
    # A brand-new plugin DB per test (store caches the schema per path).
    db_file = store._db_path()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_file + suffix)
        except FileNotFoundError:
            pass
    store._ready_path = None
    yield
    try:
        os.environ.pop("TIYASANCLOUD_PLUGIN_DB", None)
    except KeyError:  # pragma: no cover
        pass


def shutil_rmtree_files(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def write_template(template_id: str, **overrides: Any) -> Dict[str, Any]:
    payload = {
        "id": template_id,
        "name": "Support Triage Bot",
        "description": "Triages support mail",
        "parameters": [
            {"name": "company", "label": "Company", "type": "text", "required": True},
        ],
        "system_prompt": "You triage support for {{company}}.",
        "tools": ["web_search", "sshc", "send_file", "send_agent_message"],
    }
    payload.update(overrides)
    directory = os.path.join(BASE_DIR, "agent_templates")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f"{template_id}.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return payload


def base_body(**extra: Any) -> Dict[str, Any]:
    body = {
        "agent_id": "tc_1",
        "name": "customer@example.com",
        "description": "TiyasanCloud customer",
        "locale": "id",
        "account": {"id": "acc_1"},
        "instance": {"slug": "acme-server"},
        "hosting_prompt": "You run for PT Acme.",
        "profile_prompt": "The customer prefers Indonesian.",
    }
    body.update(extra)
    return body


def read_prompt(agent_id: str) -> str:
    with open(os.path.join(BASE_DIR, "agents", agent_id, "SYSTEM.md"), encoding="utf-8") as handle:
        return handle.read()


# ---------------------------------------------------------------------- tests
def test_template_path_calls_the_engine_with_the_right_arguments():
    write_template("support_triage_bot")
    body = base_body(template={
        "id": "support_triage_bot",
        "params": {"company": "PT Acme"},
        "overrides": {"variables": {"CRM_API_TOKEN": "s3cret"}},
    })

    result, created = agents.register("tc_1", body)

    assert created is True
    assert result["agent_id"] == "tc_1"
    assert result["workplace_id"] and result["connector_token"].startswith("ect_")
    assert len(ENGINE.calls) == 1
    call = ENGINE.calls[0]
    assert call["template_id"] == "support_triage_bot"
    assert call["params"] == {"company": "PT Acme"}
    assert call["if_exists"] == "error"
    assert call["overrides"]["id"] == "tc_1"
    assert call["overrides"]["variables"] == {"CRM_API_TOKEN": "s3cret"}


def test_rendered_prompt_written_into_system_md_between_the_blocks():
    write_template("support_triage_bot")
    body = base_body(template={"id": "support_triage_bot", "params": {"company": "PT Acme"}})

    agents.register("tc_1", body)

    prompt = read_prompt("tc_1")
    assert "PT Acme" in prompt
    assert prompt.lstrip().startswith(agents.PROFILE_MARK)
    assert prompt.rstrip().endswith(agents.HOSTING_END)
    assert agents.PROFILE_END in prompt and agents.HOSTING_MARK in prompt


def test_meta_records_the_template_and_params():
    write_template("support_triage_bot")
    body = base_body(template={"id": "support_triage_bot", "params": {"company": "PT Acme"}})

    agents.register("tc_1", body)

    meta = store.get_meta("tc_1")
    assert meta["template_id"] == "support_triage_bot"
    assert meta["template_params"] == {"company": "PT Acme"}


def test_wildcard_keeps_every_template_tool_but_still_confines_flags():
    write_template("support_triage_bot")
    body = base_body(template={"id": "support_triage_bot", "params": {"company": "PT Acme"}})

    result, _ = agents.register("tc_1", body)

    # ALLOWED_TOOLS defaults to "*": no filtering, host tools survive.
    assert set(DB.get_agent_tools("tc_1")) == {
        "web_search", "sshc", "send_file", "send_agent_message"
    }
    row = DB.get_agent("tc_1")
    assert row["workplace_id"] == result["workplace_id"]
    assert row["sandbox_enabled"] == 0
    assert row["bash_exec_enabled"] == 0
    assert row["agent_messaging_enabled"] == 0
    assert row["builtin_tools_enabled"] == 0
    assert row["enabled"] == 0


def test_empty_allowed_tools_also_means_no_filtering():
    PLUGIN_CONFIG["ALLOWED_TOOLS"] = ""
    write_template("support_triage_bot")
    agents.register("tc_1", base_body(template={
        "id": "support_triage_bot", "params": {"company": "PT Acme"}}))

    assert "web_search" in DB.get_agent_tools("tc_1")


def test_explicit_allowlist_still_filters():
    PLUGIN_CONFIG["ALLOWED_TOOLS"] = "web_search"
    write_template("support_triage_bot")
    agents.register("tc_1", base_body(template={
        "id": "support_triage_bot", "params": {"company": "PT Acme"}}))

    assert DB.get_agent_tools("tc_1") == ["web_search"]


def test_callers_cannot_override_id_workplace_or_enabled():
    write_template("support_triage_bot")
    body = base_body(template={
        "id": "support_triage_bot",
        "params": {"company": "PT Acme"},
        "overrides": {
            "id": "evil_agent",
            "enabled": 1,
            "sandbox_enabled": 1,
            "agent_messaging_enabled": 1,
            "builtin_tools_enabled": 1,
            "workplace_id": "wp_evil",
        },
    })

    result, created = agents.register("tc_1", body)

    assert created is True
    assert result["agent_id"] == "tc_1"
    overrides = ENGINE.calls[0]["overrides"]
    assert overrides["id"] == "tc_1"
    assert overrides["enabled"] == 0
    assert overrides["sandbox_enabled"] == 0
    assert overrides["agent_messaging_enabled"] == 0
    assert overrides["builtin_tools_enabled"] == 0
    assert "workplace_id" not in overrides or overrides["workplace_id"] == "wp_evil"
    row = DB.get_agent("tc_1")
    assert row["enabled"] == 0 and row["sandbox_enabled"] == 0
    assert row["workplace_id"] == result["workplace_id"]


def test_existing_agent_is_returned_unchanged():
    DB.create_agent({"id": "tc_1", "name": "existing", "workplace_id": "wp_old"})
    DB.workplaces["wp_old"] = {"id": "wp_old", "type": "tunnel", "status": "connected"}
    DB.create_connector({"id": "c1", "workplace_id": "wp_old", "connector_token": "ect_old"})
    write_template("support_triage_bot")

    result, created = agents.register("tc_1", base_body(template={
        "id": "support_triage_bot", "params": {"company": "PT Acme"}}))

    assert created is False
    assert result["connector_token"] == "ect_old"
    assert ENGINE.calls == []
    assert store.get_meta("tc_1").get("template_id") is None
    assert DB.get_agent("tc_1")["name"] == "existing"


def test_unknown_template_maps_to_404():
    with pytest.raises(common.ApiError) as excinfo:
        agents.register("tc_1", base_body(template={"id": "nope", "params": {}}))

    assert excinfo.value.status == 404
    assert excinfo.value.code == "template_not_found"


def test_invalid_template_maps_to_400():
    write_template("support_triage_bot")
    body = base_body(template={"id": "support_triage_bot", "params": {"bogus": "x"}})

    with pytest.raises(common.ApiError) as excinfo:
        agents.register("tc_1", body)

    assert excinfo.value.status == 400
    assert excinfo.value.code == "invalid_template"


def test_unresolved_template_maps_to_409_with_the_report():
    write_template("support_triage_bot")
    ENGINE.raise_exc = TemplateResolveError(
        "unresolved", report={"errors": ["tool 'zzz' is missing"]})

    with pytest.raises(common.ApiError) as excinfo:
        agents.register("tc_1", base_body(template={
            "id": "support_triage_bot", "params": {"company": "PT Acme"}}))

    assert excinfo.value.status == 409
    assert excinfo.value.code == "template_unresolved"
    assert "zzz" in excinfo.value.message


def test_skillset_field_is_ignored_and_the_template_wins():
    """A console that still sends 'skillset' keeps working: it is never read."""
    write_template("support_triage_bot")
    body = base_body(skillset="legacy", template={
        "id": "support_triage_bot", "params": {"company": "PT Acme"}})

    result, created = agents.register("tc_1", body)

    assert created is True
    assert result["agent_id"] == "tc_1"
    # The template built the agent and the legacy field changed nothing.
    assert len(ENGINE.calls) == 1
    assert ENGINE.calls[0]["template_id"] == "support_triage_bot"
    prompt = read_prompt("tc_1")
    assert "PT Acme" in prompt
    assert agents.PROFILE_MARK in prompt and agents.HOSTING_MARK in prompt
    assert "bash" not in DB.get_agent_tools("tc_1")
    assert store.get_meta("tc_1")["template_id"] == "support_triage_bot"
    assert store.get_meta("tc_1").get("skillset") is None


def test_skillset_only_request_is_refused_without_a_template():
    """No 'template' object and no AGENT_TEMPLATE_ID: refused, nothing created."""
    body = base_body(skillset="legacy")

    with pytest.raises(common.ApiError) as excinfo:
        agents.register("tc_1", body)

    assert excinfo.value.status == 400
    assert excinfo.value.code == "template_required"
    assert "AGENT_TEMPLATE_ID" in excinfo.value.message
    assert ENGINE.calls == []
    assert DB.get_agent("tc_1") is None
    assert DB.workplaces == {} and DB.connectors == {}
    assert store.get_meta("tc_1") == {}


def test_save_meta_ignores_the_legacy_skillset_column():
    """The column is history: passing it no longer writes anything."""
    store.save_meta("tc_1", locale="id", skillset="legacy")

    meta = store.get_meta("tc_1")
    assert meta["locale"] == "id"
    assert meta.get("skillset") is None


def test_template_required_also_when_nothing_is_sent():
    """An empty request is refused before anything is touched."""
    with pytest.raises(common.ApiError) as excinfo:
        agents.register("tc_1", base_body())

    assert excinfo.value.status == 400
    assert excinfo.value.code == "template_required"
    assert DB.get_agent("tc_1") is None


def test_default_template_setting_uses_request_params():
    PLUGIN_CONFIG["AGENT_TEMPLATE_ID"] = "support_triage_bot"
    write_template("support_triage_bot")

    body = base_body(params={"company": "PT Acme"})
    body.pop("agent_id", None)
    body["agent_id"] = "tc_1"

    result, created = agents.register("tc_1", body)

    assert created is True
    assert result["agent_id"] == "tc_1"
    assert len(ENGINE.calls) == 1
    assert ENGINE.calls[0]["template_id"] == "support_triage_bot"
    assert ENGINE.calls[0]["params"] == {"company": "PT Acme"}


if __name__ == "__main__":  # pragma: no cover - manual run
    sys.exit(pytest.main([__file__, "-q"]))
