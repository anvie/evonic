# Kanban Skill

## Overview
You have access to the Kanban board. You can search for tasks assigned to you, update task statuses, delete tasks, and log progress comments.

## Autopilot Mode

Your autopilot status is automatically included in the Agent State when you call `state("kanban:pick", ...)`. Check the `## Agent State` section in your context — the kanban state data shows `autopilot: true` or `autopilot: false`. The tool result from `state("kanban:pick", ...)` also tells you directly.

- **Autopilot OFF (default):** Follow the standard workflow below — present the task to the user and wait for approval.
- **Autopilot ON:** Skip the approval step. Call `kanban_update_status(in-progress)` immediately and start working.

You can toggle autopilot using the `/autopilot on` or `/autopilot off` command.

## Workflow

When you receive a `[Kanban Task]` notification, the notification was sent **automatically by the system** — it is NOT the user's approval to start work. You must still get explicit confirmation from the human user before doing anything (unless autopilot is ON).

Follow this exact sequence — do not skip or reorder steps:

1. **Call `state("kanban:pick", {"task_id": "..."})`.** The result tells you your autopilot status — no extra tool call needed.

2. **If autopilot is ON:**
   a. Call `kanban_update_status` with `status: "in-progress"` immediately.
   b. Proceed to work on the task using your other available tools. Use `kanban_add_comment` to update the progress if you are really making a real progress.
   c. When finished, call `kanban_update_status` with `status: "done"`.
   d. Send a completion report summarising what was done.

3. **If autopilot is OFF:**
   a. **Present the task** to the user: state the title, priority, and description clearly. Then ask: *"Boleh saya mulai mengerjakan task ini?"* (or in the user's language). **Stop here and wait for their reply. Do NOT call any other tools yet.**
   b. **If the user approves:**
      i. Call `kanban_update_status` with `status: "in-progress"` — this is **mandatory before starting any work**.
      ii. Proceed to work on the task using your other available tools. Use `kanban_add_comment` to log progress, sub-steps, or findings as you work.
      iii. Ask follow-up questions in this same chat if you hit blockers.
      iv. When finished, call `kanban_update_status` with `status: "done"` and make git commit with includes the task id in the commit message.
      v. Send a completion report summarising what was done.
   c. **If the user does not approve:**
      - Acknowledge the decision and stop.

## Pause & Resume

If you hit a blocker while working on a task (waiting for external input, dependency not ready, etc.), you can pause instead of abandoning:

1. **Pause:** Call `state("kanban:pause", {"task_id": "..."})` — this locks your tools and sets the task status to `paused`.
2. **Resume:** When the blocker is cleared, call `state("kanban:resume", {"task_id": "..."})` — this unlocks tools and sets the task back to `in-progress`.

While paused, only `state`, `kanban_search_tasks`, `kanban_get_task`, and `kanban_add_comment` are allowed.

## Postpone

If you are busy working on another task and receive a new task notification, you can postpone it:

1. **Postpone:** Call `state("kanban:postpone", {"task_id": "..."})` — this clears the pending state and keeps the task in `todo`. The scanner will re-notify you later.
2. You can continue your current work uninterrupted.

Postpone is only available for tasks in `pending` state (not yet activated).

## Task Creation

When creating kanban tasks with `kanban_create_task`, follow these rules:

1. **Self-contained descriptions** — Agents working from tasks have no memory of prior conversations. Every task description must include full context: what the overall goal is, what specific work this task covers, and what NOT to do.
2. **Dependencies** — Use the `dependencies` parameter when tasks depend on others. Never create multiple sequential tasks in one call — split them and link via dependencies.
3. **One task per independent unit** — If tasks can run in parallel, create separate tasks. If tightly coupled, merge into one.
4. **Assign to yourself by default** — When you create a task for work you will do yourself (for example a release documentation task), set `assignee` to your own agent id. Only leave a task unassigned when it is genuinely meant for another agent or a human, or when the user asked for it to stay unassigned.
5. **Priority** — `high` for blockers, `medium` for important, `low` for nice-to-haves.

### Task Description Template

Always structure descriptions with:
- **Main Goal**: The overall objective (same across related tasks)
- **This Task**: Specific scope, files, and expected outcome
- **Out of Scope**: What this task should NOT touch

## Rules

- **The `[Kanban Task]` notification is not approval** — always wait for the human user to say yes before doing any work or calling any non-kanban tool (unless autopilot is ON).
- **`kanban_update_status(in-progress)` is mandatory** — call it immediately after approval (or immediately if autopilot is ON) and before doing any actual work. Never skip this step.
- **One task at a time** — do not start another task until the current one is marked done.
- **Use the same session** — all communication stays in this chat.
- **Report on completion** — always send a summary when a task is marked done.
