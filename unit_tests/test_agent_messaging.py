"""
Unit tests for agent messaging tools.

Tests _exec_send_agent_message, _exec_escalate_to_user,
_exec_resolve_agent_approval, _on_final_answer, and _check_rate_limit.
All external dependencies (db, notifier, approval_registry, event_stream)
are mocked via unittest.mock.
"""
import time
import types
import unittest
import unittest.mock as mock
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# -------------------------------------------------------
# Module-level mock objects (created here so helpers below can reference them,
# but NOT injected into sys.modules until setup_module runs).
# -------------------------------------------------------
_mock_db = mock.MagicMock()
_mock_db.get_agent = mock.MagicMock()
_mock_db.get_latest_human_session = mock.MagicMock()
_mock_db.get_session_messages = mock.MagicMock()
_mock_db.get_web_fallback_session = mock.MagicMock(return_value=None)

_mock_notifier = mock.MagicMock()
_mock_notifier.notify_agent = mock.MagicMock()

_mock_approval = mock.MagicMock()
_mock_approval.approval_registry = mock.MagicMock()

# Keys whose original sys.modules values are saved in setup_module.
_STUB_KEYS = [
    'models', 'models.db',
    'backend.agent_runtime',
    'backend.agent_runtime.notifier',
    'backend.agent_runtime.approval',
]
_saved_modules: dict = {}

# Full snapshot of sys.modules taken before setup_module patches anything.
# Used in teardown_module to flush every module that was imported (and
# potentially cached a mock models.db) while this test file was active.
_original_modules: dict = {}


def setup_module(module):
    """Install sys.modules stubs before any test in this file runs.

    Moved out of module scope so other test files collected earlier/later
    do not see these stubs during pytest collection.
    """
    import backend as _backend_pkg

    # Snapshot ALL of sys.modules before touching anything — teardown uses
    # this to flush modules that may have been imported while our mocks
    # were active, preventing MagicMock leaks into subsequent test files.
    _original_modules.clear()
    _original_modules.update(sys.modules)

    # Save originals (None means absent).
    for key in _STUB_KEYS:
        _saved_modules[key] = sys.modules.get(key)

    # Evict any cached agent_messaging module so tests get a fresh import
    # with our stubs below.  Without this, test_tool_backends.py may have
    # already loaded the module (via _discover_tool_modules) during
    # collection with the real Database instance attached.
    sys.modules.pop('backend.tools.agent_messaging', None)

    # Stubs are real types.ModuleType objects, not bare MagicMocks: a MagicMock
    # raises AttributeError for unset dunders, and Python 3.14's import machinery
    # reads parent.__spec__ when resolving `from models.X import ...`. ModuleType
    # carries __spec__=None, so submodule resolution works; the behavioural mocks
    # are attached as attributes so existing assertions are unchanged.
    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    _models_db_stub = types.ModuleType('models.db')
    _models_db_stub.db = _mock_db
    sys.modules['models.db'] = _models_db_stub

    # Real __path__ so genuine submodule imports (e.g. `from models.chatlog
    # import ...`, triggered when the autouse conftest fixture imports the full
    # app) still resolve to the real files.
    _models_stub = types.ModuleType('models')
    _models_stub.__path__ = [os.path.join(_repo_root, 'models')]
    # Submodules that conftest's isolate_agent_dirs fixture monkeypatches by
    # name (models.chat / models.chatlog) must be present as attributes.
    _models_stub.chat = mock.MagicMock()
    _models_stub.chatlog = mock.MagicMock()
    sys.modules['models'] = _models_stub

    _notifier_stub = types.ModuleType('backend.agent_runtime.notifier')
    _notifier_stub.notify_agent = _mock_notifier.notify_agent
    sys.modules['backend.agent_runtime.notifier'] = _notifier_stub

    _approval_stub = types.ModuleType('backend.agent_runtime.approval')
    _approval_stub.approval_registry = _mock_approval.approval_registry
    sys.modules['backend.agent_runtime.approval'] = _approval_stub

    _ar_stub = types.ModuleType('backend.agent_runtime')
    _ar_stub.agent_runtime = mock.MagicMock()
    _ar_stub.notifier = _notifier_stub
    _ar_stub.approval = _approval_stub
    sys.modules['backend.agent_runtime'] = _ar_stub

    # mock.patch traverses the real `backend` module via getattr, so we need
    # to expose the mocked submodules as attributes on the real package object.
    _backend_pkg.agent_runtime = _ar_stub


def teardown_module(module):
    """Restore sys.modules to pre-test state so stubs don't leak."""
    import backend as _backend_pkg

    # First, restore the explicitly stubbed keys to their originals.
    for key, saved in _saved_modules.items():
        if saved is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = saved

    # Remove every module that was added to sys.modules while this test
    # file was active.  Any module imported during the test may have
    # cached a reference to the mock models.db or mock models package,
    # which would cause "MagicMock is not JSON serializable" errors in
    # later test files (e.g. test_history_api) when Flask routes call
    # db methods that return MagicMock objects.
    for key in list(sys.modules.keys()):
        if key not in _original_modules:
            del sys.modules[key]

    # Now put back the original modules that our stubs overwrote — not
    # just the explicitly listed keys, but also any module that was
    # present before setup_module and then evicted during the test.
    for key, value in _original_modules.items():
        if key not in sys.modules:
            sys.modules[key] = value

    # Restore backend.agent_runtime attribute if possible
    if 'backend.agent_runtime' in sys.modules:
        _backend_pkg.agent_runtime = sys.modules['backend.agent_runtime']
    elif hasattr(_backend_pkg, 'agent_runtime'):
        try:
            delattr(_backend_pkg, 'agent_runtime')
        except AttributeError:
            pass


# -------------------------------------------------------
# Helpers
# -------------------------------------------------------

def _make_agent_context(agent_id='agent_a', agent_name='Agent A',
                        user_id='user_123', depth=0):
    return {
        'id': agent_id,
        'name': agent_name,
        'user_id': user_id,
        'agent_message_depth': depth,
    }


def _make_target_agent(agent_id='agent_b', name='Agent B',
                       enabled=True, is_super=False):
    return {
        'id': agent_id,
        'name': name,
        'enabled': enabled,
        'is_super': is_super,
    }


# -------------------------------------------------------
# _check_rate_limit — no external deps, test directly
# -------------------------------------------------------

class TestCheckRateLimit(unittest.TestCase):
    """Test the internal rate limiter."""

    def setUp(self):
        from backend.tools.agent_messaging import _rate_limit_buckets, _check_rate_limit
        _rate_limit_buckets.clear()
        self._check_rate_limit = _check_rate_limit

    def test_allows_first_ten(self):
        """First 10 messages from same sender-target pair are allowed."""
        for i in range(10):
            self.assertTrue(
                self._check_rate_limit('agent_a', 'agent_b'),
                f"Message {i} should be allowed",
            )

    def test_blocks_eleventh(self):
        """11th message within window is blocked."""
        for _ in range(10):
            self._check_rate_limit('agent_a', 'agent_b')
        self.assertFalse(self._check_rate_limit('agent_a', 'agent_b'))

    def test_different_pair_independent(self):
        """Rate limit is per (sender, target) pair."""
        # Fill up quota for A→B
        for _ in range(10):
            self._check_rate_limit('agent_a', 'agent_b')
        # A→B should be blocked now
        self.assertFalse(self._check_rate_limit('agent_a', 'agent_b'))
        # A→C should still work (different target)
        self.assertTrue(self._check_rate_limit('agent_a', 'agent_c'))
        # C→B should still work (different sender)
        self.assertTrue(self._check_rate_limit('agent_c', 'agent_b'))

    def test_prunes_old_entries(self):
        """Entries older than _RATE_LIMIT_WINDOW are pruned."""
        from backend.tools.agent_messaging import _rate_limit_buckets, _RATE_LIMIT_WINDOW

        # Insert 10 old timestamps
        old = time.time() - _RATE_LIMIT_WINDOW - 10
        _rate_limit_buckets[('agent_a', 'agent_b')] = [old] * 10

        # Next call should prune them and allow
        self.assertTrue(self._check_rate_limit('agent_a', 'agent_b'))


# -------------------------------------------------------
# _exec_send_agent_message
# -------------------------------------------------------

class TestExecSendAgentMessage(unittest.TestCase):
    """Test _exec_send_agent_message with mocked db and notifier."""

    def setUp(self):
        from backend.tools.agent_messaging import (
            _rate_limit_buckets, _global_rate_limit_buckets, _fanout_buckets,
        )
        _rate_limit_buckets.clear()
        _global_rate_limit_buckets.clear()
        _fanout_buckets.clear()

    def _call(self, args, agent_context=None):
        from backend.tools.agent_messaging import _exec_send_agent_message
        ctx = agent_context or _make_agent_context()
        return _exec_send_agent_message(args, ctx)

    def test_success(self):
        """Sending to a valid, enabled agent returns success + reply_to_id."""
        target = _make_target_agent()
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=target), \
             mock.patch('backend.agent_runtime.notifier.notify_agent') as mock_notify:
            result = self._call({
                'target_agent_id': 'agent_b',
                'message': 'Hello from A!',
            })
        self.assertTrue(result.get('success'))
        self.assertIn('reply_to_id', result)
        self.assertIn('Message sent', result.get('message', ''))
        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        meta = call_kwargs.get('metadata', {})
        self.assertTrue(meta.get('agent_message'))
        self.assertEqual(meta.get('from_agent_id'), 'agent_a')
        self.assertEqual(meta.get('report_to_id'), 'user_123')

    def test_self_messaging_blocked(self):
        """Agent cannot message itself."""
        result = self._call(
            {'target_agent_id': 'agent_a', 'message': 'Hi me'},
            _make_agent_context(agent_id='agent_a'),
        )
        self.assertIn('error', result)
        self.assertIn('cannot send a message to itself', result['error'])

    def test_target_not_found(self):
        """Non-existent target returns error."""
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=None):
            result = self._call({
                'target_agent_id': 'ghost',
                'message': 'Hello?',
            })
        self.assertIn('error', result)
        self.assertIn('not found', result['error'])

    def test_target_disabled(self):
        """Disabled target returns error (unless super)."""
        target = _make_target_agent(enabled=False, is_super=False)
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=target):
            result = self._call({
                'target_agent_id': 'agent_b',
                'message': 'Hello?',
            })
        self.assertIn('error', result)
        self.assertIn('disabled', result['error'])

    def test_target_super_always_allowed(self):
        """Super agent is allowed even if enabled=False."""
        target = _make_target_agent(enabled=False, is_super=True)
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=target), \
             mock.patch('backend.agent_runtime.notifier.notify_agent'):
            result = self._call({
                'target_agent_id': 'agent_b',
                'message': 'Hello super!',
            })
        self.assertTrue(result.get('success'))

    def test_empty_target_id(self):
        """Missing target_agent_id returns error."""
        result = self._call({'target_agent_id': '', 'message': 'Hi'})
        self.assertIn('error', result)
        self.assertIn('target_agent_id', result['error'])

    def test_empty_message(self):
        """Missing message returns error."""
        result = self._call({'target_agent_id': 'agent_b', 'message': '  '})
        self.assertIn('error', result)
        self.assertIn('message', result['error'])

    def test_rate_limit_exceeded(self):
        """11th message within window is blocked."""
        target = _make_target_agent()
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=target), \
             mock.patch('backend.agent_runtime.notifier.notify_agent'):
            for i in range(10):
                result = self._call({
                    'target_agent_id': 'agent_b',
                    'message': f'Msg {i}',
                })
                self.assertTrue(result.get('success'), f'Message {i} should succeed')

            result = self._call({
                'target_agent_id': 'agent_b',
                'message': 'Msg 11',
            })
        self.assertIn('error', result)
        self.assertIn('Rate limit', result['error'])

    def test_depth_limit(self):
        """Messages at max depth are blocked."""
        target = _make_target_agent()
        ctx = _make_agent_context(depth=3)  # _MAX_DEPTH = 3
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=target):
            result = self._call({
                'target_agent_id': 'agent_b',
                'message': 'Deep msg',
            }, agent_context=ctx)
        self.assertIn('error', result)
        self.assertIn('depth', result['error'].lower())

    def test_reply_back_to_sender_blocked(self):
        """B cannot send_agent_message back to A when A is from_agent_id."""
        target = _make_target_agent(agent_id='agent_a', name='Agent A')
        ctx = {
            'id': 'agent_b',
            'name': 'Agent B',
            'user_id': '__agent__agent_a',
            'agent_message_depth': 1,
            'from_agent_id': 'agent_a',  # A delegated this task to B
        }
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=target):
            result = self._call(
                {'target_agent_id': 'agent_a', 'message': 'Hey A, I have a question'},
                agent_context=ctx,
            )
        self.assertIn('error', result)
        self.assertIn('forwarded back', result['error'])

    def test_reply_back_blocked_does_not_call_notifier(self):
        """When reply-back is blocked, notify_agent must not be called."""
        target = _make_target_agent(agent_id='agent_a', name='Agent A')
        ctx = {
            'id': 'agent_b',
            'name': 'Agent B',
            'user_id': '__agent__agent_a',
            'agent_message_depth': 1,
            'from_agent_id': 'agent_a',
        }
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=target), \
             mock.patch('backend.agent_runtime.notifier.notify_agent') as mock_notify:
            self._call(
                {'target_agent_id': 'agent_a', 'message': 'Hey A'},
                agent_context=ctx,
            )
        mock_notify.assert_not_called()

    def test_send_to_third_party_allowed_when_from_agent_set(self):
        """B can still send to C even when A is from_agent_id (only reply-back is blocked)."""
        target_c = _make_target_agent(agent_id='agent_c', name='Agent C')
        ctx = {
            'id': 'agent_b',
            'name': 'Agent B',
            'user_id': '__agent__agent_a',
            'agent_message_depth': 1,
            'from_agent_id': 'agent_a',
        }
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=target_c), \
             mock.patch('backend.agent_runtime.notifier.notify_agent'):
            result = self._call(
                {'target_agent_id': 'agent_c', 'message': 'Hey C, help needed'},
                agent_context=ctx,
            )
        self.assertTrue(result.get('success'))

    def test_no_from_agent_id_does_not_restrict_target(self):
        """Without from_agent_id in context, no reply-back restriction applies."""
        target = _make_target_agent(agent_id='agent_a', name='Agent A')
        ctx = _make_agent_context(agent_id='agent_b', user_id='user_xyz')
        # from_agent_id is NOT set — should be allowed
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=target), \
             mock.patch('backend.agent_runtime.notifier.notify_agent'):
            result = self._call(
                {'target_agent_id': 'agent_a', 'message': 'Direct msg to A'},
                agent_context=ctx,
            )
        self.assertTrue(result.get('success'))

    # -------------------------------------------------------
    # injected_system_vars validation
    # -------------------------------------------------------

    def test_injected_system_vars_valid_keys_accepted(self):
        """Valid injected_system_vars keys are accepted and passed through metadata."""
        target = _make_target_agent()
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=target), \
            mock.patch('backend.agent_runtime.notifier.notify_agent') as mock_notify:
            result = self._call({
                'target_agent_id': 'agent_b',
                'message': 'Hello',
                'injected_system_vars': {
                    'query': 'hello',
                    'my_var': 'test',
                    '_private': 'ok',
                },
            })
        self.assertTrue(result.get('success'))
        meta = mock_notify.call_args.kwargs.get('metadata', {})
        injected = meta.get('injected_system_vars', {})
        self.assertEqual(injected.get('query'), 'hello')
        self.assertEqual(injected.get('my_var'), 'test')
        self.assertEqual(injected.get('_private'), 'ok')

    def test_injected_system_vars_reserved_key_time_rejected(self):
        """Reserved key time is rejected."""
        result = self._call({
            'target_agent_id': 'agent_b',
            'message': 'Hello',
            'injected_system_vars': {'time': 'xxx'},
        })
        self.assertIn('error', result)
        self.assertIn('reserved', result['error'])
        self.assertIn('time', result['error'])

    def test_injected_system_vars_reserved_key_date_rejected(self):
        """Reserved key date is rejected."""
        result = self._call({
            'target_agent_id': 'agent_b',
            'message': 'Hello',
            'injected_system_vars': {'date': 'xxx'},
        })
        self.assertIn('error', result)
        self.assertIn('reserved', result['error'])

    def test_injected_system_vars_reserved_key_day_rejected(self):
        """Reserved key day is rejected."""
        result = self._call({
            'target_agent_id': 'agent_b',
            'message': 'Hello',
            'injected_system_vars': {'day': 'xxx'},
        })
        self.assertIn('error', result)
        self.assertIn('reserved', result['error'])

    def test_injected_system_vars_invalid_key_starts_with_digit(self):
        """Key starting with digit 123bad is rejected."""
        result = self._call({
            'target_agent_id': 'agent_b',
            'message': 'Hello',
            'injected_system_vars': {'123bad': 'x'},
        })
        self.assertIn('error', result)
        self.assertIn('invalid key', result['error'])

    def test_injected_system_vars_invalid_key_has_space(self):
        """Key with space is rejected."""
        result = self._call({
            'target_agent_id': 'agent_b',
            'message': 'Hello',
            'injected_system_vars': {'has space': 'x'},
        })
        self.assertIn('error', result)
        self.assertIn('invalid key', result['error'])

    def test_injected_system_vars_invalid_key_has_dash(self):
        """Key with dash is rejected."""
        result = self._call({
            'target_agent_id': 'agent_b',
            'message': 'Hello',
            'injected_system_vars': {'has-dash': 'x'},
        })
        self.assertIn('error', result)
        self.assertIn('invalid key', result['error'])

    def test_injected_system_vars_too_many_keys_rejected(self):
        """More than 10 keys is rejected."""
        many_keys = {f'key{i}': f'val{i}' for i in range(11)}
        result = self._call({
            'target_agent_id': 'agent_b',
            'message': 'Hello',
            'injected_system_vars': many_keys,
        })
        self.assertIn('error', result)
        self.assertIn('maximum 10 keys', result['error'])

    def test_injected_system_vars_value_too_long_rejected(self):
        """Value exceeding 1024 characters is rejected."""
        long_value = 'x' * 1025
        result = self._call({
            'target_agent_id': 'agent_b',
            'message': 'Hello',
            'injected_system_vars': {'key': long_value},
        })
        self.assertIn('error', result)
        self.assertIn('exceeds 1024', result['error'])

    def test_injected_system_vars_empty_dict_accepted(self):
        """Empty injected_system_vars dict no-ops gracefully."""
        target = _make_target_agent()
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=target), \
            mock.patch('backend.agent_runtime.notifier.notify_agent') as mock_notify:
            result = self._call({
                'target_agent_id': 'agent_b',
                'message': 'Hello',
                'injected_system_vars': {},
            })
        self.assertTrue(result.get('success'))
        meta = mock_notify.call_args.kwargs.get('metadata', {})
        self.assertIsNone(meta.get('injected_system_vars'))

    def test_injected_system_vars_none_passed_accepted(self):
        """None passed as injected_system_vars no-ops gracefully."""
        target = _make_target_agent()
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=target), \
            mock.patch('backend.agent_runtime.notifier.notify_agent') as mock_notify:
            result = self._call({
                'target_agent_id': 'agent_b',
                'message': 'Hello',
                'injected_system_vars': None,
            })
        self.assertTrue(result.get('success'))
        meta = mock_notify.call_args.kwargs.get('metadata', {})
        self.assertIsNone(meta.get('injected_system_vars'))

    def test_injected_system_vars_not_passed_accepted(self):
        """Not passing injected_system_vars at all no-ops gracefully."""
        target = _make_target_agent()
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=target), \
            mock.patch('backend.agent_runtime.notifier.notify_agent') as mock_notify:
            result = self._call({
                'target_agent_id': 'agent_b',
                'message': 'Hello',
            })
        self.assertTrue(result.get('success'))
        meta = mock_notify.call_args.kwargs.get('metadata', {})
        self.assertIsNone(meta.get('injected_system_vars'))


# -------------------------------------------------------
# Metadata injection
# -------------------------------------------------------

class TestMetadataInjection(unittest.TestCase):
    """Verify metadata fields on successful send_agent_message calls."""

    def setUp(self):
        from backend.tools.agent_messaging import (
            _rate_limit_buckets, _global_rate_limit_buckets, _fanout_buckets,
        )
        _rate_limit_buckets.clear()
        _global_rate_limit_buckets.clear()
        _fanout_buckets.clear()

    def _call(self, args, agent_context=None):
        from backend.tools.agent_messaging import _exec_send_agent_message
        ctx = agent_context or _make_agent_context()
        return _exec_send_agent_message(args, ctx)

    def test_reply_to_id_is_unique(self):
        """Each call generates a unique reply_to_id."""
        target = _make_target_agent()
        ids = set()
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=target), \
             mock.patch('backend.agent_runtime.notifier.notify_agent'):
            for _ in range(5):
                result = self._call({'target_agent_id': 'agent_b', 'message': 'Hi'})
                ids.add(result['reply_to_id'])
        self.assertEqual(len(ids), 5, "Each call must produce a unique reply_to_id")

    def test_report_to_id_from_context(self):
        """report_to_id is taken from agent_context['user_id']."""
        target = _make_target_agent()
        ctx = _make_agent_context(user_id='custom_user_42')
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=target), \
             mock.patch('backend.agent_runtime.notifier.notify_agent') as mock_notify:
            self._call({'target_agent_id': 'agent_b', 'message': 'Hi'}, agent_context=ctx)
        meta = mock_notify.call_args.kwargs.get('metadata', {})
        self.assertEqual(meta.get('report_to_id'), 'custom_user_42')

    def test_depth_incremented(self):
        """agent_message_depth is incremented by 1 in metadata."""
        target = _make_target_agent()
        ctx = _make_agent_context(depth=2)
        with mock.patch('backend.tools.agent_messaging.db.get_agent', return_value=target), \
             mock.patch('backend.agent_runtime.notifier.notify_agent') as mock_notify:
            self._call({'target_agent_id': 'agent_b', 'message': 'Hi'}, agent_context=ctx)
        meta = mock_notify.call_args.kwargs.get('metadata', {})
        self.assertEqual(meta.get('agent_message_depth'), 3)


# -------------------------------------------------------
# _exec_escalate_to_user
# -------------------------------------------------------

class TestExecEscalateToUser(unittest.TestCase):
    """Test _exec_escalate_to_user with mocked db and notifier."""

    def _call(self, args, agent_context=None):
        from backend.tools.agent_messaging import _exec_escalate_to_user
        ctx = agent_context or _make_agent_context(
            user_id='__agent__agent_a'  # inter-agent session
        )
        return _exec_escalate_to_user(args, ctx)

    def test_success(self):
        """Escalate in inter-agent session with valid human session."""
        human_session = {'external_user_id': 'user_123', 'channel_id': 'ch1'}
        with mock.patch('backend.tools.agent_messaging.db.get_latest_human_session',
                        return_value=human_session), \
             mock.patch('backend.tools.agent_messaging.db.get_web_fallback_session',
                        return_value=None), \
             mock.patch('backend.agent_runtime.notifier.notify_agent',
                       return_value={'success': True}) as mock_notify:
            result = self._call({'message': 'User, I need your approval.'})
        self.assertTrue(result.get('success'))
        self.assertIn('forwarded', result.get('message', ''))
        mock_notify.assert_called_once()
        self.assertEqual(
            mock_notify.call_args.kwargs.get('external_user_id'),
            'user_123',
        )

    def test_already_in_user_session(self):
        """Block escalate when not in inter-agent session."""
        ctx = _make_agent_context(user_id='user_123')  # normal user session
        result = self._call({'message': 'Hello?'}, agent_context=ctx)
        self.assertIn('error', result)
        self.assertIn('user session', result['error'])

    def test_no_human_session(self):
        """Block escalate when no human session exists."""
        with mock.patch('backend.tools.agent_messaging.db.get_latest_human_session',
                        return_value=None):
            result = self._call({'message': 'Hello?'})
        self.assertIn('error', result)
        self.assertIn('No active human', result['error'])


# -------------------------------------------------------
# _exec_resolve_agent_approval
# -------------------------------------------------------

class TestExecResolveAgentApproval(unittest.TestCase):
    """Test _exec_resolve_agent_approval with mocked approval_registry."""

    def _call(self, args, agent_context=None):
        from backend.tools.agent_messaging import _exec_resolve_agent_approval
        ctx = agent_context or _make_agent_context()
        return _exec_resolve_agent_approval(args, ctx)

    def test_approve_success(self):
        """Approve a pending approval returns success."""
        mock_pa = mock.MagicMock()
        mock_pa.decision = None
        mock_pa.session_id = 'sess_001'
        with mock.patch(
            'backend.agent_runtime.approval.approval_registry.get',
            return_value=mock_pa,
        ), mock.patch(
            'backend.agent_runtime.approval.approval_registry.resolve',
            return_value=True,
        ):
            result = self._call({
                'approval_id': 'apr_001',
                'decision': 'approve',
            })
        self.assertTrue(result.get('success'))
        self.assertEqual(result.get('decision'), 'approve')
        self.assertIn('approved', result.get('message', ''))

    def test_reject_success(self):
        """Reject a pending approval returns success."""
        mock_pa = mock.MagicMock()
        mock_pa.decision = None
        mock_pa.session_id = 'sess_002'
        with mock.patch(
            'backend.agent_runtime.approval.approval_registry.get',
            return_value=mock_pa,
        ), mock.patch(
            'backend.agent_runtime.approval.approval_registry.resolve',
            return_value=True,
        ):
            result = self._call({
                'approval_id': 'apr_002',
                'decision': 'reject',
            })
        self.assertTrue(result.get('success'))
        self.assertEqual(result.get('decision'), 'reject')

    def test_already_resolved(self):
        """Already resolved approval returns error."""
        mock_pa = mock.MagicMock()
        mock_pa.decision = 'approve'  # already resolved
        with mock.patch(
            'backend.agent_runtime.approval.approval_registry.get',
            return_value=mock_pa,
        ):
            result = self._call({
                'approval_id': 'apr_003',
                'decision': 'reject',
            })
        self.assertIn('error', result)
        self.assertIn('already resolved', result['error'])

    def test_not_found(self):
        """Non-existent/expired approval returns error."""
        with mock.patch(
            'backend.agent_runtime.approval.approval_registry.get',
            return_value=None,
        ):
            result = self._call({
                'approval_id': 'apr_ghost',
                'decision': 'approve',
            })
        self.assertIn('error', result)
        self.assertIn('not found', result['error'])

    def test_could_not_resolve(self):
        """Resolve returns False (just expired) returns error."""
        mock_pa = mock.MagicMock()
        mock_pa.decision = None
        with mock.patch(
            'backend.agent_runtime.approval.approval_registry.get',
            return_value=mock_pa,
        ), mock.patch(
            'backend.agent_runtime.approval.approval_registry.resolve',
            return_value=False,
        ):
            result = self._call({
                'approval_id': 'apr_004',
                'decision': 'approve',
            })
        self.assertIn('error', result)
        self.assertIn('Could not resolve', result['error'])

    def test_empty_approval_id(self):
        """Missing approval_id returns error."""
        result = self._call({'approval_id': '', 'decision': 'approve'})
        self.assertIn('error', result)
        self.assertIn('approval_id', result['error'])

    def test_invalid_decision(self):
        """Invalid decision value returns error."""
        result = self._call({'approval_id': 'apr_001', 'decision': 'maybe'})
        self.assertIn('error', result)
        self.assertIn('decision', result['error'])


# -------------------------------------------------------
# _on_final_answer
# -------------------------------------------------------

class TestOnFinalAnswer(unittest.TestCase):
    """Test _on_final_answer auto-forward with mocked db and notifier."""

    def _call(self, data):
        from backend.tools.agent_messaging import _on_final_answer
        return _on_final_answer(data)

    def _build_data(self, external_user_id='__agent__agent_a',
                    agent_id='agent_b', session_id='sess_001',
                    answer='Hello back!', tool_trace=None,
                    message_metadata=None):
        return {
            'external_user_id': external_user_id,
            'agent_id': agent_id,
            'session_id': session_id,
            'answer': answer,
            'tool_trace': tool_trace,
            '_test_metadata': message_metadata,  # used below in mock
        }

    def test_non_inter_agent_skips(self):
        """Normal user session — no forwarding."""
        with mock.patch('backend.agent_runtime.notifier.notify_agent') as mock_notify:
            self._call(self._build_data(external_user_id='user_123'))
        mock_notify.assert_not_called()

    def test_no_report_to_id_skips(self):
        """Metadata without report_to_id — no forwarding."""
        messages = [
            {'metadata': {'from_agent_id': 'agent_a'}},  # no report_to_id
        ]
        with mock.patch(
            'backend.tools.agent_messaging.db.get_session_messages',
            return_value=messages,
        ), mock.patch('backend.agent_runtime.notifier.notify_agent') as mock_notify:
            self._call(self._build_data())
        mock_notify.assert_not_called()

    def test_inter_agent_forwards_reply(self):
        """Inter-agent session with valid metadata forwards reply to A."""
        messages = [
            {'metadata': {
                'from_agent_id': 'agent_a',
                'report_to_id': 'user_123',
            }},
        ]
        target_agent = _make_target_agent(agent_id='agent_b', name='Agent B')
        with mock.patch(
            'backend.tools.agent_messaging.db.get_session_messages',
            return_value=messages,
        ), mock.patch(
            'backend.tools.agent_messaging.db.get_agent',
            return_value=target_agent,
        ), mock.patch(
            'backend.agent_runtime.notifier.notify_agent',
        ) as mock_notify:
            self._call(self._build_data(
                answer='Hello from B!',
            ))
        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        self.assertEqual(kwargs.get('agent_id'), 'agent_a')
        self.assertEqual(kwargs.get('external_user_id'), 'user_123')
        self.assertTrue(kwargs.get('trigger_llm'))

    def test_depth_preserved_in_forwarded_metadata(self):
        """_on_final_answer preserves agent_message_depth from original message in forwarded metadata."""
        messages = [
            {'metadata': {
                'from_agent_id': 'agent_a',
                'report_to_id': 'user_123',
                'agent_message_depth': 2,  # depth A→B was 2
            }},
        ]
        target_agent = _make_target_agent(agent_id='agent_b', name='Agent B')
        with mock.patch(
            'backend.tools.agent_messaging.db.get_session_messages',
            return_value=messages,
        ), mock.patch(
            'backend.tools.agent_messaging.db.get_agent',
            return_value=target_agent,
        ), mock.patch(
            'backend.agent_runtime.notifier.notify_agent',
        ) as mock_notify:
            self._call(self._build_data(answer='Task done!'))
        mock_notify.assert_called_once()
        forwarded_meta = mock_notify.call_args.kwargs.get('metadata', {})
        self.assertEqual(
            forwarded_meta.get('agent_message_depth'), 2,
            "Forwarded metadata must carry original depth so A's context is correct",
        )

    def test_depth_defaults_to_zero_when_missing(self):
        """_on_final_answer defaults agent_message_depth to 0 if not in original metadata."""
        messages = [
            {'metadata': {
                'from_agent_id': 'agent_a',
                'report_to_id': 'user_123',
                # agent_message_depth intentionally absent
            }},
        ]
        target_agent = _make_target_agent(agent_id='agent_b', name='Agent B')
        with mock.patch(
            'backend.tools.agent_messaging.db.get_session_messages',
            return_value=messages,
        ), mock.patch(
            'backend.tools.agent_messaging.db.get_agent',
            return_value=target_agent,
        ), mock.patch(
            'backend.agent_runtime.notifier.notify_agent',
        ) as mock_notify:
            self._call(self._build_data(answer='Done!'))
        mock_notify.assert_called_once()
        forwarded_meta = mock_notify.call_args.kwargs.get('metadata', {})
        self.assertEqual(forwarded_meta.get('agent_message_depth'), 0)


# -------------------------------------------------------
# _check_global_rate_limit
# -------------------------------------------------------

class TestCheckGlobalRateLimit(unittest.TestCase):
    """Test the global rate limiter (across all targets)."""

    def setUp(self):
        from backend.tools.agent_messaging import (
            _global_rate_limit_buckets,
            _check_global_rate_limit,
        )
        _global_rate_limit_buckets.clear()
        self._check_global_rate_limit = _check_global_rate_limit

    def test_allows_first_30(self):
        """First 30 messages from sender are allowed."""
        for i in range(30):
            self.assertTrue(
                self._check_global_rate_limit('agent_a'),
                f"Message {i} should be allowed",
            )

    def test_blocks_31st(self):
        """31st message within window is blocked."""
        for _ in range(30):
            self._check_global_rate_limit('agent_a')
        self.assertFalse(self._check_global_rate_limit('agent_a'))

    def test_prunes_old_entries(self):
        """Entries older than window are pruned from bucket."""
        from backend.tools.agent_messaging import (
            _global_rate_limit_buckets,
            _GLOBAL_RATE_LIMIT_WINDOW,
        )
        # Fill with 30 old timestamps
        old = time.time() - _GLOBAL_RATE_LIMIT_WINDOW - 10
        _global_rate_limit_buckets['agent_a'] = [old] * 30
        # Next call should prune and allow
        self.assertTrue(self._check_global_rate_limit('agent_a'))

    def test_different_senders_independent(self):
        """Global rate limit is per sender, not global across all."""
        # Fill up agent_a
        for _ in range(30):
            self._check_global_rate_limit('agent_a')
        # agent_a blocked
        self.assertFalse(self._check_global_rate_limit('agent_a'))
        # agent_b should still work
        self.assertTrue(self._check_global_rate_limit('agent_b'))


# -------------------------------------------------------
# _check_fanout_limit
# -------------------------------------------------------

class TestCheckFanoutLimit(unittest.TestCase):
    """Test the fan-out limiter (unique targets per turn window)."""

    def setUp(self):
        from backend.tools.agent_messaging import (
            _fanout_buckets,
            _check_fanout_limit,
        )
        _fanout_buckets.clear()
        self._check_fanout_limit = _check_fanout_limit

    def test_allows_up_to_5_unique_targets(self):
        """Up to 5 unique targets in the window are allowed."""
        for i in range(5):
            target = f'agent_{i}'
            self.assertTrue(
                self._check_fanout_limit('agent_a', target),
                f"Target {target} should be allowed",
            )

    def test_blocks_6th_unique_target(self):
        """6th unique target within the window is blocked."""
        for i in range(5):
            self._check_fanout_limit('agent_a', f'agent_{i}')
        self.assertFalse(self._check_fanout_limit('agent_a', 'agent_5'))

    def test_prunes_old_entries(self):
        """Targets older than window are pruned, freeing up slots."""
        from backend.tools.agent_messaging import (
            _fanout_buckets,
            _FANOUT_WINDOW,
        )
        # Fill with 5 old entries (past window)
        old = time.time() - _FANOUT_WINDOW - 10
        _fanout_buckets['agent_a'] = [(old, f'agent_{i}') for i in range(5)]
        # Next 5 unique targets should all be allowed since old ones pruned
        for i in range(5):
            self.assertTrue(
                self._check_fanout_limit('agent_a', f'new_target_{i}'),
                f"new_target_{i} should be allowed after prune",
            )

    def test_same_target_twice_counts_once(self):
        """Messaging the same target twice only counts as 1 unique target."""
        # Send to 4 unique targets
        for i in range(4):
            self._check_fanout_limit('agent_a', f'agent_{i}')
        # Send to same target again — should still count as 1 unique
        self.assertTrue(self._check_fanout_limit('agent_a', 'agent_0'))
        # Now send to 5th unique target — should be allowed (only 5 unique so far)
        self.assertTrue(self._check_fanout_limit('agent_a', 'agent_new'))
        # But 6th unique target — blocked
        self.assertFalse(self._check_fanout_limit('agent_a', 'agent_6th'))

    def test_different_senders_independent(self):
        """Fan-out limit is per sender."""
        # Fill up agent_a
        for i in range(5):
            self._check_fanout_limit('agent_a', f'agent_a_target_{i}')
        # agent_a blocked for new targets
        self.assertFalse(self._check_fanout_limit('agent_a', 'agent_a_target_new'))
        # agent_b still has clean slate
        self.assertTrue(self._check_fanout_limit('agent_b', 'agent_b_target_1'))




# -------------------------------------------------------
# injected_system_vars expansion
# -------------------------------------------------------

class TestInjectedVarsExpansion(unittest.TestCase):
    """Test that injected system vars expand correctly in prompts."""

    def _expand(self, prompt, vars_dict):
        """Simulate the expansion logic from build_system_prompt."""
        if vars_dict:
            for key, value in vars_dict.items():
                prompt = prompt.replace('{{' + key + '}}', str(value))
        return prompt

    def test_expansion_replaces_placeholders(self):
        """Injected vars replace placeholder syntax in the system prompt."""
        prompt = 'Hello {{name}}! Your query is: {{query}}. Unchanged.'
        result = self._expand(prompt, {'name': 'World', 'query': 'find docs'})
        self.assertIn('Hello World!', result)
        self.assertIn('Your query is: find docs.', result)
        self.assertIn('Unchanged.', result)
        self.assertNotIn('{{name}}', result)
        self.assertNotIn('{{query}}', result)

    def test_expansion_no_vars_leaves_placeholders(self):
        """Without injected vars, placeholder syntax remains as-is."""
        prompt = 'Unresolved: {{missing_var}}.'
        result = self._expand(prompt, None)
        self.assertIn('{{missing_var}}', result)
        self.assertIn('Unresolved:', result)

    def test_expansion_multiple_occurrences(self):
        """Same placeholder appearing multiple times is replaced everywhere."""
        prompt = '{{x}} and {{x}} again.'
        result = self._expand(prompt, {'x': 'val'})
        self.assertEqual(result, 'val and val again.')

    def test_expansion_value_coerced_to_string(self):
        """Non-string values are converted to string."""
        prompt = 'Count: {{n}}.'
        result = self._expand(prompt, {'n': 42})
        self.assertEqual(result, 'Count: 42.')

    def test_expansion_unknown_placeholders_unchanged(self):
        """Placeholders without a matching var are left as-is."""
        prompt = 'Known: {{a}}. Unknown: {{b}}.'
        result = self._expand(prompt, {'a': 'X'})
        self.assertIn('Known: X.', result)
        self.assertIn('{{b}}', result)



if __name__ == '__main__':
    unittest.main()
