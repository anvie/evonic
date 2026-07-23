"""
Task Complexity Classifier — determines if a task is trivial or complex.

Trivial tasks start in "execute" mode (writes allowed immediately).
Complex tasks start in "plan" mode (must plan before writing).

Uses a lightweight LLM call (no tools, no thinking) with an optional
heuristic fast-path to skip the LLM entirely for obvious cases.
"""

import json
import logging
import re
import time
from typing import Optional

import config
from backend.llm_client import LLMClient

_logger = logging.getLogger(__name__)

# Heuristic thresholds
_TRIVIAL_MAX_WORDS = 15
_COMPLEX_MIN_WORDS = 80

# Keywords that strongly suggest complexity (EN + ID equivalents — sessions
# are frequently Indonesian/code-switched and would otherwise never hit
# this deterministic fast-path)
_COMPLEX_KEYWORDS = {
    "refactor", "redesign", "migrate", "architect", "implement",
    "integrate", "optimize", "review", "analyze", "investigate",
    "debug", "troubleshoot", "upgrade", "overhaul", "restructure",
    "design", "plan", "strategy", "multiple", "several", "across",
    "gabungkan", "menggabungkan", "integrasikan", "implementasikan",
    "analisis", "selidiki", "investigasi", "migrasi", "optimalkan",
    "rancang", "desain", "beberapa",
}

# Patterns that suggest trivial single-action tasks
_TRIVIAL_PATTERNS = [
    re.compile(r"^(create|write|make|add|generate)\s+(a\s+)?(\w+\s+){0,3}file", re.I),
    re.compile(r"^(say|print|echo|output)\s+", re.I),
    re.compile(r"^(create|write)\s+hello\s+world", re.I),
    re.compile(r"^(?:(?:now\s+)?please\s+)?(?:git\s+)?(push|pull|fetch|status)\b", re.I),
]

_CLASSIFIER_SYSTEM = """You classify tasks as TRIVIAL or COMPLEX for an AI coding agent.

TRIVIAL: Can be completed in 1-2 file operations with no ambiguity. Examples:
- "Create a hello world Python file"
- "Add a .gitignore file"
- "Write a simple README"
- "Create an empty index.html"

COMPLEX: Requires research, reading existing code, multi-step changes, or design decisions. Examples:
- "Add authentication to the API"
- "Fix the bug in the payment module"
- "Refactor the database layer"
- "Create a REST API with CRUD operations"
- Any task mentioning existing code/files that need to be understood first

When in doubt, classify as COMPLEX.
Respond with exactly one word: TRIVIAL or COMPLEX"""

_OPERATION_CLASSIFIER_SYSTEM = """You classify whether an agent's current operation is TRIVIAL or COMPLEX.

TRIVIAL: Simple mechanical tasks requiring no code changes or planning. Examples:
- git push/pull/fetch/status
- restarting a service or process
- reading/displaying a file
- running a simple one-line command
- checking logs or status

COMPLEX: Any task that involves writing, editing, creating, or modifying files, or requires research and multi-step planning.

Respond with exactly one word: TRIVIAL or COMPLEX"""

# Metadata flags that mark non-conversation messages to be excluded
_UI_ONLY_META_FLAGS = {
    "busy_ack", "busy_rejection", "bash_exec", "slash_command",
    "evonet_offline", "stop_injection", "free_notification",
}


_CONTINUATION_SYSTEM = """You decide whether a user's new message starts a NEW task or CONTINUES the previous one, for an AI coding agent.

The agent just finished this task:
{goal}

CONTINUATION: feedback, bug reports, follow-ups, refinements, corrections or questions about the SAME work. Examples: "it doesn't work", "change the port to 8080", "add a button to that page", "why is it slow?", "belum bisa", "masih error"
NEW_TASK: an unrelated or clearly separate piece of work — a different project, feature, or goal than the finished task.

When in doubt, answer CONTINUATION.
Respond with exactly one word: NEW_TASK or CONTINUATION"""

# Short follow-ups ("belum bisa", "masih error", "coba lagi") are continuations.
_CONTINUATION_MAX_WORDS = 6


def classify_continuation(previous_goal: str, user_message: str) -> str:
    """Classify a message as 'new_task' or 'continuation' of previous_goal.

    Defaults to 'continuation' on any error — re-planning by surprise is
    worse than staying in the current flow.
    """
    text = (user_message or "").strip()
    if not text or not (previous_goal or "").strip():
        return "continuation"
    if len(text.split()) <= _CONTINUATION_MAX_WORDS:
        return "continuation"

    try:
        client = _get_classifier_client('cmp_model_id')
        response = classifier_chat(
            client,
            [{"role": "system",
              "content": _CONTINUATION_SYSTEM.format(goal=previous_goal.strip()[:1000])},
             {"role": "user", "content": text[:4000]}],
            max_tokens=1024, log_label="CMP continuation",
            source="cmp", archive_category="boundary")
        if not response.get("success"):
            _logger.warning("Continuation classifier LLM call failed: %s",
                            response.get("error_type"))
            return "continuation"
        msg = (response.get("response", {}).get("choices") or [{}])[0].get("message", {})
        content = (msg.get("content") or "").strip().upper()
        if not content:
            content = (msg.get("reasoning_content") or "").strip().upper()
        result = "new_task" if "NEW_TASK" in content else "continuation"
        _logger.info("Message classified as %s (LLM)", result)
        return result
    except Exception as e:
        _logger.warning("Continuation classifier failed, defaulting to continuation: %s", e)
        return "continuation"


def classifier_chat(client, messages, max_tokens: int, log_label: str = "classifier",
                    source: str = None, archive_category: str = None,
                    agent_id: str = None, session_id: str = None):
    """chat_completion for classifier-style calls, with ONE retry at a doubled
    budget when the model burns the whole max_tokens on implicit reasoning
    (finish_reason=length with empty content → error_type generation_timeout;
    deepseek-style models emit CoT even with thinking off, so any fixed budget
    occasionally loses the race).
    
    source: value for llm_usage source tag (token monitor).
    archive_category: if set and SESSION_ARCHIVE enabled, write training example
        to session_archive.db → cmp table. One of 'boundary', 'card', 'naming'.
    """
    from backend.llm_usage_events import usage_context
    with usage_context(source=source or log_label):
        response = client.chat_completion(
            messages, tools=None, temperature=0.0, enable_thinking=False,
            max_tokens=max_tokens)
        if (not response.get("success")
                and response.get("error_type") == "generation_timeout"):
            _logger.info("%s hit generation_timeout (model=%s) — retrying with max_tokens=%d",
                         log_label, getattr(client, 'model', None), max_tokens * 2)
            response = client.chat_completion(
                messages, tools=None, temperature=0.0, enable_thinking=False,
                max_tokens=max_tokens * 2)
        
        # ── Archive CMP training example if configured ──
        if archive_category and response.get("success"):
            try:
                from models.session_archive import SessionArchiver
                msg_resp = (response.get("response", {}).get("choices") or [{}])[0].get("message", {})
                output = (msg_resp.get("content")
                          or msg_resp.get("reasoning_content") or "").strip()
                system_prompt = ""
                input_text = ""
                for m in messages:
                    if m.get("role") == "system":
                        system_prompt = m.get("content", "")
                    elif m.get("role") == "user":
                        input_text = m.get("content", "")
                SessionArchiver.write_cmp_example(
                    session_id=session_id,
                    agent_id=agent_id,
                    category=archive_category,
                    system_prompt=system_prompt,
                    input=input_text,
                    output=output,
                    model=getattr(client, 'model', None),
                )
            except Exception:
                _logger.debug("CMP archive write failed", exc_info=True)
        
        return response


def _get_classifier_client(setting_key: str = 'task_classifier_model_id') -> LLMClient:
    """Build an LLMClient for classification, using the configured model or default.

    setting_key selects which System Settings model applies (e.g.
    'cmp_model_id' for CMP path-change detection). A purpose-specific key
    that is unset falls back to the task classifier model, then default.
    """
    try:
        from models.db import db
        model_id = db.get_setting(setting_key, '')
        if not model_id and setting_key != 'task_classifier_model_id':
            model_id = db.get_setting('task_classifier_model_id', '')
        if model_id:
            model = db.get_model_by_id(model_id)
            if model:
                return LLMClient(model_config=model)
            _logger.warning("Classifier model_id '%s' not found, falling back to default", model_id)
    except Exception as e:
        _logger.warning("Could not load classifier model config: %s", e)
    return LLMClient()


def _is_enabled() -> bool:
    """Check if the task classifier is enabled (DB setting overrides config default)."""
    try:
        from models.db import db
        default = '1' if config.TASK_CLASSIFIER_ENABLED else '0'
        return db.get_setting('task_classifier_enabled', default) == '1'
    except Exception:
        return config.TASK_CLASSIFIER_ENABLED


def _heuristic_classify(text: str) -> Optional[str]:
    """Fast-path heuristic. Returns 'trivial', 'complex', or None (needs LLM)."""
    words = text.split()
    word_count = len(words)

    # Very short + matches a trivial pattern -> trivial
    if word_count <= _TRIVIAL_MAX_WORDS:
        for pat in _TRIVIAL_PATTERNS:
            if pat.search(text):
                return "trivial"

    # Long message or contains complexity keywords -> complex
    if word_count >= _COMPLEX_MIN_WORDS:
        return "complex"
    lower_words = set(text.lower().split())
    if lower_words & _COMPLEX_KEYWORDS:
        return "complex"

    return None  # uncertain, need LLM


def _is_ui_only(msg: dict) -> bool:
    """Check if a message has metadata flags marking it as UI/internal only."""
    meta = msg.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    return bool(_UI_ONLY_META_FLAGS & set(meta.keys()))


def _is_final_response(msg: dict) -> bool:
    """True if the message is a final user/assistant response (not tool call/result, not internal)."""
    if _is_ui_only(msg):
        return False
    role = msg.get("role", "")
    content = (msg.get("content") or "").strip()
    if role == "user":
        return bool(content)
    if role == "assistant":
        # Must have text content AND no tool_calls
        return bool(content) and not msg.get("tool_calls")
    return False


def _get_last_n_final_responses(session_id: str, agent_id: str, n: int = 3):
    """Retrieve the last N final-response messages from a session."""
    from models.db import db
    msgs = db.get_session_messages(session_id, limit=n * 4, agent_id=agent_id) or []
    final = [m for m in msgs if _is_final_response(m)]
    return final[-n:] if len(final) >= n else final


def classify_operation_trivial(session_id: str, agent_id: str) -> str:
    """Classify whether the agent's current operation is trivial.

    Uses the last 3 final responses (user/assistant text) from the session.
    Returns 'trivial' or 'complex'. Defaults to 'complex' on any error.
    """
    if not _is_enabled():
        return "complex"

    try:
        final_msgs = _get_last_n_final_responses(session_id, agent_id, n=3)
        if not final_msgs:
            _logger.info("Operation classifier: no final responses found, defaulting to complex")
            return "complex"

        combined = []
        for m in final_msgs:
            role = m.get("role", "unknown")
            content = (m.get("content") or "").strip()
            combined.append(f"[{role}]: {content}")
        text = "\n".join(combined)

        if not text.strip():
            return "complex"

        client = _get_classifier_client()
        _t0 = time.time()
        messages = [
            {"role": "system", "content": _OPERATION_CLASSIFIER_SYSTEM},
            {"role": "user", "content": text},
        ]
        response = classifier_chat(client, messages, max_tokens=128,
                                   log_label="operation classifier")
        if not response.get("success"):
            _logger.warning("Operation classifier LLM call failed [%s] (%.1fs) — defaulting to complex",
                            response.get("error_type"), time.time() - _t0)
            return "complex"
        choices = response.get("response", {}).get("choices", [])
        if not choices:
            return "complex"
        msg = choices[0].get("message", {})
        content = msg.get("content", "").strip().upper()
        reasoning = msg.get("reasoning_content", "").strip().upper()
        if not content and reasoning:
            content = reasoning
        if "TRIVIAL" in content:
            _logger.info("Operation classified as trivial (LLM, %.1fs)", time.time() - _t0)
            return "trivial"
        _logger.info("Operation classified as complex (LLM: %s, %.1fs)",
                     content[:30] if content else "empty/missing", time.time() - _t0)
        return "complex"
    except Exception as e:
        _logger.warning("Operation classifier failed, defaulting to complex: %s", e)
        return "complex"


def classify_task(user_message: str) -> str:
    """Classify a task as 'trivial' or 'complex'.

    Returns 'trivial' or 'complex'. Defaults to 'complex' on any error.
    """
    if not _is_enabled():
        return "complex"

    text = user_message.strip()
    if not text:
        return "complex"

    # Try heuristic first
    result = _heuristic_classify(text)
    if result:
        _logger.info("Task classified as %s (heuristic)", result)
        return result

    # LLM classification
    try:
        client = _get_classifier_client()
        _t0 = time.time()
        messages = [
            {"role": "system", "content": _CLASSIFIER_SYSTEM},
            {"role": "user", "content": text},
        ]
        response = classifier_chat(client, messages, max_tokens=1024,
                                   log_label="task classifier")
        if not response.get("success"):
            _logger.warning("Task classifier LLM call failed [%s] (model=%s, %.1fs) — defaulting to complex",
                            response.get("error_type"), getattr(client, 'model', None),
                            time.time() - _t0)
            return "complex"
        choices = response.get("response", {}).get("choices", [])
        if not choices:
            return "complex"
        # Extract content from LLM response - model may put response in 'content' or 'reasoning_content'
        # (some models like deepseek-v4-flash put the response in reasoning_content field)
        msg = choices[0].get("message", {})
        content = msg.get("content", "").strip().upper()
        reasoning = msg.get("reasoning_content", "").strip().upper()
        # If content is empty, try reasoning_content as fallback
        if not content and reasoning:
            content = reasoning
        if "TRIVIAL" in content:
            _logger.info("Task classified as trivial (LLM)")
            return "trivial"
        _logger.info("Task classified as complex (LLM: %s)", content[:20] if content else "empty/missing")
        return "complex"
    except Exception as e:
        _logger.warning("Task classifier failed, defaulting to complex: %s", e)
        return "complex"
