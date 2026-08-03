"""Regression coverage for TunnelWorkplaceBackend binary file reads."""

import base64

from backend.workplaces.backends.tunnel_workplace import TunnelWorkplaceBackend


def _backend(response):
    """Create a backend with a deterministic tunnel Base64 response."""
    backend = TunnelWorkplaceBackend('test-workplace')
    backend.read_file_b64 = lambda path: response
    return backend


def test_cat_file_bytes_decodes_unpadded_base64_with_transport_whitespace():
    payload = b'PK\x03\x04excel-workbook\x00\xff'
    encoded = base64.b64encode(payload).decode('ascii').rstrip('=')
    wrapped = f'{encoded[:8]}\n {encoded[8:]}'

    result = _backend({'data': wrapped}).cat_file_bytes('/remote/report.xlsx')

    assert result == {'bytes': payload}


def test_cat_file_bytes_rejects_malformed_base64():
    result = _backend({'data': 'not-valid-base64!'}).cat_file_bytes('/remote/report.xlsx')

    assert 'error' in result
    assert result['error'].startswith('base64 decode failed:')
