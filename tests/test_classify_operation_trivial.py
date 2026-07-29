"""
Tests for the operation classifier (classify_operation_trivial) and its helpers.

Covers:
- _is_ui_only
- _is_final_response
- _get_last_n_final_responses
- classify_operation_trivial (with mocked db and classifier_chat)
"""
import pytest
from unittest.mock import patch, MagicMock


# ── _is_ui_only tests ────────────────────────────────────────────────

@pytest.mark.parametrize("meta,expected", [
    ({"busy_ack": True}, True),
    ({"busy_rejection": True}, True),
    ({"bash_exec": True}, True),
    ({"slash_command": True}, True),
    ({"evonet_offline": True}, True),
    ({"stop_injection": True}, True),
    ({"free_notification": True}, True),
    ({"error": True}, False),
    ({"stopped": True}, False),
    ({}, False),
    ({"other_flag": True}, False),
    (None, False),
])
def test_is_ui_only(meta, expected):
    from backend.task_classifier import _is_ui_only
    msg = {"metadata": meta} if meta is not None else {}
    assert _is_ui_only(msg) == expected


def test_is_ui_only_no_metadata_key():
    from backend.task_classifier import _is_ui_only
    assert _is_ui_only({"role": "user", "content": "hello"}) is False


def test_is_ui_only_metadata_is_json_string():
    from backend.task_classifier import _is_ui_only
    import json
    msg = {"metadata": json.dumps({"busy_ack": True})}
    assert _is_ui_only(msg) is True


# ── _is_final_response tests ─────────────────────────────────────────

@pytest.mark.parametrize("msg,expected", [
    # user message with content → True
    ({"role": "user", "content": "please push"}, True),
    # assistant with content, no tool_calls → True
    ({"role": "assistant", "content": "ok pushing"}, True),
    # assistant with tool_calls → False
    ({"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}, False),
    # tool role → False
    ({"role": "tool", "content": "result", "tool_call_id": "c1"}, False),
    # system → False
    ({"role": "system", "content": "system prompt"}, False),
    # assistant with empty content, no tool_calls → False
    ({"role": "assistant", "content": ""}, False),
    # assistant with None content → False
    ({"role": "assistant", "content": None}, False),
    # ui-only tagged message → False
    ({"role": "assistant", "content": "text", "metadata": {"bash_exec": True}}, False),
    # user with empty content → False
    ({"role": "user", "content": ""}, False),
])
def test_is_final_response(msg, expected):
    from backend.task_classifier import _is_final_response
    assert _is_final_response(msg) == expected


def test_is_final_response_metadata_is_json_str():
    from backend.task_classifier import _is_final_response
    import json
    msg = {"role": "assistant", "content": "hi", "metadata": json.dumps({"busy_ack": True})}
    assert _is_final_response(msg) is False


# ── _get_last_n_final_responses tests ────────────────────────────────

def _make_msg(role, content="", **kw):
    m = {"role": role, "content": content}
    m.update(kw)
    return m


def _setup_mock_db(messages):
    """Patch models.db.db so that get_session_messages returns given messages."""
    mock_db = MagicMock()
    mock_db.get_session_messages.return_value = messages
    return patch("models.db.db", mock_db)


def test_get_last_n_empty():
    with _setup_mock_db([]):
        from backend.task_classifier import _get_last_n_final_responses
        result = _get_last_n_final_responses("s1", "a1", 3)
    assert result == []


def test_get_last_n_all_final():
    msgs = [
        _make_msg("user", "hello"),
        _make_msg("assistant", "hi there"),
        _make_msg("user", "what's up"),
    ]
    with _setup_mock_db(msgs):
        from backend.task_classifier import _get_last_n_final_responses
        result = _get_last_n_final_responses("s1", "a1", 3)
    assert len(result) == 3
    assert all(m["role"] in ("user", "assistant") for m in result)


def test_get_last_n_limit():
    """With 10 messages but n=3, only the last 3 final responses are returned."""
    msgs = []
    for i in range(5):
        msgs.append(_make_msg("user", f"msg{i}"))
        msgs.append(_make_msg("assistant", f"reply{i}"))
    with _setup_mock_db(msgs):
        from backend.task_classifier import _get_last_n_final_responses
        result = _get_last_n_final_responses("s1", "a1", 3)
    assert len(result) == 3
    # msgs chronologically: msg0, reply0, msg1, reply1, msg2, reply2, msg3, reply3, msg4, reply4
    # reversed iteration collects: reply4, msg4, reply3 → then [::-1] = reply3, msg4, reply4
    assert result[0]["content"] == "reply3"
    assert result[1]["content"] == "msg4"
    assert result[2]["content"] == "reply4"


def test_get_last_n_filters_tool_calls():
    msgs = [
        _make_msg("user", "read the file"),
        _make_msg("assistant", "", tool_calls=[{"id": "c1"}]),
        _make_msg("tool", "file content", tool_call_id="c1"),
        _make_msg("assistant", "the file says hello"),
        _make_msg("user", "thanks"),
    ]
    with _setup_mock_db(msgs):
        from backend.task_classifier import _get_last_n_final_responses
        result = _get_last_n_final_responses("s1", "a1", 3)
    assert len(result) == 3
    contents = [m["content"] for m in result]
    assert contents == ["read the file", "the file says hello", "thanks"]


def test_get_last_n_filters_ui_only():
    msgs = [
        _make_msg("user", "push the code"),
        _make_msg("assistant", "pushing", metadata={"bash_exec": True}),
        _make_msg("assistant", "done, pushed successfully"),
        _make_msg("user", "great"),
    ]
    with _setup_mock_db(msgs):
        from backend.task_classifier import _get_last_n_final_responses
        result = _get_last_n_final_responses("s1", "a1", 3)
    assert len(result) == 3
    contents = [m["content"] for m in result]
    assert contents == ["push the code", "done, pushed successfully", "great"]


def test_get_last_n_fewer_than_requested():
    msgs = [_make_msg("user", "only one")]
    with _setup_mock_db(msgs):
        from backend.task_classifier import _get_last_n_final_responses
        result = _get_last_n_final_responses("s1", "a1", 3)
    assert len(result) == 1


# ── classify_operation_trivial integration tests ────────────────────

def _mock_classifier_return(value):
    """Return a mock classifier_chat response for the given classification."""
    return {
        "success": True,
        "response": {
            "choices": [{
                "message": {"content": value}
            }]
        }
    }


def _classifier_test(messages, classifier_output, expected, also_mock_enabled=True):
    """
    Helper: mock db.get_session_messages + classifier_chat + _is_enabled,
    then call classify_operation_trivial and assert.
    """
    mock_db = MagicMock()
    mock_db.get_session_messages.return_value = messages
    mock_db.get_setting.return_value = "1"  # enabled

    patches = [
        patch("models.db.db", mock_db),
        patch("backend.task_classifier.classifier_chat",
              return_value=classifier_output),
    ]

    for p in patches:
        p.start()

    try:
        from backend.task_classifier import classify_operation_trivial
        result = classify_operation_trivial("s1", "a1")
    finally:
        for p in reversed(patches):
            p.stop()

    assert result == expected


def test_trivial_push():
    msgs = [
        _make_msg("user", "please push the commits"),
        _make_msg("assistant", "ok pushing now"),
        _make_msg("user", "yes go ahead"),
    ]
    _classifier_test(msgs, _mock_classifier_return("TRIVIAL"), "trivial")


def test_trivial_restart():
    msgs = [
        _make_msg("user", "restart the service"),
        _make_msg("assistant", "restarting..."),
    ]
    _classifier_test(msgs, _mock_classifier_return("TRIVIAL"), "trivial")


def test_complex_code_change():
    msgs = [
        _make_msg("user", "add a login feature with oauth"),
    ]
    _classifier_test(msgs, _mock_classifier_return("COMPLEX"), "complex")


def test_empty_messages_defaults_to_complex():
    _classifier_test([], _mock_classifier_return("TRIVIAL"), "complex")


def test_llm_error_defaults_to_complex():
    msgs = [_make_msg("user", "push code")]
    error_response = {"success": False, "error_type": "timeout"}
    _classifier_test(msgs, error_response, "complex")


def test_llm_returns_garbage_defaults_to_complex():
    msgs = [_make_msg("user", "push code")]
    garbage = _mock_classifier_return("RANDOM_STUFF\nMORE")
    _classifier_test(msgs, garbage, "complex")


def test_none_messages_from_db():
    mock_db = MagicMock()
    mock_db.get_session_messages.return_value = None
    mock_db.get_setting.return_value = "1"

    patches = [
        patch("models.db.db", mock_db),
        patch("backend.task_classifier.classifier_chat",
              return_value=_mock_classifier_return("TRIVIAL")),
    ]

    for p in patches:
        p.start()
    try:
        from backend.task_classifier import classify_operation_trivial
        result = classify_operation_trivial("s1", "a1")
    finally:
        for p in reversed(patches):
            p.stop()

    assert result == "complex"


def test_disabled_returns_complex():
    """When task_classifier is disabled, returns 'complex' immediately."""
    mock_db = MagicMock()
    mock_db.get_setting.return_value = "0"  # disabled
    mock_db.get_session_messages.return_value = [
        _make_msg("user", "push code"),
    ]

    with patch("models.db.db", mock_db):
        from backend.task_classifier import classify_operation_trivial
        result = classify_operation_trivial("s1", "a1")

    assert result == "complex"
    mock_db.get_session_messages.assert_not_called()
