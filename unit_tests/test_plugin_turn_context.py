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
