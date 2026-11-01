"""A plugin can declare `min_evonic_version`; an older Evonic refuses to install, enable or load it, saying why."""

import json
import zipfile

import pytest

from backend import plugin_lifecycle as lifecycle
from backend import version as version_mod


@pytest.mark.parametrize('minimum,current,ok', [
    ('1.6.0', '1.6.0', True), ('1.6.0', '1.6.0-23', True), ('1.6.0', '1.7.2', True), ('1.6', '1.6.0', True),
    ('v1.6.0', '1.6.0', True), ('1.6.1', '1.6.0', False), ('1.6.0', '1.5.9', False), ('2', '1.99.99', False),
    ('1.6.0', '0.8.7-28', False), ('', '0.0.1', True), (None, '0.0.1', True),
])
def test_the_minimum_is_compared_by_major_minor_patch(minimum, current, ok):
    assert (version_mod.minimum_version_error(minimum, current=current) == '') is ok


def test_the_error_says_what_is_needed_and_what_runs():
    msg = version_mod.minimum_version_error('1.7.0', 'Plugin "pring"', current='1.6.0-23')
    assert msg == 'Plugin "pring" needs Evonic 1.7.0 or newer, but this is Evonic 1.6.0-23. Update Evonic to use it.'


@pytest.mark.parametrize('bad', ['soon', '1.x', '1.2.3.4', '>=1.6'])
def test_an_unreadable_minimum_is_reported_not_ignored(bad):
    assert 'invalid min_evonic_version' in version_mod.minimum_version_error(bad, current='9.9.9')


@pytest.fixture
def manager(tmp_path, monkeypatch):
    root = tmp_path / 'plugins'
    root.mkdir()
    monkeypatch.setattr(lifecycle, 'PLUGINS_DIR', str(root))
    monkeypatch.setattr(version_mod, 'get_version', lambda: '1.6.0-23')
    saved = {}
    monkeypatch.setattr(lifecycle.PluginManager, '_is_plugin_enabled', lambda self, pid: saved.get(pid) == '1')
    import models.db as models_db
    monkeypatch.setattr(models_db.db, 'set_setting', lambda key, value: saved.__setitem__(key.split(':', 1)[1], value))
    pm = lifecycle.PluginManager(load_plugins=False)
    pm.saved, pm.root = saved, root
    return pm


def _manifest(plugin_id='demo', **extra):
    return {'id': plugin_id, 'name': plugin_id, 'version': '1.0.0', 'events': [], **extra}


def _write(root, manifest):
    d = root / manifest['id']
    d.mkdir(parents=True, exist_ok=True)
    (d / 'plugin.json').write_text(json.dumps(manifest))
    return d


def test_a_plugin_that_needs_a_newer_evonic_cannot_be_enabled(manager):
    _write(manager.root, _manifest(min_evonic_version='1.7.0'))
    result = manager.set_plugin_enabled('demo', True)
    assert result == {'error': 'Plugin "demo" needs Evonic 1.7.0 or newer, but this is Evonic 1.6.0-23. Update Evonic to use it.'}
    assert 'demo' not in manager.saved                                   # nothing was switched on


def test_a_plugin_that_fits_enables_and_disabling_is_always_allowed(manager):
    _write(manager.root, _manifest(min_evonic_version='1.6.0'))
    assert manager.set_plugin_enabled('demo', True)['enabled'] is True
    _write(manager.root, _manifest(min_evonic_version='9.0.0'))          # now too new, but turning it off still works
    assert manager.set_plugin_enabled('demo', False)['enabled'] is False


def test_installing_from_a_folder_is_refused_before_anything_is_copied(manager, tmp_path):
    src = _write(tmp_path / 'src', _manifest(min_evonic_version='2.0.0'))
    result = manager.install_plugin_from_dir(str(src))
    assert 'needs Evonic 2.0.0 or newer' in result['error']
    assert not (manager.root / 'demo').exists()


def test_installing_a_zip_is_refused_before_anything_is_copied(manager, tmp_path):
    zpath = tmp_path / 'demo.zip'
    with zipfile.ZipFile(zpath, 'w') as zf:
        zf.writestr('demo/plugin.json', json.dumps(_manifest(min_evonic_version='2.0.0')))
    result = manager.install_plugin(str(zpath))
    assert 'needs Evonic 2.0.0 or newer' in result['error'] and not (manager.root / 'demo').exists()


def test_a_plugin_without_a_minimum_installs_as_before(manager, tmp_path):
    src = _write(tmp_path / 'src', _manifest())
    assert manager.install_plugin_from_dir(str(src))['id'] == 'demo'


def test_an_enabled_plugin_that_is_too_new_is_not_loaded_at_startup_and_the_log_says_why(manager, monkeypatch):
    _write(manager.root, _manifest('old', min_evonic_version='1.6.0'))
    _write(manager.root, _manifest('new', min_evonic_version='3.0.0'))
    manager.saved.update({'old': '1', 'new': '1'})
    loaded = []
    monkeypatch.setattr(manager, '_load_plugin', lambda pid, manifest=None: loaded.append(pid))
    manager._load_all()
    assert loaded == ['old']
    assert any('needs Evonic 3.0.0' in e['message'] for e in manager.get_logs('new'))
