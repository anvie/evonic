"""
transfer_engine.py — strategy-based file copy engine for portal_copy.

Selects the optimal transfer method based on source/destination backend types
and performs the copy using native protocols (SFTP, chunked RPC, shutil, docker cp).
"""

import base64
import logging
import os
import shutil
import tempfile

_logger = logging.getLogger(__name__)

_CHUNK_SIZE = 192 * 1024       # 192KB raw = ~256KB base64
_ASYNC_THRESHOLD = 10 * 1024 * 1024  # 10MB


def backend_type_name(backend) -> str:
    """Return a short type string for a backend instance."""
    cls = type(backend).__name__
    if cls == 'LocalBackend' or cls == 'LocalPortalBackend':
        return 'local'
    if cls == 'SSHBackend' or cls == 'RemoteWorkplaceBackend':
        return 'ssh'
    if cls == 'CloudWorkplaceBackend':
        return 'evonet'
    if cls == 'DockerBackend':
        return 'docker'
    return 'unknown'


def _get_ssh_backend(backend):
    """Unwrap RemoteWorkplaceBackend to get the underlying SSHBackend."""
    if hasattr(backend, '_ssh'):
        return backend._ssh
    return backend


def _same_host(a, b) -> bool:
    """Check if two SSH backends point to the same host."""
    a = _get_ssh_backend(a)
    b = _get_ssh_backend(b)
    return (getattr(a, '_host', None) == getattr(b, '_host', None)
            and getattr(a, '_port', None) == getattr(b, '_port', None))


def _same_workplace(a, b) -> bool:
    """Check if two Evonet backends point to the same workplace."""
    return (getattr(a, '_workplace_id', None) == getattr(b, '_workplace_id', None))


class TransferEngine:
    """Orchestrates file copies between heterogeneous backends."""

    def copy_file(self, src_backend, src_path, dst_backend, dst_path,
                  total_bytes=0, progress_cb=None):
        """Copy a file from src to dst. Returns {'ok': True} or {'error': str}."""
        strategy = self._pick_strategy(src_backend, dst_backend)
        _logger.info("portal_copy: %s -> %s via %s (%d bytes)",
                      backend_type_name(src_backend), backend_type_name(dst_backend),
                      strategy.__name__, total_bytes)
        return strategy(src_backend, src_path, dst_backend, dst_path,
                        total_bytes, progress_cb)

    def _pick_strategy(self, src, dst):
        src_t = backend_type_name(src)
        dst_t = backend_type_name(dst)

        # Same-type shortcuts
        if src_t == 'local' and dst_t == 'local':
            return self._copy_local_to_local
        if src_t == 'ssh' and dst_t == 'ssh' and _same_host(src, dst):
            return self._copy_ssh_same_host
        if src_t == 'evonet' and dst_t == 'evonet' and _same_workplace(src, dst):
            return self._copy_evonet_same_host

        # SFTP direct
        if src_t == 'local' and dst_t == 'ssh':
            return self._copy_local_to_ssh
        if src_t == 'ssh' and dst_t == 'local':
            return self._copy_ssh_to_local

        # Evonet chunked
        if src_t == 'local' and dst_t == 'evonet':
            return self._copy_local_to_evonet
        if src_t == 'evonet' and dst_t == 'local':
            return self._copy_evonet_to_local

        # Docker: stage through host temp
        if src_t == 'docker' or dst_t == 'docker':
            return self._copy_via_docker_temp

        # Cross-backend fallback (SSH<->Evonet, SSH<->SSH diff host)
        return self._copy_via_temp

    # ------------------------------------------------------------------
    # Strategy implementations
    # ------------------------------------------------------------------

    def _copy_local_to_local(self, src, src_path, dst, dst_path, total_bytes, progress_cb):
        try:
            os.makedirs(os.path.dirname(dst_path) or '.', exist_ok=True)
            shutil.copy2(src_path, dst_path)
            if progress_cb:
                progress_cb(total_bytes)
            return {'ok': True}
        except Exception as e:
            return {'error': str(e)}

    def _copy_local_to_ssh(self, src, src_path, dst, dst_path, total_bytes, progress_cb):
        ssh = _get_ssh_backend(dst)
        return ssh.sftp_upload(src_path, dst_path, progress_cb=progress_cb)

    def _copy_ssh_to_local(self, src, src_path, dst, dst_path, total_bytes, progress_cb):
        ssh = _get_ssh_backend(src)
        os.makedirs(os.path.dirname(dst_path) or '.', exist_ok=True)
        return ssh.sftp_download(src_path, dst_path, progress_cb=progress_cb)

    def _copy_ssh_same_host(self, src, src_path, dst, dst_path, total_bytes, progress_cb):
        import shlex
        ssh = _get_ssh_backend(src)
        r = ssh._exec(
            f'mkdir -p {shlex.quote(os.path.dirname(dst_path))} && '
            f'cp {shlex.quote(src_path)} {shlex.quote(dst_path)}',
            '', 300,
        )
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or 'remote cp failed'}
        if progress_cb:
            progress_cb(total_bytes)
        return {'ok': True}

    def _copy_evonet_same_host(self, src, src_path, dst, dst_path, total_bytes, progress_cb):
        import shlex
        r = src.run_bash(
            f'mkdir -p {shlex.quote(os.path.dirname(dst_path))} && '
            f'cp {shlex.quote(src_path)} {shlex.quote(dst_path)}',
            300, {},
        )
        if r.get('exit_code', 1) != 0:
            return {'error': r.get('stderr', '') or 'remote cp failed'}
        if progress_cb:
            progress_cb(total_bytes)
        return {'ok': True}

    def _copy_local_to_evonet(self, src, src_path, dst, dst_path, total_bytes, progress_cb):
        try:
            with open(src_path, 'rb') as f:
                offset = 0
                while True:
                    chunk = f.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    is_last = len(chunk) < _CHUNK_SIZE
                    b64 = base64.b64encode(chunk).decode('ascii')
                    result = dst.write_file_b64(dst_path, b64, offset, is_last)
                    if 'error' in result:
                        return result
                    offset += len(chunk)
                    if progress_cb:
                        progress_cb(offset)
                # Handle edge case: file size is exact multiple of chunk size
                if total_bytes > 0 and offset == total_bytes and not is_last:
                    result = dst.write_file_b64(dst_path, '', offset, True)
                    if 'error' in result:
                        return result
            return {'ok': True}
        except Exception as e:
            return {'error': str(e)}

    def _copy_evonet_to_local(self, src, src_path, dst, dst_path, total_bytes, progress_cb):
        try:
            os.makedirs(os.path.dirname(dst_path) or '.', exist_ok=True)
            with open(dst_path, 'wb') as f:
                offset = 0
                while True:
                    result = src.read_file_b64(src_path, offset, _CHUNK_SIZE)
                    if 'error' in result:
                        return result
                    data_b64 = result.get('data', '')
                    if not data_b64:
                        break
                    chunk = base64.b64decode(data_b64)
                    f.write(chunk)
                    offset += len(chunk)
                    if progress_cb:
                        progress_cb(offset)
                    if len(chunk) < _CHUNK_SIZE:
                        break
            return {'ok': True}
        except Exception as e:
            return {'error': str(e)}

    def _copy_via_temp(self, src, src_path, dst, dst_path, total_bytes, progress_cb):
        """Cross-backend copy by staging through a local temp file."""
        src_t = backend_type_name(src)
        dst_t = backend_type_name(dst)
        tmp_fd, tmp_path = tempfile.mkstemp(prefix='portal_copy_')
        os.close(tmp_fd)
        try:
            # Download to temp
            if src_t == 'ssh':
                r = _get_ssh_backend(src).sftp_download(src_path, tmp_path, progress_cb=None)
            elif src_t == 'evonet':
                r = self._copy_evonet_to_local(src, src_path, None, tmp_path, total_bytes, None)
            else:
                shutil.copy2(src_path, tmp_path)
                r = {'ok': True}
            if 'error' in r:
                return r

            staged_size = os.path.getsize(tmp_path)
            if progress_cb:
                progress_cb(staged_size // 2)  # halfway

            # Upload from temp
            if dst_t == 'ssh':
                r = _get_ssh_backend(dst).sftp_upload(tmp_path, dst_path, progress_cb=None)
            elif dst_t == 'evonet':
                r = self._copy_local_to_evonet(None, tmp_path, dst, dst_path, staged_size, None)
            else:
                os.makedirs(os.path.dirname(dst_path) or '.', exist_ok=True)
                shutil.copy2(tmp_path, dst_path)
                r = {'ok': True}
            if progress_cb:
                progress_cb(total_bytes)
            return r
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _copy_via_docker_temp(self, src, src_path, dst, dst_path, total_bytes, progress_cb):
        """Copy involving a Docker backend — stage through host temp file."""
        src_t = backend_type_name(src)
        dst_t = backend_type_name(dst)
        tmp_fd, tmp_path = tempfile.mkstemp(prefix='portal_copy_docker_')
        os.close(tmp_fd)
        try:
            # Extract from Docker or download from other backend
            if src_t == 'docker':
                r = src.docker_cp_out(src_path, tmp_path)
            elif src_t == 'ssh':
                r = _get_ssh_backend(src).sftp_download(src_path, tmp_path)
            elif src_t == 'evonet':
                r = self._copy_evonet_to_local(src, src_path, None, tmp_path, total_bytes, None)
            else:
                shutil.copy2(src_path, tmp_path)
                r = {'ok': True}
            if 'error' in r:
                return r

            if progress_cb:
                progress_cb(total_bytes // 2)

            # Inject into Docker or upload to other backend
            if dst_t == 'docker':
                r = dst.docker_cp_in(tmp_path, dst_path)
            elif dst_t == 'ssh':
                r = _get_ssh_backend(dst).sftp_upload(tmp_path, dst_path)
            elif dst_t == 'evonet':
                staged_size = os.path.getsize(tmp_path)
                r = self._copy_local_to_evonet(None, tmp_path, dst, dst_path, staged_size, None)
            else:
                os.makedirs(os.path.dirname(dst_path) or '.', exist_ok=True)
                shutil.copy2(tmp_path, dst_path)
                r = {'ok': True}
            if progress_cb:
                progress_cb(total_bytes)
            return r
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
