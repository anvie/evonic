from backend.plugin_hooks import apply_turn_context


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
