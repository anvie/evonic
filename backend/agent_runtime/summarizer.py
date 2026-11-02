"""
summarizer.py — background conversation summarization pipeline.

Self-contained: chunked LLM summarization, DB persistence, recap log writing.
"""

import os
import json
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, Any

from models.db import db
from backend.llm_client import llm_client, strip_thinking_tags
from config import AGENT_MAX_SUMMARIZE_BATCH as MAX_SUMMARIZE_BATCH
from backend.agent_runtime.summary_compactor import (
    compact_summary,
    MAX_SUMMARY_CHARS,
)

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOGS_DIR = os.path.join(_BASE_DIR, 'logs')


def _recap_log_path(agent_id: str) -> str:
    return os.path.join(_LOGS_DIR, 'agents', agent_id, 'sessrecap.log')


def _current_datetime_str() -> str:
    gmt7 = timezone(timedelta(hours=7))
    now = datetime.now(gmt7)
    return now.strftime('%A, %Y-%m-%d, %H:%M:%S (WIB/UTC+7)')


def _render_prompt(template: str, replacements: Dict[str, str]) -> str:
    """Substitute {key} placeholders with JSON-escaped values.

    Uses str.replace (not str.format) so stray curly braces in the template
    are ignored. Values are JSON-escaped (inner quotes, newlines, backslashes)
    so the result stays valid when the template is a JSON string.
    """
    for key, value in replacements.items():
        raw_placeholder = '{' + key + '}'
        if raw_placeholder not in template:
            continue
        # json.dumps adds outer quotes and escapes internals; strip the quotes.
        escaped = json.dumps(value, ensure_ascii=False)[1:-1]
        template = template.replace(raw_placeholder, escaped)
    return template


DEFAULT_SUMMARIZE_PROMPT = """You are a conversation summarizer. Create a factual summary of a conversation between a user and an AI assistant.

CRITICAL — CURRENT DATE/TIME: {current_datetime}
This is the authoritative reference date. ALL dates in your summary MUST reflect this
current date. If the existing summary or messages contain dates from the past (e.g.
2025 or earlier), you MUST correct them to the current date. Do NOT copy old dates.

Rules:
- Write in English only, regardless of the conversation language
- Extract ONLY facts, decisions, requests, and outcomes
- Do NOT include any style, tone, greetings, emojis, or honorifics
- Use bullet points for clarity
- If there is an existing summary, merge then create new compact summary
- Ignore the tools, focus to the agent response and user decision.
- Keep the summary concise; merge duplicate points and drop details that are no longer relevant to the current session.
- You **MUST KEEP** the user information that already provided or known like phone number, full name, contact person if any.

## Summary Example:
- User Information:
    - Full Name: ...
    - Phone Number: ...
    - Total Guests: ...
- Requests:
    - ...
    - ...
- Follow up:
    - ...
- Issues: <-- notes if user complaint, angry, frustrated, encounter error, or confused, format: issue description. agent response: xxx.
    - ...
- Facts:
    - ...

{existing_summary_section}

Messages to summarize:
{messages_text}

Write the updated factual summary. Remember: the current date is {current_datetime}. Use this date — do NOT use dates from the messages or existing summary."""


# Appended to every summarizer prompt (custom or default). The knowledge/graph
# extractor only ever sees the summary, so anything dropped here is lost
# downstream. This mandates a dedicated, always-regenerated entities section so
# named entities survive compaction even under a strong domain-specific prompt.
_ENTITY_PRESERVATION_CLAUSE = """

============================================================
MANDATORY — this overrides any conflicting instruction above.
You MUST end the summary with a section titled exactly:

## Entities & Relationships

In it, list EVERY specific named person, place, organization, venue, product, or brand the user mentioned — even casually, in passing, or as a personal anecdote (a café, a city, a district, a company, a tool). For each, give concrete details AND how it relates to other named entities (located in / works at / visited / owns / part of / uses …). Never drop a named entity as "small talk", and always carry this section forward when updating an existing summary.

IMAGES: if any message contains an image — Markdown `![name](url)` or HTML `<img src="url">` — you MUST carry the image markdown VERBATIM (exact URL, unmodified) onto the line of the entity it depicts. Never summarize an image away as "photos were sent"; downstream systems extract these URLs from the summary to set document thumbnails.

Format example:
## Entities & Relationships
- Djournal Coffee — café in Grand Indonesia, Thamrin, Jakarta; User visited (has an outdoor smoking area); photo: ![djournal.jpg](/api/attachments/12/view)
- Thamrin — district in Jakarta; located_in Jakarta
- Jakarta — city in Indonesia; User's current location

If the user mentioned no named entities at all, write:
## Entities & Relationships
- (none)"""


# Appended to every summarizer prompt (custom or default). Enforces the hard
# character cap on the persisted summary. Semantic compaction runs afterwards as
# a guarantee, but stating the budget up front keeps the model from overshooting
# and makes the drop policy explicit: compress wording, drop session-irrelevant
# details, but never remove user identity/contact or the entities section.
_LENGTH_CAP_CLAUSE = f"""

============================================================
HARD SIZE LIMIT — this overrides any conflicting instruction above.
The summary, INCLUDING the "## Entities & Relationships" section, MUST NOT
exceed {MAX_SUMMARY_CHARS} characters. Stay comfortably under the limit:
- Merge duplicate points and compress wording into terse fragments.
- Drop details that are no longer relevant to the current session (resolved
  issues, superseded plans, stale small talk, intermediate steps).
- Never drop user identity/contact details or the entities section; when space
  is tight, shorten those entries instead of removing them.
When in doubt, prefer the MOST RECENT request, decision, and outcome.
"""


def maybe_summarize(agent: dict, session_id: str,
                    summarize_guard: threading.Lock,
                    summarize_active: set,
                    llm_lock: threading.Lock) -> bool:
    """Concurrency guard: prevents duplicate summarization for the same session.

    Returns True if a summary was actually generated and persisted, False otherwise.
    """
    # LOCK ORDERING: _summarize_guard → llm_lock. _summarize_guard is acquired
    # first (here), then llm_lock is acquired inside the summarization call.
    # Never reverse this order.
    with summarize_guard:
        if session_id in summarize_active:
            print(f"[AgentRuntime] Summarize skipped: already in progress for session {session_id}")
            return False
        summarize_active.add(session_id)
    try:
        from backend.llm_usage_events import usage_context
        with usage_context('summarizer', agent.get('id'), agent.get('name'), session_id):
            return _do_summarize(agent, session_id, llm_lock)
    except Exception as e:
        print(f"[AgentRuntime] Summarization error (non-fatal): {e}")
        return False
    finally:
        with summarize_guard:
            summarize_active.discard(session_id)


def _do_summarize(agent: dict, session_id: str, llm_lock: threading.Lock) -> bool:
    """Core summarization logic with chunking and truncation protection.

    Returns True if a summary was generated and persisted, False otherwise.
    """
    from models.chatlog import chatlog_manager, _SUMMARY_COUNT_TYPES
    agent_id = agent['id']
    threshold = agent.get('summarize_threshold', 3)
    tail_size = agent.get('summarize_tail', 5)

    chatlog = chatlog_manager.get(agent_id, session_id)
    _has_jsonl = chatlog.get_last_entry() is not None

    if _has_jsonl:
        return _do_summarize_jsonl(agent, session_id, llm_lock, chatlog,
                                   threshold, tail_size, _SUMMARY_COUNT_TYPES)
    else:
        return _do_summarize_sqlite(agent, session_id, llm_lock, threshold, tail_size)


def _do_summarize_jsonl(agent: dict, session_id: str, llm_lock: threading.Lock,
                        chatlog, threshold: int, tail_size: int,
                        summary_count_types) -> bool:
    """Summarize using JSONL as the message source.

    Returns True if a summary was generated and persisted, False otherwise.
    """
    agent_id = agent['id']

    total = chatlog.count_entries(types=summary_count_types)
    if total < threshold:
        print(f"[AgentRuntime] Summarize skipped: only {total} message(s), threshold is {threshold}")
        return False

    summary_record = db.get_summary(session_id, agent_id=agent_id)

    # Get all conversation entries (user, final, intermediate) for cut-point calculation
    all_entries = chatlog.get_all_for_session(types=summary_count_types)
    if len(all_entries) <= tail_size:
        print(f"[AgentRuntime] Summarize skipped: only {len(all_entries)} entry(s), tail_size is {tail_size}")
        return False

    # Cut: summarize everything except the last tail_size entries
    cut_index = len(all_entries) - tail_size
    # Don't split a user-assistant pair at the cut
    if cut_index > 0 and all_entries[cut_index - 1].get('type') == 'user':
        cut_index -= 1
    if cut_index <= 0:
        print(f"[AgentRuntime] Summarize skipped: cut_index={cut_index}, nothing to summarize")
        return False

    last_summarized_entry = all_entries[cut_index - 1]
    new_last_ts = last_summarized_entry['ts']

    entries_from_tail = False

    # When an existing summary covers up to last_message_ts and the cut-point
    # falls on or before that boundary (because cut_index counted already-summarized
    # entries), walk forward past the already-summarized region to find new entries
    # that lie before the tail.  Without this the summarizer deadlocks: it keeps
    # hitting new_last_ts <= last_message_ts and never advances.
    if summary_record:
        last_ts = summary_record.get('last_message_ts') or 0
        if new_last_ts <= last_ts:
            # Find entries with ts > last_message_ts that are still before the tail
            fresh_entries = [
                e for e in all_entries[:cut_index]
                if e['ts'] > last_ts
            ]
            if fresh_entries:
                new_last_ts = fresh_entries[-1]['ts']
                entries_from_tail = False
            else:
                # Pre-tail had no fresh entries, but the tail may have entries
                # beyond the summary boundary.  Scan the tail region for entries
                # with ts > last_ts so we can advance past the deadlock.
                tail_fresh = [
                    e for e in all_entries[cut_index:]
                    if e['ts'] > last_ts
                ]
                if tail_fresh:
                    new_last_ts = tail_fresh[-1]['ts']
                    # Build entries_to_summarize from tail_fresh now so the
                    # summarizer has something to fold in; flag to skip the
                    # normal entries-building block below.
                    entries_to_summarize = [
                        e for e in tail_fresh
                        if e.get('type') in summary_count_types
                    ]
                    existing_summary = summary_record['summary']
                    entries_from_tail = True
                else:
                    print(f"[AgentRuntime] Summarize skipped: no new entries past last_message_ts={last_ts} (new_last_ts={new_last_ts})")
                    return False

    # Get entries to fold into summary (skip if already built from tail)
    if not entries_from_tail:
        if summary_record and summary_record.get('last_message_ts'):
            entries_to_summarize = chatlog.get_entries_between_ts(
                summary_record['last_message_ts'],
                new_last_ts,
            )
            entries_to_summarize = [e for e in entries_to_summarize
                                     if e.get('type') in summary_count_types]
            existing_summary = summary_record['summary']
        else:
            entries_to_summarize = all_entries[:cut_index]
            existing_summary = None

    if not entries_to_summarize:
        # get_entries_between_ts may return empty when both bounds are equal
        # (e.g. after advancing new_last_ts past last_message_ts, the window
        #  collapses).  Fall back to collecting entries explicitly from the
        #  region between last_message_ts and the tail start.
        if summary_record:
            last_ts = summary_record.get('last_message_ts') or 0
            tail_start_entry = all_entries[cut_index] if cut_index < len(all_entries) else None
            entries_to_summarize = [
                e for e in all_entries[:cut_index]
                if e['ts'] > last_ts
                and (tail_start_entry is None or e['ts'] <= tail_start_entry['ts'])
                and e.get('type') in summary_count_types
            ]
        if not entries_to_summarize:
            print(f"[AgentRuntime] Summarize skipped: no new entries to summarize between existing summary and new cut point")
            return False

    prompt_template = (agent.get('summarize_prompt') or DEFAULT_SUMMARIZE_PROMPT) + _ENTITY_PRESERVATION_CLAUSE + _LENGTH_CAP_CLAUSE

    chunks = [entries_to_summarize[i:i + MAX_SUMMARIZE_BATCH]
              for i in range(0, len(entries_to_summarize), MAX_SUMMARIZE_BATCH)]

    current_summary = existing_summary
    summarized_up_to_ts = (summary_record.get('last_message_ts') or 0) if summary_record else 0
    summarized_count = summary_record['message_count'] if summary_record else 0

    for chunk in chunks:
        messages_text = _format_entries_for_summary(chunk)
        existing_section = f"Existing summary to update:\n{current_summary}\n" if current_summary else ""

        prompt = _render_prompt(prompt_template, {
            'existing_summary_section': existing_section,
            'messages_text': messages_text,
            'current_datetime': _current_datetime_str(),
        })

        # LOCK ORDERING: llm_lock is acquired inside the summarization path,
        # always AFTER _summarize_guard (held by caller maybe_summarize).
        with llm_lock:
            result = llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                temperature=0.0,
                enable_thinking=False,
                max_tokens=None
            )

        if not result.get('success'):
            print(f"[AgentRuntime] Summarize LLM failed: {result.get('error_type', 'unknown')}")
            break

        choice = result['response'].get('choices', [{}])[0]
        if choice.get('finish_reason') == 'length':
            print(f"[AgentRuntime] Summary truncated (finish_reason=length), skipping chunk")
            break

        summary_text = choice.get('message', {}).get('content', '')
        if not summary_text:
            break

        summary_text, _ = strip_thinking_tags(summary_text)
        current_summary = summary_text
        summarized_up_to_ts = chunk[-1]['ts']
        summarized_count += len(chunk)

    if current_summary and summarized_up_to_ts > ((summary_record.get('last_message_ts') or 0) if summary_record else 0):
        print(f"[AgentRuntime] Summarize OK: {summarized_count} message(s) covered, last_message_ts={summarized_up_to_ts}")
        # Hard-cap the persisted summary via semantic compaction (never naive
        # mid-text truncation). Recent turns seed the relevance focus so stale
        # details are dropped first. No-op when already within budget.
        current_summary = compact_summary(
            current_summary,
            max_chars=MAX_SUMMARY_CHARS,
            llm_lock=llm_lock,
            focus_text=_format_entries_for_summary(all_entries[cut_index:])[:1500] or None,
        )
        db.upsert_summary(session_id, current_summary, 0,
                          summarized_count, agent_id=agent_id,
                          last_message_ts=summarized_up_to_ts)

        try:
            recap_path = _recap_log_path(agent_id)
            os.makedirs(os.path.dirname(recap_path), exist_ok=True)
            ts_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            recap_entry = (
                f"## {ts_str} — session={session_id} msgs_covered={summarized_count} "
                f"last_msg_ts={summarized_up_to_ts}\n"
                f"{current_summary}\n\n---\n\n"
            )
            with open(recap_path, 'a', encoding='utf-8') as f:
                f.write(recap_entry)
        except Exception as e:
            print(f"[AgentRuntime] sessrecap log write failed (non-fatal): {e}")

        from backend.event_stream import event_stream
        # REAL message turns only (user + assistant), so the organizer's recent
        # conversation isn't crowded out by tool/thinking entries.
        tail_messages = _tail_messages_from_entries(all_entries[cut_index:])
        event_stream.emit('summary_updated', {
            'agent_id': agent_id,
            'agent_name': agent.get('name', ''),
            'session_id': session_id,
            'summary': current_summary,
            'last_message_id': 0,
            'message_count': summarized_count,
            'tail_messages': tail_messages,
        })
        return True

    return False


def _do_summarize_sqlite(agent: dict, session_id: str, llm_lock: threading.Lock,
                         threshold: int, tail_size: int) -> bool:
    """Summarize using SQLite as the message source (pre-JSONL fallback).

    Returns True if a summary was generated and persisted, False otherwise."""
    agent_id = agent['id']

    total = db.get_message_count(session_id, agent_id=agent_id)
    if total < threshold:
        print(f"[AgentRuntime] Summarize skipped (sqlite): only {total} message(s), threshold is {threshold}")
        return False

    summary_record = db.get_summary(session_id, agent_id=agent_id)

    all_messages = db.get_session_messages(session_id, limit=9999, agent_id=agent_id)
    if len(all_messages) <= tail_size:
        print(f"[AgentRuntime] Summarize skipped (sqlite): only {len(all_messages)} message(s), tail_size is {tail_size}")
        return False

    cut_index = len(all_messages) - tail_size
    cut_index = _adjust_cut_for_tool_chain(all_messages, cut_index)
    if cut_index > 0 and all_messages[cut_index - 1].get('role') == 'user':
        cut_index -= 1
    if cut_index <= 0:
        print(f"[AgentRuntime] Summarize skipped (sqlite): cut_index={cut_index}, nothing to summarize")
        return False

    last_summarized_msg = all_messages[cut_index - 1]
    new_last_message_id = last_summarized_msg['id']

    if summary_record and summary_record['last_message_id'] >= new_last_message_id:
        print(f"[AgentRuntime] Summarize skipped (sqlite): summary already up to date (last_message_id={summary_record['last_message_id']} >= {new_last_message_id})")
        return False

    if summary_record:
        msgs_to_summarize = db.get_messages_between(
            session_id, summary_record['last_message_id'],
            new_last_message_id, agent_id=agent_id)
        existing_summary = summary_record['summary']
    else:
        msgs_to_summarize = all_messages[:cut_index]
        existing_summary = None

    if not msgs_to_summarize:
        print(f"[AgentRuntime] Summarize skipped (sqlite): no new messages to summarize")
        return False

    prompt_template = (agent.get('summarize_prompt') or DEFAULT_SUMMARIZE_PROMPT) + _ENTITY_PRESERVATION_CLAUSE + _LENGTH_CAP_CLAUSE

    chunks = [msgs_to_summarize[i:i + MAX_SUMMARIZE_BATCH]
              for i in range(0, len(msgs_to_summarize), MAX_SUMMARIZE_BATCH)]

    current_summary = existing_summary
    summarized_up_to = summary_record['last_message_id'] if summary_record else 0
    summarized_count = summary_record['message_count'] if summary_record else 0

    for chunk in chunks:
        messages_text = _format_messages_for_summary(chunk)
        existing_section = f"Existing summary to update:\n{current_summary}\n" if current_summary else ""

        prompt = _render_prompt(prompt_template, {
            'existing_summary_section': existing_section,
            'messages_text': messages_text,
            'current_datetime': _current_datetime_str(),
        })

        # LOCK ORDERING: llm_lock is acquired inside the summarization path,
        # always AFTER _summarize_guard (held by caller maybe_summarize).
        with llm_lock:
            result = llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                temperature=0.0,
                enable_thinking=False,
                max_tokens=None
            )

        if not result.get('success'):
            print(f"[AgentRuntime] Summarize LLM failed: {result.get('error_type', 'unknown')}")
            break

        choice = result['response'].get('choices', [{}])[0]
        if choice.get('finish_reason') == 'length':
            print(f"[AgentRuntime] Summary truncated (finish_reason=length), skipping chunk")
            break

        summary_text = choice.get('message', {}).get('content', '')
        if not summary_text:
            break

        summary_text, _ = strip_thinking_tags(summary_text)
        current_summary = summary_text
        summarized_up_to = chunk[-1]['id']
        summarized_count += len(chunk)

    if current_summary and summarized_up_to > (summary_record['last_message_id'] if summary_record else 0):
        print(f"[AgentRuntime] Summarize OK (sqlite): {summarized_count} message(s) covered, last_message_id={summarized_up_to}")
        # Hard-cap the persisted summary (semantic compaction, no mid-text
        # truncation). No-op when already within budget.
        current_summary = compact_summary(
            current_summary,
            max_chars=MAX_SUMMARY_CHARS,
            llm_lock=llm_lock,
            focus_text=_format_messages_for_summary(all_messages[cut_index:])[:1500] or None,
        )
        db.upsert_summary(session_id, current_summary, summarized_up_to,
                          summarized_count, agent_id=agent_id)

        try:
            recap_path = _recap_log_path(agent_id)
            os.makedirs(os.path.dirname(recap_path), exist_ok=True)
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            recap_entry = (
                f"## {ts} — session={session_id} msgs_covered={summarized_count} "
                f"last_msg_id={summarized_up_to}\n"
                f"{current_summary}\n\n---\n\n"
            )
            with open(recap_path, 'a', encoding='utf-8') as f:
                f.write(recap_entry)
        except Exception as e:
            print(f"[AgentRuntime] sessrecap log write failed (non-fatal): {e}")

        from backend.event_stream import event_stream
        tail_messages = [{'role': m['role'], 'content': (m.get('content') or '').strip()}
                         for m in all_messages[cut_index:]
                         if m.get('role') in ('user', 'assistant')
                         and (m.get('content') or '').strip()]
        event_stream.emit('summary_updated', {
            'agent_id': agent_id,
            'agent_name': agent.get('name', ''),
            'session_id': session_id,
            'summary': current_summary,
            'last_message_id': summarized_up_to,
            'message_count': summarized_count,
            'tail_messages': tail_messages,
        })

        return True

    return False


def _adjust_cut_for_tool_chain(messages: list, cut_index: int) -> int:
    """Move cut_index backwards to avoid splitting a tool call chain."""
    if cut_index <= 0 or cut_index >= len(messages):
        return cut_index
    while cut_index > 0:
        msg = messages[cut_index]
        if msg['role'] == 'tool':
            cut_index -= 1
            continue
        if msg['role'] == 'assistant' and msg.get('tool_calls'):
            cut_index -= 1
            continue
        break
    return cut_index


def _format_messages_for_summary(messages: list) -> str:
    """Format messages into a readable text block for the summarization LLM."""
    lines = []
    for msg in messages:
        role = msg['role'].upper()
        content = msg.get('content', '') or ''
        # NOTE: Tool calls and tool results are excluded from summary to reduce noise.
        # The summary focuses on user/assistant conversation flow only.
        if msg.get('tool_calls') or msg['role'] == 'tool':
            continue
        # Skip legacy agent-state system messages stored in chat_messages (pre-migration)
        meta = msg.get('metadata')
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = None
        if isinstance(meta, dict) and meta.get('agent_state'):
            continue
        # Skip bash_exec and slash_command — these are direct executions handled
        # outside the LLM and must not leak into the recap/summary context.
        if isinstance(meta, dict) and (meta.get('bash_exec') or meta.get('slash_command') or meta.get('no_memory')):
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _tail_messages_from_entries(entries: list) -> list:
    """Build tail messages (for the KB organizer's recent-conversation context) from
    JSONL entries — REAL message turns ONLY, preserving role:

    - ``type == 'user'`` -> role 'user'
    - ``type in ('final','intermediate')`` -> role 'assistant'
    - everything else (thinking/tool_call/tool_output/system/error) -> skipped

    Skips bash_exec/slash_command entries and empty content. Mirrors
    ``_format_entries_for_summary`` so non-message entries don't masquerade as
    'assistant' and crowd the real user turn out of the last-N window downstream.
    """
    out = []
    for entry in entries:
        etype = entry.get('type', '')
        meta = entry.get('metadata') or {}
        if meta.get('bash_exec') or meta.get('slash_command') or meta.get('no_memory'):     # no_memory: not to be remembered
            continue
        if etype == 'user':
            role = 'user'
        elif etype in ('final', 'intermediate'):
            role = 'assistant'
        else:
            continue
        content = (entry.get('content') or '').strip()
        if content:
            out.append({'role': role, 'content': content, 'ts': entry.get('ts') or 0})
    return out


def _format_entries_for_summary(entries: list) -> str:
    """Format JSONL entries into a readable text block for the summarization LLM."""
    lines = []
    for entry in entries:
        etype = entry.get('type', '')
        content = entry.get('content', '') or ''
        # Skip bash_exec and slash_command — these are direct executions handled
        # outside the LLM and must not leak into the recap/summary context.
        meta = entry.get('metadata') or {}
        if meta.get('bash_exec') or meta.get('slash_command') or meta.get('no_memory'):     # no_memory: not to be remembered
            continue
        if etype == 'user':
            lines.append(f"USER: {content}")
        elif etype in ('final', 'intermediate'):
            lines.append(f"ASSISTANT: {content}")
        # Other types (thinking, tool_call, tool_output, system, error, etc.) are skipped
    return "\n".join(lines)
