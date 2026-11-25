"""Error taxonomy: output-budget exhaustion must never read as "conversation too long".

Incident (`siwa_organizer_3-97f91d72`): a reasoning model burned its whole output
budget on chain-of-thought.  The client reported `error_type=generation_timeout`
with a detail containing the literal substring ``max_tokens`` and the old
substring heuristic turned that into "Conversation is too long for this model",
so the parent agent gave up on a recoverable batch (Kanban #930).
"""
import unittest

from backend.agent_runtime.llm_response_parser import _humanize_llm_error

# Verbatim shape produced by backend/llm_client.py for output-budget exhaustion.
REASONING_EXHAUSTED_DETAIL = (
    "Generation hit max_tokens limit (4096) without producing final answer. "
    "Reasoning length: 14460 chars."
)
CONTEXT_LENGTH_MESSAGE = "Conversation is too long for this model. Try starting a new session (/new)."


class TestReasoningExhaustionIsNotContextLength(unittest.TestCase):
    def test_raw_detail_is_not_reported_as_conversation_too_long(self):
        msg = _humanize_llm_error(REASONING_EXHAUSTED_DETAIL)
        self.assertNotEqual(msg, CONTEXT_LENGTH_MESSAGE)
        self.assertNotIn("conversation is too long", msg.lower())
        self.assertNotIn("/new", msg)

    def test_raw_detail_names_the_real_cause(self):
        msg = _humanize_llm_error(REASONING_EXHAUSTED_DETAIL).lower()
        self.assertIn("max_tokens", msg)
        self.assertIn("output", msg)

    def test_structured_error_type_wins_over_substring_guessing(self):
        # Even a detail that looks context-ish cannot override the structure.
        for detail in (REASONING_EXHAUSTED_DETAIL,
                       "Generation hit max_tokens limit (2048) without producing final answer.",
                       "prompt: context length mentioned but the real cause is output budget"):
            msg = _humanize_llm_error(detail, error_type="generation_timeout")
            self.assertNotEqual(msg, CONTEXT_LENGTH_MESSAGE, detail)
            self.assertIn("max_tokens", msg.lower())

    def test_structured_error_type_wins_case_and_whitespace_insensitively(self):
        msg = _humanize_llm_error("irrelevant detail", error_type="  Generation_Timeout ")
        self.assertNotEqual(msg, CONTEXT_LENGTH_MESSAGE)
        self.assertIn("max_tokens", msg.lower())

    def test_positional_signature_stays_backward_compatible(self):
        # Existing callers pass the detail as the only positional argument.
        self.assertIsInstance(_humanize_llm_error("rate limit reached"), str)


class TestGenuineContextLengthStillMaps(unittest.TestCase):
    def test_context_length_exceeded(self):
        msg = _humanize_llm_error("Error code: 400 - context length exceeded: 128000 tokens")
        self.assertEqual(msg, CONTEXT_LENGTH_MESSAGE)

    def test_exceed_context_style_error(self):
        msg = _humanize_llm_error(
            "request (60000 tokens) exceeds the available context size (49152 tokens), "
            "type=exceed_context_size_error")
        self.assertEqual(msg, CONTEXT_LENGTH_MESSAGE)

    def test_400_mentioning_the_context_window(self):
        msg = _humanize_llm_error(
            "LLM API error: 400 - This model's maximum context window is 65536 tokens")
        self.assertEqual(msg, CONTEXT_LENGTH_MESSAGE)

    def test_structured_context_length_error_type(self):
        msg = _humanize_llm_error("irrelevant detail", error_type="context_length_error")
        self.assertEqual(msg, CONTEXT_LENGTH_MESSAGE)


class TestOtherBranchesDoNotRegress(unittest.TestCase):
    def test_rate_limit(self):
        self.assertIn("rate limit", _humanize_llm_error("429 Too Many Requests").lower())

    def test_auth(self):
        self.assertIn("authentication", _humanize_llm_error("401 Unauthorized").lower())

    def test_image_not_supported(self):
        self.assertIn("image", _humanize_llm_error("image input is not supported").lower())

    def test_tool_message_repair(self):
        msg = _humanize_llm_error(
            "400 - An assistant message with 'tool_calls' must be followed by tool "
            "messages responding to each 'tool_call_id'.")
        self.assertIn("repaired", msg.lower())

    def test_server_error(self):
        self.assertIn("server", _humanize_llm_error("503 server error").lower())

    def test_connection_error(self):
        self.assertIn("connect", _humanize_llm_error("Connection refused").lower())


if __name__ == "__main__":
    unittest.main()
