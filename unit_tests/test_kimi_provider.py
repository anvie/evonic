"""Tests for Kimi Coding Plan User-Agent masquerade in the LLM client."""

from backend.llm_client import _build_request_headers


def test_kimi_coding_endpoint_sets_kimicli_user_agent():
    headers = _build_request_headers(
        api_key="sk-kimi-abc",
        base_url="https://api.kimi.com/coding/v1",
    )
    assert headers["User-Agent"] == "KimiCLI/1.5"
    assert headers["Authorization"] == "Bearer sk-kimi-abc"
    assert headers["Content-Type"] == "application/json"


def test_non_kimi_endpoint_omits_user_agent():
    headers = _build_request_headers(
        api_key="sk-foo",
        base_url="https://api.openai.com/v1",
    )
    assert "User-Agent" not in headers
    assert headers["Authorization"] == "Bearer sk-foo"


def test_no_api_key_omits_authorization():
    headers = _build_request_headers(api_key=None, base_url="http://localhost:11434/v1")
    assert "Authorization" not in headers
    assert "User-Agent" not in headers


def test_kimi_match_is_substring_so_subpaths_work():
    # Anyone hosting at api.kimi.com/* gets the masquerade — including the
    # /coding root path that the Anthropic-compat endpoint uses, in case we
    # ever add that format.
    headers = _build_request_headers(
        api_key="sk-kimi-x",
        base_url="https://api.kimi.com/coding",
    )
    assert headers["User-Agent"] == "KimiCLI/1.5"
