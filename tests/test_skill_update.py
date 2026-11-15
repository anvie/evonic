"""Updating an installed skill from a newer zip: diff preview, saved settings kept, rollback on failure."""
import io
import json
import zipfile

import pytest
from flask import Flask

import backend.skill_update as su
from backend.skills_manager import skills_manager
from routes.skills import skills_bp


def make_zip(path, files):
    with zipfile.ZipFile(path, 'w') as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def manifest(version, sid='demo'):
    return json.dumps({'id': sid, 'name': 'Demo', 'version': version})


@pytest.fixture
def env(tmp_path, monkeypatch):
    import backend.skills_manager as sm
    skills = tmp_path / 'skills'
    (skills / 'demo').mkdir(parents=True)
    (skills / 'demo' / 'skill.json').write_text(manifest('1.0.0'))
    (skills / 'demo' / 'SYSTEM.md').write_text('old prompt\n')
    (skills / 'demo' / 'config.json').write_text('{"KEY": "secret"}')
    monkeypatch.setattr(sm, 'SKILLS_DIR', str(skills))
    monkeypatch.setattr(su, 'SKILLS_DIR', str(skills))
    monkeypatch.setattr(su, '_stage_dir', str(tmp_path / 'stage'))
    monkeypatch.setattr(skills_manager, 'get_skill',
                        lambda sid: json.loads((skills / sid / 'skill.json').read_text()) if (skills / sid / 'skill.json').exists() else None)
    monkeypatch.setattr(skills_manager, '_run_setup', lambda *a, **k: None)
    return skills, tmp_path


def test_preview_ignores_saved_config_and_apply_keeps_it(env):
    skills, tmp = env
    z = make_zip(tmp / 'n.zip', {'demo/skill.json': manifest('1.1.0'), 'demo/SYSTEM.md': 'new prompt\n', 'demo/config.json': '{}'})
    res = su.stage_update('demo', str(z), 'n.zip')
    assert res['version_relation'] == 'newer'
    assert [c['path'] for c in res['changes']] == ['SYSTEM.md', 'skill.json']      # config.json is not a change
    out = su.apply_update('demo', res['token'])
    assert 'error' not in out
    assert (skills / 'demo' / 'SYSTEM.md').read_text() == 'new prompt\n'
    assert json.loads((skills / 'demo' / 'config.json').read_text()) == {'KEY': 'secret'}


def test_rejects_other_skill(env):
    _, tmp = env
    z = make_zip(tmp / 'o.zip', {'skill.json': manifest('2.0.0', sid='other')})
    assert 'not "demo"' in su.stage_update('demo', str(z), 'o.zip')['error']


def test_apply_rolls_back_on_failure(env, monkeypatch):
    skills, tmp = env
    z = make_zip(tmp / 'f.zip', {'skill.json': manifest('1.2.0'), 'SYSTEM.md': 'broken'})
    res = su.stage_update('demo', str(z), 'f.zip')
    monkeypatch.setattr(skills_manager, 'install_skill', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    assert 'boom' in su.apply_update('demo', res['token'])['error']
    assert (skills / 'demo' / 'SYSTEM.md').read_text() == 'old prompt\n'


def test_routes(env):
    _, tmp = env
    app = Flask(__name__)
    app.register_blueprint(skills_bp)
    c = app.test_client()
    z = make_zip(tmp / 'r.zip', {'skill.json': manifest('1.2.0')})
    r = c.post('/api/skills/demo/update/preview', data={'file': (io.BytesIO(z.read_bytes()), 'r.zip')}, content_type='multipart/form-data')
    assert r.status_code == 200
    token = r.get_json()['token']
    assert c.post('/api/skills/demo/update/discard', json={'token': token}).status_code == 200
    assert c.post('/api/skills/demo/update/apply', json={'token': token}).status_code == 400
