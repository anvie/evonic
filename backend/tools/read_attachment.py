"""Real backend implementation for the read_attachment tool.

Reads user-uploaded file attachments stored under data/attachments/<agent_id>/.
Enforces per-agent isolation, reuses the existing read_file pagination core for
text content, and returns metadata for binary documents. Documents can be sent
to the separate analyze_document tool using native provider file input.
"""

import json
import os
from typing import Any, Dict, Optional

from backend.tools._document import (
    PDF,
    TEXT_EXTENSIONS,
    analysis_guidance,
    document_category,
    is_text_document,
)
from backend.tools.read_file import read_file as _read_text_file


_ATTACHMENTS_ROOT = os.path.join('data', 'attachments')
_TEXTISH_EXTS = set(TEXT_EXTENSIONS) | {'.csv', '.tsv', '.iif'}


def _is_textish(mime_type: Optional[str], path: str) -> bool:
    """Return True if file is text-like by mime or extension."""
    return is_text_document(path, mime_type)


def _is_pdf(mime_type: Optional[str], path: str) -> bool:
    return document_category(path, mime_type) == PDF


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
    original_name = (row or {}).get('original_filename') or resolved_path
    category = document_category(original_name, mime_type)

    # Binary documents are consumed on the host by analyze_document, so no
    # workplace sync is needed just to return metadata and native guidance.
    if category and not _is_textish(mime_type, original_name):
        guidance = analysis_guidance(
            original_name, mime_type, resolved_path, enabled=True
        )
        return {
            "result": (
                _format_metadata(row, resolved_path)
                + "\n\nDocument contents are not parsed locally. "
                + guidance
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
