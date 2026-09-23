# Kanban Plugin

A plugin that adds a Kanban board to Evonic for task management.

## Features

- **Create tasks** with title, description, and priority
- **Assign tasks** to specific agents
- **Update status** (todo, in-progress, done)
- **Search and filter** tasks
- **Autopilot mode** for automated task handling
- **Live task flash**: the task title text turns solid yellow then fades back whenever its agent calls a tool

## Installation

This plugin is bundled with Evonic and installed by default.

## Usage

1. Navigate to the Kanban board via the navigation menu
2. Click "Create Task" to add a new task
3. Fill in the title, description, priority, and assignee
4. Click "Create" to save the task
5. Drag and drop tasks between columns to update status

## Dependencies

- Python 3.9+
- Evonic core platform

## Configuration

### Task activity flash

The board shows live agent activity: whenever an agent working a task invokes
a tool, that task's **title text** on the board turns **solid yellow** and then
slowly shades back to its original color. Every new tool call snaps the text
back to solid yellow and restarts the fade, so a task being actively worked on
reads as a repeating pulse. When the agent's turn ends the flash stops after a
short grace period.

How it works: the plugin subscribes to the `tool_call_started`, `tool_executed`
and `turn_complete` events. For agents with an in-progress task it publishes
`kanban_task_activity` / `kanban_task_idle` events on the durable `kanban`
realtime channel, which the board page consumes over Server-Sent Events. No
polling is involved.

| Variable | Default | Description |
| --- | --- | --- |
| `TASK_FLASH_ENABLED` | `true` | Enable the yellow title text flash on tool calls. |
| `TASK_FLASH_DECAY_SECONDS` | `5` | How long the title text takes to shade back to the original color. |

Set `TASK_FLASH_ENABLED` to `false` to disable the effect entirely. Activity
events are throttled to at most one per second per task.

