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
from __future__ import annotations

from typing import Optional, Union

import json
import os
import re

# Project root: two levels up from this file (backend/agent_state.py → project root)
_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))

# Maximum characters of plan file content injected into each LLM call
_PLAN_FILE_MAX_CHARS = 15000

GUARDED_TOOLS = {"write_file", "str_replace", "patch"}

VALID_MODES = {"plan", "execute"}

STATUS_ICON = {
    "pending": "[ ]",
    "in_progress": "[~]",
    "done": "[x]",
}

# Regex to strip leading status indicators that LLMs sometimes embed in task text.
_STATUS_PREFIX_RE = re.compile(
    r'^(?:'
    r'[\s]*(?:'
    r'[\u2610\u2611\u2612\u2713\u2714\u2717\u2718\u27f3]'   # ☐☑☒✓✔✗✘⟳
    r'|[\u23f3\u231b]'                                       # ⏳⌛
    r'|\u2705|\u274c|\U0001f504'                             # ✅❌🔄
    r'|\[(?:x|X| |~|DONE|done|TODO|WIP)\]'                  # [x] [ ] [~] [DONE] etc.
    r')'
    r')+[\s]*'
    r'(?:#\d+[\s]*)?'                                        # optional #<id>
)

# Trailing suffixes LLMs append to indicate completion.
_STATUS_SUFFIX_RE = re.compile(
    r'\s*\((?:complete|completed|done|finished)\)\s*$',
    re.IGNORECASE,
)

# Indicators that imply the task is already done.
_DONE_INDICATORS = re.compile(
    r'\u2705|\u2713|\u2714|\u2611|\u2612'                    # ✅✓✔☑☒
    r'|\[(?:x|X|DONE|done)\]'
    r'|\((?:complete|completed|done|finished)\)',
    re.IGNORECASE,
)


def _sanitize_task_text(text: str) -> tuple[str, str | None]:
    """Strip leading/trailing status indicators from task text.

    Returns (cleaned_text, inferred_status) where inferred_status is
    "done" / "in_progress" if completion markers were detected, else None.
    """
    raw = text
    # Detect status before stripping
    inferred = None
    if _DONE_INDICATORS.search(raw):
        inferred = "done"

    cleaned = _STATUS_PREFIX_RE.sub('', raw, count=1)
    cleaned = _STATUS_SUFFIX_RE.sub('', cleaned)
    cleaned = cleaned.strip() or raw.strip()
    return cleaned, inferred


class AgentState:
    def __init__(self, mode: str = "plan", tasks: list = None, next_task_id: int = 1,
                 plan_file: str = None, states: dict = None,
                 focus: bool = False, focus_reason: str = None,
                 auto_trivial: bool = False, atg: dict = None,
                 cmp: dict = None, always_execute: bool = False):
        self.mode = mode
        self.always_execute: bool = always_execute
        if self.always_execute:
            self.mode = "execute"
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
        # ATG (Atomic Task Graph) state, set by backend.agent_runtime.atg when
        # the enable_atg flag is on: {status, dag, history, repair_attempts, stats}
        self.atg: dict | None = atg
        # CMP (Context Memory Path) session-path store, set by
        # backend.agent_runtime.cmp when enable_cmp is on:
        # {version, active_id, next_id, paths: {P1: {...card+segments+snapshot}}, stats}
        self.cmp: dict | None = cmp

    # ── Blocking ────────────────────────────────────────────────────────────

    def is_blocked(self, tool_name: str) -> Union[bool, str]:
        """Return True (mode block) or a string message (state block) if the tool is blocked."""
        if self.always_execute:
            return self.is_blocked_by_state(tool_name)
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

    def set_mode(self, new_mode: str, reason: str = None,
                 session_id: str = None, agent_id: str = None) -> dict:
        """Transition to a new mode. Returns a result dict for the LLM."""
        if new_mode not in VALID_MODES:
            return {"error": f"Invalid mode '{new_mode}'. Valid modes: {sorted(VALID_MODES)}"}
        if self.always_execute:
            if new_mode == "plan":
                return {"result": f"Agent is configured with always_execute; staying in execute mode", "mode": "execute"}
            return {"result": f"Mode is execute", "mode": "execute"}
        if new_mode == "execute" and not self.plan_file:
            if session_id and agent_id:
                from backend.task_classifier import classify_operation_trivial
                if classify_operation_trivial(session_id, agent_id) == "trivial":
                    self.auto_trivial = True
                    self.mode = new_mode
                    msg = f"Mode changed: plan → execute (trivial operation, no plan required)"
                    if reason:
                        msg += f" ({reason})"
                    return {"result": msg, "mode": new_mode}
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
            self.tasks = []
            self._next_task_id = 1
            for t in tasks:
                clean, inferred = _sanitize_task_text(str(t))
                self.tasks.append({
                    "id": self._next_task_id,
                    "text": clean,
                    "status": inferred or "pending",
                })
                self._next_task_id += 1
            return {"result": f"Task list replaced with {len(self.tasks)} tasks.", "tasks": self._task_summary()}

        if action == "add":
            if not text:
                return {"error": "Action 'add' requires 'text'."}
            clean, inferred = _sanitize_task_text(str(text))
            task = {"id": self._next_task_id, "text": clean, "status": inferred or "pending"}
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

    def render(self, agent_id: str = None, atg_enabled: bool = False,
               cmp_enabled: bool = False, agent_name: str = None) -> str:
        """Render state as a markdown system message for LLM injection.

        Args:
            agent_id: If provided, plan file path is resolved relative to
                      agents/<agent-id>/ (with fallback to project root for
                      backward compatibility with old centralized plans).
            atg_enabled: When True, plan-mode instructions steer the agent to
                      compile_task_graph() instead of a free-form save_plan().
            cmp_enabled: When True and cmp state exists, render the session
                      path map + cards section.
        """
        if self.always_execute:
            mode_note = "execute — write tools are **allowed** (always_execute is on)"
        elif self.mode == "plan":
            if atg_enabled:
                mode_note = (
                    "plan — write tools are **blocked** until user approves. "
                    "After exploring, you MUST call compile_task_graph(goal, context) "
                    "to compile this task into an executable task graph "
                    "(it becomes your plan file) before set_mode('execute'). "
                    "Only use save_plan() instead if the task truly cannot be "
                    "expressed as tool steps."
                )
            else:
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
            plan_content = self._read_plan_file(agent_id)
            if plan_content:
                lines.append("")
                lines.append("### Active Plan")
                lines.append(plan_content)
        elif not self.always_execute:
            lines.append("**Plan file**: _none — use save_plan(filename, content) to create one_")

        if self.atg:
            lines.append("")
            lines.append("### Atomic Task Graph")
            lines.append(self._render_atg_summary())

        if self.cmp and cmp_enabled:
            try:
                from backend.agent_runtime.cmp import render_cmp_section
                aname = (agent_name
                         or (agent_id.replace('_', ' ').title() if agent_id else "Agent"))
                section = render_cmp_section(self.cmp, aname)
            except Exception:
                section = ""
            if section:
                lines.append("")
                lines.append("### Session Paths (CMP)")
                lines.append(section)

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

    def _render_atg_summary(self) -> str:
        """Compact one-line ATG status (never the full graph JSON)."""
        atg = self.atg or {}
        status = atg.get("status", "unknown")
        nodes = ((atg.get("dag") or {}).get("nodes") or {})
        if not nodes:
            return f"**Status**: {status}"
        counts = {}
        for nd in nodes.values():
            s = nd.get("status", "pending")
            counts[s] = counts.get(s, 0) + 1
        counts_str = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
        return f"**Status**: {status} — {len(nodes)} nodes ({counts_str})"

    def _read_plan_file(self, agent_id: str = None, max_chars: int = _PLAN_FILE_MAX_CHARS) -> str:
        """Read plan file content from disk, capped at max_chars.

        Pass max_chars=None to read the full file without truncation (used by
        the UI plan viewer); the default cap keeps the plan small when injected
        into the LLM system-state context.

        Resolution order:
        1. If agent_id is provided, try agents/<agent-id>/<plan_file> first
           (the new per-agent plan directory).
        2. Fallback to _PROJECT_ROOT/<plan_file> for backward compatibility
           with old centralized plans.
        """
        if not self.plan_file:
            return ""

        candidates = []
        if agent_id:
            agents_dir = os.path.join(_PROJECT_ROOT, 'agents')
            agent_plan = os.path.normpath(
                os.path.join(agents_dir, agent_id, self.plan_file)
            )
            agent_root = os.path.realpath(os.path.join(agents_dir, agent_id))
            if os.path.realpath(agent_plan).startswith(agent_root + os.sep):
                candidates.append(agent_plan)

        # Fallback: old centralized plan path
        legacy_path = os.path.normpath(os.path.join(_PROJECT_ROOT, self.plan_file))
        if legacy_path.startswith(_PROJECT_ROOT):
            candidates.append(legacy_path)

        if not candidates:
            return "_[plan file path rejected: outside allowed directories]_"

        last_error = None
        for path in candidates:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()
                if max_chars is not None and len(content) > max_chars:
                    content = content[:max_chars] + f"\n\n_[truncated — {len(content) - max_chars} chars omitted]_"
                return content
            except FileNotFoundError:
                last_error = f"_[plan file not found: {self.plan_file}]_"
            except Exception as e:
                last_error = f"_[plan file read error: {e}]_"
                break  # don't fallback on permission/IO errors

        return last_error or ""

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
            "atg": self.atg,
            "cmp": self.cmp,
            "always_execute": self.always_execute,
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
                atg=obj.get("atg"),
                cmp=obj.get("cmp"),
                always_execute=obj.get("always_execute", False),
            )
        except (json.JSONDecodeError, TypeError, AttributeError):
            return cls()
