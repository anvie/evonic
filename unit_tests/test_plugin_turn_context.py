from backend.plugin_hooks import (
    apply_turn_context,
    apply_user_message_transformers,
    register_final_response_handler,
    register_user_message_transformer,
    run_final_response_handlers,
    unregister_final_response_handler,
    unregister_user_message_transformer,
)


def test_turn_context_injects_prefill_before_history_and_deduplicates_tools():
    messages = [
        {"role": "system", "content": "core"},
        {"role": "user", "content": "real request"},
    ]
    existing = {"function": {"name": "existing"}}
    added = {"function": {"name": "added"}}
    tools = [existing]

    apply_turn_context(messages, tools, [{
        "system_md": "plugin context",
        "prefill_messages": [
            {"role": "user", "content": "priming question"},
            {"role": "assistant", "content": "primed answer"},
            {"role": "system", "content": "rejected role"},
        ],
        "tools": [existing, added],
    }])

    assert [m["content"] for m in messages] == [
        "core", "plugin context", "priming question", "primed answer", "real request",
    ]
    assert [t["function"]["name"] for t in tools] == ["existing", "added"]


def test_turn_context_validates_types_roles_and_size_limits():
    messages = [
        {"role": "system", "content": "core"},
        {"role": "user", "content": "real request"},
    ]

    apply_turn_context(messages, [], [
        None,
        {"system_md": 123, "prefill_messages": "not-a-list"},
        {
            "system_md": "s" * 40000,
            "prefill_messages": [
                {"role": "user", "content": "m" * 20000},
            ] + [
                {"role": "assistant", "content": f"reply-{index}"}
                for index in range(10)
            ],
        },
    ])

    injected = messages[1:-1]
    assert len(injected) == 9
    assert injected[0] == {"role": "system", "content": "s" * 32000}
    assert injected[1] == {"role": "user", "content": "m" * 16000}
    assert injected[-1] == {"role": "assistant", "content": "reply-6"}


def test_turn_context_system_modes_preserve_append_or_replace_request_prompt():
    preserve = [{"role": "system", "content": "core"},
                {"role": "user", "content": "request"}]
    apply_turn_context(preserve, [], [{
        "system_md": "plugin", "system_mode": "preserve",
        "prefill_messages": [{"role": "assistant", "content": "primed"}],
    }])
    assert preserve == [
        {"role": "system", "content": "core"},
        {"role": "assistant", "content": "primed"},
        {"role": "user", "content": "request"},
    ]

    append = [{"role": "system", "content": "core"}]
    apply_turn_context(append, [], [{"system_md": "plugin", "system_mode": "append"}])
    assert [message["content"] for message in append] == ["core", "plugin"]

    replace = [{"role": "system", "content": "core"},
               {"role": "user", "content": "request"}]
    apply_turn_context(replace, [], [{"system_md": "plugin", "system_mode": "replace"}])
    assert [message["content"] for message in replace] == ["plugin", "request"]


def test_user_message_transformers_only_change_newest_user_text_parts():
    messages = [
        {"role": "system", "content": "core"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": [
            {"type": "text", "text": "new request"},
            {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,x"}},
        ]},
    ]
    register_user_message_transformer("first", lambda _a, _s, text: text.upper())
    register_user_message_transformer("second", lambda _a, _s, text: f"[{text}]")
    try:
        assert apply_user_message_transformers("agent", "session", messages)
    finally:
        unregister_user_message_transformer("first")
        unregister_user_message_transformer("second")

    assert messages[1]["content"] == "old request"
    assert messages[-1]["content"] == [
        {"type": "text", "text": "[NEW REQUEST]"},
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,x"}},
    ]


def test_user_message_transformer_errors_fail_open(caplog):
    def broken(_agent_id, _session_id, _text):
        raise RuntimeError("broken")

    messages = [{"role": "user", "content": "unchanged"}]
    register_user_message_transformer("broken", broken)
    try:
        assert not apply_user_message_transformers("agent", "session", messages)
    finally:
        unregister_user_message_transformer("broken")

    assert messages[0]["content"] == "unchanged"
    assert "Plugin user-message transformer failed: broken" in caplog.text


def test_final_response_handler_can_retry_replace_or_fail_open(caplog):
    register_final_response_handler(
        "retry", lambda context: {
            "retry": True,
            "messages": context["messages"] + [{"role": "user", "content": "retry"}],
        })
    try:
        decision = run_final_response_handlers({
            "content": "refused", "messages": [{"role": "user", "content": "request"}],
        })
        assert decision["namespace"] == "retry"
        assert decision["messages"][-1]["content"] == "retry"
    finally:
        unregister_final_response_handler("retry")

    register_final_response_handler("replace", lambda _context: {"content": "accepted"})
    try:
        assert run_final_response_handlers({})["content"] == "accepted"
    finally:
        unregister_final_response_handler("replace")

    def broken(_context):
        raise RuntimeError("broken")

    register_final_response_handler("broken", broken)
    try:
        assert run_final_response_handlers({}) is None
    finally:
        unregister_final_response_handler("broken")
    assert "Plugin final-response handler failed: broken" in caplog.text
