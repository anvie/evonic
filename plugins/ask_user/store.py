"""
Ask User Question — shared state for the ask_user plugin.

The ask_user_question tool (running on the agent loop thread) registers a
PendingQuestion here and blocks on its event; the answer route (running on a
request thread) resolves it. Both import this module by its absolute name
(``plugins.ask_user.store``) so they share one registry instance.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MAX_QUESTIONS = 4
MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_HEADER_CHARS = 24
MAX_QUESTION_CHARS = 1000
MAX_LABEL_CHARS = 120
MAX_DESCRIPTION_CHARS = 500
MAX_OTHER_CHARS = 2000

# Labels the model may add despite the instructions — the UI always offers its own "Other".
_OTHER_LABELS = {'other', 'lainnya', 'others', 'something else'}
# Models often mark a pick in the label text ("SQLite (Recommended)"); turn that into the flag.
_RECOMMENDED_SUFFIX = re.compile(
    r'\s*[(\[]\s*(recommended|rekomendasi|direkomendasikan|disarankan)\s*[)\]]\s*$', re.IGNORECASE)


def _is_true(value) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == 'true')


def _clean_str(value, limit: int) -> str:
    if not isinstance(value, str):
        return ''
    return value.strip()[:limit]


def normalize_questions(raw) -> Tuple[List[dict], Optional[str]]:
    """Validate the tool's ``questions`` argument.

    Returns (questions, None) on success or ([], error_message) on failure.
    Error messages are addressed to the model so it can fix the call.
    """
    if not isinstance(raw, list) or not raw:
        return [], "'questions' must be a non-empty array of question objects."
    if len(raw) > MAX_QUESTIONS:
        return [], f"Ask at most {MAX_QUESTIONS} questions per call (got {len(raw)})."

    questions = []
    seen_questions = set()
    for qi, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            return [], f"Question {qi} must be an object."
        text = _clean_str(item.get('question'), MAX_QUESTION_CHARS)
        if not text:
            return [], f"Question {qi} is missing 'question' text."
        if text in seen_questions:
            return [], f"Question {qi} duplicates an earlier question."
        seen_questions.add(text)

        header = _clean_str(item.get('header'), MAX_HEADER_CHARS) or f'Q{qi}'
        multi = _is_true(item.get('multiSelect', item.get('multi_select', False)))

        raw_options = item.get('options')
        if not isinstance(raw_options, list):
            return [], f"Question {qi} needs an 'options' array."
        options = []
        seen_labels = set()
        for oi, opt in enumerate(raw_options, start=1):
            if isinstance(opt, str):
                opt = {'label': opt}
            if not isinstance(opt, dict):
                return [], f"Question {qi}, option {oi} must be an object with a 'label'."
            label = _clean_str(opt.get('label'), MAX_LABEL_CHARS)
            recommended = _is_true(opt.get('recommended'))
            suffix = _RECOMMENDED_SUFFIX.search(label)
            if suffix:
                label, recommended = label[:suffix.start()].strip(), True
            if not label:
                return [], f"Question {qi}, option {oi} is missing a 'label'."
            if label.lower() in _OTHER_LABELS:
                continue  # the UI adds its own free-text "Other"
            if label in seen_labels:
                return [], f"Question {qi} has duplicate option label '{label}'."
            seen_labels.add(label)
            options.append({
                'label': label,
                'description': _clean_str(opt.get('description'), MAX_DESCRIPTION_CHARS),
                'recommended': recommended,
            })
        if len(options) < MIN_OPTIONS:
            return [], (f"Question {qi} needs at least {MIN_OPTIONS} options "
                        "(not counting 'Other', which is added automatically).")
        if len(options) > MAX_OPTIONS:
            return [], f"Question {qi} has {len(options)} options; the maximum is {MAX_OPTIONS}."
        if not multi:
            # A single-choice question can only recommend one answer; keep the first.
            flagged = [o for o in options if o['recommended']]
            for extra in flagged[1:]:
                extra['recommended'] = False

        questions.append({
            'question': text,
            'header': header,
            'multiSelect': multi,
            'options': options,
        })
    return questions, None


def normalize_answers(questions: List[dict], raw) -> Tuple[List[dict], Optional[str]]:
    """Validate answers submitted by the web UI.

    ``raw`` is a list aligned with ``questions``; each item is
    ``{"selected": [label, ...], "other": "free text" | null}``.
    """
    if not isinstance(raw, list) or len(raw) != len(questions):
        return [], 'Answer every question.'

    answers = []
    for qi, (q, item) in enumerate(zip(questions, raw), start=1):
        if not isinstance(item, dict):
            return [], f'Answer {qi} is malformed.'
        selected = item.get('selected') or []
        if not isinstance(selected, list):
            return [], f'Answer {qi} is malformed.'
        valid_labels = [o['label'] for o in q['options']]
        # Keep the option order, not the click order.
        selected = [label for label in valid_labels if label in selected]
        other = _clean_str(item.get('other'), MAX_OTHER_CHARS) or None

        if not selected and not other:
            return [], f'Pick an option or type an answer for "{q["header"]}".'
        if not q['multiSelect'] and len(selected) + (1 if other else 0) > 1:
            return [], f'"{q["header"]}" takes a single answer.'

        parts = selected + ([other] if other else [])
        answers.append({
            'header': q['header'],
            'question': q['question'],
            'answer': ', '.join(parts),
            'selected': selected,
            'other': other,
        })
    return answers, None


def build_result(status: str, questions: List[dict], answers: Optional[List[dict]] = None,
                 chat_reply: Optional[str] = None) -> dict:
    """Build the tool result the model receives (also rendered by the web UI in history)."""
    if status == 'answered':
        pairs = '; '.join(f'"{a["question"]}" = "{a["answer"]}"' for a in answers)
        return {
            'status': 'answered',
            'answers': answers,
            'message': f'The user answered your questions: {pairs}. '
                       'Continue with these answers in mind.',
        }
    if status == 'dismissed':
        return {
            'status': 'dismissed',
            'questions': [{'header': q['header'], 'question': q['question']} for q in questions],
            'message': 'The user closed the questions without answering. Do not assume '
                       'answers. Continue without them if you can; otherwise say in your '
                       'reply what you still need to know.',
        }
    if status == 'replied_in_chat':
        return {
            'status': 'replied_in_chat',
            'questions': [{'header': q['header'], 'question': q['question']} for q in questions],
            'reply': chat_reply,
            'message': 'Instead of picking an option, the user replied in the chat: '
                       f'"{chat_reply}" (it follows as their next message). Interpret it '
                       'as their answer to your questions, or as a new instruction if it '
                       'is unrelated.',
        }
    return {
        'status': 'cancelled',
        'questions': [{'header': q['header'], 'question': q['question']} for q in questions],
        'error': 'The user stopped the agent before answering. Do not assume an answer.',
    }


@dataclass
class PendingQuestion:
    question_id: str
    session_id: str
    agent_id: str
    questions: List[dict]
    created_at: float = field(default_factory=time.time)
    event: threading.Event = field(default_factory=threading.Event)
    answers: Optional[List[dict]] = None
    dismissed: bool = False


RESOLVED_TTL_SECONDS = 600


class QuestionRegistry:
    """Thread-safe registry of questions currently waiting for an answer.

    Also remembers how recently finished questions ended, so a polling web UI
    can tell "answered elsewhere / stopped" apart from "the server forgot it"."""

    def __init__(self):
        self._pending: Dict[str, PendingQuestion] = {}
        self._resolved: Dict[str, Tuple[str, float]] = {}  # question_id -> (status, at)
        self._lock = threading.Lock()

    def create(self, session_id: str, agent_id: str, questions: List[dict]) -> PendingQuestion:
        pq = PendingQuestion(
            question_id=str(uuid.uuid4()),
            session_id=session_id,
            agent_id=agent_id,
            questions=questions,
        )
        with self._lock:
            self._pending[pq.question_id] = pq
        return pq

    def get(self, question_id: str) -> Optional[PendingQuestion]:
        with self._lock:
            return self._pending.get(question_id)

    def answer(self, question_id: str, raw_answers) -> Tuple[Optional[PendingQuestion], Optional[str]]:
        """Validate and record answers, waking the waiting tool.

        Returns (question, None) on success or (None, error_message).
        """
        with self._lock:
            pq = self._pending.get(question_id)
            if pq is None:
                return None, 'This question is no longer waiting for an answer.'
            if pq.answers is not None or pq.dismissed:
                return None, 'This question was already answered or closed.'
            answers, error = normalize_answers(pq.questions, raw_answers)
            if error:
                return None, error
            pq.answers = answers
        pq.event.set()
        return pq, None

    def dismiss(self, question_id: str) -> Tuple[Optional[PendingQuestion], Optional[str]]:
        """Close a question without answering it, waking the waiting tool."""
        with self._lock:
            pq = self._pending.get(question_id)
            if pq is None:
                return None, 'This question is no longer waiting for an answer.'
            if pq.answers is not None or pq.dismissed:
                return None, 'This question was already answered or closed.'
            pq.dismissed = True
        pq.event.set()
        return pq, None

    def remove(self, question_id: str, status: Optional[str] = None) -> None:
        """Forget a question once its tool call returns, recording how it ended."""
        now = time.time()
        with self._lock:
            self._pending.pop(question_id, None)
            if status:
                self._resolved[question_id] = (status, now)
            for qid in [q for q, (_, at) in self._resolved.items() if now - at > RESOLVED_TTL_SECONDS]:
                del self._resolved[qid]

    def resolved_status(self, question_id: str) -> Optional[str]:
        with self._lock:
            entry = self._resolved.get(question_id)
        return entry[0] if entry else None

    def pending_for_session(self, session_id: str) -> List[PendingQuestion]:
        with self._lock:
            return [pq for pq in self._pending.values()
                    if pq.session_id == session_id and pq.answers is None and not pq.dismissed]


question_registry = QuestionRegistry()
