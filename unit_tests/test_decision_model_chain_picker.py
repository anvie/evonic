"""Regression tests for the "Default decision models" chain picker.

The shared ModelChain component used to strip every model whose ``type`` is
``decision`` before rendering, so the decision chain's picker listed only chat
LLMs — all of them ineligible and therefore disabled/unclickable. The chain now
filters models by an explicit ``kind`` so the decision chain lists decision
models (e.g. a SystemOne model) and the LLM chains keep listing chat LLMs.
"""

import re
from pathlib import Path

from app import app

ROOT = Path(__file__).resolve().parents[1]


def _rendered_settings() -> str:
    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client.get("/system").get_data(as_text=True)


def _root_block(html: str, chain_id: str) -> str:
    """The opening tag of one model-chain root, attributes included."""
    start = html.index('id="%s"' % chain_id)
    return html[start:start + 700]


def test_decision_chain_root_declares_the_decision_kind():
    html = _rendered_settings()
    block = _root_block(html, "decision-model-chain")
    assert 'data-kind="decision"' in block
    assert 'data-fallbacks="4"' in block
    # The LLM chains must keep their default (no decision models listed).
    assert "data-kind" not in _root_block(html, "default-model-chain")
    assert "data-kind" not in _root_block(html, "vision-model-chain")


def test_model_chain_filters_models_by_kind():
    source = (ROOT / "static/js/model-chain.js").read_text(encoding="utf-8")
    assert 'this.kind = opts.kind || data.kind || "llm";' in source
    # The old unconditional decision-model filter must be gone.
    assert '.filter((m) => m.type !== "decision")' not in source
    assert '(m.type === "decision" ? "decision" : "llm") === kind' in source
    # Empty-state wording follows the kind instead of always saying "models".
    assert 'this.kind === "decision" ? "decision models" : "models"' in source


def test_decision_chain_instantiates_the_component_with_the_decision_kind():
    source = (ROOT / "static/js/settings-general.js").read_text(encoding="utf-8")
    match = re.search(r"    _getDecisionChain\(\) \{[\s\S]*?\n    \},", source)
    assert match, "decision chain initialiser not found"
    assert "kind: \"decision\"" in match.group(0)
    # The chain still only accepts enabled decision models.
    assert 'model.type === "decision"' in match.group(0)
