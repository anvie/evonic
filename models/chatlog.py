"""
chatlog.py — append-only JSONL chat event log per session.

Each session gets its own file at agents/<agent_id>/sessions/<session_id>.jsonl.
The session_id is {agent_id}-{hash} where hash is an 8-hex-char ID derived
from external_user_id and agent_id (see session_slug()).

Entry types:
  user        — incoming user message
  turn_begin  — marks start of an agent processing turn
  thinking    — LLM reasoning/thinking block
  tool_call   — outgoing tool invocation by LLM
  tool_output — result returned from a tool call
  intermediate — non-final assistant message (before more tool calls)
  final       — assistant's final message for the turn
  turn_end    — marks end of an agent processing turn
  pending     — user message queued while agent is busy
  system      — system-level message (slash command response, stop injection, etc.)
  error       — terminal error message from the agent

Every entry has at least: ts (epoch ms), type, session_id.
"""

import hashlib
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_AGENTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'agents')

# Entry types included in LLM context reconstruction
# 'thinking' is included so reasoning_content can be attached to the next assistant message
_LLM_CONTEXT_TYPES = frozenset({'user', 'tool_call', 'tool_output', 'intermediate', 'final', 'system', 'error', 'thinking'})

# Entry types shown in the chat UI (excludes bookkeeping entries)
_DISPLAY_TYPES = frozenset({'user', 'thinking', 'tool_call', 'tool_output', 'intermediate', 'final', 'pending', 'system', 'error'})

# Entry types counted for summarization threshold
_SUMMARY_COUNT_TYPES = frozenset({'user', 'final', 'intermediate'})

_CHUNK = 8192  # bytes to read per reverse-scan chunk


def _now_ms() -> int:
    return int(time.time() * 1000)


def session_slug(external_user_id: str, agent_id: str) -> str:
    """Derive a deterministic session ID from external_user_id and agent_id.

    Sorts the two values lexicographically, SHA1-hashes them, and returns
    the first 8 hex characters.

    Example: session_slug('alice', 'siwa') → 'a1b2c3d4' (8 hex chars)
    """
    # Guard against None — convert to empty string so sorting doesn't crash
    external_user_id = external_user_id or ''
    agent_id = agent_id or ''
    items = sorted([external_user_id, agent_id])
    h = hashlib.sha1(json.dumps(items).encode()).hexdigest()
    return h[:8]



class ChatLog:
    """Append-only JSONL event log for one session's chat history.

    Writes to agents/<agent_id>/sessions/<session_id>.jsonl.
    Each instance represents exactly one session — no session_id filtering needed.
    """

    def __init__(self, agent_id: str, session_id: str):
        self.agent_id = agent_id
        self.session_id = session_id
        from backend.subagent_manager import subagent_manager
        if subagent_manager.is_subagent(agent_id):
            from models.chat import SUB_AGENTS_TMP_DIR
            base_dir = os.path.join(SUB_AGENTS_TMP_DIR, agent_id)
        else:
            base_dir = os.path.join(_AGENTS_DIR, agent_id)
        sessions_dir = os.path.join(base_dir, 'sessions')
        os.makedirs(sessions_dir, exist_ok=True)
        # session_id is "{agent_id}-{hash}" — strip the prefix for the filename
        filename = session_id
        if filename.startswith(f'{agent_id}-'):
            filename = filename[len(agent_id) + 1:]
        self._path = os.path.join(sessions_dir, f'{filename}.jsonl')
        self._lock = threading.Lock()
        self._fh = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def open(self):
        with self._lock:
            if self._fh is None or self._fh.closed:
                self._fh = open(self._path, 'a', encoding='utf-8')

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append(self, entry: dict) -> None:
        """Serialize entry and append as a single line to the session log."""
        if 'ts' not in entry:
            entry = dict(entry)
            entry['ts'] = _now_ms()
        line = json.dumps(entry, ensure_ascii=False) + '\n'
        with self._lock:
            if self._fh is None or self._fh.closed:
                # If not using context manager, open on-demand
                with open(self._path, 'a', encoding='utf-8') as f:
                    f.write(line)
            else:
                self._fh.write(line)
                self._fh.flush()  # make visible to concurrent readers immediately

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def _iter_lines_reverse(self):
        """Yield raw lines from the end of the file, newest first."""
        try:
            size = os.path.getsize(self._path)
        except FileNotFoundError:
            return
        if size == 0:
            return

        buf = b''
        pos = size
        with open(self._path, 'rb') as f:
            while pos > 0:
                read_size = min(_CHUNK, pos)
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size)
                buf = chunk + buf
                lines = buf.split(b'\n')
                # The first element may be a partial line — keep it for the next iteration
                buf = lines[0]
                # Yield complete lines from right to left (newest first in this chunk)
                for line in reversed(lines[1:]):
                    stripped = line.strip()
                    if stripped:
                        yield stripped.decode('utf-8', errors='replace')
        # Yield whatever remains in buf (the very first line of the file)
        stripped = buf.strip()
        if stripped:
            yield stripped.decode('utf-8', errors='replace')

    def _iter_lines_forward(self):
        """Yield raw lines from the start of the file, oldest first."""
        try:
            with open(self._path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        yield stripped
        except FileNotFoundError:
            return

    def _parse(self, raw: str) -> Optional[dict]:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def tail(self, limit: int = 15, to_ts: Optional[int] = None) -> List[dict]:
        """Return up to `limit` entries, ordered by ts ascending.

        to_ts: if given, return only entries with ts < to_ts (strict less-than).
        """
        limit = max(1, min(limit, 500))
        collected = []
        for raw in self._iter_lines_reverse():
            entry = self._parse(raw)
            if entry is None:
                continue
            ts = entry.get('ts', 0)
            if to_ts is not None and ts >= to_ts:
                continue
            collected.append(entry)
            if len(collected) >= limit:
                break
        collected.reverse()
        return collected

    def tail_by_messages(self, limit: int = 30,
                          to_ts: Optional[int] = None) -> Tuple[List[dict], bool]:
        """Return up to `limit` logical messages worth of display entries, ascending.

        A "logical message" is:
          - A user entry (1 message)
          - A full agent turn (turn_begin → turn_end block = 1 message)
          - A standalone final/error/system entry not inside a turn (1 message)

        Scans backward so only _DISPLAY_TYPES entries within the limit are collected.
        Non-display types (turn_begin, turn_end) are used only for boundary counting.

        Returns: (entries: List[dict], has_more: bool)
        """
        limit = max(1, min(limit, 200))
        collected: List[dict] = []
        message_count = 0
        in_turn = False  # True while scanning backward through a turn_end…turn_begin block
        has_more = False

        for raw in self._iter_lines_reverse():
            entry = self._parse(raw)
            if entry is None:
                continue
            ts = entry.get('ts', 0)
            if to_ts is not None and ts >= to_ts:
                continue

            etype = entry.get('type', '')

            # Count message boundaries (backward scan: turn_end comes before turn_begin)
            if etype == 'turn_end':
                if not in_turn:
                    message_count += 1
                    in_turn = True
                    if message_count > limit:
                        has_more = True
                        break
            elif etype == 'turn_begin':
                in_turn = False
            elif etype == 'user':
                message_count += 1
                if message_count > limit:
                    has_more = True
                    break
            elif etype in ('final', 'error', 'system') and not in_turn:
                # Standalone message not wrapped in a turn block
                message_count += 1
                if message_count > limit:
                    has_more = True
                    break

            # Collect only displayable entries
            if etype in _DISPLAY_TYPES:
                collected.append(entry)

        collected.reverse()
        return collected, has_more

    def get_entries_after_ts(self, after_ts: int,
                              types: Optional[frozenset] = None) -> List[dict]:
        """Return all entries with ts > after_ts, ascending.

        Scans backward from the end of the file so only the new tail is read,
        not the entire history. This is O(new_entries) instead of O(all_entries).
        """
        results = []
        for raw in self._iter_lines_reverse():
            entry = self._parse(raw)
            if entry is None:
                continue
            ts = entry.get('ts', 0)
            if ts <= after_ts:
                break  # entries are chronological — everything before this is older
            if types is not None and entry.get('type') not in types:
                continue
            results.append(entry)
        results.reverse()
        return results

    def get_entries_between_ts(self, after_ts: int, up_to_ts: int) -> List[dict]:
        """Return entries with after_ts < ts <= up_to_ts, ascending."""
        results = []
        for raw in self._iter_lines_forward():
            entry = self._parse(raw)
            if entry is None:
                continue
            ts = entry.get('ts', 0)
            if ts <= after_ts:
                continue
            if ts > up_to_ts:
                # File is chronological — stop early once we're past the window
                break
            results.append(entry)
        return results

    def get_all_for_session(self, types: Optional[frozenset] = None) -> List[dict]:
        """Return all entries (oldest first), optionally filtered by type."""
        results = []
        for raw in self._iter_lines_forward():
            entry = self._parse(raw)
            if entry is None:
                continue
            if types is not None and entry.get('type') not in types:
                continue
            results.append(entry)
        return results

    def count_entries(self, types: Optional[frozenset] = None) -> int:
        """Count entries, optionally filtered by type."""
        n = 0
        for raw in self._iter_lines_forward():
            entry = self._parse(raw)
            if entry is None:
                continue
            if types is not None and entry.get('type') not in types:
                continue
            n += 1
        return n

    def get_last_entry(self, types: Optional[frozenset] = None) -> Optional[dict]:
        """Return the most recent entry, optionally filtered by type."""
        for raw in self._iter_lines_reverse():
            entry = self._parse(raw)
            if entry is None:
                continue
            if types is not None and entry.get('type') not in types:
                continue
            return entry
        return None

    def get_entries_for_llm(self, after_ts: Optional[int] = None,
                             limit: int = 50) -> List[Dict[str, Any]]:
        """Return entries needed for LLM context building, as LLM message dicts.

        Reconstructs the tool_calls structure expected by the OpenAI API:
        - Consecutive tool_call entries that share a preceding intermediate entry
          (or an implicit empty assistant turn) are grouped into a single assistant
          message with a tool_calls list.
        - Each subsequent tool_output becomes a {role: "tool"} message.

        after_ts: if set, only include entries with ts > after_ts (for summary tail).
        limit: max number of semantic messages (user/final/intermediate) to consider.
              Mechanical entries (thinking/tool_call/tool_output) between them are
              always included so tool-heavy turns don't inflate the count.
        """
        raw_entries: List[dict] = []

        if after_ts is not None:
            # Forward scan from after_ts
            for raw in self._iter_lines_forward():
                entry = self._parse(raw)
                if entry is None:
                    continue
                if entry.get('ts', 0) <= after_ts:
                    continue
                if entry.get('type') in _LLM_CONTEXT_TYPES:
                    raw_entries.append(entry)
        else:
            # Read last `limit` semantic messages (tail scan).
            # Count only semantic entries (user, final, intermediate) toward
            # the limit — these represent actual conversation turns.  Mechanical
            # entries (thinking, tool_call, tool_output) between them are always
            # collected so tool-heavy turns don't inflate the count.
            collected = []
            semantic_count = 0
            for raw in self._iter_lines_reverse():
                entry = self._parse(raw)
                if entry is None:
                    continue
                if entry.get('type') in _LLM_CONTEXT_TYPES:
                    collected.append(entry)
                    if entry.get('type') in _SUMMARY_COUNT_TYPES:
                        semantic_count += 1
                        if semantic_count >= limit:
                            break
            collected.reverse()
            raw_entries = collected

        return _reconstruct_llm_messages(raw_entries)

    def clear(self) -> None:
        """Truncate the session log file, removing all entries."""
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
            try:
                open(self._path, 'w', encoding='utf-8').close()
            except FileNotFoundError:
                pass  # No file to clear — nothing to do


# ------------------------------------------------------------------
# LLM message reconstruction
# ------------------------------------------------------------------

def _fix_interleaved_user_messages(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fix user/system messages recorded between a tool_call and its tool_output.

    When a user sends a message while a tool is still executing, the chatlog records
    the user message before the tool_output (by wall-clock order). But the LLM
    actually received them in the opposite order: tool result first, then user message
    on the next iteration. Reorder so tool responses immediately follow their
    assistant/tool_calls message, with deferred user/system messages placed after.
    """
    result: List[Dict[str, Any]] = []
    i = 0
    while i < len(msgs):
        msg = msgs[i]
        if msg.get('role') == 'assistant' and msg.get('tool_calls'):
            tc_ids = {tc['id'] for tc in msg.get('tool_calls', [])}
            j = i + 1
            deferred: List[Dict[str, Any]] = []
            tool_responses: List[Dict[str, Any]] = []
            found_ids: set = set()
            while j < len(msgs):
                next_msg = msgs[j]
                next_role = next_msg.get('role', '')
                next_tc_id = next_msg.get('tool_call_id', '')
                if next_role == 'tool' and next_tc_id in tc_ids:
                    tool_responses.append(next_msg)
                    found_ids.add(next_tc_id)
                    j += 1
                elif next_role in ('user', 'system'):
                    # Defer: user/system message was recorded out-of-order due to
                    # mid-execution injection. This can happen before ANY tool response
                    # OR between tool responses (e.g., tc1 done, user sends message,
                    # tc2 still running). Always defer to after all tool responses so
                    # the API sees tool messages immediately following the tool_calls
                    # assistant message.
                    deferred.append(next_msg)
                    j += 1
                else:
                    break
                if found_ids == tc_ids:
                    break  # All expected tool responses collected
            # If some tool responses are missing (agent interrupted before recording
            # outputs, or history limit cut them off), inject synthetic error
            # responses so the API contract is satisfied: every tool_call_id in the
            # assistant message must have a corresponding tool response.
            missing_ids = tc_ids - found_ids
            for mid in missing_ids:
                tool_responses.append({
                    'role': 'tool',
                    'tool_call_id': mid,
                    'content': '{"error": "Tool execution was interrupted before completion."}',
                })
            result.append(msg)
            result.extend(tool_responses)
            result.extend(deferred)
            i = j
        else:
            result.append(msg)
            i += 1
    return result


def _reconstruct_llm_messages(entries: List[dict]) -> List[Dict[str, Any]]:
    """Convert a list of JSONL entries to the OpenAI messages array format.

    Groups consecutive tool_call entries into a single assistant message with
    tool_calls array, matching what the LLM originally produced.
    """
    messages: List[Dict[str, Any]] = []
    i = 0
    _pending_reasoning: str = ''  # buffered reasoning_content for the next assistant message
    _call_counter = 0  # fallback ID generator for tool_call/tool_output
    _pending_tool_ids: List[str] = []  # IDs from last assistant tool_calls for matching tool_outputs
    while i < len(entries):
        entry = entries[i]
        etype = entry.get('type', '')
        content = entry.get('content') or ''

        if etype == 'thinking':
            # Concatenate consecutive reasoning chunks (streaming)
            _pending_reasoning += content
            i += 1

        elif etype == 'user':
            # Skip slash command and web "!" bash-exec user messages — they are
            # handled directly and must never enter LLM context.
            _umeta = entry.get('metadata') or {}
            if _umeta.get('slash_command') or _umeta.get('bash_exec'):
                i += 1
                continue
            _pending_reasoning = ''  # reasoning before a user message is irrelevant
            _pending_tool_ids = []
            msg: Dict[str, Any] = {'role': 'user', 'content': content}
            _meta = entry.get('metadata') or {}
            img = _meta.get('image_url')
            if img:
                msg['_image_url'] = img  # caller handles multimodal formatting
            audio = _meta.get('audio_url')
            if audio:
                msg['_audio_url'] = audio
            video = _meta.get('video_url')
            if video:
                msg['_video_url'] = video
            wrapped = _meta.get('wrapped')
            if wrapped:
                msg['_wrapped'] = True
            atts = _meta.get('attachment_infos')
            if isinstance(atts, list):
                msg['attachment_infos'] = atts
            att = _meta.get('attachment_info')
            if att:
                msg['attachment_info'] = att
            messages.append(msg)
            i += 1

        elif etype in ('intermediate', 'final', 'error'):
            _meta = entry.get('metadata') or {}
            if _meta.get('busy_ack') or _meta.get('busy_rejection') or _meta.get('evonet_offline'):
                i += 1
                continue
            _pending_tool_ids = []
            asst: Dict[str, Any] = {'role': 'assistant', 'content': content}
            if _pending_reasoning:
                asst['reasoning_content'] = _pending_reasoning
                _pending_reasoning = ''
            messages.append(asst)
            i += 1

        elif etype == 'system':
            # Skip slash command and web "!" bash-exec responses — they were saved
            # with metadata.slash_command / metadata.bash_exec and must never enter
            # LLM context.
            _smeta = entry.get('metadata') or {}
            if _smeta.get('slash_command') or _smeta.get('bash_exec'):
                i += 1
                continue
            # System injections were sent as user messages to the LLM
            _pending_tool_ids = []
            messages.append({'role': 'user', 'content': content})
            i += 1

        elif etype == 'tool_call':
            # Collect the immediately preceding intermediate content (if any) and
            # all consecutive tool_call entries into one assistant message.
            preceding_content = ''
            preceding_reasoning = _pending_reasoning
            _pending_reasoning = ''
            # If the previous message was an intermediate, merge it
            if messages and messages[-1].get('role') == 'assistant' and 'tool_calls' not in messages[-1]:
                prev = messages.pop()
                preceding_content = prev.get('content', '')
                # Preserve reasoning_content from the popped intermediate if not already set
                if not preceding_reasoning:
                    preceding_reasoning = prev.get('reasoning_content', '')

            tool_calls_list = []
            _pending_tool_ids = []
            while i < len(entries) and entries[i].get('type') == 'tool_call':
                tc = entries[i]
                try:
                    args_str = json.dumps(tc.get('params', {}), ensure_ascii=False)
                except (TypeError, ValueError):
                    args_str = '{}'
                tc_id = tc.get('id', '') or ''
                if not tc_id.strip():
                    _call_counter += 1
                    tc_id = f'call_{_call_counter}'
                tool_calls_list.append({
                    'id': tc_id,
                    'type': 'function',
                    'function': {
                        'name': tc.get('function', ''),
                        'arguments': args_str,
                    },
                })
                _pending_tool_ids.append(tc_id)
                i += 1

            asst_msg: Dict[str, Any] = {
                'role': 'assistant',
                'content': preceding_content,
                'tool_calls': tool_calls_list,
            }
            if preceding_reasoning:
                asst_msg['reasoning_content'] = preceding_reasoning
            messages.append(asst_msg)

        elif etype == 'tool_output':
            tc_id = entry.get('tool_call_id', '') or ''
            if not tc_id.strip():
                if _pending_tool_ids:
                    tc_id = _pending_tool_ids.pop(0)
                else:
                    _call_counter += 1
                    tc_id = f'call_{_call_counter}'
            messages.append({
                'role': 'tool',
                'tool_call_id': tc_id,
                'content': content,
            })
            i += 1

        else:
            # Skip turn_begin, turn_end, pending
            i += 1

    return _drop_orphaned_tool_messages(
        _fix_interleaved_user_messages(messages))


def _drop_orphaned_tool_messages(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove tool messages whose tool_call_id has no preceding assistant(tool_calls).

    This can happen when the summary watermark timestamp ties with a tool_call
    entry — the forward scan excludes the tool_call (ts <= watermark) but
    includes its tool_output (later ts), producing an orphaned tool message
    that the LLM API rejects.  Also drops duplicate tool responses for the
    same tool_call_id (can occur when synthetic placeholders were injected
    and the real response appears later).
    """
    declared_ids: set = set()
    responded_ids: set = set()
    result: List[Dict[str, Any]] = []
    for msg in msgs:
        if msg.get('tool_calls'):
            for tc in msg['tool_calls']:
                declared_ids.add(tc['id'])
        if msg.get('role') == 'tool':
            tcid = msg.get('tool_call_id', '')
            if tcid not in declared_ids or tcid in responded_ids:
                continue  # orphaned or duplicate — skip
            responded_ids.add(tcid)
        result.append(msg)
    return result


# ------------------------------------------------------------------
# Manager singleton
# ------------------------------------------------------------------

class ChatLogManager:
    """Caches ChatLog instances per (agent_id, slug) — one file handle per session."""

    def __init__(self):
        self._logs: Dict[Tuple[str, str], ChatLog] = {}
        self._lock = threading.Lock()

    def get(self, agent_id: str, slug: str) -> ChatLog:
        key = (agent_id, slug)
        with self._lock:
            if key not in self._logs:
                self._logs[key] = ChatLog(agent_id, slug)
            return self._logs[key]

    def evict(self, agent_id: str, slug: str) -> None:
        """Remove a ChatLog from the cache (e.g., after session deletion)."""
        key = (agent_id, slug)
        with self._lock:
            self._logs.pop(key, None)

    def list_sessions(self, agent_id: str) -> List[str]:
        """Return session IDs (hash-only, without agent_id prefix) of all session log files for an agent."""
        from backend.subagent_manager import subagent_manager
        if subagent_manager.is_subagent(agent_id):
            from models.chat import SUB_AGENTS_TMP_DIR
            base_dir = os.path.join(SUB_AGENTS_TMP_DIR, agent_id)
        else:
            base_dir = os.path.join(_AGENTS_DIR, agent_id)
        sessions_dir = os.path.join(base_dir, 'sessions')
        try:
            return [f[:-6] for f in os.listdir(sessions_dir) if f.endswith('.jsonl')]
        except FileNotFoundError:
            return []


chatlog_manager = ChatLogManager()
