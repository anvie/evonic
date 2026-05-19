# How to Create Schedules

This guide explains how to create scheduled jobs (reminders, recurring tasks, automations) using the `create_schedule` tool. It covers trigger types, action types, and when to use each combination.

---

## Tool Reference

**Tool:** `create_schedule`

**Parameters:**

| Parameter | Required | Description |
|---|---|---|
| `name` | Yes | Human-readable name (e.g. "Daily standup", "Deploy check") |
| `trigger_type` | Yes | One of: `date`, `interval`, `cron` |
| `trigger_config` | Yes | Trigger-specific configuration (see below) |
| `action_type` | Yes | One of: `static_message`, `session_prompt`, `emit_event` |
| `action_config` | Yes | Action-specific configuration (see below) |
| `max_runs` | No | Max times the schedule fires. Auto-set to 1 for `date` triggers. |

---

## Trigger Types

### 1. `date` — One-shot (runs once at a specific time)

**When to use:** Single reminders, one-time notifications, delayed execution.

**Examples:**
- "Remind me to buy groceries at 11 AM tomorrow."
- "Notify me when the deploy finishes in 30 minutes."
- "Send a reminder for the meeting at 3 PM today."

**Config format:**
```json
{
  "run_date": "2026-05-21T11:00:00"
}
```

**Important:** The time is always in ISO 8601 format. Convert to UTC if the schedule system uses UTC. The current timezone is WIB (UTC+7).

**Auto-cleaned up:** `max_runs` is automatically set to 1.

---

### 2. `interval` — Recurring every N seconds/minutes/hours

**When to use:** Regular polling, health checks, monitoring.

**Examples:**
- "Check server status every 5 minutes."
- "Send a heartbeat every 30 seconds."
- "Pull latest git changes every hour."

**Config format:**
```json
{
  "seconds": 300
}
```

Options (use only one): `seconds`, `minutes`, `hours`.

**Caution:** Do not use intervals shorter than 30 seconds unless absolutely necessary. Very short intervals (under 10 seconds) may cause excessive load.

---

### 3. `cron` — Cron-style schedule (daily, weekly, weekday, etc.)

**When to use:** Daily routines, weekday-only tasks, fixed-time-of-day schedules.

**Examples:**
- Daily at 9 AM: "Daily standup reminder."
- Weekdays at 6 PM: "End-of-day summary."
- Every Monday at 8 AM: "Weekly planning session."

**Config format:**
```json
{
  "hour": 9,
  "minute": 0
}
```

Available keys (all optional, defaults to `*`):
- `minute` (0-59)
- `hour` (0-23)
- `day_of_week` (0-6, where 0=Monday, or mon-fri, mon,tue,wed)
- `day` (1-31)
- `month` (1-12)

**Common patterns:**

| Pattern | Config |
|---|---|
| Daily at 9 AM | `{"hour": 9, "minute": 0}` |
| Daily at 4 PM WIB (21:00 UTC) | `{"hour": 21, "minute": 0}` |
| Weekdays at 8 AM | `{"hour": 8, "minute": 0, "day_of_week": "mon-fri"}` |
| Every Monday at 9 AM | `{"hour": 9, "minute": 0, "day_of_week": "mon"}` |
| Every hour at minute 0 | `{"minute": 0}` |
| Every 30 minutes | `{"minute": "*/30"}` |

**Key rule for cron:** If you say "daily", use `cron` with `hour` and `minute`. Do NOT use `interval` for daily tasks — cron is the correct choice for time-of-day schedules.

---

## Action Types

### `static_message` — Simple, pre-written notification

**When to use:**
- The message content is fully known ahead of time.
- No LLM processing, no tool access needed.
- Low priority or informational.

**Config:**
```json
{
  "agent_id": "siwa",
  "message": "Time for daily standup!"
}
```

If `agent_id` is omitted, the schedule runs as the calling agent.

**LLM cost:** None.

---

### `session_prompt` — Full LLM session with tool access

**When to use:**
- The task requires reading data, analysing state, or making decisions.
- The content is dynamic — you do not know the result ahead of time.
- Keywords in the prompt: check, analyse, summarise, review, look, find, generate, decide, report, evaluate.

**Config:**
```json
{
  "agent_id": "siwa",
  "message": "Check my Kanban tasks. If there are none, commit any unstaged changes and push to origin."
}
```

**LLM cost:** Full — the agent processes the prompt with its tools, just like a user message.

---

### `emit_event` — Internal system signal (no user message)

**When to use:**
- Triggering a hook, plugin, or automation internally.
- No user or agent receives a message.

**Config:**
```json
{
  "event_name": "system.healthcheck",
  "payload": {
    "source": "scheduler"
  }
}
```

`payload` is optional (any JSON object).

**LLM cost:** None.

---

## Decision Flowchart: Which Combination to Use?

```
What kind of schedule is it?
│
├── One-time reminder or delayed task?
│   └── TRIGGER: date
│       └── "Remind me at 3 PM to call John" → static_message
│       └── "At 3 PM, check PR status and report" → session_prompt
│
├── Recurring at a fixed time every day/week?
│   └── TRIGGER: cron
│       └── "Daily standup at 9 AM" → static_message
│       └── "Every morning, check Kanban and push changes" → session_prompt
│
├── Frequent recurring (every few minutes/hours)?
│   └── TRIGGER: interval
│       └── "Health check every 5 minutes" → session_prompt
│       └── "Notify every 30 min that system is running" → static_message
│
└── Internal system automation?
    └── TRIGGER: any
        └── ACTION: emit_event
```

---

## Practical Examples

### Example 1: One-shot reminder (static)
"Remind me to buy groceries at 11 AM WIB tomorrow (2026-05-21)."

```json
{
  "name": "Buy groceries reminder",
  "trigger_type": "date",
  "trigger_config": { "run_date": "2026-05-21T04:00:00" },
  "action_type": "static_message",
  "action_config": {
    "message": "Time to buy groceries (instant noodles etc.)!"
  }
}
```

### Example 2: Daily Kanban check (session_prompt)
"Every day at 21:00 UTC (04:00 WIB), check Kanban and push changes."

```json
{
  "name": "Daily Kanban check and push",
  "trigger_type": "cron",
  "trigger_config": { "hour": 21, "minute": 0 },
  "action_type": "session_prompt",
  "action_config": {
    "message": "Check all assigned todos on Kanban. If there are none, commit any unstaged changes in the evonic project and push to origin."
  }
}
```

### Example 3: Interval health check
"Check server status every 5 minutes."

```json
{
  "name": "Server health check",
  "trigger_type": "interval",
  "trigger_config": { "minutes": 5 },
  "action_type": "session_prompt",
  "action_config": {
    "message": "Run a quick server health check: check disk usage, memory, and CPU. Report if anything exceeds 80%."
  }
}
```

### Example 4: Daily static reminder
"Send a reminder for daily standup every weekday at 9 AM."

```json
{
  "name": "Daily standup reminder",
  "trigger_type": "cron",
  "trigger_config": { "hour": 9, "minute": 0, "day_of_week": "mon-fri" },
  "action_type": "static_message",
  "action_config": {
    "message": "Daily standup now! Gather in the main channel."
  }
}
```

---

## Quick Decision Table

| User says... | Trigger | Action |
|---|---|---|
| "Remind me at [time]" | `date` | `static_message` (simple reminder) or `session_prompt` (if needs to do something) |
| "Daily at [time]" | `cron` | `static_message` (fixed text) or `session_prompt` (needs to check/do things) |
| "Every X minutes/hours" | `interval` | `session_prompt` (monitoring/checking) or `static_message` (periodic nudge) |
| "Every weekday at [time]" | `cron` with `day_of_week: "mon-fri"` | Depends on task complexity |
| "Trigger internal event" | any | `emit_event` |

---

## Important Rules

1. **Time zone:** Current timezone is WIB (UTC+7). When specifying ISO dates, convert to the appropriate timezone. If the system uses UTC, adjust accordingly.

2. **One-shot cleanup:** `date` triggers automatically set `max_runs=1` and clean themselves up after firing. No manual cancellation needed.

3. **Cron vs Interval:** If the user says "daily at [time]", use `cron` — not `interval`. Use `interval` only for sub-daily frequencies (minutes to hours).

4. **Session_prompt cost awareness:** Each `session_prompt` firing consumes LLM tokens. For frequent schedules (e.g. every 5 minutes), keep the prompt short and the task lightweight.

5. **Cancelling a schedule:** Use `cancel_schedule` with the schedule UUID. List active schedules with `list_schedules`.

6. **Max runs:** For `interval` or `cron` schedules that should stop after a certain number of firings, set `max_runs` (e.g. `max_runs: 10` to fire only 10 times then auto-disable).
