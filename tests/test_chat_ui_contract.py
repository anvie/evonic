"""Contract tests for the generated chat UI bundle.

`static/js/chat-ui.js` is generated from `static/js/chat-ui/*.js` by
`scripts/build_chat_ui.py`. Two classes of silent breakage happened before and
must not recur:

1. The bundle was hand-edited (features added directly to the generated file)
   and the next rebuild dropped them.
2. A rebuild dropped a feature the page templates still call, so the SSE
   handler threw a TypeError and the rest of the turn was never rendered.

These tests assert the bundle is exactly what the build script produces, that
every `chatUI.<method>()` call in the templates resolves to a defined method,
and that intermediate responses (the text an agent writes before a tool call)
stay inside the reasoning timeline, except for the last one before an ask_user
card, which is surfaced as a chat message so the question has visible context.
"""

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / 'static' / 'js' / 'chat-ui.js'
BUILD_SCRIPT = ROOT / 'scripts' / 'build_chat_ui.py'
TEMPLATES = ROOT / 'templates'


def _build_into(tmp_path):
    spec = importlib.util.spec_from_file_location('build_chat_ui', BUILD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path / 'chat-ui.js'
    module.OUT_FILE = str(out)
    module.build()
    return out.read_text(encoding='utf-8')


def _bundle_text():
    return BUNDLE.read_text(encoding='utf-8')


def test_bundle_matches_build_output(tmp_path):
    """The checked-in bundle must be byte-identical to a fresh build."""
    generated = _build_into(tmp_path)
    assert generated == _bundle_text(), (
        'static/js/chat-ui.js is out of sync with static/js/chat-ui/*.js. '
        'Edit the source modules, then run: python3 scripts/build_chat_ui.py'
    )


def test_templates_only_call_defined_chatui_methods():
    """Every chatUI.<method>( used by a template must exist in the bundle."""
    source = _bundle_text()
    missing = []
    for template in sorted(TEMPLATES.glob('*.html')):
        text = template.read_text(encoding='utf-8')
        for name in set(re.findall(r'chatUI\.([A-Za-z_$][\w$]*)\s*\(', text)):
            defined = re.search(
                r'(?m)^\s*(?:async\s+|static\s+)*(?:function\s+)?%s\s*\([^)]*\)\s*\{'
                % re.escape(name),
                source,
            )
            if not defined:
                missing.append(f'{template.name}: chatUI.{name}()')
    assert not missing, (
        'Templates call ChatUI methods that the bundle does not define: '
        + ', '.join(missing)
    )


def test_intermediate_responses_stay_in_the_reasoning_timeline():
    """History replay must keep intermediate text in the trail, not post it as a message."""
    source = _bundle_text()

    for name in ('splitForIntermediate', 'rememberIntermediate', 'forgetIntermediate',
                 'revealIntermediate'):
        assert re.search(r'(?m)^\s*%s\s*\(' % name, source), (
            f'ChatUI.{name}() is missing; the pages call it on every intermediate '
            f'response and on every ask_user call'
        )

    # The transport adapter also matches this entry type (it maps it to an event);
    # the history renderer is the last definition in the bundle.
    start = source.rfind("if (entry.type === 'intermediate') {")
    assert start != -1, 'history replay no longer handles intermediate entries'
    end = source.find("if (entry.type === 'final') {", start)
    assert end > start, 'unexpected history replay structure'
    block = source[start:end]

    assert 'is_final: false' in block, (
        'intermediate history entries must be ingested into the reasoning timeline'
    )
    assert 'rememberIntermediate' in block, (
        'intermediate history entries must be remembered so an ask_user card can '
        'surface the last one'
    )
    assert "appendMessage('assistant'" not in block, (
        'intermediate history entries must not be posted as chat messages; only the '
        'send_intermediate_responses branch may, via splitForIntermediate()'
    )
    assert 'send_intermediate_responses' in block, (
        'turns that forwarded their intermediate text must still replay as messages'
    )


def test_ask_user_reveals_the_last_intermediate():
    """The bundle's replay and both pages must surface the last intermediate on ask_user."""
    source = _bundle_text()
    start = source.rfind("if (entry.type === 'tool_call') {")
    assert start != -1, 'history replay no longer handles tool_call entries'
    block = source[start:start + 600]
    assert "ask_user" in block and 'revealIntermediate' in block, (
        'history replay must call revealIntermediate() when the tool call is ask_user'
    )

    for template_name in ('sessions.html', 'agent_detail.html'):
        text = (TEMPLATES / template_name).read_text(encoding='utf-8')
        reveals = [m.start() for m in re.finditer(r'revealIntermediate', text)]
        assert reveals, f'{template_name} never surfaces the last intermediate response'
        # The gate may sit on the enclosing `if`, a couple of lines up.
        assert all('ask_user' in text[max(0, at - 300):at] for at in reveals), (
            f'{template_name}: revealIntermediate() must be gated on the ask_user tool'
        )
        # Live SSE turn and JSONL history replay — both paths must do it.
        assert len(reveals) >= 2, (
            f'{template_name}: expected revealIntermediate() in both the realtime '
            f'handler and the history renderer'
        )


def _intermediate_branch(template_name):
    """Extract the `type === 'intermediate'` branch of a template's JSONL renderer."""
    text = (TEMPLATES / template_name).read_text(encoding='utf-8')
    start = text.find("type === 'intermediate') {")
    assert start != -1, f'{template_name} no longer handles intermediate entries'
    end = text.find("type === 'final') {", start)
    assert end > start, f'unexpected history renderer structure in {template_name}'
    return text[start:end]


def test_templates_render_intermediate_history_in_the_timeline():
    """The pages render JSONL history themselves; intermediates must stay in the trail."""
    for template_name in ('sessions.html', 'agent_detail.html'):
        block = _intermediate_branch(template_name)
        assert "type: 'response'" in block and 'is_final: false' in block, (
            f'{template_name}: intermediate history entries must be added to the '
            f'reasoning timeline'
        )
        assert 'rememberIntermediate' in block, (
            f'{template_name}: intermediate history entries must be remembered for ask_user'
        )
        assert 'appendMessage' not in block, (
            f'{template_name}: intermediate history entries must not be posted as chat '
            f'messages; only the send_intermediate_responses branch may, via '
            f'splitForIntermediate()'
        )
