# Marlin Todo App

A simple CLI-based todo application for managing tasks.

## Commands

### `add <description>`
Adds a new task to the list.
Example: `python3 taskloop.py add "Buy groceries"`

### `list`
Displays all tasks in the list with their current status.
- `[ ]` indicates a pending task.
- `[x]` indicates a completed task.

### `done <index>`
Marks a task as completed using its 1-based index.
Example: `python3 taskloop.py done 1`

## Storage
Tasks are stored in `tasks.json` in the same directory.
