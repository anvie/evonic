"""Regression coverage for TunnelWorkplaceBackend binary file transfers."""

import base64

from backend.workplaces.backends.tunnel_workplace import (
    WRITE_FILE_BYTES_CHUNK_SIZE,
    TunnelWorkplaceBackend,
)


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


def _chunk_recording_backend():
    """Create a backend that records write_file_b64 calls instead of sending them."""
    backend = TunnelWorkplaceBackend('test-workplace')
    calls = []
    backend.make_dirs = lambda path: {'ok': True}
    backend.write_file_b64 = lambda path, encoded, offset, is_last: calls.append(
        (path, encoded, offset, is_last)
    ) or {'ok': True}
    return backend, calls


def test_write_file_bytes_sends_bounded_chunks_with_offsets_and_finalization():
    data = bytes(range(256)) * ((WRITE_FILE_BYTES_CHUNK_SIZE // 256) + 1)
    backend, calls = _chunk_recording_backend()

    result = backend.write_file_bytes('/remote/artifact.bin', data)

    assert result == {'ok': True}
    assert [call[2] for call in calls] == list(
        range(0, len(data), WRITE_FILE_BYTES_CHUNK_SIZE)
    )
    assert [call[3] for call in calls] == [False] * (len(calls) - 1) + [True]
    assert b''.join(base64.b64decode(call[1]) for call in calls) == data
    assert all(
        len(base64.b64decode(call[1])) <= WRITE_FILE_BYTES_CHUNK_SIZE
        for call in calls
    )


def test_write_file_bytes_finalizes_empty_file():
    backend, calls = _chunk_recording_backend()

    result = backend.write_file_bytes('/remote/empty.bin', b'')

    assert result == {'ok': True}
    assert calls == [('/remote/empty.bin', '', 0, True)]


def test_write_file_bytes_stops_after_chunk_failure():
    data = b'x' * (WRITE_FILE_BYTES_CHUNK_SIZE * 2)
    backend = TunnelWorkplaceBackend('test-workplace')
    calls = []
    backend.make_dirs = lambda path: {'ok': True}

    def fail_first_chunk(path, encoded, offset, is_last):
        calls.append((path, encoded, offset, is_last))
        return {'error': 'write failed'}

    backend.write_file_b64 = fail_first_chunk

    result = backend.write_file_bytes('/remote/artifact.bin', data)

    assert result == {'error': 'write failed'}
    assert len(calls) == 1


def test_write_file_bytes_returns_directory_creation_error():
    backend = TunnelWorkplaceBackend('test-workplace')
    backend.make_dirs = lambda path: {'error': 'directory creation failed'}
    backend.write_file_b64 = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError('write should not be called')
    )

    result = backend.write_file_bytes('/remote/artifact.bin', b'data')

    assert result == {'error': 'directory creation failed'}
