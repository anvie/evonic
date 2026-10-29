"""
WorkplaceManager — manages execution backends for Workplace objects.

Multiple agent sessions can share the same Workplace (same workplace_id).
For local workplaces, separate backends are maintained for sandboxed
vs. non-sandboxed execution. For tunnel, remote and bwrap workplaces,
one backend instance per workplace_id is shared across sessions.

Tunnel workplaces are 1:1 with an agent; their backend is created when Evonet connects.
Local, Remote and Bwrap workplaces' backends are created on first access and cached.
Bwrap workplaces run all commands inside a bubblewrap sandbox (Linux-only).
"""

import json
import logging
import threading
from typing import Optional

from backend.tools.lib.exec_backend import ExecutionBackend

_logger = logging.getLogger(__name__)


class WorkplaceManager:

    def __init__(self):
        self._backends: dict[tuple[str, bool], ExecutionBackend] = {}   # (workplace_id, sandbox_enabled) → backend
        self._lock = threading.Lock()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def get_backend(self, workplace_id: str, sandbox_enabled: bool = False) -> ExecutionBackend:
        """Return (or create) the backend for a workplace. Raises RuntimeError if not ready."""
        key = (workplace_id, sandbox_enabled)
        with self._lock:
            if key in self._backends:
                return self._backends[key]

        workplace = self._load_workplace(workplace_id)
        if not workplace:
            raise RuntimeError(f"Workplace '{workplace_id}' not found.")

        workplace_type = workplace.get('type')
        config = self._parse_config(workplace)

        if workplace_type == 'local':
            backend = self._create_local(config, sandbox_enabled=sandbox_enabled)
            with self._lock:
                self._backends[key] = backend
            self._set_status(workplace_id, 'connected')
            return backend

        if workplace_type == 'bwrap':
            bwrap_key = (workplace_id, False)   # sandbox flag is irrelevant for bwrap
            with self._lock:
                if bwrap_key in self._backends:
                    return self._backends[bwrap_key]
            backend = self._create_bwrap(workplace_id, workplace, config)
            with self._lock:
                self._backends[bwrap_key] = backend
            self._set_status(workplace_id, 'connected')
            return backend

        if workplace_type == 'remote':
            return self._connect_remote(workplace_id, config)

        if workplace_type == 'tunnel':
            tunnel_key = (workplace_id, False)
            with self._lock:
                backend = self._backends.get(tunnel_key)
            if backend is None:
                raise RuntimeError(
                    f"Tunnel Workplace '{workplace_id}' is not connected. "
                    "Please start Evonet on the target device."
                )
            return backend

        raise RuntimeError(f"Unknown workplace type: {workplace_type!r}")

    def connect(self, workplace_id: str) -> dict:
        """Explicitly trigger connection for a workplace. No-op for tunnel (Evonet connects)."""
        workplace = self._load_workplace(workplace_id)
        if not workplace:
            return {'ok': False, 'error': 'Workplace not found'}
        workplace_type = workplace.get('type')
        config = self._parse_config(workplace)

        if workplace_type == 'local':
            local_key = (workplace_id, False)
            with self._lock:
                if local_key not in self._backends:
                    self._backends[local_key] = self._create_local(config)
            self._set_status(workplace_id, 'connected')
            return {'ok': True, 'status': 'connected'}

        if workplace_type == 'bwrap':
            try:
                bwrap_key = (workplace_id, False)
                with self._lock:
                    exists = bwrap_key in self._backends
                if not exists:
                    backend = self._create_bwrap(workplace_id, workplace, config)
                    with self._lock:
                        self._backends[bwrap_key] = backend
                self._set_status(workplace_id, 'connected')
                return {'ok': True, 'status': 'connected'}
            except Exception as e:
                return {'ok': False, 'error': str(e)}  # _create_bwrap already set status='error'

        if workplace_type == 'remote':
            try:
                self._connect_remote(workplace_id, config)
                return {'ok': True, 'status': 'connected'}
            except Exception as e:
                return {'ok': False, 'error': str(e)}

        if workplace_type == 'tunnel':
            tunnel_key = (workplace_id, False)
            with self._lock:
                connected = tunnel_key in self._backends
            status = 'connected' if connected else 'disconnected'
            return {'ok': True, 'status': status, 'note': 'Tunnel workplaces connect when Evonet starts.'}

        return {'ok': False, 'error': f'Unknown workplace type: {workplace_type}'}

    def disconnect(self, workplace_id: str) -> dict:
        """Disconnect and destroy the backend for a workplace."""
        any_destroyed = False
        for sandbox_flag in (False, True):
            key = (workplace_id, sandbox_flag)
            with self._lock:
                backend = self._backends.pop(key, None)
            if backend is not None:
                any_destroyed = True
                try:
                    backend.destroy()
                except Exception as e:
                    _logger.warning("Error destroying backend for workplace %s (sandbox=%s): %s",
                                    workplace_id, sandbox_flag, e)
        if not any_destroyed:
            return {'ok': True, 'detail': 'No active backend.'}
        self._set_status(workplace_id, 'disconnected')
        return {'ok': True, 'status': 'disconnected'}

    def get_status(self, workplace_id: str) -> dict:
        """Return connection status of a workplace."""
        workplace = self._load_workplace(workplace_id)
        if not workplace:
            return {'status': 'not_found', 'workplace_id': workplace_id}
        with self._lock:
            backend = self._backends.get((workplace_id, True)) or self._backends.get((workplace_id, False))
        if backend is None:
            return {
                'status': workplace.get('status', 'disconnected'),
                'workplace_id': workplace_id,
                'type': workplace.get('type'),
                'error': workplace.get('error_msg'),
                'last_connected_at': workplace.get('last_connected_at'),
            }
        try:
            backend_status = backend.status()
        except Exception:
            backend_status = {}
        # For tunnel backends, the backend object persists across disconnects but
        # the actual WS may be gone — use evonet_connected to get the real state.
        if workplace.get('type') == 'tunnel':
            live = backend_status.get('evonet_connected', False)
            actual_status = 'connected' if live else 'disconnected'
        else:
            actual_status = 'connected'
        return {
            'status': actual_status,
            'workplace_id': workplace_id,
            'type': workplace.get('type'),
            'backend': backend_status,
            'last_connected_at': workplace.get('last_connected_at'),
        }

    def set_backend_workspace(self, workplace_id: str, workspace: str) -> None:
        """Update workspace path on any cached backends that support it.

        Called by ``/cd`` slash command for remote/tunnel agents so the
        in-memory backend reflects the new path immediately.
        """
        with self._lock:
            for (wid, _sandbox), backend in self._backends.items():
                if wid == workplace_id and hasattr(backend, 'set_workspace'):
                    backend.set_workspace(workspace)

    # -------------------------------------------------------------------------
    # Tunnel connector callbacks (called by ConnectorRelay)
    # -------------------------------------------------------------------------

    def on_connector_connected(self, workplace_id: str, ws, replay_ok: bool = False) -> None:
        workplace = self._load_workplace(workplace_id)
        if not workplace:
            _logger.warning("Connector connected for unknown workplace %s", workplace_id)
            return
        config = self._parse_config(workplace)
        from backend.workplaces.backends.tunnel_workplace import TunnelWorkplaceBackend
        tunnel_key = (workplace_id, False)
        with self._lock:
            existing = self._backends.get(tunnel_key)
            if isinstance(existing, TunnelWorkplaceBackend):
                backend = existing
            else:
                backend = TunnelWorkplaceBackend(
                    workplace_id=workplace_id,
                    workspace=config.get('workspace_path'),
                )
                self._backends[tunnel_key] = backend
        backend.on_ws_connected(ws, replay_ok=replay_ok)
        self._set_status(workplace_id, 'connected')
        # Reconnected Evonet device: refresh the agent PATH from its rc files.
        self._mark_path_stale(workplace_id)
        _logger.info("Tunnel workplace %s connected via Evonet", workplace_id)

    def on_connector_disconnected(self, workplace_id: str) -> None:
        tunnel_key = (workplace_id, False)
        with self._lock:
            backend = self._backends.get(tunnel_key)
        if backend is not None:
            from backend.workplaces.backends.tunnel_workplace import TunnelWorkplaceBackend
            if isinstance(backend, TunnelWorkplaceBackend):
                backend.on_ws_disconnected()
        self._set_status(workplace_id, 'disconnected')
        _logger.info("Tunnel workplace %s disconnected", workplace_id)

    def on_connector_message(self, workplace_id: str, data: dict) -> None:
        tunnel_key = (workplace_id, False)
        with self._lock:
            backend = self._backends.get(tunnel_key)
        if backend is not None:
            from backend.workplaces.backends.tunnel_workplace import TunnelWorkplaceBackend
            if isinstance(backend, TunnelWorkplaceBackend):
                backend.on_message(data)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _load_workplace(self, workplace_id: str) -> Optional[dict]:
        try:
            from models.db import db
            return db.get_workplace(workplace_id)
        except Exception as e:
            _logger.error("Failed to load workplace %s: %s", workplace_id, e)
            return None

    def _parse_config(self, workplace: dict) -> dict:
        cfg = workplace.get('config', '{}')
        if isinstance(cfg, str):
            try:
                return json.loads(cfg)
            except (json.JSONDecodeError, TypeError):
                return {}
        return cfg or {}

    def _mark_path_stale(self, workplace_id: str) -> None:
        """Ask shell_path to re-resolve the agent PATH after a (re)connect."""
        try:
            from backend.tools.lib.shell_path import mark_workplace_stale
            mark_workplace_stale(workplace_id)
        except Exception as e:
            _logger.debug("PATH re-resolution flag failed for workplace %s: %s",
                          workplace_id, e)

    def _set_status(self, workplace_id: str, status: str, error_msg: Optional[str] = None) -> None:
        try:
            from models.db import db
            db.update_workplace_status(workplace_id, status, error_msg)
        except Exception as e:
            _logger.warning("Failed to update status for workplace %s: %s", workplace_id, e)
        try:
            from backend.event_stream import event_stream
            event_stream.emit('workplace_status_changed', {
                'workplace_id': workplace_id,
                'status': status,
                'error_msg': error_msg,
            })
        except Exception:
            pass

    def _create_local(self, config: dict, sandbox_enabled: bool = False) -> ExecutionBackend:
        from backend.workplaces.backends.local_workplace import LocalWorkplaceBackend
        return LocalWorkplaceBackend(config=config, sandbox_enabled=sandbox_enabled)

    def _create_bwrap(self, workplace_id: str, workplace: dict, config: dict) -> ExecutionBackend:
        from backend.tools.lib.backends.bwrap_backend import BwrapBackend
        backend = BwrapBackend(
            session_id=f'workplace-bwrap-{workplace_id}',
            workspace=config.get('workspace_path'),
            agent_id=f'workplace-{workplace_id}',
            agent_name=workplace.get('name') or workplace_id,   # sandbox hostname = workplace name
        )
        st = backend.status()
        if not st.get('available'):
            msg = st.get('error') or 'bubblewrap is not available on this host.'
            self._set_status(workplace_id, 'error', msg)
            raise RuntimeError(msg)
        return backend

    def _connect_remote(self, workplace_id: str, config: dict) -> ExecutionBackend:
        self._set_status(workplace_id, 'connecting')
        try:
            from backend.workplaces.backends.remote_workplace import RemoteWorkplaceBackend
            backend = RemoteWorkplaceBackend(config=config, workplace_id=workplace_id)
            with self._lock:
                self._backends[(workplace_id, False)] = backend
            self._set_status(workplace_id, 'connected')
            # A fresh SSH session may see different rc-file state (e.g. ~/bin
            # added to PATH), so re-resolve the agent PATH on the next bash call.
            self._mark_path_stale(workplace_id)
            return backend
        except Exception as e:
            self._set_status(workplace_id, 'error', str(e))
            raise RuntimeError(f"Failed to connect remote workplace '{workplace_id}': {e}") from e


# Module-level singleton
workplace_manager = WorkplaceManager()
