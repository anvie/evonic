from unittest.mock import patch, MagicMock

from backend.tools.lib import decision_adapters as da


def _headers_for(provider, base_url):
    adapter = da.OpenAIStructuredDecisionAdapter({"provider": provider, "base_url": base_url, "api_key": "k", "model_name": "m"})
    inv = MagicMock()
    inv.cancellation = None
    inv.to_dict.return_value = {}
    resp = MagicMock(status_code=400)
    with patch.object(da, "validate_decision_compatibility"), patch.object(da, "_json_schema", return_value={}), \
         patch.object(da.requests, "post", return_value=resp) as post:
        adapter.decide(inv)
    return post.call_args.kwargs["headers"]


def test_opencode_gets_session_and_ua():
    h = _headers_for("opencode_go", "https://opencode.ai/zen/go/v1")
    assert h["x-opencode-session"]
    assert h["User-Agent"].startswith("Mozilla/5.0")


def test_other_provider_has_no_session():
    h = _headers_for("openrouter", "https://openrouter.ai/api/v1")
    assert "x-opencode-session" not in h
    assert h["Authorization"] == "Bearer k"
    assert "User-Agent" not in h
