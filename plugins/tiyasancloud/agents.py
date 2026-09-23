"""Registering TiyasanCloud customer agents.

One console account = one server = one agent. The plugin only ever creates an
agent (confined, with its prompt blocks) and switches it on once its connector
is up. Reading, changing or deleting an agent afterwards is Evonic's business:
an operator deletes it in Evonic, and the prompt changes through the agent's
own WhatsApp conversation.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Dict, Optional, Tuple

from .common import ApiError, agent_prefixes, config, evonic_unavailable, invalid_body, valid_agent_id
from . import store

HOSTING_MARK = "<!-- tiyasan-hosting -->"
HOSTING_END = "<!-- /tiyasan-hosting -->"
PROFILE_MARK = "<!-- tiyasan-profile -->"
PROFILE_END = "<!-- /tiyasan-profile -->"

LOCALES = ("id", "en")

#: What a registration without a template (and without AGENT_TEMPLATE_ID) gets.
TEMPLATE_REQUIRED_HINT = (
    "this endpoint only creates agents from a template: send a 'template' object "
    "({id, params, overrides}) or set AGENT_TEMPLATE_ID in the plugin settings"
)


def _base_dir() -> str:
    from config import BASE_DIR
    return BASE_DIR


def system_prompt_path(agent_id: str) -> str:
    """Where Evonic reads an agent's live prompt from."""
    return os.path.join(_base_dir(), "agents", agent_id, "SYSTEM.md")


def write_prompt(agent_id: str, content: str) -> None:
    path = system_prompt_path(agent_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.islink(path):
        raise ApiError(500, "evonic_unavailable", "SYSTEM.md is a symbolic link")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def put_block(prompt: str, start: str, end: str, body: Optional[str], *, on_top: bool = False) -> str:
    """Replace (or drop) one marked block, leaving every other line untouched."""
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    block = f"{start}\n{body.strip()}\n{end}" if body and body.strip() else ""
    # The console may send the block with its markers already attached.
    if body and body.strip().startswith(start):
        inner = body.strip()
        inner = inner[len(start):]
        if inner.rstrip().endswith(end):
            inner = inner.rstrip()[: -len(end)]
        block = f"{start}\n{inner.strip()}\n{end}"
    if pattern.search(prompt):
        cleaned = pattern.sub(block, prompt, count=1)
    elif not block:
        cleaned = prompt
    elif on_top:
        cleaned = f"{block}\n\n{prompt.lstrip()}"
    else:
        cleaned = f"{prompt.rstrip()}\n\n{block}\n"
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip() + "\n"


def block_hash(body: Optional[str]) -> Optional[str]:
    if not body or not body.strip():
        return None
    return "sha256:" + hashlib.sha256(body.strip().encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ validation

def _text(body: Dict[str, Any], key: str, *, required: bool, limit: int) -> Optional[str]:
    value = body.get(key)
    if value is None or str(value).strip() == "":
        if required:
            raise invalid_body(f"'{key}' is required")
        return None
    text = str(value).strip()
    if len(text) > limit:
        raise invalid_body(f"'{key}' is longer than {limit} characters")
    return text


def _obj(body: Dict[str, Any], key: str, *, required: bool) -> Optional[dict]:
    value = body.get(key)
    if value is None:
        if required:
            raise invalid_body(f"'{key}' is required")
        return None
    if not isinstance(value, dict):
        raise invalid_body(f"'{key}' must be an object")
    return value


# -------------------------------------------------------------------- register

def register(agent_id: str, body: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Create the agent, or report the one that exists. Returns (result, created?).

    A new agent is created confined to its tunnel workplace, with our prompt
    blocks, and disabled; the worker enables it once its connector is up
    (``activate_ready``). An existing agent is never changed and never receives
    template parameters: the result only repeats its connector token, so a
    console that lost the first answer can still start the connector.

    The agent is always built by the template engine: from the request's
    ``template`` object when it carries one, else from the plugin setting
    ``AGENT_TEMPLATE_ID``. A ``skillset`` field a console still sends is
    ignored, never rejected. A request with neither template nor setting is
    refused (``template_required``).
    """
    from models.db import db

    existing = db.get_agent(agent_id)
    if existing:
        return _existing_result(db, agent_id, existing), False

    name = _text(body, "name", required=True, limit=60)
    description = _text(body, "description", required=False, limit=500) or ""
    locale = (str(body.get("locale") or "").strip().lower() or None)
    if locale not in LOCALES:
        raise invalid_body("'locale' must be 'id' or 'en'")
    account = _obj(body, "account", required=True)
    instance = _obj(body, "instance", required=True)
    slug = str((instance or {}).get("slug") or "").strip()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?", slug):
        raise invalid_body("'instance.slug' is required (the server's name)")
    hosting_prompt = _text(body, "hosting_prompt", required=True, limit=20000)
    profile_prompt = _text(body, "profile_prompt", required=False, limit=20000)
    callback_url = _text(body, "callback_url", required=False, limit=500)
    if callback_url and not callback_url.startswith("https://"):
        raise invalid_body("'callback_url' must be https")

    template = _template_request(body)
    if template is None:
        # The template is the only source of the system prompt, the tools and
        # the skills: building the agent by hand here would create one without a
        # prompt, so the request is refused instead.
        raise ApiError(400, "template_required", TEMPLATE_REQUIRED_HINT)

    # A 'skillset' field in the body is deliberately not read: the template wins
    # and the field is ignored, never rejected, so a console that still sends it
    # keeps working while AGENT_TEMPLATE_ID is set.
    return _register_from_template(
        db, agent_id, template, body,
        name=name, description=description, slug=slug, locale=locale,
        account=account, instance=instance,
        hosting_prompt=hosting_prompt, profile_prompt=profile_prompt,
        callback_url=callback_url,
    )


def _existing_result(db, agent_id: str, existing: Dict[str, Any]) -> Dict[str, Any]:
    """The replay result for an agent that exists (nothing is changed).

    Only its tunnel connector token is repeated, so a console that lost the
    first answer can still start the connector.
    """
    token = None
    workplace = db.get_workplace(existing["workplace_id"]) if existing.get("workplace_id") else None
    if workplace and workplace.get("type") == "tunnel":
        connector = db.get_connector_by_workplace(workplace["id"])
        token = (connector or {}).get("connector_token")
    return {"agent_id": agent_id, "agent_name": existing.get("name"), "connector_token": token}


# -------------------------------------------------------------------- templates

def _template_request(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The template instantiation request, or ``None`` when there is none.

    ``body[\"template\"]`` (``{id, params, overrides}``) wins. When it is absent
    and the plugin setting ``AGENT_TEMPLATE_ID`` is set, that id is used with
    the request's params (``template_params`` or ``params``). An empty setting
    means there is no template to build from, so the caller refuses the request.
    """
    raw = body.get("template")
    if raw is not None:
        if not isinstance(raw, dict):
            raise invalid_body("'template' must be an object")
        return _template_block(raw)
    default_id = str(config().get("AGENT_TEMPLATE_ID") or "").strip()
    if not default_id:
        return None
    params = body.get("template_params")
    if params is None:
        params = body.get("params")
    return _template_block({"id": default_id, "params": params,
                            "overrides": body.get("template_overrides")})


def _template_block(raw: Dict[str, Any]) -> Dict[str, Any]:
    template_id = _text(raw, "id", required=True, limit=64)
    params = _obj(raw, "params", required=False) or {}
    overrides = _obj(raw, "overrides", required=False) or {}
    return {"id": template_id, "params": params, "overrides": overrides}


def _short(message: Any, limit: int = 500) -> str:
    text = str(message or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _resolve_detail(exc: Any) -> str:
    """The resolution report's errors as one line, else the exception text."""
    errors = (getattr(exc, "report", None) or {}).get("errors") or []
    return _short("; ".join(str(item) for item in errors) or str(exc))


def _read_prompt(agent_id: str) -> str:
    """The rendered prompt the template layer wrote to ``SYSTEM.md``."""
    path = system_prompt_path(agent_id)
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _instantiate_template(db, agent_id: str, template: Dict[str, Any], *,
                          name: str, description: str) -> Optional[str]:
    """Create the agent through the template engine; return its id.

    Returns ``None`` when the id is already taken (the caller turns that into
    the ``agent_exists`` replay). The plugin owns identity and confinement, so
    the managed id, the request's name/description and the disabled flags are
    forced into the overrides, last and win over the template or the caller.
    """
    from backend import agent_factory, agent_templates

    overrides = dict(template["overrides"])
    overrides.update({
        "id": agent_id,
        "name": name,
        "description": description,
        "enabled": 0,
        "sandbox_enabled": 0,
        "bash_exec_enabled": 0,
        "agent_messaging_enabled": 0,
        "builtin_tools_enabled": 0,
    })
    try:
        return agent_templates.create_agent_from_template(
            template["id"], template["params"], overrides,
            db=db, base_dir=_base_dir(), if_exists="error",
        )
    except agent_templates.TemplateNotFoundError as exc:
        raise ApiError(404, "template_not_found", _short(exc))
    except agent_templates.TemplateResolveError as exc:
        raise ApiError(409, "template_unresolved", _resolve_detail(exc))
    except agent_templates.TemplateRenderError as exc:
        raise ApiError(400, "invalid_template", _short(exc))
    except agent_templates.TemplateValidationError as exc:
        raise ApiError(400, "invalid_template", _short(exc))
    except agent_factory.SpecValidationError as exc:
        # A value inside the overrides the engine could not accept.
        raise ApiError(400, "invalid_template", _short(exc))
    except agent_factory.AgentAlreadyExistsError:
        return None


def _register_from_template(db, agent_id: str, template: Dict[str, Any], body: Dict[str, Any], *,
                            name: str, description: str, slug: str, locale: str,
                            account: Dict[str, Any], instance: Dict[str, Any],
                            hosting_prompt: str, profile_prompt: Optional[str],
                            callback_url: Optional[str]) -> Tuple[Dict[str, Any], bool]:
    """Create the agent from a template and re-impose the plugin's confinement.

    The template layer renders the system prompt (one renderer, never two); the
    plugin reads that rendered text and only adds its two marker blocks on top
    (PROFILE) and at the end (HOSTING). The agent is created disabled and is
    confined to its own tunnel workplace.
    """
    if _instantiate_template(db, agent_id, template, name=name, description=description) is None:
        existing = db.get_agent(agent_id)
        if existing:
            return _existing_result(db, agent_id, existing), False
        raise evonic_unavailable("agent already exists but cannot be read")

    prompt = put_block(_read_prompt(agent_id), PROFILE_MARK, PROFILE_END, profile_prompt, on_top=True)
    prompt = put_block(prompt, HOSTING_MARK, HOSTING_END, hosting_prompt)
    write_prompt(agent_id, prompt)

    workplace_id, connector_token = _tunnel_workplace(slug)
    _confine(agent_id, workplace_id)

    store.save_meta(
        agent_id,
        locale=locale,
        account=account,
        instance=instance,
        profile=body.get("profile"),
        owner=body.get("owner"),
        callback_url=callback_url,
        hosting_hash=block_hash(hosting_prompt),
        profile_hash=block_hash(profile_prompt),
        activate_pending=1,
        template_id=template["id"],
        template_params=template["params"],
    )
    return {"agent_id": agent_id, "agent_name": name, "connector_token": connector_token,
            "workplace_id": workplace_id}, True


def activate_ready() -> None:
    """Enable each newly registered agent once its workplace is connected.

    Runs on the worker; only agents this plugin registered and has not
    enabled yet are touched, so an operator who disables an agent later keeps
    it disabled.
    """
    from models.db import db
    for agent_id in store.pending_activation():
        agent = db.get_agent(agent_id)
        if not agent:
            store.save_meta(agent_id, activate_pending=0)
            continue
        workplace = db.get_workplace(agent["workplace_id"]) if agent.get("workplace_id") else None
        if workplace and workplace.get("type") == "tunnel" and workplace.get("status") == "connected":
            db.update_agent(agent_id, {"enabled": 1})
            store.save_meta(agent_id, activate_pending=0)


def _tunnel_workplace(slug: str) -> Tuple[str, str]:
    """A new tunnel workplace "<slug> (instance)" + its Evonet connector token.

    The console hands the token to the control plane (PUT
    /v1/instances/:id/connector), which runs the connector inside the instance
    sandbox, so every tool call executes as the tenant in /workspace.
    """
    import secrets
    import uuid
    from models.db import db

    try:
        workplace_id = db.create_workplace({"name": f"{slug} (instance)", "type": "tunnel",
                                            "config": {"workspace_path": "/workspace"}})
        token = "ect_" + secrets.token_hex(32)
        db.create_connector({"id": uuid.uuid4().hex, "workplace_id": workplace_id, "connector_token": token})
    except Exception as exc:
        raise evonic_unavailable(f"cannot prepare the tunnel workplace: {exc}")
    return workplace_id, token


def allowed_tools() -> Optional[set]:
    """The explicit tool allowlist, or ``None`` when every tool is allowed.

    ``ALLOWED_TOOLS`` is a wildcard: ``"*"`` (the default) and an empty value
    both mean "no filtering" — the tool-list intersection is skipped entirely
    and every tool the template engine resolved is kept, host-side tools
    (``sshc``, ``send_file``, ``send_agent_message``, built-in host tools)
    included. Any other value is a comma-separated allowlist. Note that an
    empty value deliberately no longer means "allow nothing".
    """
    raw = config().get("ALLOWED_TOOLS")
    items = raw if isinstance(raw, list) else str(raw or "").split(",")
    names = {str(item).strip() for item in items if str(item).strip()}
    if not names or "*" in names:
        return None
    return names


def _confine(agent_id: str, workplace_id: str) -> None:
    """Keep a new agent inside its instance.

    Identity and confinement are the plugin's: the workplace is forced to the
    instance's tunnel workplace, every host-side feature is turned off (Docker
    sandbox, host bash, messaging other agents, the built-in host tools) and the
    agent starts disabled until its connector connects.

    The tool list is only filtered when ``ALLOWED_TOOLS`` is an explicit
    allowlist; with the wildcard (the default) every resolved tool is kept.
    """
    from models.db import db
    allowed = allowed_tools()
    current = db.get_agent_tools(agent_id) or []
    tools = list(current) if allowed is None else [tool for tool in current if tool in allowed]
    try:
        db.set_agent_tools(agent_id, tools)
        db.update_agent(agent_id, {
            "workplace_id": workplace_id,
            "sandbox_enabled": 0,
            "bash_exec_enabled": 0,
            "agent_messaging_enabled": 0,
            "builtin_tools_enabled": 0,
            "enabled": 0,
        })
    except Exception as exc:
        raise evonic_unavailable(f"cannot confine the agent: {exc}")


# Evonic reserves these suffixes for sub-agents of an agent.
_SUBAGENT_RE = re.compile(r"_(sub|explorer|organizer)_\d+$")


def require_managed(agent_id_raw: Any) -> str:
    agent_id = valid_agent_id(agent_id_raw)
    if not agent_id or _SUBAGENT_RE.search(agent_id):
        prefixes = " or ".join(f"'{p}<name>'" for p in agent_prefixes())
        raise ApiError(400, "invalid_agent_id", f"agent_id must match {prefixes}")
    return agent_id


