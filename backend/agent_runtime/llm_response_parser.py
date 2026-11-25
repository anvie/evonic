"""
llm_response_parser.py — LLM response parsing: error humanization, continuation
nudges, emergency compaction.

Part of the diet llm_loop.py refactor (Layout C / Pipeline).
"""

import json as _json
import logging
import re
import threading

_logger = logging.getLogger(__name__)

# ── Error humanization ──────────────────────────────────────────────────────

# A reasoning model that burns its whole output budget on chain-of-thought is
# reported by the client as ``error_type="generation_timeout"`` with a detail like
# "Generation hit max_tokens limit (4096) without producing final answer.
# Reasoning length: 14460 chars."  That detail contains the literal substring
# ``max_tokens``, so the generic context-length heuristic used to swallow it and
# answer "conversation too long" -- telling the caller (and the parent agent) the
# wrong problem.  Keep the two causes separate.
_CONTEXT_LENGTH_MESSAGE = (
    "Conversation is too long for this model. Try starting a new session (/new)."
)
_REASONING_EXHAUSTED_MESSAGE = (
    "The model ran out of output tokens while still reasoning and never produced "
    "a final answer (it hit its max_tokens limit). This is an output-budget "
    "problem, not a context-length problem. Retry the request; if it keeps "
    "happening, raise max_tokens or lower the thinking budget for this model."
)

# Structural mapping first: a structured ``error_type`` from the client wins over
# any substring guess.  Unknown/other types fall through to the heuristics below.
_STRUCTURAL_ERROR_MESSAGES = {
    "generation_timeout": _REASONING_EXHAUSTED_MESSAGE,
    "context_length_error": _CONTEXT_LENGTH_MESSAGE,
}

# Unambiguous markers of the client's reasoning-exhaustion detail, used only when
# no structured ``error_type`` is supplied (older callers, replayed logs, tests).
_REASONING_EXHAUSTION_MARKERS = (
    "without producing final answer",
    "hit max_tokens limit",
    "max_tokens limit",
)


def _is_reasoning_exhaustion(text: str) -> bool:
    """True if ``text`` describes output-budget exhaustion, not a long prompt."""
    return any(marker in text for marker in _REASONING_EXHAUSTION_MARKERS)


def _humanize_llm_error(raw: str, error_type: str = None) -> str:
    """Wrap raw LLM API errors into human-friendly messages.

    ``error_type`` is the structured error type reported by the client.  When
    provided it is honoured before any substring matching, so a
    ``generation_timeout`` (reasoning/output-budget exhaustion) can never be
    reported as a context-length problem.  Optional to stay backward compatible
    with existing positional callers.
    """
    # Try to parse nested JSON error payload from the raw string
    parsed = None
    try:
        outer = _json.loads(raw)
        if isinstance(outer, dict):
            msg = outer.get('error', {})
            if isinstance(msg, dict):
                parsed = msg
            elif isinstance(msg, str):
                parsed = {'message': msg}
            elif 'message' in outer:
                parsed = {'message': outer['message']}
    except (_json.JSONDecodeError, ValueError, TypeError):
        pass

    text = raw.lower()

    # Structural causes first: never guess from substrings when the client told
    # us what happened.  This is what keeps reasoning exhaustion (a detail that
    # literally contains "max_tokens") out of the context-length branch.
    structural = _STRUCTURAL_ERROR_MESSAGES.get(str(error_type or "").strip().lower())
    if structural:
        return structural
    if _is_reasoning_exhaustion(text):
        return _REASONING_EXHAUSTED_MESSAGE

    if 'image input is not supported' in text or 'image' in text and 'not supported' in text:
        return "The model doesn't support image input. Try sending a text-only message, or switch to a model that supports vision."
    if 'rate limit' in text or re.search(r'\b429\b', text):
        return "API rate limit reached. Please wait a moment and try again."
    if 'unauthorized' in text or 'invalid api key' in text or re.search(r'\b401\b', text):
        return "API authentication failed. Check your API key configuration."
    if ('insufficient tool messages' in text or 'must be followed by tool' in text
            or ('tool_calls' in text and 'tool_call_id' in text)):
        return (
            "The conversation history was temporarily out of sync (a tool call was "
            "missing its result). It has been repaired automatically — please try again."
        )
    # Genuine context-length failures.  The bare ``max_tokens`` substring that
    # used to live here is what misrouted reasoning-exhaustion details; only
    # unambiguous context wording counts now.
    if ('context length' in text or 'context size' in text
            or 'exceed_context' in text or 'exceeds the available context' in text
            or 'context window' in text or 'maximum context' in text):
        return _CONTEXT_LENGTH_MESSAGE

    _5xx_match = re.search(r'\b(5\d\d)\b', text)
    if _5xx_match or 'server_error' in text or 'temporarily unavailable' in text or 'server error' in text:
        code = _5xx_match.group(1) if _5xx_match else '5xx'
        if parsed and parsed.get('message'):
            return f"LLM server error: {parsed.get('message')}. Try again later."
        return f"LLM server is having issues ({code}). Try again later."

    if 'connection' in text or 'timeout' in text or 'connect' in text:
        return "Cannot connect to the LLM server. Check your internet connection or try again later."

    if parsed and parsed.get('message'):
        return f"Failed to call LLM: {parsed.get('message')}"

    return raw


# ── Continuation nudge patterns ─────────────────────────────────────────────

_CONTINUATION_PATTERNS = [
    r"saya akan melanjutkan",
    r"tunggu sebentar",
    r"sedang diproses",
    r"saya akan lakukan",
    r"mari kita lanjutkan",
    r"selanjutnya saya",
    r"langkah (selanjutnya|berikutnya)",
    r"let me (continue|proceed|do that|work on)",
    r"i('ll| will) (now |)(continue|proceed|start|begin|do|work|implement|create|update|check|run)",
    r"(sekarang |)saya (akan|perlu|coba)",
    r"baik,? (saya|mari|kita)",
    r"(siap|oke|ok),?\s+(saya |)(buat|buatkan|kerjakan|lakukan|jalankan|mulai|coba)\b",
    r"^saya (buat|buatkan|kerjakan|lakukan|jalankan|mulai)\b",
    r"(akan saya|saya akan) (buat|buatkan|kerjakan|lakukan|jalankan)\b",
]
CONTINUATION_RE = re.compile("|".join(_CONTINUATION_PATTERNS), re.IGNORECASE)

_PLANNING_PATTERNS = [
    r"(adalah|berikut) .*?(plan|rencana|draft)",
    r"(apakah .*?setuju|sudah oke)",
    r"sudah (dibuat|selesai|berhasil|dikerjakan|dikirim|dijadwalkan)",
    r"\bringkasan\b",
    r"berikut (hasil|laporan|report|data|daftar|detail|informasi|status)",
    r"\bcatatan\s*:",
    r"\b(mau|perlu|ingin|perlukah|haruskah) saya\b",
    r"\b(shall|should) I\b",
    r"\b(would you like|do you want) me to\b",
]
PLANNING_RE = re.compile("|".join(_PLANNING_PATTERNS), re.IGNORECASE)

CONTINUATION_NUDGE = (
    "[SYSTEM] What's the status update? If there's still something to continue, "
    "please keep going; if nothing else remains, reply only with [DONE]"
)
MAX_CONTINUATION_NUDGES = 3


def should_nudge_continuation(content: str, nudge_count: int) -> str:
    """Decide what the loop should do for a no-tool-call response.

    Returns:
        "nudge"   – inject a continuation nudge and re-enter the loop
        "final"   – treat the response as the final answer (PLANNING_RE negated)
        "none"    – no continuation phrase detected; fall through normally
    """
    if not content or nudge_count >= MAX_CONTINUATION_NUDGES:
        return "none"
    if not CONTINUATION_RE.search(content):
        return "none"
    if PLANNING_RE.search(content):
        return "final"
    return "nudge"


# ── Emergency compaction ────────────────────────────────────────────────────

def _emergency_compact_messages(messages: list, llm, llm_lock: threading.Lock,
                                 session_id: str, agent_id: str) -> 'list | None':
    """Compact the messages list to fit within context on exceed_context_size_error.

    Strategy:
    1. Separate leading system messages from conversation messages.
    2. Take the last 5 non-tool conversation entries as "recent context".
    3. Use LLM to rewrite the existing summary, keeping only points
       relevant to the recent context (drop <30% relevance entries).
    4. Rebuild the messages list with the compacted summary.
    5. Persist the compacted summary to DB.

    Returns the new messages list, or None on failure.
    """
    from models.db import db
    from backend.llm_client import strip_thinking_tags
    from backend.agent_runtime.summary_compactor import (
        compact_summary as _compact_summary,
        MAX_SUMMARY_CHARS as _max_summary_chars,
    )

    # --- 1. Separate system messages from conversation ---
    system_msgs = []
    conv_msgs = []
    in_system_block = True
    for m in messages:
        if in_system_block and m.get('role') == 'system':
            system_msgs.append(m)
        else:
            in_system_block = False
            conv_msgs.append(m)

    # --- 2. Last 5 non-tool conversation entries ---
    text_conv = [m for m in conv_msgs
                 if m.get('role') != 'tool' and not m.get('tool_calls')]
    last_5 = text_conv[-5:]

    # --- 3. Find existing summary text from system messages ---
    existing_summary = ''
    old_summary_idx = None
    for i, m in enumerate(system_msgs):
        if '## Prior conversation summary' in (m.get('content') or ''):
            existing_summary = (m.get('content') or '').replace(
                '## Prior conversation summary\n', '', 1
            ).replace('## Prior conversation summary (compacted)\n', '', 1)
            old_summary_idx = i
            break

    # --- 4. Format last 5 conversations for the compaction prompt ---
    recent_lines = []
    for m in last_5:
        role = m.get('role', '').upper()
        content = m.get('content') or ''
        if content:
            recent_lines.append(f"{role}: {content}")
    recent_text = "\n".join(recent_lines)

    existing_summary_capped = existing_summary[:4000]
    recent_text_capped = recent_text[:3000]

    compact_prompt = (
        "You are a context compaction engine. Rewrite the conversation summary below "
        "to include ONLY points relevant to the recent conversation exchanges.\n\n"
        "Rules:\n"
        "- Remove any point with less than 30% relevance to the recent conversation\n"
        "- Always keep user identity info (name, phone, contact, etc.)\n"
        "- Always keep unresolved issues and pending tasks\n"
        "- Use concise single-line bullet points\n"
        f"- The rewritten summary MUST NOT exceed {_max_summary_chars} characters\n"
        "- Output ONLY the compacted summary, no explanation\n\n"
        f"## Existing Summary:\n{existing_summary_capped or '(none)'}\n\n"
        f"## Recent Conversation (last 5 exchanges):\n{recent_text_capped or '(none)'}\n\n"
        "## Compacted Summary:"
    )

    # --- 5. LLM call for compaction ---
    _logger.info("Calling LLM for compaction (summary=%dc, recent=%dc, prompt_total=%dc)",
                 len(existing_summary_capped), len(recent_text_capped), len(compact_prompt))
    try:
        # _logger.info("[LOCK] _llm_lock - WAITING (session=%s, compaction)", session_id)
        with llm_lock:
            # _logger.info("[LOCK] _llm_lock - ACQUIRED (session=%s, compaction)", session_id)
            result = llm.chat_completion(
                messages=[{"role": "user", "content": compact_prompt}],
                tools=None,
                temperature=0.0,
                enable_thinking=False,
                max_tokens=1024,
            )
    except Exception as e:
        _logger.error("Compaction LLM call raised exception: %s", e)
        return None

    if not result.get('success'):
        _logger.warning("Compaction LLM call failed: error_type=%s detail=%s",
                        result.get('error_type'),
                        str(result.get('error_detail') or result.get('response', ''))[:200])
        return None

    choice = result['response'].get('choices', [{}])[0]
    compacted_summary = (choice.get('message', {}).get('content') or '').strip()
    _logger.debug("Compaction LLM returned %d chars", len(compacted_summary))
    if not compacted_summary:
        _logger.warning("Empty compacted summary — aborting")
        return None

    compacted_summary, _ = strip_thinking_tags(compacted_summary)

    # Hard-cap the compacted summary so this emergency path obeys the same
    # persisted-summary budget as the regular summarizer. Semantic compaction
    # first, deterministic line-level pruning as the last resort.
    compacted_summary = _compact_summary(
        compacted_summary,
        max_chars=_max_summary_chars,
        llm=llm,
        llm_lock=llm_lock,
        focus_text=recent_text_capped,
    )

    # --- 6. Persist compacted summary to DB ---
    try:
        summary_record = db.get_summary(session_id, agent_id=agent_id)
        last_msg_id = summary_record['last_message_id'] if summary_record else 0
        msg_count = summary_record['message_count'] if summary_record else 0
        db.upsert_summary(session_id, compacted_summary, last_msg_id, msg_count,
                          agent_id=agent_id)
    except Exception as e:
        _logger.warning("Emergency compaction DB persist failed (non-fatal): %s", e)

    # --- 7. Rebuild messages list ---
    new_messages = []
    if system_msgs:
        new_messages.append(system_msgs[0])
    new_messages.append({
        "role": "system",
        "content": f"## Prior conversation summary (compacted)\n{compacted_summary}",
    })
    for i, m in enumerate(system_msgs[1:], start=1):
        if i == old_summary_idx:
            continue
        new_messages.append(m)
    new_messages.extend(last_5)

    _logger.info("Compaction rebuilt %d messages (was %d, last_5=%d, summary_chars=%d)",
                 len(new_messages), len(messages), len(last_5), len(compacted_summary))
    return new_messages
