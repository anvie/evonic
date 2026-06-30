"""Regression test for LM Studio provider entry in PROVIDER_DEFAULTS.

The setup wizard's provider dropdown (CLI `evonic setup` and `/api/setup`)
is sourced from `backend.setup.PROVIDER_DEFAULTS`. If `lmstudio` is missing,
users with LM Studio running locally are forced to pick "Custom" and type
the base URL by hand — a process that has historically produced malformed
URLs (e.g. `http://localhost:1234/api/v1/` doubling the `/v1` segment and
breaking `LLMClient`'s request URL construction).

This test pins the LM Studio entry's shape so that:

  1. The provider is selectable in the wizard.
  2. The base_url points at the canonical OpenAI-compatible LM Studio
     server endpoint (port 1234, `/v1`).
  3. No API key is required (LM Studio's local server is unauthenticated
     by default).
  4. The local-server discovery path (`test_connection`) works against a
     real LM Studio server when one is running. The test is skipped
     gracefully when the server is unreachable, so CI in environments
     without LM Studio still passes.
"""
import importlib.util
import os
import unittest
from unittest import mock

_SETUP_PY = os.path.join(
    os.path.dirname(__file__), '..', 'backend', 'setup.py'
)


def _load_setup_module():
    """Load backend/setup.py with its `config` and `models.db` deps stubbed.

    The module imports `config` and `models.db` at the top level, so we
    have to inject lightweight stubs into sys.modules before loading it
    from source. We only need PROVIDER_DEFAULTS and test_connection() to
    be importable, so the stubs can be minimal.
    """
    import sys
    import types

    if 'config' not in sys.modules:
        config_stub = types.ModuleType('config')
        # type: ignore[attr-defined]
        config_stub.BASE_DIR = os.path.dirname(_SETUP_PY)
        sys.modules['config'] = config_stub

    if 'models' not in sys.modules:
        sys.modules['models'] = types.ModuleType('models')

    if 'models.db' not in sys.modules:
        db_stub = types.ModuleType('models.db')
        # type: ignore[attr-defined]
        db_stub.db = mock.MagicMock()
        sys.modules['models.db'] = db_stub

    spec = importlib.util.spec_from_file_location('_setup_under_test', _SETUP_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_setup = _load_setup_module()
PROVIDER_DEFAULTS = _setup.PROVIDER_DEFAULTS
test_connection = _setup.test_connection


class TestLMStudioProviderEntry(unittest.TestCase):
    """Pins the LM Studio entry in PROVIDER_DEFAULTS."""

    def test_lmstudio_is_a_registered_provider(self):
        """The wizard's provider list must include 'lmstudio'."""
        self.assertIn(
            'lmstudio', PROVIDER_DEFAULTS,
            "PROVIDER_DEFAULTS must include an 'lmstudio' entry so the "
            "setup wizard exposes LM Studio as a first-class option. "
            "Without it, users are forced into 'Custom' which has a "
            "history of producing malformed base_url values "
            "(e.g. 'http://localhost:1234/api/v1/' that double '/v1').",
        )

    def test_lmstudio_base_url_uses_canonical_v1_endpoint(self):
        """Default base_url must be http://localhost:1234/v1 (no /api/v1)."""
        cfg = PROVIDER_DEFAULTS['lmstudio']
        self.assertEqual(
            cfg['base_url'], 'http://localhost:1234/v1',
            "LM Studio's OpenAI-compatible server is served at port 1234 "
            "with the /v1 prefix. Anything else (notably /api/v1) causes "
            "LLMClient to construct '.../v1/v1/models' on the request line.",
        )

    def test_lmstudio_does_not_require_an_api_key(self):
        """LM Studio's local server is unauthenticated by default."""
        cfg = PROVIDER_DEFAULTS['lmstudio']
        self.assertFalse(
            cfg['api_key_required'],
            "LM Studio's local server is unauthenticated; marking it "
            "api_key_required=True blocks the wizard's no-key path.",
        )

    def test_lmstudio_is_marked_as_a_local_provider(self):
        """Provider type must be 'local' so the UI routes it correctly."""
        cfg = PROVIDER_DEFAULTS['lmstudio']
        self.assertEqual(
            cfg['type'], 'local',
            "LM Studio runs on the user's machine; provider type must be "
            "'local' so the wizard surfaces it under the local section "
            "and skips the API-key flow.",
        )

    def test_lmstudio_entry_has_all_required_keys(self):
        """Shape parity with sibling entries (llama.cpp, ollama)."""
        required = {
            'type', 'base_url', 'api_key_required',
            'placeholder_model', 'label', 'description',
        }
        cfg = PROVIDER_DEFAULTS['lmstudio']
        missing = required - set(cfg.keys())
        self.assertFalse(
            missing,
            f"LM Studio provider entry is missing keys: {sorted(missing)}. "
            f"Add them for shape parity with sibling providers.",
        )


class TestLMStudioConnection(unittest.TestCase):
    """Verifies the test_connection() probe works against a live LM Studio.

    Skipped if no LM Studio server is reachable on the default port.
    """

    # The default LM Studio local server URL. Matches PROVIDER_DEFAULTS.
    _BASE_URL = 'http://localhost:1234/v1'

    def setUp(self):
        # Probe once; if it's down, skip the whole class — these checks
        # exist to catch URL regressions against a real server, not to
        # gate CI on having LM Studio installed.
        import socket
        host = 'localhost'
        port = 1234
        try:
            with socket.create_connection((host, port), timeout=0.5):
                self._server_up = True
        except OSError:
            self._server_up = False

    def test_connection_succeeds_against_live_lmstudio(self):
        if not self._server_up:
            self.skipTest(
                "LM Studio server not reachable on localhost:1234 — "
                "skipping live probe."
            )
        result = test_connection(self._BASE_URL, api_key=None)
        self.assertTrue(
            result['success'],
            f"test_connection() failed against live LM Studio at "
            f"{self._BASE_URL}: {result.get('message')}",
        )
        # A real LM Studio with models loaded reports a non-zero count.
        self.assertIn('models available', result['message'])


if __name__ == '__main__':
    unittest.main()
