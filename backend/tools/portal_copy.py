"""
portal_copy — copy files between workspace and portal paths, or between portals.

Supports binary files of any size. Files > 10MB are transferred in the background
with a job_id returned for polling via copy_status.
"""

import os
import threading
import time
import uuid

from backend.tools._portal import is_portal_path, resolve_portal_path
from backend.tools._workspace import resolve_workspace_path
from backend.tools.lib.transfer_engine import TransferEngine, backend_type_name, _ASYNC_THRESHOLD

try:
    from config import SANDBOX_WORKSPACE as _WORKSPACE_ROOT
except ImportError:
    _WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _resolve_path(agent, path):
    """Resolve a source or destination path to (backend, real_path).

    Returns (backend, real_path) on success, or (None, error_message) on failure.
    """
    agent_id = (agent or {}).get('id')

    if is_portal_path(path):
        return resolve_portal_path(agent_id, path)

    # Workspace path: resolve through the agent's execution backend
    from backend.tools.lib.exec_backend import registry
    session_id = (agent or {}).get('session_id') or 'default'
    backend = registry.get_backend(session_id, agent)
    real_path = resolve_workspace_path(agent, path, _WORKSPACE_ROOT)
    real_path = backend.resolve_path(real_path)
    return (backend, real_path)


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _run_transfer_job(job_id, engine, src_backend, src_path, dst_backend, dst_path, total_bytes):
    """Background thread target for async transfers."""
    from models.db import db
    db.update_transfer_job(job_id, {'status': 'running'})

    last_update = [0.0]

    def progress_cb(bytes_done):
        now = time.time()
        if now - last_update[0] >= 2:
            db.update_transfer_job(job_id, {'bytes_transferred': bytes_done})
            last_update[0] = now

    try:
        result = engine.copy_file(src_backend, src_path, dst_backend, dst_path,
                                  total_bytes=total_bytes, progress_cb=progress_cb)
        if 'error' in result:
            db.update_transfer_job(job_id, {
                'status': 'failed',
                'error_msg': result['error'],
                'completed_at': _now(),
            })
        else:
            db.update_transfer_job(job_id, {
                'status': 'completed',
                'bytes_transferred': total_bytes,
                'completed_at': _now(),
            })
    except Exception as e:
        db.update_transfer_job(job_id, {
            'status': 'failed',
            'error_msg': str(e),
            'completed_at': _now(),
        })


def execute(agent, args: dict) -> dict:
    source = args.get('source', '').strip()
    destination = args.get('destination', '').strip()

    if not source:
        return {'error': "Missing required argument: 'source'"}
    if not destination:
        return {'error': "Missing required argument: 'destination'"}

    # Resolve source
    src_backend, src_path = _resolve_path(agent, source)
    if src_backend is None:
        return {'error': src_path}

    # Resolve destination
    dst_backend, dst_path = _resolve_path(agent, destination)
    if dst_backend is None:
        return {'error': dst_path}

    # Verify source exists and get size
    stat = src_backend.file_stat(src_path)
    if not stat.get('exists'):
        return {'error': f'Source file not found: {source}'}
    total_bytes = stat.get('size', 0)

    engine = TransferEngine()

    if total_bytes <= _ASYNC_THRESHOLD:
        # Synchronous transfer
        result = engine.copy_file(src_backend, src_path, dst_backend, dst_path,
                                  total_bytes=total_bytes)
        if 'error' in result:
            return result
        return {
            'result': 'success',
            'source': source,
            'destination': destination,
            'bytes_copied': total_bytes,
        }
    else:
        # Async transfer
        from models.db import db
        agent_id = (agent or {}).get('id', '')
        session_id = (agent or {}).get('session_id', 'default')
        job_id = uuid.uuid4().hex[:12]
        db.create_transfer_job({
            'id': job_id,
            'agent_id': agent_id,
            'session_id': session_id,
            'source_path': source,
            'dest_path': destination,
            'source_backend_type': backend_type_name(src_backend),
            'dest_backend_type': backend_type_name(dst_backend),
            'total_bytes': total_bytes,
        })

        thread = threading.Thread(
            target=_run_transfer_job,
            args=(job_id, engine, src_backend, src_path, dst_backend, dst_path, total_bytes),
            daemon=True,
        )
        thread.start()

        size_mb = total_bytes / (1024 * 1024)
        return {
            'result': 'transfer_started',
            'job_id': job_id,
            'total_bytes': total_bytes,
            'message': f'File is {size_mb:.1f}MB — transferring in background. '
                       f'Poll with copy_status(job_id="{job_id}").',
        }
