# Agent Panel Builder

## Overview
You can build an interactive UI panel on your agent detail page. The panel displays action buttons that users can click to trigger predefined operations — either executing a bash script or sending a prompt to your agent session.

Your panel appears on your agent detail page in the Evonic web UI. Each action is shown as a button with a label and optional parameter inputs.

## Action Types

There are two types of panel actions:

- **`script`** — A bash script that is executed as the configured `run_as_user`. The script is **predefined by you (the agent)** — users cannot inject arbitrary commands at click time. Use this for system operations, deployments, monitoring checks, or any task that runs in the shell.

- **`prompt`** — A text message that is sent to your agent session. When a user clicks this button (after filling in parameters), the resulting message is delivered to you just like a user message. Use this to trigger conversational workflows.

## Parameter Syntax

Both `script` and `prompt` content can include `{{param_name}}` placeholders. When a user clicks the action button, they are prompted to fill in values for these parameters before the action runs. The filled values are substituted into the content.

Example script with parameters:
```bash
#!/bin/bash
curl -X POST https://api.example.com/deploy \
  -H "Authorization: Bearer {{api_token}}" \
  -d '{"environment": "{{env}}", "version": "{{version}}"}'
```

When the button is clicked, the user fills in `api_token`, `env`, and `version`, and the script runs with those values substituted.

## Parameter Definitions

Each parameter in the `params` array has these fields:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Variable name (used in `{{name}}` placeholders) |
| `label` | string | Display label shown to the user |
| `type` | string | Input type: `"text"`, `"password"`, `"number"`, `"select"` |
| `required` | boolean | Whether the parameter is mandatory |
| `default` | string | Default value (optional) |

## Slash Commands

Any action can be assigned its own chat slash command via `slash_command`, so the user can run it straight from chat instead of clicking the button — e.g. `slash_command: "deploy"` makes `/deploy` execute that action.

- Names are lowercase, start with a letter, may contain digits, `-` and `_`, max 32 chars.
- They must not collide with a built-in command or with another action of the same agent.
- Parameters are passed **positionally**, in the order they appear in `params`: `/deploy prod v2`. Missing required parameters produce a usage hint instead of running the action.
- Assign one whenever the user asks to "run this with a slash command"; pass `""` to `panel_update_action` to remove it.
- Every action also stays reachable through `/panel` (list) and `/panel:<label-slug>` (run).

## Best Practices

1. **Clear button labels** — Use descriptive, action-oriented labels (e.g., "Deploy to Staging", not "Click me").
2. **Descriptive param names** — Parameter names should be meaningful (e.g., `environment`, `api_key`, `branch`).
3. **Short scripts** — Keep scripts concise. For complex logic, have the script call an external tool or API.
4. **One action per button** — Each button should do one clear thing. Break complex workflows into multiple buttons.
5. **Use `password` type for secrets** — Mark sensitive parameters (tokens, keys) as `"password"` type so the UI masks them.

## Critical Rule: Scripts Are Predefined

**Scripts must be fully predefined by the agent using the panel tools.** Users cannot inject arbitrary commands at click time — they can only fill in parameter values for `{{placeholders}}` you have defined. This is a deliberate security boundary to prevent arbitrary code execution.

When writing a script action, think of it as a **template**: you define the full script body with explicit placeholders, and the user supplies only the parameter values at execution time.

## Tool Reference

| Tool | Description |
|------|-------------|
| `panel_add_action` | Add a new action button to your panel |
| `panel_update_action` | Update an existing action button |
| `panel_remove_action` | Remove an action button |
| `panel_list_actions` | List all action buttons on your panel |

### panel_add_action

Add a new action button. The `agent_id` must be your own agent ID (agents cannot modify other agents' panels, unless you are the super agent).

Parameters:
- `agent_id` (required) — Your agent ID
- `label` (required) — Button label text
- `action_type` (required) — `"script"` or `"prompt"`
- `content` (required) — Script body or prompt text. Use `{{param_name}}` for user-fillable placeholders.
- `params` (optional) — Array of parameter definitions. Default: `[]`
- `sort_order` (optional) — Display order position. Default: `0`
- `slash_command` (optional) — Chat slash command that runs this action, e.g. `"deploy"` → `/deploy`

### panel_update_action

Update an existing action. Only the `agent_id` and `action_id` are required — all other fields are optional and will only update if provided.

Parameters:
- `agent_id` (required) — Your agent ID
- `action_id` (required) — The action ID to update
- `label` (optional) — New button label
- `action_type` (optional) — `"script"` or `"prompt"`
- `content` (optional) — New script or prompt content
- `params` (optional) — New parameter definitions
- `sort_order` (optional) — New sort position
- `enabled` (optional) — Enable or disable the action
- `slash_command` (optional) — New chat slash command; `""` removes the assignment

### panel_remove_action

Permanently remove an action button from your panel.

Parameters:
- `agent_id` (required) — Your agent ID
- `action_id` (required) — The action ID to remove

### panel_list_actions

List all action buttons currently on your panel.

Parameters:
- `agent_id` (required) — Your agent ID

## Authorization

You can only manage panel actions for your own agent ID. The only exception is the super agent, which can manage panels for any agent.
