"""Codex (Responses API) usage propagation — the context monitor depends on
prompt_tokens > 0, and this path used to hardcode zeros (frozen meter)."""

from unittest.mock import MagicMock, patch

from backend.provider.codex_client import _map_usage


def test_map_usage_responses_api_keys():
    assert _map_usage({'input_tokens': 95000, 'output_tokens': 420,
                       'total_tokens': 95420}) == {
        'prompt_tokens': 95000, 'completion_tokens': 420, 'total_tokens': 95420}
    detailed = _map_usage({'input_tokens': 95000, 'output_tokens': 420,
                           'input_tokens_details': {'cached_tokens': 12000},
                           'output_tokens_details': {'reasoning_tokens': 80}})
    assert detailed['prompt_tokens_details']['cached_tokens'] == 12000
    assert detailed['completion_tokens_details']['reasoning_tokens'] == 80
    # total derived when absent
    assert _map_usage({'input_tokens': 10, 'output_tokens': 5}) == {
        'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
    assert _map_usage({}) == {}
    assert _map_usage(None) == {}


def test_codex_client_uses_default_model_provider_for_oauth():
    from backend.llm_client import LLMClient
    from models.db import db

    model = {
        'provider': 'openai',
        'api_format': 'codex',
        'base_url': 'https://chatgpt.com/backend-api/codex',
        'model_name': 'gpt-5.6-luna',
    }
    with patch.object(db, 'get_default_model', return_value=model), \
         patch.object(db, 'resolve_model_config', side_effect=lambda value: value):
        client = LLMClient()

    assert client.provider == 'openai'
    assert client._codex_provider_id == 'openai'

    with patch('backend.provider.oauth_codex.get_valid_token', return_value='tok') as get_token, \
         patch('backend.provider.codex_client.CodexClient') as codex_cls:
        codex_cls.return_value.send_request.return_value = {
            'success': True,
            'response': {'choices': [{'message': {'content': 'ok'}}], 'usage': {}},
        }
        client._codex_chat_completion([{'role': 'user', 'content': 'test'}])

    get_token.assert_called_once()
    assert get_token.call_args.args[1] == 'openai'


def test_codex_chat_completion_propagates_usage():
    from backend.llm_client import LLMClient

    client = LLMClient.__new__(LLMClient)  # skip config-loading __init__
    client.base_url = 'https://chatgpt.com/backend-api/codex'
    client.model = 'gpt-5.6-terra'
    client.max_tokens = 4096
    client.temperature = None
    client.thinking = False
    client.timeout = 120
    client.provider = 'codex'
    client._codex_provider_id = 'codex'

    fake_result = {
        'success': True,
        'response': {
            'id': 'resp_x', 'model': 'gpt-5.6-terra',
            'choices': [{'index': 0,
                         'message': {'role': 'assistant', 'content': 'hi'},
                         'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 1234, 'completion_tokens': 56,
                      'total_tokens': 1290},
        },
    }
    codex = MagicMock()
    codex.send_request.return_value = fake_result
    messages = [{'role': 'user', 'content': 'halo'}]
    with patch('backend.provider.oauth_codex.get_valid_token',
               return_value='tok'), \
         patch('backend.provider.codex_client.CodexClient',
               return_value=codex), \
         patch('backend.llm_usage_events.record_llm_usage') as record_usage:
        result = client._codex_chat_completion(messages)

    assert result['success']
    assert result['prompt_tokens'] == 1234        # not hardcoded zero anymore
    assert result['completion_tokens'] == 56
    assert result['total_tokens'] == 1290
    # traces/archive get a request payload now (was None)
    assert result['request_payload']['model'] == 'gpt-5.6-terra'
    assert result['request_payload']['messages'][0]['content'] == 'halo'
    record_usage.assert_called_once_with(
        model='gpt-5.6-terra',
        provider='codex',
        prompt_tokens=1234,
        completion_tokens=56,
        total_tokens=1290,
        cached_tokens=0,
        reasoning_tokens=0,
        usage_details_available=False,
        duration_ms=result['duration_ms'],
        messages=messages,
        response_text='hi',
    )


def test_estimate_context_tokens_from_messages_and_tools():
    from backend.llm_usage_events import estimate_context_tokens
    msgs = [
        {'role': 'system', 'content': 'You are a helpful agent. ' * 50},
        {'role': 'user', 'content': 'halo dunia'},
        {'role': 'assistant', 'content': '',
         'tool_calls': [{'function': {'name': 'bash',
                                      'arguments': '{"script": "ls -la /very/long/path"}'}}]},
    ]
    tools = [{'type': 'function', 'function': {'name': 'bash',
              'description': 'run a shell command ' * 20,
              'parameters': {'type': 'object', 'properties': {}}}}]
    n = estimate_context_tokens(msgs, tools)
    # system prose + tool-call args + tool schema all counted → clearly > 100
    assert n > 100
    # tools contribute materially
    assert estimate_context_tokens(msgs, tools) > estimate_context_tokens(msgs, None)
    assert estimate_context_tokens([], None) == 0


def test_context_usage_estimated_when_provider_reports_zero():
    """The Codex path returns prompt_tokens=0; the persist logic must fall
    back to a local estimate so the meter reflects real context growth."""
    from backend.llm_usage_events import estimate_context_tokens
    # a bigger message set estimates larger than a tiny one — monotonic,
    # which is all the context monitor needs to stop being frozen.
    small = estimate_context_tokens([{'role': 'user', 'content': 'hi'}], None)
    big = estimate_context_tokens(
        [{'role': 'user', 'content': 'x' * 8000}], None)
    assert big > small


def test_persist_context_usage_emits_state_changed():
    """The context bar updates live: persisting context_usage must emit
    'evonic:agent-state-changed' (forwarded to the browser as the
    'state_changed' SSE event) — before this, only a manual Refresh
    re-fetched /chat/state."""
    from backend.agent_runtime import llm_loop
    with patch.object(llm_loop, 'db') as mock_db, \
         patch('backend.event_stream.event_stream.emit') as mock_emit:
        mock_db.get_session_state.return_value = '{}'
        llm_loop._persist_context_usage('sess-1', 'agent-1',
                                        {'prompt_tokens': 10})
        mock_db.upsert_session_state.assert_called_once()
        mock_emit.assert_called_once_with(
            'evonic:agent-state-changed',
            {'agent_id': 'agent-1', 'session_id': 'sess-1'})
