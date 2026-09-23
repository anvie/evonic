# TiyasanCloud plugin for Evonic

This is the Evonic side of [TiyasanCloud](https://tiyasancloud.com): every
customer gets one server and one AI agent, and this plugin is the only thing
the TiyasanCloud console talks to.

It replaces version 1.0.0 of the plugin, which paired customers by asking
Baileys for a device-linking code (turning each customer's own number into a
bot) and had no way to cap token spend. See "Changes from 1.0.0" below.

Start with [PLUGIN.md](PLUGIN.md) for installation, settings and the full API.

## What it does

- **One call registers a customer's agent.** `POST /register_agent` creates
  the agent from the console's onboarding form, confined to its instance's
  tunnel workplace, writes the two TiyasanCloud blocks into its `SYSTEM.md`,
  and returns its WhatsApp link. The agent is built by Evonic’s parameterized
  **template engine** (`backend.agent_templates`): from the request’s `template`
  object, else from the `AGENT_TEMPLATE_ID` plugin setting, which renders the
  system prompt and resolves the tools and skills. A `skillset` field is no
  longer read (ignored, never rejected). The plugin never lists, reads, changes
  or deletes agents afterwards: that is Evonic’s business.
- **Monthly token quota.** Registration sets `tokens_monthly` and a soft
  threshold; the period rolls with the calendar month. Evonic then stops at the
  hard cap without calling the LLM, and warns the agent before it gets there.
  `GET /token_usage` reports what was used.
- **One-tap WhatsApp on the shared number.** Setup returns a `wa.me` link to
  the active shared channel with a ready-made, plain-language message. The
  customer taps it and presses send; their number is connected from that
  message. No number to type, no QR, no new WhatsApp session.

## How it fits into Evonic

| Concern | What the plugin uses |
| --- | --- |
| Agents, channels, workplaces | `models.db` mixins (`get_agent`, `create_agent`, `create_workplace`, `create_connector`, `update_channel`, …) |
| Prompt | the agent's `SYSTEM.md`, between its own markers (the template engine renders the base, the plugin adds the blocks) |
| Templates | `backend.agent_templates.create_agent_from_template(...)` (one renderer, never two) |
| Token usage | `plugins.token_monitor.db.usage_db.by_agent(since, rollup_subagents=True)` |
| Quota enforcement | `register_turn_gate`, `register_message_interceptor`, `register_tool_result_gate` |
| Pairing | the shared channel's inbox of unrouted senders + `routes` in its config |
| Sending replies | the channel instance's `send_message` |

Everything above is an existing, public Evonic surface. The plugin adds no core
patches, and `on_disable` removes every hook and worker it registered.

Its own state (agent metadata from the console, limits, pairing codes, linked
numbers, the callback outbox) lives in `data/db/plugins/tiyasancloud.db`.

## Verified against Evonic

Version 2.0.0, besides the unit tests, was run inside the production Evonic Python
environment against a copy of its database (the service and live data were not
touched): loaded the way `PluginManager` loads plugins, registered into the real
hook registries, created/updated/deleted an agent through the Flask API, read
real Token Monitor usage, blocked an over-quota agent through the real turn-gate
chain, and paired a number through the real shared-channel inbox, with Evonic's
own `_lookup_route` resolving the routes it wrote. 46/46 checks passed.
Version 3.0.0 narrows the API to registration (same agent creation,
confinement, quota hooks and pairing code underneath).

## Layout

| File | Contents |
| --- | --- |
| `plugin.json` | Manifest and settings. |
| `handler.py` | Lifecycle: registers the quota hooks, starts the two workers. |
| `routes.py` | The HTTP API (both path prefixes, auth on every route). |
| `agents.py` | Agent registration (template only), confinement, the `SYSTEM.md` blocks, switching a new agent on. |
| `quota.py` | Limits, usage, state and the three enforcement hooks. |
| `whatsapp.py` | Active channel + number, the WhatsApp link and its message, redeeming from the inbox, routes, status. |
| `callbacks.py` | Signed, queued, retried callbacks to the console. |
| `store.py` | The plugin's SQLite tables. |
| `common.py` | Config, auth, validation, error shape. |
| `tests/` | Unit tests (`test_register_template.py`); Evonic is stubbed, so they run anywhere. |

## Changes from 3.1.0

| 3.1.0 | Now |
| --- | --- |
| `POST /register_agent` fell back to the legacy `skillset` path when the request carried no `template` | The template is the only path. A request without a `template` object is built from `AGENT_TEMPLATE_ID`; with that setting empty it is refused with `400 template_required` instead of creating an agent without a prompt |
| `skillset` chose the prompt, tools and skills, and `store.save_meta` wrote the `skillset` column | `skillset` is no longer read: a caller that still sends one is ignored, never rejected. `_skillset()`, `DEFAULT_KB` and `_copy_default_kb()` are gone; the column stays as read-only history |

## Changes from 3.0.0

| 3.0.0 | Now |
| --- | --- |
| An agent could only be built from a legacy `skillset` id | `POST /register_agent` accepts an optional `template` object (`{id, params, overrides}`) and instantiates through `backend.agent_templates.create_agent_from_template(...)`; without it, the `skillset` path is byte-for-byte the same |
| Tools were filtered against a curated `ALLOWED_TOOLS` allowlist | **Owner decision:** the tool filter is a wildcard. `ALLOWED_TOOLS` defaults to `"*"` (and an empty value also means "no filtering"), so every tool the template engine resolves is kept; set a comma-separated list to filter again |
| No record of what an agent was built from | `store.save_meta` records `template_id` and `template_params` (JSON) for audit; `_migrate()` adds the columns to existing databases |

Template errors keep the `{error, message}` shape: unknown template → `404
template_not_found`; invalid params/overrides or an unrenderable prompt → `400
invalid_template`; a template needing tools/skills Evonic lacks → `409
template_unresolved`. An existing agent is still returned unchanged (the early
`get_agent` branch wins) and never receives template parameters.

## Changes from 1.0.0

| 1.0.0 | Now |
| --- | --- |
| `register_agent` took the customer's phone number, created a **new** `whatsapp_shared` channel per customer and asked Baileys for a device-linking code | No number asked: a one-tap `wa.me` link to the **existing** shared number, verified by the plugin itself |
| Required `SharedWhatsAppChannel.request_pairing_code()`, which Evonic does not have | Uses only existing Evonic APIs |
| Agent name forced to `TC <email>`; no personalisation | Name, locale, profile and prompt blocks come from the console |
| `update_agent` returned `501` | Gone: an agent is only registered; later changes happen in Evonic |
| Token usage read through Token Monitor's private `usage_db._connect()` and hand-written SQL | Public `usage_db.by_agent(...)`, with sub-agent rollup |
| No quota enforcement | Soft cap, hard cap, suspension, mid-turn stop |
| No agent deletion, WhatsApp status, unlink or callbacks | All present |
| Agents kept Evonic's defaults, including host tools (`sshc`), agent messaging and built-in host tools | Confined on every registration: own tunnel workplace, sandbox / host bash / agent messaging / built-in host tools off. The tool filter is a wildcard by default (see "Changes from 3.0.0") |
| Agent id `tca_<user_id>` | `id_<slug>`, matching the console (prefix configurable) |

`register_agent` and `token_usage` keep their names. `enable_agent` and
`update_agent` are gone: the plugin switches a new agent on once its connector
is up, and changes after registration are made in Evonic.
