"""A broken sandbox must fail with the real reason, and quickly.

A missing sandbox image used to surface as `Failed to start container: ...` plus
docker's own wording, which says nothing about the image being absent or how to
build it. The failure was fast either way — what was missing was a cause an
operator can act on. The SSH path had the opposite problem: it was slow,
inheriting a 300 s connect timeout, so an unreachable workplace waited out the
kernel's retransmit budget before anyone learned the host was down.
"""

from unittest.mock import patch

import pytest

from backend.tools.lib.backends import docker_backend


class _Result:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _fresh_image_cache():
    """The verdict cache is module-global; a negative one must not leak into
    another test (it is cached for a third of the TTL)."""
    docker_backend._reset_image_cache()
    yield
    docker_backend._reset_image_cache()


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
    docker_backend._reset_image_cache()
    container_id, error = docker_backend._get_or_create_container(
        'test-session-3', workspace=str(tmp_path),
    )
    assert container_id is None
    assert 'permission denied' in error
    assert 'cannot read or write files' in error


def test_present_image_is_inspected_once_and_run_proceeds(monkeypatch, tmp_path):
    calls = []

    def fake_docker(*a, **k):
        calls.append(a)
        return _Result(0, '[]', '') if a[0] == 'image' else _Result(0, 'cid123\n', '')

    monkeypatch.setattr(docker_backend, '_docker', fake_docker)
    docker_backend._reset_image_cache()

    assert docker_backend.sandbox_image_error() == ''
    assert docker_backend.sandbox_image_error() == ''
    assert [c[0] for c in calls] == ['image'], 'a present image must be inspected once'

    container_id, error = docker_backend._get_or_create_container(
        'test-session-4', workspace=str(tmp_path))

    assert (container_id, error) == ('cid123', None)
    assert 'run' in [c[0] for c in calls], 'a present image goes on to start a container'


def test_a_missing_image_never_runs_a_container(monkeypatch, tmp_path):
    calls = []

    def fake_docker(*a, **k):
        calls.append(a)
        return _Result(1, '', 'Error: No such image: evonic-sandbox:latest')

    monkeypatch.setattr(docker_backend, '_docker', fake_docker)
    docker_backend._reset_image_cache()

    container_id, error = docker_backend._get_or_create_container(
        'test-session-5', workspace=str(tmp_path))

    assert container_id is None
    assert docker_backend.SANDBOX_IMAGE in error
    assert all(c[0] != 'run' for c in calls), 'no run, so no pull attempt, no hang'


def test_a_silent_runtime_is_named_not_waited_on(monkeypatch):
    def fake_docker(*a, **k):
        raise docker_backend.subprocess.TimeoutExpired(cmd='docker', timeout=5)

    monkeypatch.setattr(docker_backend, '_docker', fake_docker)
    docker_backend._reset_image_cache()

    error = docker_backend.sandbox_image_error()

    assert 'did not answer within 5s' in error
    assert 'not responding' in error


class _FakeProc:
    pid = 4242

    def __init__(self, returncode):
        self.returncode = returncode


def _run_bash_with_poll(monkeypatch, returncode):
    backend = docker_backend.DockerBackend(session_id='pit-373-bash')
    monkeypatch.setattr(
        docker_backend, '_get_or_create_container', lambda *a, **k: ('cid', None))
    monkeypatch.setattr(docker_backend, '_docker_popen', lambda *a, **k: _FakeProc(returncode))
    monkeypatch.setattr(
        docker_backend.DockerBackend, '_poll_proc', staticmethod(lambda *a, **k: (None, None)))
    return backend.run_bash('true', timeout=7, env={})


def test_a_timed_out_bash_call_says_timed_out(monkeypatch):
    result = _run_bash_with_poll(monkeypatch, returncode=None)

    assert result['error'] == 'Execution timed out after 7s'
    assert result['exit_code'] == -1


def test_a_killed_bash_call_still_says_stopped_by_user(monkeypatch):
    result = _run_bash_with_poll(monkeypatch, returncode=-15)

    assert result['error'] == 'Execution stopped by user'
    assert result['exit_code'] == -9


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
