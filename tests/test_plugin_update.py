"""Updating an installed plugin from a newer zip: diff preview, then apply with rollback."""
import io
import json
import zipfile

import pytest
from flask import Flask

import backend.plugin_update as pu
from backend.plugin_manager import plugin_manager
from routes.plugins import plugins_bp


def make_zip(path, files):
    with zipfile.ZipFile(path, 'w') as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def manifest(version, pid='sample'):
    return json.dumps({'id': pid, 'name': 'Sample', 'version': version})


@pytest.fixture
def env(tmp_path, monkeypatch):
    plugins = tmp_path / 'plugins'
    (plugins / 'sample').mkdir(parents=True)
    (plugins / 'sample' / 'plugin.json').write_text(manifest('1.0.0'))
    (plugins / 'sample' / 'a.py').write_text('x = 1\nkeep = 1\n')
    (plugins / 'sample' / 'gone.txt').write_text('bye')
    monkeypatch.setattr(pu, 'PLUGINS_DIR', str(plugins))
    monkeypatch.setattr(pu, '_stage_dir', str(tmp_path / 'stage'))
    monkeypatch.setattr(plugin_manager, '_read_manifest',
                        lambda pid: json.loads((plugins / pid / 'plugin.json').read_text()) if (plugins / pid / 'plugin.json').exists() else None)
    reloads = []
    monkeypatch.setattr(plugin_manager, 'reload_plugin', lambda pid: reloads.append(pid))
    import backend.plugin_lifecycle as pl
    monkeypatch.setattr(pl, 'PLUGINS_DIR', str(plugins))
    return plugins, tmp_path, reloads


def test_preview_diff_and_apply(env):
    plugins, tmp, reloads = env
    z = make_zip(tmp / 'new.zip', {'sample/plugin.json': manifest('1.1.0'), 'sample/a.py': 'x = 2\nkeep = 1\n', 'sample/new.md': 'hi'})
    res = pu.stage_update('sample', str(z), 'new.zip')
    assert res['version_relation'] == 'newer'
    by_path = {c['path']: c for c in res['changes']}
    assert by_path['a.py']['status'] == 'modified' and by_path['a.py']['added'] == 1 and by_path['a.py']['removed'] == 1
    assert by_path['new.md']['status'] == 'added' and by_path['gone.txt']['status'] == 'removed'
    assert (plugins / 'sample' / 'gone.txt').exists()          # preview changes nothing

    out = pu.apply_update('sample', res['token'])
    assert 'error' not in out
    assert (plugins / 'sample' / 'new.md').exists() and not (plugins / 'sample' / 'gone.txt').exists()
    assert pu.apply_update('sample', res['token'])['error']      # token is single use


def test_rejects_other_plugin(env):
    _, tmp, _ = env
    z = make_zip(tmp / 'o.zip', {'plugin.json': manifest('2.0.0', pid='other')})
    assert 'not "sample"' in pu.stage_update('sample', str(z), 'o.zip')['error']


def test_apply_rolls_back_on_failure(env, monkeypatch):
    plugins, tmp, _ = env
    z = make_zip(tmp / 'n.zip', {'plugin.json': manifest('0.9.0'), 'a.py': 'broken'})
    res = pu.stage_update('sample', str(z), 'n.zip')
    assert res['version_relation'] == 'older'
    monkeypatch.setattr(plugin_manager, 'install_plugin', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    out = pu.apply_update('sample', res['token'])
    assert 'boom' in out['error']
    assert (plugins / 'sample' / 'a.py').read_text() == 'x = 1\nkeep = 1\n'


def test_routes(env):
    _, tmp, _ = env
    app = Flask(__name__)
    app.register_blueprint(plugins_bp)
    c = app.test_client()
    z = make_zip(tmp / 'r.zip', {'plugin.json': manifest('1.2.0')})
    r = c.post('/api/plugins/sample/update/preview', data={'file': (io.BytesIO(z.read_bytes()), 'r.zip')}, content_type='multipart/form-data')
    assert r.status_code == 200
    token = r.get_json()['token']
    assert c.post('/api/plugins/sample/update/discard', json={'token': token}).status_code == 200
    assert c.post('/api/plugins/sample/update/apply', json={'token': token}).status_code == 400


def test_junk_dirs_in_zip_are_ignored(tmp_path):
    from backend.zip_validator import validate_upload_zip
    z = make_zip(tmp_path / 'j.zip', {'plugin.json': manifest('1.0.0'), '.pytest_cache/CACHEDIR.TAG': 'x',
                                      '__MACOSX/._a': 'x', 'prompts/guides.md.bak-20261104': 'x', 'a.py.orig': 'x', 'b.py~': 'x', '.DS_Store': 'x', 'a.py': 'x'})
    assert validate_upload_zip(str(z)) == (True, '')
    bad = make_zip(tmp_path / 'b.zip', {'plugin.json': manifest('1.0.0'), 'run.exe': 'x'})
    assert not validate_upload_zip(str(bad))[0]
