# Agent templates

An **agent template** is a JSON blueprint for a complete agent: identity, a parameterized system prompt, configuration defaults (basic *and* advanced settings), tools, skills, variables and knowledge-base files. Templates are the supported way to hand out a repeatable agent — from the **Templates** tab on `/agents`, from the template editor, or programmatically from a plugin or skill.

- Canonical (writable) templates live in `agent_templates/<id>.json`.
- `skillsets/` is the **legacy, read-only** root. Both are listed, `agent_templates/` wins an id collision, and a collision is always reported (never silent shadowing).
- Templates store **declarations only**. Secret variable *values* are supplied when an agent is created and are never written into a template file.

## 1. Authoring a template

A template is a JSON object. `id`, `name` and `system_prompt` are required; everything else is optional.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string | Slug (`^[a-z0-9][a-z0-9_-]{0,63}$`). It is also the file name and a filesystem path, so it is validated and traversal-guarded. |
| `name` | string | Display name (≤ 200 chars). |
| `description` | string | ≤ 2000 chars. |
| `category` | string | Free-form grouping used by the UI (default `general`). |
| `icon` | string | Icon name (default empty). |
| `schema_version` | integer | Must be `1`. |
| `parameters` | array | Prompt parameters (below). |
| `system_prompt` | string | Prompt body with `{{ placeholders }}`. |
| `defaults` | object | Agent settings applied unless overridden. |
| `tools` | array of string | Tool ids. |
| `skills` | array of string | Installed skill ids. |
| `variables` | array | Variable declarations (below). |
| `kb_files` | object | `{ "relative/path.md": "content" }`. |

### Parameters

Each entry accepts `name`, `label`, `type`, `required`, `default`, `options`, `description`, `placeholder`, `min`, `max` and `integer`.

- `type` is one of `text`, `number`, `boolean`, `select`.
- Defaults are type-checked at **save** time, so a template can never ship a default the renderer would reject.
- A parameter may be `required: true` **and** carry a `default` — that is the recommended combination, because the template still works untouched (see `support_triage_bot.company`).
- `options` is required for `select`; `min` / `max` / `integer` only for `number`.
- Resolution order when an agent is created: **caller value → declared default → empty string**, and a `required` parameter that resolves to empty is an error.

### Placeholders

`{{ name }}` is substituted with the resolved parameter value (single pass; substituted text is never re-scanned). Values render as-is; booleans render as `true` / `false`.

- Every placeholder must be declared, and every declared parameter should be used — an undeclared placeholder is a **save-time error**, an unused parameter is a warning in the resolution report.
- A literal brace is written `\{{` (JSON: `"\\{{"`).
- `defaults.name` and `defaults.description` may also carry placeholders.

### Defaults

`defaults` keys are validated against the agent-factory field allowlist and coerced exactly like a UI-created agent (booleans become `0`/`1`, enum aliases are canonicalised). Server-controlled columns (`id`, `is_super`, `owner`, timestamps) are **not** accepted, and workplace/channel bindings (`workplace_id`, `primary_channel_id`) are instantiation-time inputs that a template must never bake in.

Typical basic keys: `name`, `description`, `enabled`, `vision_enabled`, `artifacts_enabled`, `attachments_enabled`, `agent_messaging_enabled`.
Typical advanced keys: `sandbox_enabled`, `bash_exec_enabled`, `disable_turn_prefetch`, `enable_cmp`, `memory_engine`, `kb_organizer_mode`, `tool_compression_enabled`, `summarize_threshold`, `outbound_buffer_seconds`.

### Variables

```json
{ "key": "CRM_API_TOKEN", "is_secret": true, "required": true, "description": "…" }
```

- `key` must match `^[A-Za-z_][A-Za-z0-9_]*$`.
- A **secret** declaration may not carry a `default` — values only ever arrive at instantiate time.
- A required variable without a value fails the create unless the caller explicitly allows unresolved variables.

### Knowledge base

`kb_files` maps a **relative** path to its content. Paths are validated with the same guard as agent import/export (no absolute paths, no `..`, no backslashes), and size is capped. The factory additionally copies its standard KB files when `defaults/` exists in the deployment.

## 2. Shipped examples

| Template | Shows |
| --- | --- |
| `support_triage_bot` | required text param **with default**, select, boolean and number params, 16 `defaults` keys spanning basic + advanced, 4 tools, 2 skills, 3 variables (**one secret**) and 2 KB files. |
| `data_analyst` | sandboxed analyst: number/select/boolean params, `runpy`/`read_file`/`write_file` tooling, `explorer` + `subagent` skills, a secret DSN variable and a metrics glossary. |
| `hello_world_showcase` | smallest walkthrough: one parameter of every type, two tools, one skill, two variables and one KB file. |

Copy one, change the `id`, then edit it in `/template/<id>` and use the simulation panel before creating real agents.

## 3. Creating an agent from a template

### In the UI

- `/agents` → **Templates** tab: browse, preview and *Create agent from this*.
- `/template/<id>`: the full editor (identity, parameters, prompt, defaults, tools, skills, variables, KB) with a live rendered-prompt preview and a test conversation.
- HTTP surface (all routes require an authenticated caller; writes require a privileged caller):

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/templates` | List template summaries. |
| `GET` | `/api/templates/<id>` | Canonical template + dependency/resolution report. |
| `POST` | `/api/templates` | Create a template (privileged). |
| `PUT` / `DELETE` | `/api/templates/<id>` | Update / delete a canonical template (privileged). |
| `POST` | `/api/templates/<id>/render` | Render the prompt + resolved config; persists nothing. |
| `POST` | `/api/templates/<id>/simulate` | Ephemeral run (auth'd, rate-limited, output capped). |
| `POST` | `/api/templates/<id>/instantiate` | Create the agent → `201` + `Location`; a deterministic `id` replays as `200`. |

### Programmatically (plugins / skills)

```python
from backend.agent_templates import create_agent_from_template

agent_id = create_agent_from_template(
    "support_triage_bot",
    params={"company": "Globex", "tone": "professional"},
    overrides={
        "variables": {"CRM_API_TOKEN": token},   # secret value, never stored in the template
        "name": "Globex Triage",
        "tools": ["calculator", "recall"],       # structural override
    },
)
```

Signature:

```python
create_agent_from_template(
    template_id, params=None, overrides=None, *,
    db=None, base_dir=None, if_exists=None,
    allow_missing_deps=False, allow_unresolved_variables=False,
) -> str
```

- `params` may only contain parameters the template declares; unknown names are rejected.
- `overrides` accepts the agent-factory field allowlist **plus** the structural keys `id`, `tools`, `skills`, `variables`, `knowledge_base`. Precedence is **overrides > defaults**, and `params` only feed the prompt.
- `if_exists` is `None` (replay an explicitly supplied `id`, or suffix a derived one), `"error"` (raise) or any other factory policy.
- `allow_missing_deps=True` skips the tool/skill resolution check; `allow_unresolved_variables=True` tolerates required variables without a value.
- Missing tools/skills raise a `TemplateResolveError` carrying the full resolution report (`.report`) before anything is written.
- Use `preview_template(template_id, params)` for a dry render and `resolve_template(template_id)` for the editor-facing dependency report. Neither writes anything.
- No Flask context is required.

## 4. Simulation containment

`POST /api/templates/<id>/simulate` and `simulation_runtime.simulate(...)` run the template's **real** agent — full prompt, tools, skills, variables and KB — inside a throwaway scope:

- **No DB row.** The ephemeral agent lives only in the in-memory sub-agent registry, keyed by a unique `sim-<template>-<hex>` id, with its spec carried inline. Any `session_index` rows a run materialises are deleted on teardown, and the simulator never creates an `agents` row.
- **Workspace confined to `/tmp`.** The workspace, KB, `SYSTEM.md` and artifacts land under `/tmp/evonic_agent_template_simulation/<sim_id>/` (`agents/<id>/…` and `shared/agents/<id>/…`), and the per-agent chat DB / chatlog / LLM trace are routed to `/tmp/evonic-sub-agents/<sim_id>/`. Nothing is written under `BASE_DIR`.
- **Forced sandbox.** A simulation always selects an isolating docker/bwrap backend, whatever the template's `sandbox_enabled` / `run_as_user` / `workplace_id` says, and drops any explicit backend override (e.g. an SSH backend installed by `sshc`). It never falls back to unsandboxed host execution.
- **Outbound effects intercepted.** Durable or externally-visible tools are short-circuited to a synthetic success and recorded in a **simulated outbox** the UI displays: `create_schedule` / `update_schedule` / `cancel_schedule`, `send_agent_message`, `send_channel_message`, `send_notification`, `escalate_to_user`, `send_file`, memory writes (`remember`, `evomem`, `memorize`, `save_memory`, `forget_memory`), `database_query`, `sshc` and the `kanban_*` / `evomem_*` namespaces (their read-only members stay allowed for fidelity).
- **Guaranteed teardown.** A `finally` / context-manager close removes the registry entry, the session-index rows, the cached sidecar handles, the `/tmp` trees and any stray live directory — on success, on error and when an SSE generator is abandoned mid-stream. A background orphan sweeper reaps trees with no live run after 600 s.

### Residual risks

Interception is a denylist, not a wall:

- **Workspace-scoped effects are real.** Bash, `runpy`, file and artifact operations actually execute — inside the throwaway tree, which is deleted on teardown. Containment depends on a working sandbox backend; if docker/bwrap cannot be started the run fails instead of degrading to host execution.
- **Tools outside the denylist still reach the outside world.** A network-capable or custom/plugin tool that is not on the list (or not in the `kanban_*` / `evomem_*` namespaces) performs its real side effect. Treat a simulation as trustworthy for *your* data, not as proof that no external call happened.
- **Allowed reads are not recorded.** The simulated outbox lists intercepted calls only, so it is not a complete transcript of what the run touched.
- **A hard kill (`SIGKILL`, power loss) can leave `/tmp` trees** until the orphan sweeper runs (`DEFAULT_ORPHAN_TTL` = 600 s, sweeper interval 300 s).
- **Secrets transit the run.** A secret supplied via `overrides.variables` is held for the duration of the run so the tools can use it; the template file never receives it, and the simulation layer logs ids and paths rather than values.
- **Simulation costs tokens.** It is free only in the sense that it does not persist anything — the route is authenticated, rate-limited per caller + template and its output is capped.
- **The simulation is not a security boundary for plugins.** A plugin or custom tool that ignores the agent context can still do anything the server process can do; interception only covers calls that pass through the tool-execution chokepoint.

## 5. Verification

- `unit_tests/test_agent_templates.py` — engine contract (storage roots, traversal guards, renderer, defaults/params validation, instantiate precedence).
- `unit_tests/test_templates_routes.py` — HTTP contract (authZ, rate limits, idempotent instantiate, render persistence).
- `unit_tests/test_simulation_runtime.py`, `test_simulation_containment.py`, `test_simulation_outbound_interceptor.py` — containment, force-sandbox and interceptor behaviour.
- `unit_tests/test_template_examples_e2e.py` — end-to-end check of the shipped examples: schema/load round-trip, dependency resolution, feature coverage, **UI instantiation vs `create_agent_from_template` producing the same agent spec**, and a simulation that leaves no DB rows, no files under `BASE_DIR` and no secret in any log record.

```bash
venv/bin/python -m pytest unit_tests/test_template_examples_e2e.py -q
```
