"""Export and import of model configuration."""
import json
import threading

import pytest

from backend import model_transfer
from models.db import db


@pytest.fixture
def client():
    from app import app
    db._tls = threading.local()
    db._init_tables()
    for seeded in db.get_llm_models():   # the fresh database ships with a few models
        db.delete_model(seeded['id'])
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess['authenticated'] = True
        yield c


def _provider(pid='acme', api_key='sk-secret-123', base_url='https://api.acme.test/v1'):
    db.create_provider({
        'id': pid, 'name': 'Acme AI', 'type': 'remote', 'base_url': base_url,
        'api_key': api_key, 'api_format': 'openai', 'connection_options': {'headers': {'X-Token': 'tok-secret'}},
    })


def _model(mid='acme/fast', **overrides):
    data = {'id': mid, 'name': 'Acme Fast', 'type': 'llm', 'provider': 'acme', 'model_name': 'fast-1',
            'max_tokens': 4096, 'timeout': 30, 'thinking': 1, 'thinking_budget': 2048,
            'temperature': 0.3, 'reasoning_effort': 'high', 'context_window': 128000}
    data.update(overrides)
    return db.create_model(data)


def _export(client):
    resp = client.get('/api/models/export')
    assert resp.status_code == 200
    return resp, json.loads(resp.get_data(as_text=True))


def _import(client, doc, dry_run=False):
    resp = client.post('/api/models/import', json={'data': doc, 'dry_run': dry_run})
    return resp.status_code, resp.get_json()


def test_export_is_a_download_and_leaves_secrets_out(client):
    _provider()
    _model(api_key='sk-model-secret', base_url='https://user:pw@models.acme.test/v1')
    resp, doc = _export(client)
    assert 'attachment' in resp.headers['Content-Disposition'] and '.json' in resp.headers['Content-Disposition']
    text = resp.get_data(as_text=True)
    for secret in ('sk-secret-123', 'sk-model-secret', 'tok-secret', 'user:pw'):
        assert secret not in text
    assert doc['format'] == model_transfer.FORMAT
    assert [p['id'] for p in doc['providers']] == ['acme']   # only providers a model points at
    assert 'api_key' not in doc['providers'][0] and 'api_key' not in doc['models'][0]
    assert doc['models'][0]['base_url'] is None              # an endpoint with credentials in it is dropped


def test_export_can_be_limited_to_one_model(client):
    _provider()
    _provider('other', api_key='sk-other', base_url='https://other.test/v1')
    _model('acme/fast')
    _model('acme/slow', name='Slow', model_name='slow-1')
    _model('other/m', name='Other', provider='other', model_name='m')

    resp = client.get('/api/models/export?id=acme/fast')
    doc = json.loads(resp.get_data(as_text=True))
    assert [m['id'] for m in doc['models']] == ['acme/fast'] and [p['id'] for p in doc['providers']] == ['acme']
    assert 'acme-fast' in resp.headers['Content-Disposition']

    two = json.loads(client.get('/api/models/export?id=acme/fast&id=other/m').get_data(as_text=True))
    assert {m['id'] for m in two['models']} == {'acme/fast', 'other/m'}
    assert client.get('/api/models/export?id=nope').status_code == 404
    assert len(json.loads(client.get('/api/models/export').get_data(as_text=True))['models']) == 3


def test_export_then_import_recreates_models_and_providers_without_keys(client):
    _provider()
    _model()
    _, doc = _export(client)
    db.delete_model('acme/fast')
    db.delete_provider('acme')

    status, report = _import(client, doc)
    assert status == 200 and report['models']['create'] == [{'id': 'acme/fast', 'name': 'Acme Fast'}]
    assert report['providers']['create'] == ['acme'] and report['needs_credentials'] == ['acme']

    model = db.get_model_by_id('acme/fast')
    assert (model['model_name'], model['max_tokens'], model['timeout'], model['temperature']) == ('fast-1', 4096, 30, 0.3)
    assert model['thinking'] and model['reasoning_effort'] == 'high' and model['context_window'] == 128000
    provider = db.get_provider('acme')
    assert provider['base_url'] == 'https://api.acme.test/v1' and not provider['api_key']


def test_dry_run_reports_without_changing_anything(client):
    _provider()
    _model()
    _, doc = _export(client)
    db.delete_model('acme/fast')
    status, report = _import(client, doc, dry_run=True)
    assert status == 200 and report['dry_run'] and report['models']['create'][0]['id'] == 'acme/fast'
    assert db.get_model_by_id('acme/fast') is None


def test_existing_models_and_providers_are_left_alone(client):
    _provider(api_key='sk-keep-me')
    _model(name='Local Name')
    _, doc = _export(client)
    doc['models'][0]['name'] = 'Changed In File'
    doc['providers'][0]['base_url'] = 'https://elsewhere.test'

    _, report = _import(client, doc)
    assert report['models']['skipped'] == [{'id': 'acme/fast', 'name': 'Changed In File', 'reason': 'already exists'}]
    assert report['providers']['existing'] == ['acme'] and report['models']['create'] == []
    assert db.get_model_by_id('acme/fast')['name'] == 'Local Name'
    provider = db.get_provider('acme')
    assert provider['api_key'] == 'sk-keep-me' and provider['base_url'] == 'https://api.acme.test/v1'


def test_a_bad_entry_is_reported_and_the_rest_still_imports(client):
    _provider()
    doc = {'format': model_transfer.FORMAT, 'providers': [], 'models': [
        {'id': 'acme/ok', 'name': 'Fine', 'type': 'llm', 'provider': 'acme', 'model_name': 'ok-1'},
        {'id': 'acme/bad-type', 'name': 'X', 'type': 'nonsense', 'provider': 'acme', 'model_name': 'x'},
        {'id': 'acme/bad-temp', 'name': 'X', 'type': 'llm', 'provider': 'acme', 'model_name': 'x', 'temperature': 9},
        {'id': 'ghost/m', 'name': 'X', 'type': 'llm', 'provider': 'ghost', 'model_name': 'x'},
        {'id': 'acme/no-name', 'type': 'llm', 'provider': 'acme', 'model_name': 'x'},
    ]}
    status, report = _import(client, doc)
    assert status == 200
    assert [m['id'] for m in report['models']['create']] == ['acme/ok']
    errors = {e['id']: e['error'] for e in report['models']['errors']}
    assert set(errors) == {'acme/bad-type', 'acme/bad-temp', 'ghost/m', 'acme/no-name'}
    assert 'unknown provider' in errors['ghost/m'] and 'temperature' in errors['acme/bad-temp']
    assert db.get_model_by_id('acme/bad-type') is None


def test_credentials_in_a_file_are_ignored(client):
    _provider()
    doc = {'format': model_transfer.FORMAT, 'providers': [], 'models': [
        {'id': 'acme/leaky', 'name': 'Leaky', 'type': 'llm', 'provider': 'acme', 'model_name': 'l', 'api_key': 'sk-injected'},
    ]}
    _import(client, doc)
    assert not db.get_model_by_id('acme/leaky').get('api_key')


def test_the_default_model_is_never_exported_or_imported(client):
    _provider()
    _model(is_default=1)
    _model('acme/other', name='Other', model_name='other-1')
    with db._connect() as conn:
        conn.execute("UPDATE llm_models SET is_default = 1 WHERE id = 'acme/fast'")
        conn.commit()
    _, doc = _export(client)
    assert [m['is_default'] for m in doc['models']] == [False, False]

    # a file that claims a default (edited by hand) does not get one either
    db.delete_model('acme/fast')
    db.delete_model('acme/other')
    for entry in doc['models']:
        entry['is_default'] = True
    _import(client, doc)
    assert {m['id'] for m in db.get_llm_models()} == {'acme/fast', 'acme/other'}
    assert not any(m['is_default'] for m in db.get_llm_models())


def test_wrong_format_or_shape_is_refused(client):
    for body in ({'data': {'format': 'something-else', 'models': []}}, {'data': ['not', 'a', 'dict']}, {}):
        resp = client.post('/api/models/import', json=body)
        assert resp.status_code == 400 and not resp.get_json()['success']
