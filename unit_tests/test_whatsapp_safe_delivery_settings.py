import config

from app import app
from models.db import db


WHATSAPP_SETTINGS = {
    'whatsapp_safe_delivery_enabled',
    'whatsapp_pool_window_seconds',
    'whatsapp_min_send_interval_seconds',
    'whatsapp_typing_chars_per_second',
    'whatsapp_max_typing_delay_seconds',
    'whatsapp_delay_jitter_ratio',
    'whatsapp_max_outbound_per_minute',
    'whatsapp_natural_formatting_enabled',
}


def _authenticated_client():
    client = app.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True
    return client


def test_general_settings_returns_whatsapp_defaults_from_config():
    settings = _authenticated_client().get('/api/settings/general').get_json()

    assert WHATSAPP_SETTINGS <= settings.keys()
    assert settings['whatsapp_safe_delivery_enabled'] is config.WHATSAPP_SAFE_DELIVERY_ENABLED
    assert settings['whatsapp_pool_window_seconds'] == config.WHATSAPP_POOL_WINDOW_SECONDS
    assert settings['whatsapp_min_send_interval_seconds'] == config.WHATSAPP_MIN_SEND_INTERVAL_SECONDS
    assert settings['whatsapp_typing_chars_per_second'] == config.WHATSAPP_TYPING_CHARS_PER_SECOND
    assert settings['whatsapp_max_typing_delay_seconds'] == config.WHATSAPP_MAX_TYPING_DELAY_SECONDS
    assert settings['whatsapp_delay_jitter_ratio'] == config.WHATSAPP_DELAY_JITTER_RATIO
    assert settings['whatsapp_max_outbound_per_minute'] == config.WHATSAPP_MAX_OUTBOUND_PER_MINUTE
    assert settings['whatsapp_natural_formatting_enabled'] is config.WHATSAPP_NATURAL_FORMATTING_ENABLED


def test_batch_settings_saves_all_whatsapp_values():
    values = {
        'whatsapp_safe_delivery_enabled': False,
        'whatsapp_pool_window_seconds': 3.5,
        'whatsapp_min_send_interval_seconds': 4.0,
        'whatsapp_typing_chars_per_second': 12.0,
        'whatsapp_max_typing_delay_seconds': 20.0,
        'whatsapp_delay_jitter_ratio': 0.25,
        'whatsapp_max_outbound_per_minute': 120,
        'whatsapp_natural_formatting_enabled': 'off',
    }

    response = _authenticated_client().post('/api/settings/batch', json={'settings': values})

    assert response.status_code == 200
    assert response.get_json() == {'success': True, 'results': {
        **values,
        'whatsapp_natural_formatting_enabled': False,
    }}
    assert db.get_setting('whatsapp_safe_delivery_enabled') == '0'
    assert db.get_setting('whatsapp_natural_formatting_enabled') == '0'


def test_batch_settings_rejects_invalid_whatsapp_booleans_and_non_finite_numbers():
    response = _authenticated_client().post('/api/settings/batch', json={'settings': {
        'whatsapp_safe_delivery_enabled': 'not-a-bool',
        'whatsapp_pool_window_seconds': 'NaN',
    }})

    body = response.get_json()
    assert body['success'] is True
    assert body['partial'] is True
    assert body['results'] == {}
    assert body['errors'] == [
        'whatsapp_safe_delivery_enabled: must be a boolean',
        'whatsapp_pool_window_seconds: must be a finite number',
    ]


def test_batch_settings_clamps_whatsapp_values_to_runtime_bounds():
    response = _authenticated_client().post('/api/settings/batch', json={'settings': {
        'whatsapp_pool_window_seconds': -1,
        'whatsapp_min_send_interval_seconds': 100,
        'whatsapp_delay_jitter_ratio': 1,
        'whatsapp_max_outbound_per_minute': 1000,
    }})

    assert response.get_json() == {'success': True, 'results': {
        'whatsapp_pool_window_seconds': 0.1,
        'whatsapp_min_send_interval_seconds': 60.0,
        'whatsapp_delay_jitter_ratio': 0.5,
        'whatsapp_max_outbound_per_minute': 600,
    }}
