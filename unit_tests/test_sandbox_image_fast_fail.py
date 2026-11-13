"""A broken sandbox must fail with the real reason, and quickly.

A missing sandbox image used to surface as `Failed to start container: ...` plus
docker's own wording, which says nothing about the image being absent or how to
build it. The failure was fast either way — what was missing was a cause an
operator can act on. The SSH path had the opposite problem: it was slow,
inheriting a 300 s connect timeout, so an unreachable workplace waited out the
kernel's retransmit budget before anyone learned the host was down.
"""

from unittest.mock import patch

from backend.tools.lib.backends import docker_backend


class _Result:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_missing_image_names_the_image_and_the_build_command(monkeypatch, tmp_path):
    monkeypatch.setattr(
        docker_backend, '_docker',
        lambda *a, **k: _Result(1, '', 'Error: No such image: evonic-sandbox:latest'),
    )
    container_id, error = docker_backend._get_or_create_container(
        'test-session', workspace=str(tmp_path),
    )
    assert container_id is None
    assert docker_backend.SANDBOX_IMAGE in error
    assert f'docker build -t {docker_backend.SANDBOX_IMAGE}' in error


def test_pull_denied_is_reported_as_a_missing_image(monkeypatch, tmp_path):
    monkeypatch.setattr(
        docker_backend, '_docker',
        lambda *a, **k: _Result(1, '', 'docker: pull access denied for evonic-sandbox'),
    )
    container_id, error = docker_backend._get_or_create_container(
        'test-session-2', workspace=str(tmp_path),
    )
    assert container_id is None
    assert 'not present on this host' in error


def test_any_other_docker_failure_keeps_the_raw_stderr(monkeypatch, tmp_path):
    monkeypatch.setattr(
        docker_backend, '_docker',
        lambda *a, **k: _Result(1, '', 'permission denied while trying to connect'),
    )
    container_id, error = docker_backend._get_or_create_container(
        'test-session-3', workspace=str(tmp_path),
    )
    assert container_id is None
    assert 'Failed to start container' in error
    assert 'permission denied' in error


def test_ssh_connect_uses_the_configured_timeout():
    from backend.tools.lib.backends import ssh_backend
    import config

    captured = {}

    class FakeClient:
        def set_missing_host_key_policy(self, _policy):
            pass

        def connect(self, **kwargs):
            captured.update(kwargs)

        def get_transport(self):
            class T:
                def set_keepalive(self, _seconds):
                    pass
            return T()

    with patch('paramiko.SSHClient', return_value=FakeClient()), \
            patch('paramiko.AutoAddPolicy'):
        ssh_backend.SSHBackend(host='127.0.0.1', username='nobody', port=2222)

    expected = config.SSH_CONNECT_TIMEOUT
    assert captured['timeout'] == expected
    assert captured['banner_timeout'] == expected
    assert captured['auth_timeout'] == expected
    assert expected <= 30, 'a dead host must not sit on the TCP retransmit budget'
