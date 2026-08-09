"""
prefetch.py — Turn context pre-fetcher.

Pre-builds message list and static context in a background thread after each
turn completes, so the next turn starts with cached data instead of hitting
disk (DB + JSONL) again.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)

# TTL for prefetched context.  Short enough that stale data is detected quickly
# but long enough to cover rapid multi-turn conversations (autopilot, etc.).
_PREFETCH_TTL_SECONDS = 15.0


@dataclass
class _PrefetchEntry:
    """Cached turn context for a single session."""
    session_id: str
    agent_id: str
    messages: List[Dict[str, Any]]
    tools: List[Dict[str, Any]]
    system_prompt: str
    agent_context: Dict[str, Any]
    summary_watermark: Any = None
    timestamp: float = field(default_factory=time.time)
    # Last user-message content in the cached messages list, used as a
    # lightweight staleness check (if a new user message arrived, the last
    # content will differ).
    last_user_content: str = ""


class TurnPrefetcher:
    """Prefetches turn context after each turn so the next turn starts faster.

    Usage (in AgentRuntime):
        self._prefetcher = TurnPrefetcher()

        # After a turn completes:
        self._prefetcher.submit(agent, ctx, messages, tools, system_prompt,
                                agent_context, chatlog)

        # At the start of the next turn:
        entry = self._prefetcher.try_get(ctx.session_id)
        if entry:
            messages, tools, system_prompt = \
                entry.messages, entry.tools, entry.system_prompt
    """

    def __init__(self):
        self._cache: Dict[str, _PrefetchEntry] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='turn-prefetch')

    def submit(self, agent: dict, ctx, messages: list, tools: list,
               system_prompt: str, agent_context: dict) -> None:
        """Submit a background prefetch for the given session.

        Called from _do_process_inner() right after a turn finishes.
        The prefetch task re-loads messages from disk so that the next
        turn can skip the I/O.
        """
        session_id = ctx.session_id
        agent_id = agent['id']
        db_agent_id = getattr(ctx, 'session_db_agent_id', None) or agent_id

        self._executor.submit(
            self._do_prefetch, session_id, agent_id, db_agent_id,
            agent, ctx, messages, tools, system_prompt, agent_context)

    def _do_prefetch(self, session_id: str, agent_id: str, db_agent_id: str,
                     agent: dict, ctx, messages: list, tools: list,
                     system_prompt: str, agent_context: dict) -> None:
        """Background: re-load messages from JSONL and build fresh context."""
        try:
            from models.db import db
            from models.chatlog import chatlog_manager
            from backend.agent_runtime import context as _ctx
            from backend.channels.registry import channel_manager

            # Rebuild system prompt (may have changed since last turn)
            fresh_system_prompt = _ctx.build_system_prompt(agent)

            # Rebuild tools
            fresh_tools = _ctx.build_tools(agent)

            # Rebuild agent context. Explorers use their own configured tool
            # set; everyone else inherits from the DB.
            if agent.get('is_explorer'):
                from backend.agent_runtime import explorer as _explorer
                assigned_tool_ids = list(_explorer.tool_ids(agent))
            else:
                assigned_tool_ids = db.get_agent_tools(db_agent_id)

            # Inter-agent communication is enabled by the agent-level toggle;
            # send_agent_message is therefore available without a separate tool
            # assignment, matching build_tools() exposure.
            if (agent.get('agent_messaging_enabled') != 0
                    and 'send_agent_message' not in assigned_tool_ids):
                assigned_tool_ids.append('send_agent_message')

            # Inject skill tool IDs from assigned skills into assigned_tool_ids.
            # This mirrors build_tools() Layer 9 auto-injection and ensures
            # the authorization guard allows execution of skill tools that
            # belong to skills explicitly assigned to the agent.
            assigned_skill_ids = set(db.get_agent_skills(db_agent_id))
            if assigned_skill_ids:
                from backend.skills_manager import skills_manager
                _existing_ids = set(assigned_tool_ids)
                for _sd in skills_manager.get_all_skill_tool_defs():
                    _tid = _sd.get('id', '')
                    if not _tid:
                        continue
                    # Parse skill:<skill_id>:<fn_name>
                    _parts = _tid.split(':', 2)
                    if len(_parts) != 3 or _parts[0] != 'skill':
                        continue
                    _skill_id = _parts[1]
                    if _skill_id in assigned_skill_ids and _tid not in _existing_ids:
                        assigned_tool_ids.append(_tid)

            # Auto-assign artifact tools (save_artifact, list_artifacts, fetch_artifact)
            # only when artifacts_enabled is True for the agent.
            if agent.get('artifacts_enabled', True):
                if 'save_artifact' not in assigned_tool_ids:
                    assigned_tool_ids.append('save_artifact')

                # Agents with save_artifact automatically get list_artifacts.
                # fetch_artifact is only auto-assigned for agents with workplace or sandbox;
                # local agents can access artifacts directly via bash/runpy.
                if 'save_artifact' in assigned_tool_ids:
                    if 'list_artifacts' not in assigned_tool_ids:
                        assigned_tool_ids.append('list_artifacts')
                    if agent.get('workplace_id') or agent.get('sandbox_enabled', 0):
                        if 'fetch_artifact' not in assigned_tool_ids:
                            assigned_tool_ids.append('fetch_artifact')

            # Agents with vision_enabled automatically get describe_image.
            # No DB assignment needed — every vision-capable agent can analyze images.
            if agent.get('vision_enabled', 1) and 'describe_image' not in assigned_tool_ids:
                assigned_tool_ids.append('describe_image')

            # Agents with audio_enabled automatically get transcribe_audio.
            if agent.get('audio_enabled') and 'transcribe_audio' not in assigned_tool_ids:
                assigned_tool_ids.append('transcribe_audio')

            # Check whether describe_image is assigned — passed through to
            # build_message_entry so the hint is only injected when available.
            has_describe_image = 'describe_image' in assigned_tool_ids

            from backend.channels.whatsapp_identity import resolve_identity
            _identity = resolve_identity(ctx.channel_id, ctx.external_user_id)

            fresh_agent_context = {
                'id': agent_id,
                '_db_agent_id': agent.get('_db_agent_id', agent_id),
                'name': agent.get('name', ''),
                'agent_name': agent.get('name', ''),
                'agent_model': None,
                'user_id': ctx.external_user_id,
                'user_phone': _identity['user_phone'],
                'user_jid': _identity['user_jid'],
                'user_id_namespace': _identity['user_id_namespace'],
                'channel_id': ctx.channel_id,
                'session_id': session_id,
                'assigned_tool_ids': assigned_tool_ids,
                'workspace': agent.get('workspace') or None,
                'workplace_id': agent.get('workplace_id') or None,
                'send_file_allowed_path_regex': agent.get('send_file_allowed_path_regex', ''),
                'is_super': bool(agent.get('is_super')),
                'is_subagent': bool(agent.get('is_subagent')),
                'is_explorer': bool(agent.get('is_explorer')),
                'parent_id': agent.get('parent_id'),
                '_sandbox_parent_session_id': agent.get('_sandbox_parent_session_id'),
                '_sandbox_parent_workspace': agent.get('_sandbox_parent_workspace'),
                'agent_messaging_enabled': bool(agent.get('agent_messaging_enabled')),
                'sandbox_enabled': agent.get('sandbox_enabled', 1),
                'safety_checker_enabled': agent.get('safety_checker_enabled', 1),
                'disable_parallel_tool_execution': agent.get('disable_parallel_tool_execution', 0),
                'disable_turn_prefetch': agent.get('disable_turn_prefetch', 0),
                'variables': db.get_agent_variables_dict(agent_id),
                'run_as_user': agent.get('run_as_user'),
                'vision_model_id': agent.get('vision_model_id'),
                'vision_enabled': agent.get('vision_enabled', 1),
                'audio_enabled': agent.get('audio_enabled', 0),
                'messaging_acl': agent.get('messaging_acl'),
                'messaging_acl_mode': agent.get('messaging_acl_mode', 'whitelist'),
                'enable_atg': bool(agent.get('enable_atg')) and bool(agent.get('enable_agent_state')),
                'enable_cmp': bool(agent.get('enable_cmp')) and bool(agent.get('enable_agent_state')),
            }

            # Re-load messages from JSONL (most expensive I/O, ~10-200ms)
            chatlog = chatlog_manager.get(db_agent_id, session_id)
            summary_record = db.get_summary(session_id, agent_id=db_agent_id)

            # Build message list from JSONL
            fresh_messages = [{"role": "system", "content": fresh_system_prompt}]

            if summary_record:
                fresh_messages.append({
                    "role": "system",
                    "content": f"## Prior conversation summary\n{summary_record['summary']}"
                })

            # CMP offload: scope history to the active path's segments via the
            # SAME shared builder the runtime uses (prefetch assumes the next
            # turn continues the active path; the runtime discards the hit on
            # a switch/branch decision).
            _jsonl_entries = None
            if agent.get('enable_cmp') and agent.get('enable_agent_state'):
                try:
                    import json as _json
                    from backend.agent_runtime.cmp import assembler as _cmp_asm
                    _sess_raw = db.get_session_state(session_id, agent_id=db_agent_id)
                    _cmp_state = (_json.loads(_sess_raw) or {}).get('cmp') if _sess_raw else None
                    if _cmp_asm.should_filter(_cmp_state):
                        _jsonl_entries = _cmp_asm.build_history(
                            chatlog, summary_record, _cmp_state)
                except Exception:
                    _logger.exception("CMP prefetch history filter failed — full history")
                    _jsonl_entries = None
            if _jsonl_entries is None:
                from backend.agent_runtime import context as _ctx
                _jsonl_entries = chatlog.get_entries_for_llm_trail(
                    after_ts=summary_record.get('last_message_ts') if summary_record else None,
                    **_ctx.trail_history_kwargs(agent_id),
                )
            _use_jsonl = bool(_jsonl_entries) or chatlog.get_last_entry() is not None

            if _use_jsonl:
                conv_msgs = _jsonl_entries
                # Without summary: skip leading non-user messages.
                # With summary: keep assistant msgs (unsummarized continuation)
                # but skip orphaned tool responses (no preceding tool_calls).
                tail_start = 0
                if not summary_record:
                    while (tail_start < len(conv_msgs)
                           and conv_msgs[tail_start].get('role') != 'user'):
                        tail_start += 1
                else:
                    while (tail_start < len(conv_msgs)
                           and conv_msgs[tail_start].get('role') == 'tool'):
                        tail_start += 1
                for msg in conv_msgs[tail_start:]:
                    fresh_messages.append(msg)
            else:
                # Fall back to SQLite
                if summary_record:
                    raw_tail = db.get_messages_after(
                        session_id, summary_record['last_message_id'],
                        agent_id=db_agent_id)
                    # Skip orphaned tool responses, keep the rest.
                    tail_start = 0
                    while (tail_start < len(raw_tail)
                           and raw_tail[tail_start].get('role') == 'tool'):
                        tail_start += 1
                    for msg in raw_tail[tail_start:]:
                        fresh_messages.append(
                            _ctx.build_message_entry(msg, agent, has_describe_image))
                else:
                    history = db.get_session_messages(
                        session_id, limit=50, agent_id=db_agent_id)
                    for msg in history:
                        fresh_messages.append(
                            _ctx.build_message_entry(msg, agent, has_describe_image))

            # Keep an authoritative metadata-only file index outside the prunable tail.
            _ctx.sync_session_attachment_manifest(fresh_messages, session_id, db_agent_id)

            # Ensure messages don't end with assistant role
            while (len(fresh_messages) > 1
                   and fresh_messages[-1].get('role') == 'assistant'):
                fresh_messages.pop()

            # Inject inter-agent context notes
            if ctx.external_user_id.startswith("__agent__"):
                _other_id = ctx.external_user_id[len("__agent__"):]
                _other_agent = db.get_agent(_other_id)
                _other_name = _other_agent.get('name', _other_id) if _other_agent else _other_id
                if (getattr(ctx, 'session_db_agent_id', None)
                        and ctx.session_db_agent_id != agent_id):
                    _db_owner = db.get_agent(ctx.session_db_agent_id)
                    _db_owner_name = (_db_owner.get('name', ctx.session_db_agent_id)
                                      if _db_owner else ctx.session_db_agent_id)
                    _context_note = (
                        "## Inter-Agent Session (Cross-Agent Processing)\n"
                        f"You are processing a shared session owned by **{_db_owner_name}** "
                        f"(id: `{ctx.session_db_agent_id}`)."
                    )
                else:
                    _context_note = (
                        "## Inter-Agent Session\n"
                        f"You are currently in a private session with another agent: "
                        f"**{_other_name}** (id: `{_other_id}`)."
                    )
                fresh_messages.insert(1, {"role": "system", "content": _context_note})

            # Inject channel-specific instructions
            if ctx.channel_id:
                chan_inst = channel_manager.get_channel_instance(ctx.channel_id)
                if chan_inst:
                    chan_instr = chan_inst.get_system_instructions()
                    if chan_instr:
                        fresh_messages.insert(1, {"role": "system", "content": chan_instr})

            # Inject channel user identity (authoritative name for this session).
            # Skip if already present in the message list (from JSONL history) to
            # avoid duplicates when rebuilding from scratch.
            _already_injected = any(
                "## Current User" in (m.get("content") or "")
                for m in fresh_messages[:6]
            )
            if ctx.channel_id and not ctx.external_user_id.startswith("__agent__") and not _already_injected:
                user_id_ctx = _ctx.build_user_identity_context(
                    ctx.channel_id, ctx.external_user_id,
                )
                if user_id_ctx:
                    fresh_messages.insert(1, {"role": "system", "content": user_id_ctx})

            # Record last user message for staleness detection
            last_user = ""
            for m in reversed(fresh_messages):
                if m.get('role') == 'user':
                    last_user = m.get('content', '')
                    break

            entry = _PrefetchEntry(
                session_id=session_id,
                agent_id=agent_id,
                messages=fresh_messages,
                tools=fresh_tools,
                system_prompt=fresh_system_prompt,
                agent_context=fresh_agent_context,
                summary_watermark=(summary_record.get('last_message_ts')
                                   if summary_record else None),
                last_user_content=last_user,
            )

            with self._lock:
                self._cache[session_id] = entry

            _logger.debug("Prefetch complete for session %s (%d messages)",
                          session_id, len(fresh_messages))
        except Exception:
            _logger.debug("Prefetch failed for session %s", session_id, exc_info=True)

    def try_get(self, session_id: str, *,
                summary_watermark: Any = None) -> Optional[_PrefetchEntry]:
        """Return context only when its age and summary watermark are current."""
        with self._lock:
            entry = self._cache.get(session_id)
            if entry is None:
                return None
            if (time.time() - entry.timestamp > _PREFETCH_TTL_SECONDS
                    or entry.summary_watermark != summary_watermark):
                del self._cache[session_id]
                return None
            return entry

    def invalidate(self, session_id: str) -> None:
        """Remove cached entry for a session (e.g. when a new message arrives)."""
        with self._lock:
            self._cache.pop(session_id, None)

    def shutdown(self) -> None:
        """Shut down the background executor."""
        self._executor.shutdown(wait=False)
