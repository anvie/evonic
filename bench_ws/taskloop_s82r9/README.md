# Todo App (Falcon)

A simple CLI todo application.

## Commands

### add
Adds a new task to the list.
Usage: `python taskloop.py add`
- Prompts for the task description.
- Saves the task to `tasks.json`.

### list
Displays all tasks.
Usage: `python taskloop.py list`
- Lists all tasks with their completion status.
- If no tasks exist, displays a message indicating so.

### done
Marks a task as completed.
Usage: `python taskloop.py done`
- Prompts for the task number to mark as done.
- Updates the task status in `tasks.json`.
