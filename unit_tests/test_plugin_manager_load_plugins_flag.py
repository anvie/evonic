"""PluginManager(load_plugins=False) must not execute plugin handler modules.

Read-only callers (CLI `plugin list`, requirements checks) construct a
PluginManager only to read manifests. Loading handlers there runs arbitrary
module-level plugin code in a short-lived process, which previously mutated
shared state owned by the live server (the kanban scanner schedules).
"""

import json
import os
import subprocess
import sys

import pytest

from backend import plugin_lifecycle as lifecycle


def _write_plugin(root, plugin_id, marker):
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True)
    (plugin_dir / 'plugin.json').write_text(json.dumps({
        'id': plugin_id,
        'name': plugin_id.title(),
        'version': '1.0.0',
        'events': ['turn_complete'],
    }))
    (plugin_dir / 'handler.py').write_text(
        'open(r"{marker}", "w").write("handler executed")\n'.format(marker=marker)
    )
    return plugin_dir


@pytest.fixture
def plugin_root(tmp_path, monkeypatch):
    root = tmp_path / 'plugins'
    root.mkdir()
    monkeypatch.setattr(lifecycle, 'PLUGINS_DIR', str(root))
    # DB-independent: treat every manifest as enabled.
    monkeypatch.setattr(lifecycle.PluginManager, '_is_plugin_enabled',
                        lambda self, plugin_id: True)
    return root


def test_load_plugins_false_skips_handler_execution(plugin_root, tmp_path):
    marker = tmp_path / 'marker.txt'
    _write_plugin(plugin_root, 'sideeffect', marker)

    pm = lifecycle.PluginManager(load_plugins=False)

    assert not marker.exists()
    assert pm.list_plugins()[0]['id'] == 'sideeffect'
    assert pm._modules == {}


def test_default_still_loads_handlers(plugin_root, tmp_path):
    marker = tmp_path / 'marker.txt'
    _write_plugin(plugin_root, 'sideeffect', marker)

    pm = lifecycle.PluginManager()

    assert marker.exists()
    assert 'sideeffect' in pm._modules


def test_importing_plugin_manager_stays_side_effect_free():
    """The process-wide singleton must stay lazy.

    Importing backend.plugin_manager (which any short-lived CLI/script does) must
    not construct the manager, because construction loads and executes every
    enabled plugin handler. That is exactly how a read-only CLI command used to
    disarm the live kanban scanner by rewriting its schedule rows.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import sys, backend.plugin_manager as m;"
        "print(len([k for k in sys.modules if k.startswith('plugin_pkg_')]),"
        " m._plugin_manager_instance is None)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=root,
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "0 True", proc.stdout
