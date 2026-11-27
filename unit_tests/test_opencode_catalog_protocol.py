"""OpenCode Fetch models: SystemOne is chosen per model from the catalog's recommended_api."""
from routes.providers import _mark_opencode_systemone


def test_marks_only_systemone_models():
    models = [{"id": "jev-1.14", "name": "jev-1.14"}, {"id": "beam-free", "name": "beam-free"}]
    _mark_opencode_systemone(models, {"data": [{"id": "jev-1.14", "recommended_api": "systemone"},
                                               {"id": "beam-free", "recommended_api": "chat-completions"}]})
    assert models[0]["api"] == "systemone" and "api" not in models[1]


def test_tolerates_payload_without_catalog():
    models = [{"id": "x", "name": "x"}]
    _mark_opencode_systemone(models, {})
    assert "api" not in models[0]
