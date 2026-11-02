"""Static assertions for the thinking-timeline safety badges.

The chat UI ships twice: as ES modules under static/js/chat-ui/ and as the
generated bundle static/js/chat-ui.js that the templates actually load.  Both
must expose the badge, and the templates must bust the cache when they change.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as fh:
        return fh.read()


def test_badge_module_exists_and_exports_api():
    src = _read('static', 'js', 'chat-ui', 'badges.js')
    for name in ('export function describeBadge', 'export function buildBadge',
                 'export function applyBadge', 'export function approvalLabel'):
        assert name in src, name
    assert 'safety-badge' in src
    # Every decision source must have a distinct human-readable label.
    for label in ('DMSS', 'Rules', 'You approved', 'You declined', 'Timed out'):
        assert label in src, label


def test_turn_renders_badge_and_decision_maker():
    src = _read('static', 'js', 'chat-ui', 'turn.js')
    assert 'applyBadge($entry, safety, approval)' in src
    assert '_applySafetyBadge($target, data.safety, data.approval)' in src
    assert 'data.decided_by' in src
    assert "'user:web'" in src


def test_bundle_is_built_from_the_modules():
    build = _read('scripts', 'build_chat_ui.py')
    assert "'badges.js'" in build
    bundle = _read('static', 'js', 'chat-ui.js')
    for marker in ('safety-badge', 'describeBadge', 'approvalLabel',
                   '_applySafetyBadge', 'data-decided-by'):
        assert marker in bundle, marker


def test_replay_forwards_safety_provenance():
    for parts in (('static', 'js', 'chat-ui.js'),
                  ('static', 'js', 'chat-ui', 'transport.js'),
                  ('static', 'js', 'chat-ui', 'index.js')):
        src = _read(*parts)
        assert 'entry.safety' in src, parts
        assert 'entry.approval' in src, parts


def test_cache_buster_bumped():
    for template in ('agent_detail.html', 'sessions.html'):
        src = _read('templates', template)
        assert "filename='js/chat-ui.js') }}?v=77" in src, template
