# TiyasanCloud plugin

Registers the AI agent behind every TiyasanCloud customer, in one call that
also returns its WhatsApp link, and enforces a monthly token quota inside
Evonic. The plugin only ever **creates** agents: it has no endpoint to list,
read, change or delete them. Deleting an agent is done in Evonic by an
operator, and the agent's prompt changes through its own WhatsApp
conversation.

## Install

Copy this directory into Evonic's plugin directory as `plugins/tiyasancloud`,
enable it in Evonic, then fill in the settings:

| Setting | Meaning |
| --- | --- |
| `ACCESS_TOKEN` | Shared secret with the console; also signs callbacks. |
| `SHARED_CHANNEL_ID` | Optional. Empty = the first connected `whatsapp_shared` channel. |
| `SHARED_NUMBER` | Optional override. Empty = read from the channel's WhatsApp session. |
| `MAX_LINKED_PER_AGENT` | WhatsApp numbers per agent (default 1). Checked when a link is made and again when its message arrives, so a leftover link can't add a second number. |
| `PAIR_CODE_TTL_MINUTES` | How long a WhatsApp link stays valid (default 10 minutes). |
| `AGENT_ID_PREFIX` | Comma-separated prefixes of the agents this plugin manages (default `tc_,id_`: `tc_<user>` for new agents, `id_<slug>` for older ones). |
| `ENFORCE_QUOTA` | Off = report usage but never block a turn. |
| `PAIRING_POLL_SECONDS` | How often incoming pairing codes are picked up (default 3). |
| `ALLOWED_TOOLS` | Tool filter. `"*"` (default) or empty = **no filtering**: every tool the template engine assigns is kept, host-side tools included. A comma-separated list keeps only those tools. |
| `AGENT_TEMPLATE_ID` | The template every registration **without** its own `template` object is built from. **Set this**: the template is the only source of the system prompt, tools and skills, so with it empty a request that carries no `template` is refused with `400 template_required`. |

The plugin never creates a WhatsApp channel and never asks a customer to scan a
QR: an operator connects the shared number once, in Evonic.

## Authentication

Every endpoint needs `Authorization: Bearer <ACCESS_TOKEN>` or
`X-Access-Token: <ACCESS_TOKEN>`; anything else is `401 unauthorized`. Evonic
exempts `/plugin/*` from sessions and CSRF, so the plugin checks each request
itself, in constant time.

## API

Routes live under `/plugin/tiyasancloud/v1` (and `/plugin/tiyasan/v1`, the name
in the console's spec). Errors are `{"error": "<code>", "message": "<text>"}`.

| Method + path | Purpose |
| --- | --- |
| `POST /register_agent` | Create the agent and its "chat on WhatsApp" link, in one transaction. |
| `GET /token_usage?agent_id=&from=&to=` | Tokens used; `from`/`to` default to the current period. |
| `POST /agents/<id>/whatsapp/link` | A fresh "chat on WhatsApp" link (max 5 per agent per hour). |
| `GET /agents/<id>/whatsapp` | Linked numbers, channel status, the link still waiting to be used. |
| `DELETE /agents/<id>/whatsapp/users/<phone>` | Unlink one number. |
| `GET /health` | Plugin, shared-channel status, pending callbacks. |

`agent_id` must carry one of the `AGENT_ID_PREFIX` prefixes, so Evonic's own agents can never be
touched through this API.

### Registering an agent

The console registers one agent per user: `agent_id` = `tc_<user id>` and
`name` = the user's email; the assistant's own name is chosen later, on
WhatsApp.

`POST /register_agent` takes `agent_id`, `name`, `description`, `locale`,
`account`, `instance` (`slug`), `hosting_prompt`, `profile_prompt`, `limits`,
`callback_url` and the `template` object (below). A `skillset` field is no
longer read: a caller that still sends one is ignored, never rejected.

- **New agent → `201`** with `agent_id`, `agent_name`, `connector_token`,
  `workplace_id` and `whatsapp.link`.
- **Agent already exists → `200`** with `error: "agent_exists"` and
  `whatsapp: null`. Nothing is changed and no link is made; the answer repeats
  the `connector_token`, so a console that lost the first answer can still
  start the connector.

A new agent is created by Evonic’s template engine (agent dir and KB from
`backend/agent_factory`, workspace under `shared/agents/<id>`, the tools and
skills the template resolves), with `model_id` left to the global default.

**Confinement.** A customer agent runs only inside its own server. The plugin:

- creates its own **tunnel workplace** `<slug> (instance)` rooted at
  `/workspace`, with an Evonet connector token;
- keeps every tool the agent was created with (the owner's wildcard decision);
  set `ALLOWED_TOOLS` to a comma-separated list to filter them again;
- turns off everything that runs on the Evonic host: Docker sandbox, host
  bash, messaging other agents and the built-in host tools.

The console hands `connector_token` to the control plane
(`PUT /v1/instances/:id/connector`), which runs the connector inside the
instance sandbox. The agent starts **disabled**; the plugin's worker switches
it on once, as soon as its workplace reports `connected`. An operator who
disables it later keeps it disabled.

`SYSTEM.md` gets the template’s rendered prompt with two blocks of ours:
`<!-- tiyasan-profile -->` … `<!-- /tiyasan-profile -->` at the top and
`<!-- tiyasan-hosting -->` … `<!-- /tiyasan-hosting -->` at the end. After
registration the prompt is Evonic's. Ids that look like Evonic sub-agents
(`_sub_N`, `_explorer_N`, `_organizer_N`) are refused.

### Building the agent from a template

`POST /register_agent` builds **every** agent through Evonic’s template engine
(`backend.agent_templates.create_agent_from_template`), never by hand. The request
carries a `template` object; when it does not, the plugin falls back to
`AGENT_TEMPLATE_ID`. With neither, the request is refused (`400 template_required`)
instead of creating an agent without a prompt. Since 3.2.0 the legacy `skillset`
branch, its `DEFAULT_KB` / `_copy_default_kb` helpers and the `skillset` store write
are gone.

```json
{
  "agent_id": "tc_1",
  "name": "customer@example.com",
  "account": {"id": "acc_1"},
  "instance": {"slug": "acme-server"},
  "hosting_prompt": "You run for PT Acme.",
  "profile_prompt": "The customer prefers Indonesian.",
  "template": {
    "id": "support_triage_bot",
    "params": {"company": "PT Acme"},
    "overrides": {"variables": {"CRM_API_TOKEN": "…"}}
  }
}
```

- `template.id` — the template's slug.
- `template.params` — values for the template's declared parameters.
- `template.overrides` — optional structural overrides the engine accepts
  (`variables`, `tools`, `skills`, `knowledge_base`, and allowlisted columns).

The plugin still owns identity and confinement, and re-imposes them **after**
instantiation: the agent id is the managed id from `agent_id` (the request name
and description also win over the template), `workplace_id` is forced to the
tunnel workplace, `sandbox_enabled`/`bash_exec_enabled`/`agent_messaging_enabled`/
`builtin_tools_enabled` are `0`, and the agent starts `enabled = 0`. Callers can
never override `id`, the workplace or those flags.

The **rendered** system prompt comes from the template layer — one renderer,
never two. The plugin reads it from `SYSTEM.md` and only adds its own blocks:
`<!-- tiyasan-profile -->` on top and `<!-- tiyasan-hosting -->` at the end.
`template_id` and `template_params` are recorded in the plugin store for audit.

Errors use the same `{error, message}` shape:

| Situation | Status | `error` |
| --- | --- | --- |
| Unknown template id | `404` | `template_not_found` |
| Invalid params/overrides, or an unrenderable prompt | `400` | `invalid_template` |
| Template needs tools/skills Evonic lacks | `409` | `template_unresolved` |
| The agent already exists | `200` | `agent_exists` (replay, unchanged) |
| No `template` object and no `AGENT_TEMPLATE_ID` | `400` | `template_required` |

An existing agent is returned first, before any template is read, so it is
never modified and never receives template parameters.

When the request carries no `template` object, the plugin instantiates from
`AGENT_TEMPLATE_ID` with the request’s params (`template_params`, else `params`).

### Token quota

`limits` in the registration takes `tokens_monthly` (`null` = unlimited),
`soft_pct` (default 80), `hard` (default true), `suspended`, `period`
(`start`/`end`, ISO-8601 with a timezone; default the calendar month, UTC),
`rollup_subagents` and the three `messages`. Limits are set once, so the
period rolls by itself: once it ends, the calendar month we are in takes its
place. Usage comes from Token Monitor's public API, which already folds
sub-agents into their parent, and is cached for 30 seconds.

Enforcement uses Evonic's hooks: a turn gate answers with `messages.hard` or
`messages.suspended` without calling the LLM, a message interceptor injects
`messages.soft` once the soft threshold is crossed, and a tool-result gate ends
a turn that runs past the quota mid-loop. `state` is `ok`, `soft`, `hard` or
`suspended`; if Token Monitor is down the agent view reports `unknown` and
nothing is blocked. The soft warning is injected once per turn.

### WhatsApp

The customer never types a phone number, never scans a QR and never opens a
WhatsApp session. After setup:

1. The plugin picks the active shared channel (the first connected
   `whatsapp_shared` channel, or `SHARED_CHANNEL_ID`) and reads its number from
   the channel's WhatsApp session (`creds.json`, written when the operator
   scanned the QR).
2. `POST /register_agent` returns `whatsapp.link`: a `wa_link` (`wa.me`) and an
   `api_link` (`api.whatsapp.com/send`) to that number, with a ready-made
   message in the customer's language, for example:

   > Halo! 👋 Saya mau mulai ngobrol dengan Asisten Kopi Senja lewat WhatsApp.
   >
   > Kode saya: K7Q2XP

   The console only shows a "Chat on WhatsApp" button. A new link (when the
   first one expired) comes from `POST /agents/<id>/whatsapp/link`.
3. The customer taps the button and presses send. The shared channel drops
   the unknown sender into its inbox; the plugin's worker finds the code
   there, routes the **sender's** number to the agent and replies:

   > Hore, sudah tersambung! 🎉 Mulai sekarang kamu bisa ngobrol dengan
   > Asisten Kopi Senja di sini. …

Every message the customer sees is plain language, written for people who are
not technical: no "pairing", "verification", "OTP" or "token", and the next
step is always spelled out.

A link works once, lasts `PAIR_CODE_TTL_MINUTES`, and a new link cancels the
previous one. Codes come from `secrets` (Evonic's own generator uses
`random`). Only our own message ("Kode saya: …") or a message that is just the
code counts, so ordinary chat is never taken for a wrong code. Five bad codes
within 15 minutes put a sender in a 15-minute cool-off. The message stays well
under the 200 characters Evonic keeps from an unknown sender (a long agent name
is shortened, never the code).

When WhatsApp addresses the chat by LID, the inbox carries the phone in
`alt_user_id`; both ids are routed (and both removed on unlink), and replies go
to the id the chat uses. Other messages from unknown numbers stay in the inbox
for the operator; their sender gets one friendly pointer per day.
`access_mode` stays `assigned_only`, so an unrouted number can never reach an
agent.

**Prerequisite:** an operator creates a `whatsapp_shared` channel in Evonic
and scans its QR once. Until then setup still succeeds and
`whatsapp.error` is `whatsapp_unavailable`.

### Callbacks

When `callback_url` is set, the plugin `POST`s `token_soft`, `token_hard`,
`whatsapp_linked`, `whatsapp_unlinked` and `whatsapp_channel_down` events with
`X-Tiyasan-Signature` (HMAC-SHA256 of `timestamp.body`, keyed with
`ACCESS_TOKEN`) and `X-Tiyasan-Timestamp`. Delivery is queued and retried after
10 s, 1 min and 10 min, then dropped. Quota events fire once per state per
period.

## What this plugin does not do

It contains no Evonic core changes and needs none: pairing, routing, quota
hooks, workplaces and token data all use existing Evonic APIs. It never lists,
reads, changes or deletes an agent, never creates or deletes channels, never
picks a model for a customer, and never touches agents outside its prefix. It
does not filter the tools a template resolves (the owner's wildcard decision):
whatever the template engine assigns is kept, host-side tools included, since
the customer runs in their own instance; set `ALLOWED_TOOLS` to a
comma-separated list to filter them again.

## Tests

```
python -m pytest tests
```

The tests stub Evonic (`config`, `models.db`, Token Monitor, `requests`), so
they run anywhere; the HTTP tests are skipped when Flask is missing.
