"""
Kanban Board Plugin — Event Handlers

Handles the full kanban workflow:
- State handler (kanban:pick, kanban:activate, kanban:finish)
- Tool guard (block tools while task is pending)
- Message interceptor (remind agent to update status / log progress)
- Builtin suppressor (hide builtin:update_tasks when kanban skill assigned)
- Scanner/notifier (periodic scan + agent notification via scheduler)
- on_tool_executed, on_kanban_task_created, on_kanban_task_updated, on_schedule_fired
"""
from typing import Optional

import json as _json
import os
import re
import threading
import time

from backend.slash_commands import command_registry

PLUGIN_ID = 'kanban'
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_SCHEDULE_NAME = 'kanban_scan'
_STALE_SCHEDULE_NAME = 'kanban_stale_scan'


def _get_owner_name():
    try:
        from models.db import db
        return db.get_setting('owner_name') or 'UI User'
    except Exception:
        return 'UI User'


def _is_autopilot(agent_id: str) -> bool:
    """Check autopilot for an agent via settings table (kanban plugin-owned)."""
    try:
        from models.db import db as _db2
        if _db2.get_setting(f'autopilot:{agent_id}', '0') == '1':
            return True
    except Exception:
        pass
    return False


# ─── Shared workflow state ─────────────────────────────────────────────────────

_pending_tasks: dict = {}            # agent_id -> task_id (acknowledged, not started)
_active_tasks: dict = {}             # agent_id -> task_id (in-progress)
_paused_tasks: dict = {}             # agent_id -> task_id (paused, waiting to resume)
_task_state_since: dict = {}         # agent_id -> float (time.time() when state last entered)
_progress_reminder_armed: dict = {}  # agent_id -> bool
_approval_granted: dict = {}         # agent_id -> task_id (user approved, autopilot=OFF)
_awaiting_approval: set = set()      # agents that have already presented task, now waiting silently
_notifier_paused: bool = False       # global notifier pause flag (UI toggle)


def _is_notifier_paused() -> bool:
    return _notifier_paused


def _set_notifier_paused(paused: bool):
    global _notifier_paused
    _notifier_paused = paused
    _log(f'Notifier {"paused" if paused else "resumed"} via UI toggle')

# ── Allowed tools while a task is pending (before activate) ──────────────────
KANBAN_ALLOWED_TOOLS = {
    'use_skill', 'unload_skill',
    'kanban_search_tasks', 'kanban_update_status', 'kanban_update_task',
    'kanban_add_comment',
    'kanban_get_task', 'kanban_get_comments',
    'set_mode', 'save_plan',
    'state',
}
KANBAN_ALLOWED_TOOLS_LIST = sorted(KANBAN_ALLOWED_TOOLS)

# Tools allowed while waiting for user approval (autopilot=OFF, pending state).
# Excludes kanban_update_status / kanban_update_task so the LLM cannot
# self-start work before the user has confirmed.
KANBAN_APPROVAL_PENDING_TOOLS = sorted(
    KANBAN_ALLOWED_TOOLS - {'kanban_update_status', 'kanban_update_task'}
)

# ── Message interceptor patterns ─────────────────────────────────────────────
RE_START = re.compile(
    r'(saya mulai|mari kita mulai|let\'?s (start|begin)|mulai mengerjakan|'
    r'saya akan (mulai|mengerjakan|memulai)|baik[,.]?\s*saya\s*(akan|mulai)|'
    r'oke[,.]?\s*saya\s*(akan|mulai)|i\'?ll (start|begin)|'
    r'starting now|let me (start|begin|work on)|i will (start|begin))',
    re.IGNORECASE,
)

RE_PROGRESS = re.compile(
    r'(sedang (mengerjakan|memproses|mencari|membuat|menyelesaikan)|'
    r'working on|in progress|step \d|langkah \d|checkpoint|'
    r'sudah (selesai\s+)?(sebagian|beberapa)|partially (done|complete)|'
    r'halfway|progress update|berhasil (menemukan|mengambil|membuat|menyelesaikan)|'
    r'telah (berhasil|selesai|mengambil|membuat)|i (have|\'ve) (completed|finished|done|found|created|retrieved))',
    re.IGNORECASE,
)

RE_DONE = re.compile(
    r'(^|\b)(selesai|sudah selesai|beres|sudah beres|task selesai|'
    r'pekerjaan selesai|semua selesai|all done|sudah done|'
    r'completed|finished|task (is |has been )?(complete|done|finished)|'
    r'work (is |has been )?(complete|done|finished))(\b|$)',
    re.IGNORECASE,
)
# Patterns that indicate the agent is generating a plan or requesting approval
RE_PLAN = re.compile(
    r'(rencana|plan|rencanakan|save_plan|set_mode|'
    r'Boleh saya mulai|present.*plan|approval|'
    r'switch(ing)? to execute|mode.*plan|'
    r'wait(ing)? for (user )?approval|'
    r'I\'ll (create|make|write|draft) a plan|'
    r'saya akan (buat|rencanakan|susun)|'
    r'step-by-step plan|let me outline|'
    r'before I (start|begin)'
    r')',
    re.IGNORECASE,
)


# ─── Approval classifier ─────────────────────────────────────────────────────

def _classify_approval(agent_message: str, user_message: str) -> bool:
    """Use LLM as a yes/no classifier: did the user approve the agent's request?

    Makes a minimal call (max_tokens=20, temperature=0, no thinking) so latency
    is low.  Returns False on any error so the gate stays closed.
    """
    print(f'[kanban/classifier] ENTER user={user_message!r:.60}')
    try:
        from backend.llm_client import llm_client
        # Feed agent+user exchange as a conversation pair and ask the model to classify
        messages = [
            {"role": "assistant", "content": agent_message[:500]},
            {"role": "user",      "content": user_message[:200]},
            {"role": "user",      "content": "Did I just approve the agent to start working on the task? Reply with only 'yes' or 'no'."},
        ]
        print(f'[kanban/classifier] calling LLM...')
        result = llm_client.chat_completion(
            messages=messages,
            temperature=0,
            max_tokens=2048,
            enable_thinking=False,
        )
        print(f'[kanban/classifier] LLM result success={result.get("success")} keys={list(result.keys())}')
        text = ''
        choices = (result.get('response') or {}).get('choices') or []
        print(f'[kanban/classifier] choices count={len(choices)}')
        if choices:
            msg = choices[0].get('message') or {}
            text = msg.get('content', '') or ''
            reasoning = (msg.get('reasoning_content') or msg.get('reasoning') or '').strip()
            print(f'[kanban/classifier] raw content={text!r:.80} reasoning_tail={repr(reasoning[-60:]) if reasoning else ""}')
            from backend.llm_client import strip_thinking_tags
            text, _ = strip_thinking_tags(text)
            if not text.strip() and reasoning:
                last_line = reasoning.rsplit('\n', 1)[-1].lower().strip()
                if last_line.startswith('yes'):
                    text = 'yes'
                elif last_line.startswith('no'):
                    text = 'no'
        approved = text.strip().lower().startswith('yes')
        print(f'[kanban/classifier] final text={text!r} approved={approved}')
        return approved
    except Exception as e:
        import traceback
        print(f'[kanban/classifier] EXCEPTION: {e}\n{traceback.format_exc()}')
        return False


def _classify_followup(comment_content: str, prior_comment: str = None) -> bool:
    """Use LLM as a yes/no classifier: does this comment require the agent to do follow-up work?

    Args:
        comment_content: The new comment(s) to classify. If multiple new comments
            were posted, they should be merged into one string before calling this.
        prior_comment: Optional — the last comment that existed before the new ones,
            included as context so the LLM can interpret replies/references correctly.

    Returns True if the comment asks the agent to fix, revise, correct, or do
    additional work on the task.  Returns False on any error (safe default — no
    false re-opens).
    """
    print(f'[kanban/followup-classifier] ENTER comment={comment_content!r:.80}')
    try:
        from backend.llm_client import llm_client
        context_block = ''
        if prior_comment:
            context_block = (
                f'For context, the last comment before these new ones was:\n'
                f'"{prior_comment[:1000]}"\n\n'
            )
        messages = [
            {
                "role": "user",
                "content": (
                    "A task has been marked as done. The following new comment(s) were left on it:\n\n"
                    f'"{comment_content[:2500]}"\n\n'
                    f'{context_block}'
                    "Does this comment request the agent to fix, revise, correct, or do additional "
                    "work on the task? Reply with only 'yes' or 'no'."
                ),
            },
        ]
        result = llm_client.chat_completion(
            messages=messages,
            temperature=0,
            max_tokens=4096,
            enable_thinking=False,
        )
        text = ''
        choices = (result.get('response') or {}).get('choices') or []
        if choices:
            msg = choices[0].get('message') or {}
            text = msg.get('content', '') or ''
            reasoning = (msg.get('reasoning_content') or msg.get('reasoning') or '').strip()
            from backend.llm_client import strip_thinking_tags
            text, _ = strip_thinking_tags(text)
            if not text.strip() and reasoning:
                last_line = reasoning.rsplit('\n', 1)[-1].lower().strip()
                if last_line.startswith('yes'):
                    text = 'yes'
                elif last_line.startswith('no'):
                    text = 'no'
        needs_followup = text.strip().lower().startswith('yes')
        print(f'[kanban/followup-classifier] result={text!r} needs_followup={needs_followup}')
        return needs_followup
    except Exception as e:
        import traceback
        print(f'[kanban/followup-classifier] EXCEPTION: {e}\n{traceback.format_exc()}')
        return False


# ━━━ Dashboard card handler ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def dashboard_todo_tasks_card(sdk):
    """Return todo tasks for the dashboard card."""
    try:
        from plugins.kanban.db import kanban_db
        all_tasks = [t for t in kanban_db.get_all() if not t.get('archived_at')]
        todo_tasks = [
            {
                'id': t.get('id'),
                'title': t.get('title', ''),
                'description': t.get('description', ''),
                'created_at': t.get('created_at', ''),
            }
            for t in all_tasks
            if t.get('status') == 'todo'
        ]
        in_progress = sum(1 for t in all_tasks if t.get('status') == 'in-progress')
        todo = sum(1 for t in all_tasks if t.get('status') == 'todo')
        done = sum(1 for t in all_tasks if t.get('status') == 'done')
        return {
            'id': 'kanban_todo_tasks',
            'title': 'Todo Tasks',
            'link': '/board/kanban',
            'items': todo_tasks[:10],
            'count': len(todo_tasks),
            'feature_card': {
                'count': len(all_tasks),
                'detail': f'{in_progress} active · {todo} todo · {done} done',
                'border_color': 'rose',
                'bg_color': 'rose',
                'icon_color': 'rose',
            },
        }
    except Exception:
        return None



# ─── Scanner state ────────────────────────────────────────────────────────────

_scanner_schedule_id: str | None = None
_stale_scanner_schedule_id: str | None = None
_lock = threading.Lock()
_pending_scan_timer: threading.Timer | None = None
_classified_comments: set = set()  # comment IDs already classified (avoids re-LLM per scan)


# ─── Logging ──────────────────────────────────────────────────────────────────

def _log(message: str, level: str = 'info', sdk=None):
    if sdk is not None:
        sdk.log(message, level)
    else:
        try:
            from backend.plugin_manager import plugin_manager
            plugin_manager.add_log(PLUGIN_ID, level, message)
        except Exception:
            pass


# ─── Config ───────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    from backend.plugin_manager import plugin_manager
    return plugin_manager.get_plugin_config(PLUGIN_ID)


def _get_kanban_skill_agents() -> list:
    """Return agent IDs that have the kanban skill (or are super agent).

    Replaces the old ELIGIBLE_AGENTS config-driven filter.  Agents are
    eligible to receive kanban notifications if and only if they have
    the kanban skill assigned (or are the super agent).
    """
    try:
        from models.db import db
        from backend.skills_manager import skills_manager

        if not skills_manager.is_skill_enabled('kanban'):
            return []

        agents = db.get_agents()
        result = []
        for agent in agents:
            agent_id = agent['id']
            if agent.get('is_super'):
                result.append(agent_id)
                continue
            try:
                if 'kanban' in db.get_agent_skills(agent_id):
                    result.append(agent_id)
            except Exception:
                # Per-agent skill lookup failed — don't crash the whole list
                import traceback
                traceback.print_exc()
        return result
    except Exception:
        import traceback
        traceback.print_exc()
        return []


# ─── Task helpers ─────────────────────────────────────────────────────────────

def _load_tasks():
    try:
        from plugins.kanban.db import kanban_db
        return kanban_db.get_all()
    except Exception:
        return []


def _assign_task(task_id: str, agent_id: str) -> bool:
    try:
        from plugins.kanban.db import kanban_db
        task = kanban_db.assign(task_id, agent_id)
        if task:
            try:
                from backend.event_stream import event_stream
                event_stream.emit('kanban_task_updated', {'task': task})
            except Exception:
                pass
            return True
        return False
    except Exception:
        return False


# ─── Agent helpers ────────────────────────────────────────────────────────────

def _agent_has_kanban_skill(agent_id: str) -> bool:
    try:
        from backend.skills_manager import skills_manager
        if not skills_manager.is_skill_enabled('kanban'):
            return False
        from models.db import db
        agent = db.get_agent(agent_id)
        if agent and agent.get('is_super'):
            return True
        return 'kanban' in db.get_agent_skills(agent_id)
    except Exception:
        import traceback
        traceback.print_exc()
        return False



def _notify_agent(agent_id: str, task: dict, channel_type: str, sdk=None, force: bool = False, force_delay: bool = False) -> dict:
    """Send a trigger message to the agent about a kanban task.

    Returns:
        {'success': True} on success, or {'success': False, 'reason': <str>} on failure.
        Reasons: 'busy' (agent working on another task), 'no_skill' (missing kanban skill),
                 'no_session' (no active channel session), 'deduplicated' (duplicate notification),
                  'delayed' (task created within delay window).

    Args:
        force: If True, bypass the busy guard and override any pending task state.
               Use for manual user-triggered notifications so the agent always
               receives the full [System/Task] message instead of only the
               [SYSTEM REMINDER] injected by the message interceptor.
        force_delay: If True, bypass the TASK_DELAY_SECONDS setting and notify
                     the agent immediately regardless of how recently the task
                     was created. This is used when the user clicks the
                     "Trigger Agents" button to force immediate delivery.
    """

    # Task delay check — bypassed when force_delay=True (e.g. user clicked Trigger Agents)
    if not force_delay:
        if _is_task_delayed(task, _load_config()):
            _log(
                f'Task {task["id"]} created within delay window, skipping for agent {agent_id}',
                'info', sdk,
            )
            return {'success': False, 'reason': 'delayed'}

    if not force and (agent_id in _pending_tasks or agent_id in _active_tasks or agent_id in _paused_tasks):
        busy_task_id = _pending_tasks.get(agent_id) or _active_tasks.get(agent_id) or _paused_tasks.get(agent_id)
        _log(
            f'Agent {agent_id} is busy with task {busy_task_id}, '
            f'deferring task "{task["title"]}" until current task is done',
            'info', sdk,
        )
        return {'success': False, 'reason': 'busy'}

    # DB-level guard: block if the agent has any in-progress task in the database.
    # Catches cases where _active_tasks is stale (e.g. after a process restart or
    # when the agent was interrupted before clearing in-memory state).
    if not force:
        try:
            from plugins.kanban.db import kanban_db
            active = kanban_db.get_active_task_for_agent(agent_id)
            if active:
                # Self-heal: restore _active_tasks so subsequent checks are fast
                _active_tasks[agent_id] = active['id']
                _task_state_since.setdefault(agent_id, time.time())
                _log(
                    f'Agent {agent_id} has in-progress task {active["id"]} in DB, '
                    f'deferring task "{task["title"]}"',
                    'info', sdk,
                )
                return {'success': False, 'reason': 'busy'}
        except Exception:
            pass

    if force:
        _pending_tasks.pop(agent_id, None)

    if not _agent_has_kanban_skill(agent_id):
        _log(f'Agent {agent_id} does not have kanban skill assigned, skipping', 'warn', sdk)
        return {'success': False, 'reason': 'no_skill'}

    task_id = task['id']
    task_ref = f'#{task_id}'
    title = task['title']
    priority = task.get('priority', 'low')
    description = task.get('description', '')

    body = (
        f'A task has been assigned to you.\n\n'
        f"<task>\n"
        f'**Task:** {task_ref} — {title}\n'
        f'**Priority:** {priority}\n'
    )
    if description:
        body += f'**Description:** {description}\n'

    body += f"</task>\n"

    body += (
        f'\nPlease follow these steps:\n'
        f'1. Call `state("kanban:pick", {{"task_id": "{task_id}"}})` to acknowledge this task — '
        f'the result will tell you whether autopilot is ON or OFF\n'
        f'2. Call `use_skill("kanban")` to load your kanban tools\n'
        f'3. If autopilot is ON: briefly inform the user you were assigned task {task_ref} and will start immediately, '
        f'then call `kanban_update_status(task_id="{task_id}", status="in-progress")`, '
        f'then call `state("kanban:activate", {{"task_id": "{task_id}"}})`\n'
        f'4. If autopilot is OFF: present the task to the user and ask for confirmation before starting — '
        f'status-changing tools are blocked until the user approves\n'
        f'5. Once the user approves (autopilot=OFF): call `state("kanban:activate", {{"task_id": "{task_id}"}})` — '
        f'this automatically sets status to in-progress and unlocks all tools. '
        f'Do NOT call kanban_update_status manually in this case.\n'
        f'6. Complete the task using your tools\n'
        f'7. When done, call `kanban_update_status(task_id="{task_id}", status="done")`\n'
        f'8. Call `state("kanban:finish", {{"task_id": "{task_id}"}})` to close the task\n'
        f'9. Send a completion report starting with "{task_ref} selesai:" followed by a brief summary\n'
        "\nStart by calling state(\"kanban:pick\", ...) now."
    )

    # Clear session context before sending the notification so the task message
    # arrives as the first (and only) message in a clean session, rather than
    # being deleted later when the agent calls state('kanban:activate').
    _clear_cfg = _load_config().get('CLEAR_CONTEXT_ON_NEW_TASK', True)
    if _clear_cfg:
        try:
            from backend.agent_runtime.notifier import _resolve_agent_target
            from models.db import db as _db_clr
            _ext_uid, _ch_id = _resolve_agent_target(agent_id, channel_type)
            if not _ext_uid:
                _ext_uid = f'__system__{agent_id}'
                _ch_id = None
            _clr_session_id = _db_clr.get_or_create_session(agent_id, _ext_uid, _ch_id)
            _db_clr.clear_session(_clr_session_id, agent_id=agent_id)
            try:
                from backend.agent_runtime import agent_runtime
                agent_runtime._session_skill_mds.pop(_clr_session_id, None)
                agent_runtime._session_skill_tools.pop(_clr_session_id, None)
            except Exception:
                pass
            _log(f'Cleared session context for agent {agent_id} before task notification', 'info', sdk)
        except Exception as _clr_exc:
            _log(f'Failed to pre-clear session for agent {agent_id}: {_clr_exc}', 'warn', sdk)

    # Pre-set agent to execute mode when autopilot is ON
    _pre_set_execute_mode(agent_id, task, sdk)

    from backend.agent_runtime.notifier import notify_agent
    result = notify_agent(
        agent_id=agent_id,
        tag='System/Task',
        message=body,
        channel_type=channel_type,
        dedup=True,
    )
    if result['success']:
        _pending_tasks[agent_id] = task_id
        _task_state_since[agent_id] = time.time()
        _log(f'Notified agent {agent_id} about task "{task["title"]}"', 'info', sdk)
        return {'success': True}
    elif result.get('reason') == 'deduplicated':
        _log(f'Skipped duplicate notification for agent {agent_id} task "{task["title"]}"', 'info', sdk)
        return {'success': False, 'reason': 'deduplicated'}
    else:
        _log(f'Failed to notify agent {agent_id}: {result.get("reason")}', 'error', sdk)
        return {'success': False, 'reason': 'no_session'}

def _pre_set_execute_mode(agent_id: str, task: dict, sdk=None) -> bool:
    """When autopilot is ON, pre-set agent state to execute mode with a task plan file.

    Prevents the runtime from resetting to plan mode when it sees a [System/Task]
    notification. Returns True if state was set, False if autopilot is OFF or error.
    """
    autopilot = _is_autopilot(agent_id)
    if not autopilot:
        return False

    task_id = task['id']
    title = task['title']
    description = task.get('description', '')

    # @FIXME(robin): we need to make the agent state in execution mode directly without this workaround.
    # Create a plan file for the task so set_mode('execute') won't reject
    plan_dir = os.path.join(PLUGIN_DIR, '..', '..', 'plan')
    os.makedirs(plan_dir, exist_ok=True)
    plan_path = os.path.join(plan_dir, f'kanban-task-{task_id}.md')
    try:
        with open(plan_path, 'w', encoding='utf-8') as f:
            f.write(f'# Task #{task_id}: {title}\n\n{description}\n')
    except Exception:
        pass  # Plan file is optional; state can still work without it

    # Persist agent state in execute mode with the plan file
    try:
        from backend.agent_state import AgentState
        from models.db import db as _db2
        state = AgentState(
            mode='execute',
            plan_file=f'plan/kanban-task-{task_id}.md',
        )
        _db2.upsert_agent_state(state.serialize(), agent_id=agent_id)
        _log(
            f'Pre-set agent {agent_id} to execute mode for task #{task_id} (autopilot ON)',
            'info', sdk,
        )
        return True
    except Exception as e:
        _log(f'Failed to pre-set execute mode for agent {agent_id}: {e}', 'warn', sdk)
        return False



def _notify_agent_followup(agent_id: str, task: dict, merged_content: str,
                           channel_type: str, sdk=None,
                           prior_content: str = None, comment_author: str = None) -> bool:
    if comment_author is None:
        comment_author = _get_owner_name()
    """Notify an agent that a completed task needs follow-up based on user comment(s).

    Respects the busy guard — if the agent is already working on another task,
    the notification is skipped and the follow-up will be picked up next scan.
    """
    if agent_id in _pending_tasks or agent_id in _active_tasks or agent_id in _paused_tasks:
        busy_task_id = _pending_tasks.get(agent_id) or _active_tasks.get(agent_id) or _paused_tasks.get(agent_id)
        _log(
            f'Agent {agent_id} is busy with task {busy_task_id}, '
            f'deferring follow-up for task "{task["title"]}" until current task is done',
            'info', sdk,
        )
        return False

    if not _agent_has_kanban_skill(agent_id):
        _log(f'Agent {agent_id} does not have kanban skill, skipping follow-up', 'warn', sdk)
        return False

    task_id = task['id']
    task_ref = f'#{task_id}'
    title = task['title']
    priority = task.get('priority', 'low')
    description = task.get('description', '')

    body = (
        f'A completed task has a new comment that requires your attention.\n\n'
        f'<task>\n'
        f'**Task:** {task_ref} — {title}\n'
        f'**Priority:** {priority}\n'
    )
    #if description:
    #    body += f'**Description:** {description}\n'
    body += f'</task>\n\n'
    if prior_content:
        body += f'**Previous comment (for context):**\n> {prior_content}\n\n'
    body += (
        f'**Comment from {comment_author}:**\n'
        f'> {merged_content}\n\n'
        f'The task has been moved back to in-progress. Please follow these steps:\n'
        f'1. Call `use_skill("kanban")` to load your kanban tools\n'
        f'2. Call `kanban_get_task({{"task_id":{"task_id"}}})` to get the task detail\n'
        f'3. Call `state("kanban:pick", {{"task_id": "{task_id}"}})` to acknowledge the task\n'
        f'4. Call `state("kanban:activate", {{"task_id": "{task_id}"}})` to begin work\n'
        f'5. Address the follow-up comment\n'
        f'6. When done, call `kanban_update_status(task_id="{task_id}", status="done")`\n'
        f'7. Call `state("kanban:finish", {{"task_id": "{task_id}"}})` to close the task\n'
        f'\nStart by calling state("kanban:pick", ...) now.'
    )

    # Pre-set agent to execute mode when autopilot is ON
    _pre_set_execute_mode(agent_id, task, sdk)

    from backend.agent_runtime.notifier import notify_agent
    result = notify_agent(
        agent_id=agent_id,
        tag='System/Task Follow-up',
        message=body,
        channel_type=channel_type,
        dedup=True,
    )
    if result['success']:
        _pending_tasks[agent_id] = task_id
        _task_state_since[agent_id] = time.time()
        _log(f'Notified agent {agent_id} about follow-up on task "{task["title"]}"', 'info', sdk)
        return True
    elif result.get('reason') == 'deduplicated':
        _log(f'Skipped duplicate follow-up notification for agent {agent_id} task "{task["title"]}"', 'info', sdk)
        return False
    else:
        _log(f'Failed to notify agent {agent_id} about follow-up: {result.get("reason")}', 'error', sdk)
        return False



# ─────────────────────────────────────────────────────────────────────────────
# Task delay filter
# ─────────────────────────────────────────────────────────────────────────────

def _is_task_delayed(task: dict, config: dict) -> bool:
    """Return True if the task was created within the delay window.

    The task must exist for TASK_DELAY_SECONDS before it is eligible for
    notification.  This gives the task creator time to correct or modify
    the task after creation.
    """
    created_at = task.get('created_at')
    if not created_at:
        return False  # no timestamp -> notify immediately
    try:
        from datetime import datetime, timezone, timedelta
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        delay = config.get('TASK_DELAY_SECONDS', 300)
        now = datetime.now(timezone.utc)
        return (now - created) < timedelta(seconds=delay)
    except Exception:
        return False  # parse error -> notify immediately


# ─── Scanner ──────────────────────────────────────────────────────────────────

def _notify_stale_task(agent_id: str, task: dict, channel_type: str, sdk=None):
    """Resume a stale in-progress task the agent lost track of.

    Bypasses pick/approve since the task is already in-progress in the DB.
    Sets _active_tasks directly so tool guards work correctly.
    """
    task_id = task['id']
    task_ref = f'#{task_id}'
    title = task['title']
    description = task.get('description', '')

    body = (
        f'You have a stale in-progress task that was not properly closed.\n\n'
        f'<task>\n'
        f'**Task:** {task_ref} — {title}\n'
        f'**Status:** in-progress (stale)\n'
    )
    if description:
        body += f'**Description:** {description}\n'
    body += (
        f'</task>\n\n'
        f'Please resume or close this task:\n'
        f'1. Call `use_skill("kanban")` to load your kanban tools\n'
        f'2. Review the task and either continue working on it, or if it is already done:\n'
        f'   - Call `kanban_update_status(task_id="{task_id}", status="done")`\n'
        f'   - Call `state("kanban:finish", {{"task_id": "{task_id}"}})`\n'
        f'3. Send a completion report starting with "{task_ref} selesai:" followed by a brief summary\n'
    )

    # Guard: skip if agent is currently processing an LLM turn
    try:
        from backend.agent_runtime import agent_runtime as _ar
        if _ar.is_agent_busy(agent_id):
            #_log(f'Agent {agent_id} is busy (LLM turn), deferring stale task reminder for {task_id}', 'info', sdk)
            return
    except Exception:
        pass

    # Mark as active directly — task is already in-progress, skip pick/approve
    _active_tasks[agent_id] = task_id
    _task_state_since[agent_id] = time.time()
    _pending_tasks.pop(agent_id, None)
    _paused_tasks.pop(agent_id, None)

    # Pre-set agent to execute mode when autopilot is ON
    _pre_set_execute_mode(agent_id, task, sdk)

    from backend.agent_runtime.notifier import notify_agent
    result = notify_agent(
        agent_id=agent_id,
        tag='System/Task',
        message=body,
        channel_type=channel_type,
        dedup=True,
    )
    if result['success']:
        _log(f'Sent stale task reminder to agent {agent_id} for task {task_id}', 'info', sdk)
    elif result.get('reason') == 'deduplicated':
        _log(f'Skipped duplicate stale reminder for agent {agent_id} task {task_id}', 'info', sdk)
    else:
        _log(f'Failed to send stale task reminder to agent {agent_id}: {result.get("reason")}', 'error', sdk)
        _active_tasks.pop(agent_id, None)


def _scan_stale_tasks(sdk=None):
    """Scan for in-progress tasks the agent is no longer tracking and re-notify."""
    config = _load_config()
    eligible = _get_kanban_skill_agents()
    if not eligible:
        return
    channel_type = config.get('CHANNEL_TYPE', 'telegram')
    stale_timeout = int(config.get('STALE_PENDING_TIMEOUT_SECONDS', 300))
    try:
        from plugins.kanban.db import kanban_db
        try:
            from backend.agent_runtime import agent_runtime as _ar
        except Exception:
            _ar = None

        # ── Check 1: agents NOT in any dict but with in-progress task in DB ─────
        for agent_id in eligible:
            if agent_id in _active_tasks or agent_id in _pending_tasks or agent_id in _paused_tasks:
                continue
            if _ar is not None and _ar.is_agent_busy(agent_id):
                _log(f'Agent {agent_id} is busy (LLM turn in progress), skipping stale scan', 'info', sdk)
                continue
            stale = kanban_db.get_active_task_for_agent(agent_id)
            if stale:
                _log(f'Detected stale in-progress task {stale["id"]} for agent {agent_id}', 'warn', sdk)
                _notify_stale_task(agent_id, stale, channel_type, sdk)

        # ── Check 2: agents IN pending/active dict but NOT runtime-busy too long ─
        # This catches the case where the LLM turn ended (or never started due to
        # concurrency gate blocking) without completing the kanban workflow, leaving
        # stale entries in _pending_tasks / _active_tasks.
        now = time.time()
        for agent_id in list(eligible):
            stuck_task_id = _pending_tasks.get(agent_id) or _active_tasks.get(agent_id)
            if not stuck_task_id:
                continue  # paused is intentional, skip
            if _ar is not None and _ar.is_agent_busy(agent_id):
                continue  # actively running an LLM turn — not stuck
            since = _task_state_since.get(agent_id)
            if since is None or (now - since) < stale_timeout:
                continue  # within grace period
            _log(
                f'Agent {agent_id} stuck in kanban state for {int(now - since)}s with task '
                f'{stuck_task_id} but no active LLM turn — clearing stale state',
                'warn', sdk,
            )
            _pending_tasks.pop(agent_id, None)
            _active_tasks.pop(agent_id, None)
            _task_state_since.pop(agent_id, None)
            _progress_reminder_armed.pop(agent_id, None)
            _approval_granted.pop(agent_id, None)
            _awaiting_approval.discard(agent_id)
            # Clear persisted focus mode so the agent can accept new messages/tasks
            try:
                from models.db import db as _mdb
                from backend.agent_state import AgentState
                _state_content = _mdb.get_agent_state(agent_id=agent_id)
                if _state_content:
                    _ms = AgentState.deserialize(_state_content)
                    if _ms and _ms.focus:
                        _ms.focus = False
                        _ms.focus_reason = None
                        _mdb.upsert_agent_state(_ms.serialize(), agent_id=agent_id)
            except Exception:
                pass
    except Exception:
        pass


def _scan_and_notify(sdk=None) -> dict:
    """Scan todo tasks and notify their assigned eligible agents.

    Returns a dict with result details so callers (esp. the UI) can show
    which agents were notified and which were skipped and why.
    """
    config = _load_config()
    eligible = _get_kanban_skill_agents()
    results = {'notified': 0, 'failed': 0, 'details': []}
    if not eligible:
        return results
    if _notifier_paused:
        _log('Notifier is paused — skipping scan', 'info', sdk)
        results['paused'] = True
        return results

    channel_type = config.get('CHANNEL_TYPE', 'telegram')
    tasks = _load_tasks()

    # Sort tasks by priority: high > medium > low, then by id (smallest first) within same priority
    priority_order = {'high': 0, 'medium': 1, 'low': 2}
    tasks.sort(key=lambda t: (priority_order.get(t.get('priority', 'low'), 3), t.get('id', 0)))

    for task in tasks:
        if task.get('status') != 'todo':
            continue

        assignee = task.get('assignee')
        task_id = task['id']
        title = task['title']

        if not assignee:
            results['details'].append({
                'task_id': task_id,
                'title': title,
                'agent_id': None,
                'success': False,
                'reason': 'no_assignee',
            })
            continue

        if assignee not in eligible:
            results['details'].append({
                'task_id': task_id,
                'title': title,
                'agent_id': assignee,
                'success': False,
                'reason': 'not_eligible',
            })
            continue

        if _is_task_delayed(task, config):
            _log(
                f'Skipping task {task_id} (created within delay window {config.get("TASK_DELAY_SECONDS", 300)}s)',
                'info', sdk,
            )
            results['details'].append({
                'task_id': task_id,
                'title': title,
                'agent_id': assignee,
                'success': False,
                'reason': 'delayed',
            })
            continue

        try:
            from plugins.kanban.db import kanban_db as _kdb_dep
            if _kdb_dep.has_unmet_dependencies(task_id):
                _log(f'Skipping task {task_id} (has unmet dependencies)', 'info', sdk)
                results['details'].append({
                    'task_id': task_id,
                    'title': title,
                    'agent_id': assignee,
                    'success': False,
                    'reason': 'blocked_by_dependency',
                })
                continue
        except Exception:
            pass

        notify_result = _notify_agent(assignee, task, channel_type, sdk)
        if notify_result.get('success'):
            results['notified'] += 1
            results['details'].append({
                'task_id': task_id,
                'title': title,
                'agent_id': assignee,
                'success': True,
            })
        else:
            results['failed'] += 1
            results['details'].append({
                'task_id': task_id,
                'title': title,
                'agent_id': assignee,
                'success': False,
                'reason': notify_result.get('reason', 'unknown'),
            })

    return results


def _scan_comments_for_followup(sdk=None):
    """Check done tasks for new user comments that require follow-up work.

    For each done (non-archived) task assigned to an eligible agent, fetches
    comments posted after completion.  Runs each unclassified comment through an
    LLM classifier; if a comment requests corrections or additional work the task
    is reopened to in-progress and the agent is notified.
    """
    config = _load_config()
    eligible = _get_kanban_skill_agents()
    if not eligible:
        return

    channel_type = config.get('CHANNEL_TYPE', 'telegram')
    tasks = _load_tasks()

    try:
        from plugins.kanban.db import kanban_db
    except Exception:
        return

    for task in tasks:
        if task.get('status') != 'done':
            continue
        if task.get('archived_at'):
            continue
        assignee = task.get('assignee')
        if not assignee or assignee not in eligible:
            continue
        completed_at = task.get('completed_at')
        if not completed_at:
            continue

        task_id = task['id']

        try:
            new_comments = kanban_db.get_comments_since(
                task_id, completed_at, exclude_authors=[assignee]
            )
        except Exception:
            continue

        # Filter out already-classified comments
        unclassified = [c for c in new_comments if c.get('id') not in _classified_comments]
        if not unclassified:
            continue

        # Mark all as classified upfront to avoid re-processing on next scan
        for comment in unclassified:
            _classified_comments.add(comment.get('id'))

        # Merge multiple new comments into one string for a single LLM call
        if len(unclassified) == 1:
            merged_content = unclassified[0].get('content', '').strip()
        else:
            parts = []
            for i, c in enumerate(unclassified, 1):
                author = c.get('author') or 'unknown'
                body = c.get('content', '').strip()
                parts.append(f'[Comment {i} by {author}]: {body}')
            merged_content = '\n\n'.join(parts)

        if not merged_content:
            continue

        # Fetch the last comment before completed_at as context for the LLM
        try:
            prior = kanban_db.get_last_comment_before(task_id, completed_at)
            prior_content = prior.get('content', '').strip() if prior else None
        except Exception:
            prior_content = None

        ids_preview = [str(c.get('id')) for c in unclassified]
        needs_followup = _classify_followup(merged_content, prior_comment=prior_content)
        _log(
            f'Task {task_id}: classified {len(unclassified)} comment(s) '
            f'(ids={ids_preview}) needs_followup={needs_followup} '
            f'preview={merged_content[:60]!r}',
            'info', sdk,
        )

        if not needs_followup:
            continue

        # Reopen the task
        try:
            old_status = task.get('status', 'done')
            kanban_db.update(task_id, {'status': 'in-progress', 'completed_at': None})
            kanban_db.log_task_status_change(task_id, old_status, 'in-progress')
            try:
                from backend.event_stream import event_stream
                updated_task = kanban_db.get(task_id)
                event_stream.emit('kanban_task_updated', {'task': updated_task})
            except Exception:
                pass
            # Use updated task dict for notification
            task = kanban_db.get(task_id) or task
        except Exception as exc:
            _log(f'Failed to reopen task {task_id} for follow-up: {exc}', 'error', sdk)
            continue

        # Determine author label for the notification
        if len(unclassified) == 1:
            notify_author = unclassified[0].get('author') or _get_owner_name()
        else:
            authors = list(dict.fromkeys(
                c.get('author') for c in unclassified if c.get('author')
            ))
            notify_author = ', '.join(authors) if authors else _get_owner_name()

        _notify_agent_followup(
            assignee, task, merged_content, channel_type, sdk,
            prior_content=prior_content, comment_author=notify_author,
        )


def _setup_scheduler():
    global _scanner_schedule_id
    try:
        from backend.scheduler import scheduler
        config = _load_config()
        interval = int(config.get('SCAN_INTERVAL_SECONDS', 300))

        for s in scheduler.list_schedules(owner_type='plugin', owner_id=PLUGIN_ID):
            if s['name'] == _SCHEDULE_NAME:
                scheduler.cancel_schedule(s['id'])

        sched = scheduler.create_schedule(
            name=_SCHEDULE_NAME,
            owner_type='plugin',
            owner_id=PLUGIN_ID,
            trigger_type='interval',
            trigger_config={'seconds': interval},
            action_type='emit_event',
            action_config={'event_name': 'kanban_scan', 'payload': {}},
        )
        _scanner_schedule_id = sched['id']
        _log(f'Scheduler job registered (interval: {interval}s, id: {_scanner_schedule_id})')
    except Exception as e:
        _log(f'Failed to set up scheduler: {e}', 'error')


def _setup_stale_scheduler():
    global _stale_scanner_schedule_id
    try:
        from backend.scheduler import scheduler
        config = _load_config()
        interval = int(config.get('STALE_SCAN_INTERVAL_SECONDS', 60))

        for s in scheduler.list_schedules(owner_type='plugin', owner_id=PLUGIN_ID):
            if s['name'] == _STALE_SCHEDULE_NAME:
                scheduler.cancel_schedule(s['id'])

        sched = scheduler.create_schedule(
            name=_STALE_SCHEDULE_NAME,
            owner_type='plugin',
            owner_id=PLUGIN_ID,
            trigger_type='interval',
            trigger_config={'seconds': interval},
            action_type='emit_event',
            action_config={'event_name': 'kanban_stale_scan', 'payload': {}},
        )
        _stale_scanner_schedule_id = sched['id']
        _log(f'Stale scheduler registered (interval: {interval}s, id: {_stale_scanner_schedule_id})')
    except Exception as e:
        _log(f'Failed to set up stale scheduler: {e}', 'error')


# ─── Autopilot slash command ──────────────────────────────────────────────────

def _autopilot_handler(
    session_id: str,
    agent_id: str,
    external_user_id: str,
    channel_id: str | None,
    args: str,
) -> str:
    from models.db import db

    arg = args.strip().lower() if args else ""
    if not arg:
        state = "enabled" if _is_autopilot(agent_id) else "disabled"
        return f"Autopilot mode is currently {state}."
    if arg not in ("on", "off"):
        return "Usage: `/autopilot on` or `/autopilot off`"

    db.set_setting(f'autopilot:{agent_id}', '1' if arg == 'on' else '0')
    status = "enabled" if arg == "on" else "disabled"
    agent_name = agent_id
    try:
        from models.db import db as _db2
        agent_data = _db2.get_agent(agent_id)
        if agent_data:
            agent_name = agent_data.get("name", agent_id)
    except Exception:
        pass
    return f"Autopilot {status} for agent {agent_name}."


command_registry.register(
    "autopilot",
    _autopilot_handler,
    "Enable/disable autopilot mode for automatic task processing",
)




# ─── State handler ────────────────────────────────────────────────────────────

def _state_handler(agent_id: str, session_id: str, agent_state, label: str, data):
    """Handle kanban workflow state transitions via the state() built-in tool."""

    task_id = data.get('task_id') if isinstance(data, dict) else data

    # ── kanban:pick ───────────────────────────────────────────────────────────
    if label == 'kanban:pick':
        if not task_id:
            return {
                'result': 'error',
                'message': "Missing task_id. Call state('kanban:pick', {'task_id': '<id>'}).",
            }
        existing_active = _active_tasks.get(agent_id)
        if existing_active and existing_active != task_id:
            return {
                'result': 'error',
                'message': (
                    f"You cannot pick task '{task_id}' — you are still active on task "
                    f"'{existing_active}'. Call state('kanban:finish', {{'task_id': '{existing_active}'}}) "
                    f"to complete it first."
                ),
            }
        # Block pick if task has unmet dependencies
        try:
            from plugins.kanban.db import kanban_db as _kdb_pick
            unmet = _kdb_pick.get_unmet_dependencies(task_id)
            if unmet:
                blocking = ', '.join(f"#{t['id']} '{t['title']}'" for t in unmet)
                return {
                    'result': 'error',
                    'message': (
                        f"Cannot pick task #{task_id} — it has unmet dependencies. "
                        f"The following tasks must be completed first: {blocking}. "
                        f"Please work on those tasks first."
                    ),
                }
        except Exception:
            pass
        _pending_tasks[agent_id] = task_id
        _task_state_since[agent_id] = time.time()
        _awaiting_approval.discard(agent_id)
        autopilot = _is_autopilot(agent_id)
        if autopilot:
            return {
                'result': 'success',
                'state': 'pending',
                'data': {'task_id': task_id, 'autopilot': True},
                'message': (
                    f"Kanban task '{task_id}' acknowledged — state set to pending. "
                    f"Autopilot is ON — skip user approval, call kanban_update_status(in-progress) immediately.\n"
                    f"Next steps:\n"
                    f"1. Call kanban_update_status(task_id='{task_id}', status='in-progress')\n"
                    f"2. Then call state('kanban:activate', {{'task_id': '{task_id}'}}) to unlock all tools"
                ),
            }
        else:
            return {
                'result': 'success',
                'state': 'pending',
                'data': {'task_id': task_id, 'autopilot': False},
                'allowed_tools': KANBAN_APPROVAL_PENDING_TOOLS,
                'message': (
                    f"Kanban task '{task_id}' acknowledged — state set to pending. "
                    f"Autopilot is OFF — present the task to the user and wait for approval.\n"
                    f"Status-changing tools are blocked until the user approves.\n"
                    f"Next steps:\n"
                    f"1. Present the task details to the user and ask for confirmation\n"
                    f"2. Once the user approves, call state('kanban:activate', {{'task_id': '{task_id}'}}) "
                    f"to update status to in-progress and unlock all tools"
                ),
            }

    # ── kanban:activate ───────────────────────────────────────────────────────
    if label == 'kanban:activate':
        if not task_id:
            return {
                'result': 'error',
                'message': "Missing task_id. Call state('kanban:activate', {'task_id': '<id>'}).",
            }
        # Block activate if task has unmet dependencies
        try:
            from plugins.kanban.db import kanban_db as _kdb_act
            unmet = _kdb_act.get_unmet_dependencies(task_id)
            if unmet:
                blocking = ', '.join(f"#{t['id']} '{t['title']}'" for t in unmet)
                return {
                    'result': 'error',
                    'message': (
                        f"Cannot activate task #{task_id} — it has unmet dependencies. "
                        f"The following tasks must be completed first: {blocking}. "
                        f"Please work on those tasks first."
                    ),
                }
        except Exception:
            pass
        # Gate: autopilot=OFF requires explicit user approval before activation
        autopilot = _is_autopilot(agent_id)
        if not autopilot and _approval_granted.get(agent_id) != task_id:
            # Inline fallback: if approval wasn't pre-set (e.g. model called activate
            # without waiting for the SYSTEM REMINDER), check the DB conversation now.
            try:
                from models.db import db as _db2
                recent = _db2.get_session_messages(session_id, limit=10, agent_id=agent_id)
                last_user_msg = None
                last_agent_msg = None
                for msg in reversed(recent):
                    role = msg.get('role', '')
                    if role == 'user' and last_user_msg is None:
                        text = msg.get('content', '') or ''
                        if not text.startswith('[System') and not text.startswith('[SYSTEM'):
                            last_user_msg = text
                    elif role == 'assistant' and last_agent_msg is None:
                        text = msg.get('content', '') or ''
                        if text:
                            last_agent_msg = text
                    if last_user_msg and last_agent_msg:
                        break
                if last_user_msg and last_agent_msg:
                    if _classify_approval(last_agent_msg, last_user_msg):
                        _approval_granted[agent_id] = task_id
            except Exception:
                pass
        if not autopilot and _approval_granted.get(agent_id) != task_id:
            return {
                'result': 'error',
                'message': (
                    f"Cannot activate task '{task_id}' — user has not approved yet. "
                    f"Present the task to the user and wait for their explicit confirmation before calling activate."
                ),
            }
        # Clear approval flag and waiting state now that activation is proceeding
        _approval_granted.pop(agent_id, None)
        _awaiting_approval.discard(agent_id)
        # Validate task existence and archive status before activation
        try:
            from plugins.kanban.db import kanban_db
            task = kanban_db.get(task_id)
            if task is None:
                return {
                    'result': 'error',
                    'message': f"Cannot activate task '{task_id}' — task does not exist.",
                }
            if task.get('archived_at'):
                return {
                    'result': 'error',
                    'message': f"Cannot activate task '{task_id}' — task is archived.",
                }
            if task.get('status') not in ('in-progress', 'done'):
                # Auto-promote to in-progress on activation (no separate kanban_update_status call needed)
                old_status = task.get('status', 'todo')
                kanban_db.update(task_id, {'status': 'in-progress'})
                kanban_db.log_task_status_change(task_id, old_status, 'in-progress')
                try:
                    from backend.event_stream import event_stream
                    updated_task = kanban_db.get(task_id)
                    event_stream.emit('kanban_task_updated', {'task': updated_task})
                except Exception:
                    pass
        except Exception:
            pass  # DB unavailable — allow activation
        _pending_tasks.pop(agent_id, None)
        _active_tasks[agent_id] = task_id
        _task_state_since[agent_id] = time.time()
        _progress_reminder_armed[agent_id] = False
        # Set focus mode so other sessions are rejected while this task is active
        _title = task_id
        if agent_state is not None:
            try:
                from plugins.kanban.db import kanban_db as _kdb
                _task = _kdb.get(task_id)
                _title = _task['title'] if _task else task_id
            except Exception:
                pass
            agent_state.focus = True
            agent_state.focus_reason = f"mengerjakan task #{task_id}: {_title}"
        # Build task detail snippet for the activation message (used after context clear)
        _task_detail = ''
        try:
            from plugins.kanban.db import kanban_db as _kdb2
            _t = _kdb2.get(task_id)
            if _t:
                _task_detail = (
                    f'\n\n<task>\n'
                    f'**Task:** #{task_id} — {_t["title"]}\n'
                    f'**Priority:** {_t.get("priority", "low")}\n'
                )
                if _t.get('description'):
                    _task_detail += f'**Description:** {_t["description"]}\n'
                _task_detail += '</task>'
        except Exception:
            pass
        # Context was already cleared in _notify_agent before the task notification
        # was sent — nothing to do here.
        _ctx_cleared = False
        _ctx_note = (
            '\n\n**Note:** Chat history has been cleared for a fresh start. '
            'Call `use_skill("kanban")` to reload your kanban tools.'
            if _ctx_cleared else ''
        )
        return {
            'result': 'success',
            'state': 'active',
            'data': {'task_id': task_id},
            #'allowed_tools': None,
            'blocked_tools': None,
            'message': (
                f"Task '{task_id}' is now active. All tools are unlocked — proceed with the work."
                f"{_task_detail}"
                f"{_ctx_note}\n"
                f"When finished, call state('kanban:finish', {{'task_id': '{task_id}'}})."
            ),
        }

    # ── kanban:finish ─────────────────────────────────────────────────────────
    if label == 'kanban:finish':
        if not task_id:
            return {
                'result': 'error',
                'message': "Missing task_id. Call state('kanban:finish', {'task_id': '<id>'}).",
            }
        try:
            from plugins.kanban.db import kanban_db
            task = kanban_db.get(task_id)
            if task and task.get('status') != 'done':
                current_status = task.get('status', 'unknown')
                return {
                    'result': 'error',
                    'state': 'active',
                    'data': {'task_id': task_id},
                    'message': (
                        f"Cannot finish — task '{task_id}' status is '{current_status}', not 'done'. "
                        f"Call kanban_update_status(task_id='{task_id}', status='done') first."
                    ),
                }
        except Exception:
            pass  # DB unavailable — allow finish

        # --- Process Recorder: save full agent execution before state clear ---
        # Must happen BEFORE _active_tasks.pop so the agent is still marked busy,
        # preventing race with next task assignment (which may clear the session).
        try:
            config = _load_config()
            if config.get('ENABLE_PROCESS_RECORDER', False):
                from plugins.kanban.db import kanban_db as _kdb
                from models.db import db as _main_db
                session_id = agent_state.session_id if agent_state else None
                if session_id:
                    messages = _main_db.get_session_messages(session_id, limit=9999, agent_id=agent_id)
                    if messages:
                        _kdb.save_process_log(task_id, agent_id, session_id, messages)
        except Exception:
            pass  # Never block task completion on recorder failure

        _active_tasks.pop(agent_id, None)
        _pending_tasks.pop(agent_id, None)
        _paused_tasks.pop(agent_id, None)
        _task_state_since.pop(agent_id, None)
        _progress_reminder_armed.pop(agent_id, None)
        _approval_granted.pop(agent_id, None)
        _awaiting_approval.discard(agent_id)
        # Clear focus mode — agent is now free to accept all sessions
        if agent_state is not None:
            agent_state.focus = False
            agent_state.focus_reason = None
        finish_reminder = _load_config().get('FINISH_REMINDER', '').strip()
        msg = f"Task '{task_id}' completed. State cleared — ready for the next task."
        if finish_reminder:
            msg += f"\n\n**IMPORTANT!** {finish_reminder}"
        return {
            'result': 'success',
            'state': '',  # empty → executor calls clear_state
            'message': msg,
        }

    # \u2500\u2500 kanban:pause \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    if label == 'kanban:pause':
        if not task_id:
            return {
                'result': 'error',
                'message': "Missing task_id. Call state('kanban:pause', {'task_id': '<id>'}).",
            }
        if _active_tasks.get(agent_id) != task_id:
            return {
                'result': 'error',
                'message': (
                    f"Cannot pause task '{task_id}' \u2014 it is not currently active for this agent. "
                    f"Only the agent working on the task can pause it."
                ),
            }
        # Pause the task: move from active to paused, set status to 'paused'
        _active_tasks.pop(agent_id, None)
        _paused_tasks[agent_id] = task_id
        _task_state_since[agent_id] = time.time()
        try:
            from plugins.kanban.db import kanban_db
            task = kanban_db.get(task_id)
            if task and task.get('status') == 'in-progress':
                old_status = task.get('status', 'in-progress')
                kanban_db.update(task_id, {'status': 'paused'})
                kanban_db.log_task_status_change(task_id, old_status, 'paused')
                try:
                    from backend.event_stream import event_stream
                    updated_task = kanban_db.get(task_id)
                    event_stream.emit('kanban_task_updated', {'task': updated_task})
                except Exception:
                    pass
        except Exception:
            pass  # DB unavailable \u2014 allow pause in memory
        # Clear focus mode so other sessions are accepted while paused
        if agent_state is not None:
            agent_state.focus = False
            agent_state.focus_reason = None
        return {
            'result': 'success',
            'state': 'paused',
            'data': {'task_id': task_id},
            'blocked_tools': KANBAN_ALLOWED_TOOLS_LIST,
            'message': (
                f"Task '{task_id}' paused. The task status is now 'paused'. "
                f"Tools are locked until you resume. "
                f"When ready to continue, call state('kanban:resume', {{'task_id': '{task_id}'}})."
            ),
        }

    # \u2500\u2500 kanban:resume \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    if label == 'kanban:resume':
        if not task_id:
            return {
                'result': 'error',
                'message': "Missing task_id. Call state('kanban:resume', {'task_id': '<id>'}).",
            }
        if _paused_tasks.get(agent_id) != task_id:
            return {
                'result': 'error',
                'message': (
                    f"Cannot resume task '{task_id}' \u2014 it is not paused for this agent. "
                    f"Only the agent that paused the task can resume it."
                ),
            }
        # Resume the task: move from paused to active, set status back to 'in-progress'
        _paused_tasks.pop(agent_id, None)
        _active_tasks[agent_id] = task_id
        _task_state_since[agent_id] = time.time()
        _progress_reminder_armed[agent_id] = False
        try:
            from plugins.kanban.db import kanban_db
            task = kanban_db.get(task_id)
            if task and task.get('status') == 'paused':
                old_status = task.get('status', 'paused')
                kanban_db.update(task_id, {'status': 'in-progress'})
                kanban_db.log_task_status_change(task_id, old_status, 'in-progress')
                try:
                    from backend.event_stream import event_stream
                    updated_task = kanban_db.get(task_id)
                    event_stream.emit('kanban_task_updated', {'task': updated_task})
                except Exception:
                    pass
        except Exception:
            pass  # DB unavailable \u2014 allow resume in memory
        # Re-set focus mode
        if agent_state is not None:
            try:
                from plugins.kanban.db import kanban_db as _kdb
                _task = _kdb.get(task_id)
                _title = _task['title'] if _task else task_id
            except Exception:
                _title = task_id
            agent_state.focus = True
            agent_state.focus_reason = f"mengerjakan task #{task_id}: {_title}"
        return {
            'result': 'success',
            'state': 'active',
            'data': {'task_id': task_id},
            'blocked_tools': None,
            'message': (
                f"Task '{task_id}' resumed. Back to active \u2014 continue working. "
                f"When finished, call kanban_update_status(task_id='{task_id}', status='done'), "
                f"then call state('kanban:finish', {{'task_id': '{task_id}'}})."
            ),
        }

    # \u2500\u2500 kanban:postpone \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    if label == 'kanban:postpone':
        if not task_id:
            return {
                'result': 'error',
                'message': "Missing task_id. Call state('kanban:postpone', {'task_id': '<id>'}).",
            }
        # Only allow postponing a task that is pending (not yet activated)
        if _pending_tasks.get(agent_id) != task_id:
            return {
                'result': 'error',
                'message': (
                    f"Cannot postpone task '{task_id}' — it is not in pending state for this agent. "
                    f"Only a pending (not yet activated) task can be postponed."
                ),
            }
        # Clear pending state — task stays in 'todo' so scanner will re-notify later
        _pending_tasks.pop(agent_id, None)
        _approval_granted.pop(agent_id, None)
        _awaiting_approval.discard(agent_id)
        return {
            'result': 'success',
            'state': '',
            'data': {'task_id': task_id},
            'blocked_tools': None,
            'message': (
                f"Task '{task_id}' postponed. The task status remains 'todo' and will be "
                f"re-notified by the scanner later. You can continue your current work."
            ),
        }

    return None  # label not handled


# ─── Tool guard ───────────────────────────────────────────────────────────────

def _tool_guard(agent_id: str, tool_name: str, args: dict) -> Optional[dict]:
    """Block tools for agents with a pending or paused task.

    Pending: Autopilot=ON allows KANBAN_ALLOWED_TOOLS. Autopilot=OFF allows
             KANBAN_APPROVAL_PENDING_TOOLS (excludes update_status/update_task).
    Paused:  Only allow state(kanban:resume) and minimal kanban read tools.
    """
    # \u2500\u2500 Paused guard \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    paused_task_id = _paused_tasks.get(agent_id)
    if paused_task_id:
        # Allow only state (to resume) and kanban read tools
        if tool_name in ('state', 'kanban_search_tasks', 'kanban_get_task', 'kanban_add_comment'):
            return None
        msg = (
            f"Tool '{tool_name}' is blocked — task '{paused_task_id}' is paused. "
            f"Call state('kanban:resume', {{'task_id': '{paused_task_id}'}}) to resume work."
        )
        return {'block': True, 'error': msg}

    task_id = _pending_tasks.get(agent_id)
    if not task_id:
        return None

    # Determine autopilot setting — governs which tools are allowed in pending state
    autopilot = _is_autopilot(agent_id)

    allowed = KANBAN_ALLOWED_TOOLS if autopilot else KANBAN_APPROVAL_PENDING_TOOLS
    if tool_name in allowed:
        return None

    # Self-heal: verify task is still pending on disk
    try:
        from plugins.kanban.db import kanban_db
        task = kanban_db.get(task_id)
        if task is None or task.get('status') in ('done', 'in-progress'):
            _pending_tasks.pop(agent_id, None)
            return None
    except Exception:
        pass

    if autopilot:
        msg = (
            f"Tool '{tool_name}' is blocked — you have a pending kanban task '{task_id}'. "
            f"Call state('kanban:activate', {{'task_id': '{task_id}'}}) to start it first."
        )
    else:
        msg = (
            f"Tool '{tool_name}' is blocked — task '{task_id}' is waiting for user approval. "
            f"Present the task to the user and wait for their explicit confirmation. "
            f"Do NOT start working until the user says yes. "
            f"Once they approve, call state('kanban:activate', {{'task_id': '{task_id}'}}) to begin."
        )
    return {'block': True, 'error': msg}


# ─── Message interceptor ──────────────────────────────────────────────────────

def _message_interceptor(agent_id: str, content: str, messages: list):
    """Inject kanban reminders based on workflow state.

    on_tool_executed updates state asynchronously via the event stream, so we
    cannot rely on _pending_tasks/_active_tasks/_progress_reminder_armed being
    up-to-date by the time this runs. Instead we derive the current-iteration
    tool calls and results directly from the messages list, which is already
    populated synchronously before this function is called.
    """
    # ── derive what tools were called+succeeded in this LLM iteration ──────────
    # Walk messages in reverse: collect tool results, stop at assistant message.
    just_called: dict = {}   # fn_name -> {'args': dict, 'result': dict}
    tool_results_by_id: dict = {}

    for msg in reversed(messages):
        role = msg.get('role', '')
        if role == 'tool':
            tc_id = msg.get('tool_call_id', '')
            try:
                tool_results_by_id[tc_id] = _json.loads(msg.get('content', '{}'))
            except Exception:
                tool_results_by_id[tc_id] = {}
        elif role == 'assistant':
            for tc in (msg.get('tool_calls') or []):
                fn = tc.get('function', {}).get('name', '')
                try:
                    args = _json.loads(tc.get('function', {}).get('arguments', '{}'))
                except Exception:
                    args = {}
                just_called[fn] = {
                    'args': args,
                    'result': tool_results_by_id.get(tc.get('id', ''), {}),
                }
            break   # only inspect the most recent assistant turn

    # ── sync _progress_reminder_armed from just_called (compensate for async) ──
    if agent_id in _active_tasks:
        for fn in just_called:
            if fn not in KANBAN_ALLOWED_TOOLS:
                _progress_reminder_armed[agent_id] = True
                break

    # ── pending: agent picked task but hasn't started it yet ────────────────────
    if agent_id in _pending_tasks:
        task_id = _pending_tasks[agent_id]

        # Skip if kanban:activate or kanban_update_status(in-progress) was just called this turn
        # (on_tool_executed hasn't cleared _pending_tasks yet — it's async)
        ku = just_called.get('kanban_update_status', {})
        if ku.get('args', {}).get('status') == 'in-progress':
            return None
        st = just_called.get('state', {})
        if st.get('args', {}).get('label') == 'kanban:activate':
            # Only skip when activation actually succeeded; if it failed (e.g. approval
            # gate rejected it), fall through so the classifier can run this turn.
            if st.get('result', {}).get('result') == 'success':
                return None

        # ── autopilot=OFF: check if user just approved via LLM classifier ────
        autopilot = _is_autopilot(agent_id)

        print(f'[kanban/interceptor] pending agent={agent_id} task={task_id} '
              f'autopilot={autopilot} granted={_approval_granted.get(agent_id)!r} '
              f'awaiting={agent_id in _awaiting_approval}')
        if not autopilot and _approval_granted.get(agent_id) != task_id:
            # Find last real user message and last agent text (for classifier context)
            last_user_msg = None
            last_agent_msg = None
            for msg in reversed(messages):
                role = msg.get('role', '')
                if role == 'user' and last_user_msg is None:
                    text = msg.get('content', '') or ''
                    # Skip system-injected messages
                    if not text.startswith('[System') and not text.startswith('[SYSTEM'):
                        last_user_msg = text
                elif role == 'assistant' and last_agent_msg is None:
                    text = msg.get('content', '') or ''
                    if text:
                        last_agent_msg = text
                if last_user_msg is not None and last_agent_msg is not None:
                    break

            print(f'[kanban/interceptor] last_user={last_user_msg!r:.60} has_agent_msg={bool(last_agent_msg)}')
            if last_user_msg and last_agent_msg:
                approved = _classify_approval(last_agent_msg, last_user_msg)
                if approved:
                    _approval_granted[agent_id] = task_id
                    _log(f'Approval granted for agent {agent_id} on task {task_id} '
                         f'(user: {last_user_msg!r:.60})')

        if not autopilot and _approval_granted.get(agent_id) != task_id:
            if agent_id not in _awaiting_approval:
                # First time in this pending state — tell agent to present the task
                _awaiting_approval.add(agent_id)
                _present_reminder = (
                    f"[SYSTEM REMINDER] You have acknowledged task '{task_id}'. "
                    f"Present the task to the user and ask for their confirmation. "
                    f"Once they approve, call state('kanban:activate', {{'task_id': '{task_id}'}})."
                )
                # Dedup: skip if already in recent messages
                _tail = messages[-6:]
                if any(m.get('role') == 'user' and m.get('content') == _present_reminder
                       for m in _tail):
                    return None
                return {'inject': _present_reminder}
            # Already presented — stay silent. Tool guard blocks unauthorized work.
            # Agent just needs to wait; injecting a reminder here causes re-present loops.
            return None

        # ── autopilot=ON pending: detect plan generation and nudge ──
        if autopilot and content and RE_PLAN.search(content):
            _plan_nudge = (
                f"[SYSTEM REMINDER] Autopilot is ON — no plan or approval needed. "
#                f"Call kanban_update_status(task_id='{task_id}', status='in-progress') "
#                f"and state('kanban:activate', {{'task_id': '{task_id}'}}) NOW."
            )
            if not any(m.get('role') == 'user' and m.get('content') == _plan_nudge
                       for m in messages):
                return {'inject': _plan_nudge}

        _approved_reminder = (
            f"[SYSTEM] User approved task '{task_id}'. "
#            f"Call state('kanban:activate', {{'task_id': '{task_id}'}}) NOW. "
#            f"Do NOT respond with text first — make the tool call immediately."
        )
        # Dedup: skip if the same reminder was already injected anywhere in messages.
        # Using only messages[-6:] caused infinite loops when each iteration added
        # messages that pushed the original injection out of the window.
        if any(m.get('role') == 'user' and m.get('content') == _approved_reminder
               for m in messages):
            return None
        return {'inject': _approved_reminder}

    if agent_id not in _active_tasks:
        return None

    task_id = _active_tasks[agent_id]

    # ── active: skip if kanban_update_status(done) was just called this turn ───
    ku = just_called.get('kanban_update_status', {})
    if ku.get('args', {}).get('status') == 'done':
        return None

    # ── done reminder: LLM text indicates task is complete ───────────────────
    if content and RE_DONE.search(content):
        return {
            'inject': (
                f"[SYSTEM REMINDER] You indicated the work is done. You MUST:\n"
                f"1. Call kanban_update_status(task_id='{task_id}', status='done')\n"
                f"2. Call state('kanban:finish', {{'task_id': '{task_id}'}})\n"
                f"Do this NOW before sending your final response."
            )
        }

    # ── autopilot=ON active: detect plan generation and nudge ──
    _autopilot_active = _is_autopilot(agent_id)
    if _autopilot_active and content and RE_PLAN.search(content):
        _active_plan_nudge = (
            f"[SYSTEM REMINDER] You are in autopilot mode. "
            f"Execute directly — skip plan generation. "
            f"Your task is #{task_id} — work on it now."
        )
        if not any(m.get('role') == 'user' and m.get('content') == _active_plan_nudge
                   for m in messages):
            return {'inject': _active_plan_nudge}

    # ── progress reminder: agent used a real tool, time to log progress ─────
    if _progress_reminder_armed.get(agent_id, False):
        _progress_reminder_armed[agent_id] = False
        return {
            'inject': (
                f"[SYSTEM REMINDER] If You just made progress on task '{task_id}'. "
                f"Please call kanban_add_comment(task_id='{task_id}', content='<brief progress summary>') "
                f"to log this update on the kanban board."
            )
        }

    return None


# ─── Builtin suppressor ───────────────────────────────────────────────────────

def _builtin_suppressor(agent_id: str, builtin_id: str) -> bool:
    """Hide builtin:update_tasks when the agent has kanban skill assigned."""
    if builtin_id != 'builtin:update_tasks':
        return False
    try:
        from models.db import db
        return 'kanban' in (db.get_agent_skills(agent_id) or [])
    except Exception:
        return False


# ─── Event handlers ───────────────────────────────────────────────────────────

def on_kanban_task_created(event, sdk):
    """Task created — notification is handled by the periodic scanner, not here."""
    pass


def on_kanban_task_updated(event, sdk):
    """Clear workflow state when a task status changes."""
    task = event.get('task', {})
    if not task:
        return

    task_id = task.get('id', '')
    status = task.get('status', '')
    assignee = task.get('assignee', '')

    if status in ('todo', 'done', 'paused'):
        # Clear agent's pending/active state for this task so the next scan
        # can re-notify them (handles agent stopped mid-workflow, task reset, etc.)
        with _lock:
            if assignee:
                if _pending_tasks.get(assignee) == task_id:
                    _pending_tasks.pop(assignee, None)
                    _approval_granted.pop(assignee, None)
                    _awaiting_approval.discard(assignee)
                if _active_tasks.get(assignee) == task_id:
                    _active_tasks.pop(assignee, None)
                if _paused_tasks.get(assignee) == task_id:
                    # Task was manually changed to 'paused' from outside (not via state)
                    pass  # _paused_tasks already correct


def on_schedule_fired(event, sdk):
    """Route scheduled events to the appropriate scan function."""
    if event.get('owner_type') != 'plugin' or event.get('owner_id') != PLUGIN_ID:
        return
    if _notifier_paused:
        _log('Notifier is paused — skipping scheduled scan', 'info', sdk)
        return
    if event.get('name') == _STALE_SCHEDULE_NAME:
        _scan_stale_tasks(sdk)
    else:
        _scan_and_notify(sdk)
        _scan_comments_for_followup(sdk)


def on_tool_executed(event, sdk):
    """Update workflow state on kanban tool calls; arm progress reminder on work."""
    tool_name = event.get('tool_name', '')
    agent_id = event.get('agent_id', '')
    if not agent_id:
        return

    # ── kanban_add_comment: re-arm progress reminder ──────────────────────────
    if tool_name == 'kanban_add_comment':
        result = event.get('tool_result', {})
        if isinstance(result, str):
            try:
                result = _json.loads(result)
            except Exception:
                pass
        if isinstance(result, dict) and result.get('status') == 'success':
            _progress_reminder_armed[agent_id] = True
        return

    # ── kanban_update_status: update workflow state ───────────────────────────
    if tool_name in ('kanban_update_status', 'kanban_update_task'):
        result = event.get('tool_result', {})
        if isinstance(result, str):
            try:
                result = _json.loads(result)
            except Exception:
                return
        task = result.get('task', {})
        task_status = task.get('status')

        if task_status == 'in-progress':
            task_id = task.get('id', '')
            _pending_tasks.pop(agent_id, None)
            if task_id:
                _active_tasks[agent_id] = task_id
                _task_state_since[agent_id] = time.time()
            _progress_reminder_armed[agent_id] = False
            _log(f'Guard cleared for agent {agent_id} — task activated', 'info', sdk)

        elif task_status == 'paused':
            task_id = task.get('id', '')
            _pending_tasks.pop(agent_id, None)
            if task_id:
                _active_tasks.pop(agent_id, None)
                _paused_tasks[agent_id] = task_id
                _task_state_since[agent_id] = time.time()
            _progress_reminder_armed.pop(agent_id, None)
            _log(f'Task paused for agent {agent_id} — task {task_id}', 'info', sdk)

        elif task_status == 'done':
            _pending_tasks.pop(agent_id, None)
            _active_tasks.pop(agent_id, None)
            _paused_tasks.pop(agent_id, None)
            _task_state_since.pop(agent_id, None)
            _progress_reminder_armed.pop(agent_id, None)
            _log(f'Guard cleared for agent {agent_id} — task done, triggering scan in 10s', 'info', sdk)
            global _pending_scan_timer
            with _lock:
                if _pending_scan_timer is not None:
                    _pending_scan_timer.cancel()
                def _scan_all(sdk=sdk):
                    _scan_and_notify(sdk)
                    _scan_comments_for_followup(sdk)
                _pending_scan_timer = threading.Timer(20.0, _scan_all)
                _pending_scan_timer.daemon = True
                _pending_scan_timer.start()
        return

    # ── Any other non-kanban tool: arm the progress reminder ──────────────────
    # Only arm if tool succeeded — don't trigger reminder for blocked/errored calls
    if tool_name not in KANBAN_ALLOWED_TOOLS and agent_id in _active_tasks:
        if not event.get('has_error', False):
            _progress_reminder_armed[agent_id] = True


# ─── Busy message provider ────────────────────────────────────────────────────

def _busy_message_provider(agent_id: str, agent_state) -> Optional[str]:
    """Return a contextual message when the agent is busy with a kanban task."""
    task_id = _active_tasks.get(agent_id) or _pending_tasks.get(agent_id) or _paused_tasks.get(agent_id)
    if not task_id:
        return None
    try:
        from plugins.kanban.db import kanban_db
        task = kanban_db.get(task_id)
        if task:
            status_text = {
                'in-progress': 'working on',
                'paused': 'pausing task',
                'todo': 'waiting on task',
            }.get(task.get('status', ''), 'working on')
            return (
                f"Sorry, I'm {status_text} #{task_id}: {task['title']}. "
                f"Wait until it's done."
            )
    except Exception:
        pass
    return None


# ─── One-time migration: kanban_agent → kanban skill ID ──────────────────────

def _migrate_skill_id():
    """Rename legacy 'kanban_agent' skill ID references in the DB to 'kanban'."""
    try:
        from models.db import db
        conn = db._connect()
        with conn:
            conn.execute(
                "UPDATE settings SET key = 'skill_enabled:kanban' WHERE key = 'skill_enabled:kanban_agent'"
            )
            conn.execute(
                "UPDATE settings SET key = REPLACE(key, 'skill_config:kanban_agent:', 'skill_config:kanban:')"
                " WHERE key LIKE 'skill_config:kanban_agent:%'"
            )
            conn.execute(
                "UPDATE agent_skills SET skill_id = 'kanban' WHERE skill_id = 'kanban_agent'"
            )
    except Exception:
        pass

_migrate_skill_id()

# ─── Register handlers ────────────────────────────────────────────────────────

try:
    from backend.plugin_manager import (
        register_state_handler,
        register_tool_guard,
        register_message_interceptor,
        register_builtin_suppressor,
        register_busy_message_provider,
    )
    register_state_handler('kanban', _state_handler)
    register_tool_guard(_tool_guard)
    register_message_interceptor(_message_interceptor)
    register_builtin_suppressor(_builtin_suppressor)
    register_busy_message_provider(_busy_message_provider)
except Exception:
    pass

# ─── Register scheduler jobs when module is loaded ───────────────────────────
_setup_scheduler()
_setup_stale_scheduler()

# ─── CLI Command Handlers ──────────────────────────────────────────────────────

def cli_kanban_add_task(title, description=None, priority=None, assignee=None):
    """Create a new kanban task via CLI."""
    from plugins.kanban.db import kanban_db
    from datetime import datetime, timezone
    import sys

    if priority is None:
        priority = 'low'
    if priority not in ('low', 'medium', 'high'):
        print(f"Error: Invalid priority '{priority}'. Must be low, medium, or high.")
        sys.exit(1)

    now = datetime.now(timezone.utc).isoformat()
    task = kanban_db.create({
        "title": title,
        "description": description or "",
        "status": "todo",
        "priority": priority,
        "assignee": assignee,
        "completed_at": None,
        "created_at": now,
        "updated_at": now,
    })
    print(f"Task created: #{task['id']} \"{task['title']}\" (priority: {task['priority']}, assignee: {task['assignee'] or 'none'})")


def cli_kanban_rm_task(task_id):
    """Delete a kanban task by ID."""
    from plugins.kanban.db import kanban_db
    import sys

    if not task_id.isdigit():
        print(f"Error: Invalid task ID '{task_id}'. Must be a number.")
        sys.exit(1)

    tid = int(task_id)
    task = kanban_db.get(tid)
    if not task:
        print(f"Error: Task #{tid} not found.")
        sys.exit(1)

    kanban_db.delete(tid)
    print(f"Task #{tid} \"{task['title']}\" deleted.")


def cli_kanban_update_task(task_id, title=None, description=None, status=None, priority=None, assignee=None):
    """Update a kanban task field by ID."""
    from plugins.kanban.db import kanban_db
    from datetime import datetime, timezone
    import sys

    if not task_id.isdigit():
        print(f"Error: Invalid task ID '{task_id}'. Must be a number.")
        sys.exit(1)

    tid = int(task_id)
    task = kanban_db.get(tid)
    if not task:
        print(f"Error: Task #{tid} not found.")
        sys.exit(1)

    fields = {}
    if title is not None:
        fields['title'] = title
    if description is not None:
        fields['description'] = description
    if status is not None:
        fields['status'] = status
    if priority is not None:
        if priority not in ('low', 'medium', 'high'):
            print(f"Error: Invalid priority '{priority}'. Must be low, medium, or high.")
            sys.exit(1)
        fields['priority'] = priority
    if assignee is not None:
        fields['assignee'] = assignee if assignee.lower() not in ('none', 'null', '') else None

    if not fields:
        print("Error: No fields to update. Specify at least one of: --title, --description, --status, --priority, --assignee")
        sys.exit(1)

    # Auto-set completed_at when status changes to done
    if status == 'done' and not task.get('completed_at'):
        fields['completed_at'] = datetime.now(timezone.utc).isoformat()

    updated = kanban_db.update(tid, fields)
    if not updated:
        print(f"Error: Failed to update task #{tid}.")
        sys.exit(1)

    parts_out = []
    for k, v in fields.items():
        if k == 'completed_at':
            continue
        parts_out.append(f"{k}={v or 'none'}")
    print(f"Task #{tid} updated: {', '.join(parts_out)}")
