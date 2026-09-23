"""
Tool: fetch_artifact — copy files from host artifacts into the sandbox
execution environment (reverse of save_artifact).

Agents who need to inspect artifact images or other binary files inside
the sandbox can use this tool to fetch them from the host artifacts
directory into the execution backend (Docker container, SSH remote,
tunnel, or local filesystem).

Usage:
  - filename (required): name of the artifact to fetch
  - dest_path (optional): destination path in the sandbox; defaults to
    /workspace/<filename>
"""

import os

from backend.tools._workspace import effective_agent_id
from backend.tools.lib.simulation_scope import shared_agents_dir

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _artifacts_dir(agent_id: str, artifacts_root: str | None = None) -> str:
    root = artifacts_root or os.path.join(BASE_DIR, 'shared', 'agents')
    return os.path.join(root, agent_id, 'artifacts')


def execute(agent: dict, args: dict) -> dict:
    agent_id = effective_agent_id(agent)
    if not agent_id:
        return {'error': 'Agent ID not found in context'}

    filename = args.get('filename', '').strip()
    dest_path = args.get('dest_path', '').strip()

    if not filename:
        return {
            'error': 'The "filename" parameter is required. '
                     'Provide the name of an artifact file, e.g. filename="chart.png"'
        }

    # Security: prevent path traversal
    if '/' in filename or '\\' in filename or '..' in filename:
        return {
            'error': 'Invalid filename: must not contain "/", "\\", or "..". '
                     'Use a plain basename like "chart.png" or "output.json"'
        }

    artifacts_dir = _artifacts_dir(agent_id, shared_agents_dir(agent))
    source_path = os.path.join(artifacts_dir, filename)

    if not os.path.isfile(source_path):
        return {'error': f'Artifact not found: "{filename}". Use list_artifacts to see available files.'}

    # Local agents (no workplace, no sandbox) can access artifacts directly via
    # bash/runpy at the artifacts directory. Return the absolute path instead of
    # copying the file — there's no sandbox to copy into.
    from backend.tools.lib.simulation_scope import force_sandbox
    # A simulation forces an isolating backend and drops any workplace (which
    # may resolve to a remote host the sim temp-root workspace is absent on).
    forced = force_sandbox(agent)
    workplace_id = None if forced else agent.get('workplace_id')
    sandbox_enabled = True if forced else agent.get('sandbox_enabled', False)
    if not workplace_id and not sandbox_enabled:
        return {
            'result': 'You are running as a local agent and can access artifacts directly.',
            'filepath': source_path,
            'filename': filename,
            'hint': f'This file is at: {source_path}. Use read_file, bash, or runpy to access it directly.',
        }

    # Default destination: /workspace/<filename> in the execution environment
    if not dest_path:
        dest_path = os.path.join('/workspace', filename)

    try:
        # --- Read bytes from host artifacts directory ---
        with open(source_path, 'rb') as f:
            data = f.read()

        source_size = len(data)

        # --- Resolve execution backend ---

        if workplace_id:
            from backend.workplaces.manager import workplace_manager

            try:
                backend = workplace_manager.get_backend(
                    workplace_id, sandbox_enabled=sandbox_enabled
                )
            except RuntimeError as e:
                return {'error': f'Workplace error: {str(e)}'}
        elif sandbox_enabled:
            # Sandbox with no workplace: resolve path through execution backend
            from backend.tools.lib.exec_backend import registry as exec_registry
            session_id = agent.get('session_id', 'default')
            backend = exec_registry.get_backend(session_id, agent)
        else:
            # No workplace, no sandbox: use local backend
            from backend.tools.lib.backends.local_backend import LocalBackend
            backend = LocalBackend(session_id=agent.get('session_id', 'default'))

        # Resolve destination path when the backend has path translation
        resolved = backend.resolve_path(dest_path) if hasattr(backend, 'resolve_path') else dest_path

        # --- Write bytes to execution backend ---
        write_result = backend.write_file_bytes(resolved, data, create_dirs=True)
        if 'error' in write_result:
            return {'error': f'Failed to write to destination: {write_result["error"]}'}

        # Verify destination size
        file_stat = backend.file_stat(resolved)
        dest_size = file_stat.get('size', -1)

        if dest_size != source_size:
            # Try to clean up partial write
            try:
                backend.delete_file(resolved)
            except Exception:
                pass
            return {
                'error': (
                    f'Size mismatch: artifact has {source_size} bytes '
                    f'but destination has {dest_size} bytes. '
                    f'Destination cleaned up.'
                )
            }

        return {
            'result': 'Artifact fetched successfully',
            'filepath': dest_path,
            'filename': filename,
            'size': source_size,
        }

    except FileNotFoundError:
        return {'error': f'Artifact not found: "{filename}"'}
    except PermissionError:
        return {'error': f'Permission denied reading artifact: "{filename}"'}
    except Exception as e:
        return {'error': f'Failed to fetch artifact "{filename}": {str(e)}'}
