"""
Agent State — deterministic agent mode and task tracking.

Tracks the agent's current working mode ("plan" or "execute") and a task list.
Write tools are blocked in plan mode, forcing the agent to create a plan before
executing any file-modifying operations.

A plan file (markdown on disk) can be linked via save_plan() or set_plan_file().
The file path is persisted in this state; render() reads and injects the file
content on every LLM call so the agent retains full context even after
conversation summarization or server restarts.

Usage:
    ms = AgentState()                    # starts in "plan" mode
    ms.is_blocked("write_file")           # True in plan mode
    ms.set_plan_file("plan/my-plan.md")   # link a plan file
    ms.set_mode("execute")                # transition after user approval
    ms.is_blocked("write_file")           # False in execute mode

    ms.update_tasks("set", tasks=["Read config", "Fix bug", "Write fix"])
    ms.update_tasks("done", task_id=1)
    ms.update_tasks("in_progress", task_id=2)

    ms.render()                           # markdown for LLM injection
    ms.serialize()                        # JSON string for DB persistence
    AgentState.deserialize(json_str)     # restore from DB
"""

from typing import Optional, Union

import json
import os

# Project root: two levels up from this file (backend/agent_state.py → project root)
_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))

# Maximum characters of plan file content injected into each LLM call
_PLAN_FILE_MAX_CHARS = 4000

GUARDED_TOOLS = {"write_file", "str_replace", "patch", "file_edit", "file_create"}

VALID_MODES = {"plan", "execute"}

STATUS_ICON = {
    "pending": "[ ]",
    "in_progress": "[~]",
    "done": "[x]",
}


class AgentState:
    def __init__(self, mode: str = "plan", tasks: list = None, next_task_id: int = 1,
                 plan_file: str = None, states: dict = None,
                 focus: bool = False, focus_reason: str = None,
                 auto_trivial: bool = False):
        self.mode = mode
        self.tasks: list[dict] = tasks or []
        self._next_task_id = next_task_id
        self.plan_file: str | None = plan_file  # relative path e.g. "plan/my-plan.md"
        # Namespace-keyed state slots registered by system/plugins via the `state` tool.
        # Each slot: {state: str, data: any, blocked_tools: list|None, allowed_tools: list|None}
        self.states: dict = states or {}
        self.auto_trivial: bool = auto_trivial  # True when classifier auto-set execute mode
        # Focus mode: when True, agent will not accept messages from other sessions.
        # Plugins (e.g. kanban) set this when starting a long-running exclusive task
        # and clear it when the task finishes. For short-term turn-level exclusivity,
        # the runtime's _busy_agents flag is used instead.
        self.focus: bool = focus
        self.focus_reason: str | None = focus_reason

    # ── Blocking ────────────────────────────────────────────────────────────

    def is_blocked(self, tool_name: str) -> Union[bool, str]:
        """Return True (mode block) or a string message (state block) if the tool is blocked."""
        if self.mode == "plan" and tool_name in GUARDED_TOOLS:
            return True
        return self.is_blocked_by_state(tool_name)

    def is_blocked_by_state(self, tool_name: str) -> Optional[str]:
        """Check all state slots for tool blocks. Returns a blocking message or None."""
        for ns, slot in self.states.items():
            allowed = slot.get("allowed_tools")
            blocked = slot.get("blocked_tools")
            state_label = slot.get("state", "unknown")
            if allowed is not None and tool_name not in allowed:
                return (
                    f"Tool '{tool_name}' is not allowed in state '{ns}:{state_label}'. "
                    f"Allowed tools: {allowed}"
                )
            if blocked is not None and tool_name in blocked:
                return f"Tool '{tool_name}' is blocked in state '{ns}:{state_label}'."
        return None

    # ── State slots ──────────────────────────────────────────────────────────

    def set_state(self, namespace: str, state: str, data=None,
                  blocked_tools: list = None, allowed_tools: list = None) -> None:
        """Set the state slot for a namespace."""
        self.states[namespace] = {
            "state": state,
            "data": data,
            "blocked_tools": blocked_tools,
            "allowed_tools": allowed_tools,
        }

    def get_state(self, namespace: str) -> Optional[dict]:
        """Get the current state slot for a namespace, or None."""
        return self.states.get(namespace)

    def clear_state(self, namespace: str) -> None:
        """Remove a state slot."""
        self.states.pop(namespace, None)

    # ── Mode transitions ────────────────────────────────────────────────────

    def set_mode(self, new_mode: str, reason: str = None) -> dict:
        """Transition to a new mode. Returns a result dict for the LLM."""
        if new_mode not in VALID_MODES:
            return {"error": f"Invalid mode '{new_mode}'. Valid modes: {sorted(VALID_MODES)}"}
        if new_mode == "execute" and not self.plan_file:
            return {
                "error": (
                    "Cannot switch to execute mode without a plan file. "
                    "Save your plan first using save_plan(filename, content), "
                    "then present it to the user for approval."
                )
            }
        old_mode = self.mode
        self.mode = new_mode
        msg = f"Mode changed: {old_mode} → {new_mode}"
        if reason:
            msg += f" ({reason})"
        return {"result": msg, "mode": new_mode}

    def set_plan_file(self, path: str) -> dict:
        """Link a plan file to this state. Path should be relative to project root."""
        if not path:
            return {"error": "path must be a non-empty string."}
        self.plan_file = path
        return {"result": f"Plan file set: {path}", "plan_file": path}

    # ── Task management ─────────────────────────────────────────────────────

    def update_tasks(self, action: str, task_id: int = None,
                     text: str = None, tasks: list = None) -> dict:
        """
        Manage the task list.

        Actions:
            "set"         — Replace the entire task list with a list of text strings.
            "add"         — Add a single new task (requires text).
            "done"        — Mark a task as done (requires task_id).
            "in_progress" — Mark a task as in_progress (requires task_id).
            "remove"      — Remove a task (requires task_id).
        """
        if action == "set":
            if not isinstance(tasks, list):
                return {"error": "Action 'set' requires a 'tasks' list of strings."}
            # Preserve completed tasks so history isn't lost when starting a new plan.
            done_tasks = [t for t in self.tasks if t.get("status") == "done"]
            self.tasks = list(done_tasks)
            self._next_task_id = max((t["id"] for t in self.tasks), default=0) + 1
            for t in tasks:
                self.tasks.append({
                    "id": self._next_task_id,
                    "text": str(t),
                    "status": "pending",
                })
                self._next_task_id += 1
            return {"result": f"Task list set with {len(self.tasks)} tasks ({len(done_tasks)} completed preserved).", "tasks": self._task_summary()}

        if action == "add":
            if not text:
                return {"error": "Action 'add' requires 'text'."}
            task = {"id": self._next_task_id, "text": str(text), "status": "pending"}
            self.tasks.append(task)
            self._next_task_id += 1
            return {"result": f"Task #{task['id']} added.", "task_id": task['id']}

        if action in ("done", "in_progress"):
            if task_id is None:
                return {"error": f"Action '{action}' requires 'task_id'."}
            task = self._find_task(task_id)
            if task is None:
                return {"error": f"Task #{task_id} not found."}
            task["status"] = "done" if action == "done" else "in_progress"
            return {"result": f"Task #{task_id} marked as {task['status']}.", "tasks": self._task_summary()}

        if action == "remove":
            if task_id is None:
                return {"error": "Action 'remove' requires 'task_id'."}
            before = len(self.tasks)
            self.tasks = [t for t in self.tasks if t["id"] != task_id]
            if len(self.tasks) == before:
                return {"error": f"Task #{task_id} not found."}
            return {"result": f"Task #{task_id} removed.", "tasks": self._task_summary()}

        return {"error": f"Unknown action '{action}'. Valid: set, add, done, in_progress, remove"}

    def _find_task(self, task_id: int):
        for t in self.tasks:
            if t["id"] == task_id:
                return t
        return None

    def _task_summary(self) -> list:
        return [{"id": t["id"], "text": t["text"], "status": t["status"]} for t in self.tasks]

    # ── Rendering ────────────────────────────────────────────────────────────

    def render(self) -> str:
        """Render state as a markdown system message for LLM injection."""
        if self.mode == "plan":
            mode_note = (
                "plan — write tools are **blocked** until user approves. "
                "You MUST call save_plan() before set_mode('execute')."
            )
        else:
            mode_note = "execute — write tools are **allowed**"

        lines = [
            "## Agent State",
            f"**Mode**: {mode_note}",
        ]

        if self.auto_trivial and self.mode == "execute":
            lines.append(
                "**Auto-classified**: trivial — write tools are allowed. "
                "If this task is actually complex, call set_mode('plan') to switch to planning mode."
            )

        if self.focus:
            reason_note = f" — {self.focus_reason}" if self.focus_reason else ""
            lines.append(f"**Focus**: active{reason_note} (messages from other sessions are rejected)")

        if self.plan_file:
            lines.append(f"**Plan file**: `{self.plan_file}`")
            plan_content = self._read_plan_file()
            if plan_content:
                lines.append("")
                lines.append("### Active Plan")
                lines.append(plan_content)
        else:
            lines.append("**Plan file**: _none — use save_plan(filename, content) to create one_")

        if self.tasks:
            lines.append("")
            lines.append("### Task List")
            for t in self.tasks:
                icon = STATUS_ICON.get(t.get("status"), "[ ]")
                text = t.get("text") or "(no description)"
                lines.append(f"- {icon} #{t['id']} {text}")
        else:
            lines.append("")
            lines.append("_No tasks defined yet. Use update_tasks(action='set', tasks=[...]) to define your plan._")

        if self.states:
            lines.append("")
            lines.append("### Active States")
            for ns, slot in self.states.items():
                state_label = slot.get("state", "unknown")
                detail = f"**{ns}**: `{state_label}`"
                allowed = slot.get("allowed_tools")
                blocked = slot.get("blocked_tools")
                data = slot.get("data")
                if allowed is not None:
                    detail += f" — allowed tools: {allowed}"
                elif blocked:
                    detail += f" — blocked tools: {blocked}"
                if data:
                    detail += f" — {data}"
                lines.append(f"- {detail}")

        return "\n".join(lines)

    def _read_plan_file(self) -> str:
        """Read plan file content from disk, capped at _PLAN_FILE_MAX_CHARS."""
        if not self.plan_file:
            return ""
        path = os.path.join(_PROJECT_ROOT, self.plan_file)
        path = os.path.normpath(path)
        # Safety: must stay within project root
        if not path.startswith(_PROJECT_ROOT):
            return "_[plan file path rejected: outside project root]_"
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            if len(content) > _PLAN_FILE_MAX_CHARS:
                content = content[:_PLAN_FILE_MAX_CHARS] + f"\n\n_[truncated — {len(content) - _PLAN_FILE_MAX_CHARS} chars omitted]_"
            return content
        except FileNotFoundError:
            return f"_[plan file not found: {self.plan_file}]_"
        except Exception as e:
            return f"_[plan file read error: {e}]_"

    # ── Persistence ──────────────────────────────────────────────────────────

    def serialize(self) -> str:
        """Serialize to JSON string for DB storage."""
        return json.dumps({
            "mode": self.mode,
            "tasks": self.tasks,
            "next_task_id": self._next_task_id,
            "plan_file": self.plan_file,
            "states": self.states,
            "focus": self.focus,
            "focus_reason": self.focus_reason,
            "auto_trivial": self.auto_trivial,
        })

    @classmethod
    def deserialize(cls, data: str) -> "AgentState":
        """Restore from a JSON string. Returns a fresh AgentState on parse error."""
        try:
            obj = json.loads(data)
            return cls(
                mode=obj.get("mode", "plan"),
                tasks=obj.get("tasks", []),
                next_task_id=obj.get("next_task_id", 1),
                plan_file=obj.get("plan_file"),
                states=obj.get("states", {}),
                focus=obj.get("focus", False),
                focus_reason=obj.get("focus_reason"),
                auto_trivial=obj.get("auto_trivial", False),
            )
        except (json.JSONDecodeError, TypeError, AttributeError):
            return cls()
