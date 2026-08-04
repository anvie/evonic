"""Regression coverage for SSHBackend binary file reads."""

import base64
from unittest.mock import MagicMock, Mock

from backend.tools.lib.backends.ssh_backend import SSHBackend


def _backend():
    """Create an unconnected backend suitable for unit testing I/O methods."""
    backend = SSHBackend.__new__(SSHBackend)
    backend._client = Mock()
    backend._connect = Mock()
    return backend


def test_cat_file_bytes_reads_binary_data_through_sftp():
    backend = _backend()
    sftp = backend._client.open_sftp.return_value
    remote_file = sftp.file.return_value.__enter__.return_value
    payload = b'PK\x03\x04excel-workbook\x00\xff'
    remote_file.read.return_value = payload

    result = backend.cat_file_bytes('/remote/report.xlsx')

    assert result == {'bytes': payload}
    sftp.file.assert_called_once_with('/remote/report.xlsx', 'rb')
    sftp.close.assert_called_once_with()


def test_cat_file_bytes_decodes_wrapped_base64_when_sftp_unavailable():
    backend = _backend()
    backend._client.open_sftp.side_effect = OSError('SFTP subsystem unavailable')
    payload = b'PK\x03\x04' + bytes(range(256)) * 4
    backend._exec = Mock(return_value={
        'exit_code': 0,
        'stdout': base64.encodebytes(payload).decode('ascii'),
    })

    result = backend.cat_file_bytes('/remote/report.xlsx')

    assert result == {'bytes': payload}
    command = backend._exec.call_args.args[0]
    assert 'python3 -c' in command
    assert 'base64.b64encode' in command
