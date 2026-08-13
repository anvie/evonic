"""Real backend implementation for the read_attachment tool.

Reads user-uploaded file attachments stored under data/attachments/<agent_id>/.
Enforces per-agent isolation, reuses the existing read_file pagination core for
text content, and returns metadata for binary files. PDFs are analyzed by the
separate analyze_pdf tool using native model document input.
"""

import json
import os
from typing import Any, Dict, Optional

from backend.tools.read_file import read_file as _read_text_file


_ATTACHMENTS_ROOT = os.path.join('data', 'attachments')
_TEXTISH_MIMES = {
    'application/json',
    'application/xml',
    'application/x-yaml',
    'application/yaml',
    'application/csv',
    'application/javascript',
    'application/x-sh',
    'application/sql',
}

_TEXTISH_EXTS = {
    '.txt', '.md', '.markdown', '.log',
    '.json', '.yaml', '.yml', '.xml', '.csv', '.tsv',
    '.py', '.js', '.ts', '.tsx', '.jsx', '.java', '.go', '.rs',
    '.c', '.cc', '.cpp', '.h', '.hpp', '.rb', '.php', '.kt', '.swift',
    '.html', '.htm', '.css', '.scss', '.sql', '.sh', '.toml', '.ini',
    '.cfg', '.conf', '.env',
}


def _is_textish(mime_type: Optional[str], path: str) -> bool:
    """Return True if file is text-like by mime or extension."""
    if mime_type:
        m = mime_type.lower()
        if m.startswith('text/'):
            return True
        if m in _TEXTISH_MIMES:
            return True
    ext = os.path.splitext(path)[1].lower()
    return ext in _TEXTISH_EXTS


def _is_pdf(mime_type: Optional[str], path: str) -> bool:
    if mime_type and mime_type.lower() == 'application/pdf':
        return True
    return path.lower().endswith('.pdf')


def _agent_root(agent_id: str) -> str:
    return os.path.realpath(os.path.join(_ATTACHMENTS_ROOT, agent_id))


def _path_within_agent(path: str, agent_id: str) -> bool:
    """Check that `path` resolves inside the agent's attachments root."""
    real = os.path.realpath(path)
    root = _agent_root(agent_id)
    # Ensure prefix boundary with separator
    if real == root:
        return False
    return real.startswith(root + os.sep)


def _format_metadata(row: Optional[Dict[str, Any]], fallback_path: str,
                     workplace_path: Optional[str] = None) -> str:
    """Return a JSON metadata block for binary attachments."""
    if row:
        meta = {
            'filename': row.get('original_filename') or row.get('filename'),
            'mime_type': row.get('mime_type'),
            'file_type': row.get('file_type'),
            'size_bytes': row.get('size_bytes'),
            'created_at': row.get('created_at'),
            'path': row.get('file_path'),
        }
    else:
        try:
            size = os.path.getsize(fallback_path)
        except OSError:
            size = None
        meta = {
            'filename': os.path.basename(fallback_path),
            'mime_type': None,
            'file_type': None,
            'size_bytes': size,
            'created_at': None,
            'path': fallback_path,
        }
    if workplace_path and workplace_path != meta.get('path'):
        meta['workplace_path'] = workplace_path
    return (
        "[Attachment metadata — binary file, not directly readable as text]\n\n"
        + json.dumps(meta, indent=2)
    )


def execute(agent, args: dict) -> dict:
    """Tool entrypoint. Returns a dict or a string result."""
    agent = agent or {}
    agent_id = agent.get('id') or ''
    if not agent_id:
        return {"error": "Agent context is missing — cannot resolve attachment ownership."}

    attachment_id = args.get('attachment_id')
    raw_path = args.get('path')
    offset = args.get('offset') or 1
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 1

    from models.db import db

    row: Optional[Dict[str, Any]] = None
    resolved_path: Optional[str] = None

    if attachment_id is not None:
        try:
            attachment_id = int(attachment_id)
        except (TypeError, ValueError):
            return {"error": "Invalid attachment_id — must be an integer."}
        row = db.get_attachment(attachment_id)
        if not row:
            return {
                "error": (
                    f"Attachment ID {attachment_id} was not found. Use the numeric "
                    "'Attachment ID' shown in the attachment metadata; do not use "
                    "a session ID or a number inferred from the file path."
                )
            }
        if row['agent_id'] != agent_id and not agent.get('is_super'):
            return {"error": "Access denied — attachment belongs to a different agent."}
        resolved_path = row.get('file_path')
        if not resolved_path or not os.path.isfile(resolved_path):
            return {"error": "Attachment file is missing on disk. It may have been moved by save_artifact, manually deleted, or expired via retention cleanup."}
    elif raw_path:
        # Path-based access: resolve and enforce agent root prefix.
        target_agent_id = agent_id
        if agent.get('is_super'):
            # Super agents may read any agent's attachment by path; still must
            # resolve within data/attachments/<some_agent>/.
            real = os.path.realpath(raw_path)
            root = os.path.realpath(_ATTACHMENTS_ROOT)
            if not real.startswith(root + os.sep):
                return {"error": "Access denied — path is outside the attachments root."}
            resolved_path = real
        else:
            if not _path_within_agent(raw_path, target_agent_id):
                return {"error": "Access denied — path is outside this agent's attachments directory."}
            resolved_path = os.path.realpath(raw_path)
        if not os.path.isfile(resolved_path):
            return {"error": "Attachment file not found at the provided path."}
    else:
        return {"error": "Provide either 'attachment_id' or 'path'."}

    mime_type = (row or {}).get('mime_type')

    # PDF bytes are consumed on the host by analyze_pdf, so no workplace sync
    # is needed just to return their metadata and native-analysis guidance.
    if _is_pdf(mime_type, resolved_path):
        return {
            "result": (
                _format_metadata(row, resolved_path)
                + "\n\nPDF contents are not parsed locally. Call `analyze_pdf` "
                + (f"with attachment_id={attachment_id}." if attachment_id is not None
                   else "with the numeric Attachment ID.")
            )
        }

    # If the agent operates in a remote workplace (SSH/tunnel/etc.), ensure the
    # attachment file is available on the remote filesystem.  This is needed
    # because _read_text_file routes through the execution backend when the
    # agent has a workplace, and would otherwise fail to find the file.
    try:
        from backend.tools._ensure_workplace_file import ensure_workplace_file
        workplace_path = ensure_workplace_file(resolved_path, agent)
    except (ImportError, RuntimeError) as e:
        return {"error": f"Failed to prepare attachment for workplace: {e}"}

    # Dispatch — use the workplace path for operations that route through the
    # execution backend, and the original host path for direct filesystem access.
    if _is_textish(mime_type, resolved_path):
        return {"result": _read_text_file(workplace_path, offset=offset)}

    # Binary fallback — metadata only.
    return {"result": _format_metadata(row, resolved_path, workplace_path)}
