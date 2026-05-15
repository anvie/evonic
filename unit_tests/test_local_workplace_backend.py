"""Tests for LocalWorkplaceBackend ExecutionBackend compliance."""

import unittest
from unittest import mock

from backend.tools.lib.exec_backend import ExecutionBackend
from backend.workplaces.backends.local_workplace import LocalWorkplaceBackend


class TestLocalWorkplaceBackend(unittest.TestCase):
    def test_is_concrete_execution_backend(self):
        backend = LocalWorkplaceBackend(
            config={'workspace_path': '/tmp/ws'},
            sandbox_enabled=False,
        )
        self.assertIsInstance(backend, ExecutionBackend)

    def test_file_methods_delegate_to_inner(self):
        inner = mock.MagicMock()
        inner.file_exists.return_value = True
        inner.file_stat.return_value = {'exists': True, 'size': 1, 'is_binary': False}
        inner.read_file.return_value = {'content': 'hi'}
        inner.write_file.return_value = {'ok': True}
        inner.make_dirs.return_value = {'ok': True}

        backend = LocalWorkplaceBackend(
            config={'workspace_path': '/tmp/ws'},
            sandbox_enabled=False,
        )
        backend._inner = inner

        self.assertTrue(backend.file_exists('/a'))
        inner.file_exists.assert_called_once_with('/a')

        self.assertEqual(backend.file_stat('/a')['size'], 1)
        inner.file_stat.assert_called_once_with('/a')

        self.assertEqual(backend.read_file('/a')['content'], 'hi')
        inner.read_file.assert_called_once_with('/a')

        self.assertEqual(backend.write_file('/a', 'x', create_dirs=False)['ok'], True)
        inner.write_file.assert_called_once_with('/a', 'x', create_dirs=False)

        self.assertEqual(backend.make_dirs('/d')['ok'], True)
        inner.make_dirs.assert_called_once_with('/d')
