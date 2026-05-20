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

def _humanize_llm_error(raw: str) -> str:
    """Wrap raw LLM API errors into human-friendly messages."""
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

    if 'image input is not supported' in text or 'image' in text and 'not supported' in text:
        return "The model doesn't support image input. Try sending a text-only message, or switch to a model that supports vision."
    if 'rate limit' in text or re.search(r'\b429\b', text):
        return "API rate limit reached. Please wait a moment and try again."
    if 'unauthorized' in text or 'invalid api key' in text or re.search(r'\b401\b', text):
        return "API authentication failed. Check your API key configuration."
    if ('context length' in text or 'max_tokens' in text or 'too long' in text
            or 'context size' in text or 'exceed_context' in text or 'exceeds the available context' in text):
        return "Conversation is too long for this model. Try starting a new session (/new)."

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
        "- Output ONLY the compacted summary, no explanation\n\n"
        f"## Existing Summary:\n{existing_summary_capped or '(none)'}\n\n"
        f"## Recent Conversation (last 5 exchanges):\n{recent_text_capped or '(none)'}\n\n"
        "## Compacted Summary:"
    )

    # --- 5. LLM call for compaction ---
    _logger.info("Calling LLM for compaction (summary=%dc, recent=%dc, prompt_total=%dc)",
                 len(existing_summary_capped), len(recent_text_capped), len(compact_prompt))
    try:
        _logger.info("[LOCK] _llm_lock - WAITING (session=%s, compaction)", session_id)
        with llm_lock:
            _logger.info("[LOCK] _llm_lock - ACQUIRED (session=%s, compaction)", session_id)
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
