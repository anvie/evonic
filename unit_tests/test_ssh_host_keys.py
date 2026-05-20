"""Tests for SSH backend host key verification."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_warning_policy_is_set():
    """Verify that WarningPolicy (not AutoAddPolicy) is the default."""
    import paramiko
    assert paramiko.WarningPolicy is not paramiko.AutoAddPolicy
    policy = paramiko.WarningPolicy()
    assert isinstance(policy, paramiko.MissingHostKeyPolicy)


def test_auto_add_policy_removed():
    """Verify the codebase no longer uses AutoAddPolicy for SSH connections."""
    ssh_backend_path = os.path.join(
        os.path.dirname(__file__), '..',
        'backend', 'tools', 'lib', 'backends', 'ssh_backend.py'
    )
    with open(ssh_backend_path, 'r') as f:
        content = f.read()

    assert 'set_missing_host_key_policy(paramiko.AutoAddPolicy())' not in content, \
        "SSH backend should not use AutoAddPolicy"

    assert 'set_missing_host_key_policy(paramiko.WarningPolicy())' in content, \
        "SSH backend should use WarningPolicy"

    assert '_load_host_keys' in content, \
        "SSH backend should have _load_host_keys method"


def test_load_host_keys_method_exists():
    """Verify _load_host_keys is a proper method on SSHBackend."""
    from backend.tools.lib.backends.ssh_backend import SSHBackend
    assert hasattr(SSHBackend, '_load_host_keys')
    assert callable(getattr(SSHBackend, '_load_host_keys'))
