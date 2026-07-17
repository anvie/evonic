"""Real backend implementation for the read_attachment tool.

Reads user-uploaded file attachments stored under data/attachments/<agent_id>/.
Enforces per-agent isolation, reuses the existing read_file pagination core for
text content, attempts PDF text extraction via pypdf when available, and falls
back to a metadata block for opaque binary files.
"""

import json
import os
from typing import Any, Dict, Optional

from backend.tools.read_file import read_file as _read_text_file


_ATTACHMENTS_ROOT = os.path.join('data', 'attachments')
_PDF_TEXT_CAP_BYTES = 100 * 1024  # 100 KB cap on extracted PDF text

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


def _read_pdf_text(path: str, offset: int) -> str:
    """Extract text from a PDF using pypdf if available; paginate by line."""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = None
        return (
            "[PDF text extraction unavailable: install 'pypdf' to enable]\n\n"
            + json.dumps({
                'filename': os.path.basename(path),
                'mime_type': 'application/pdf',
                'size_bytes': size,
                'path': path,
            }, indent=2)
        )

    try:
        reader = PdfReader(path)
    except Exception as e:  # pragma: no cover - depends on file contents
        return f"Error: Failed to open PDF: {e}"

    out_parts = []
    total = 0
    truncated = False
    for i, page in enumerate(reader.pages):
        try:
            txt = page.extract_text() or ''
        except Exception:
            txt = ''
        header = f"--- Page {i + 1} ---\n"
        chunk = header + txt + "\n"
        if total + len(chunk) > _PDF_TEXT_CAP_BYTES:
            remaining = _PDF_TEXT_CAP_BYTES - total
            if remaining > 0:
                out_parts.append(chunk[:remaining])
            truncated = True
            break
        out_parts.append(chunk)
        total += len(chunk)

    body = ''.join(out_parts)
    if not body.strip():
        return (
            "[PDF contains no extractable text — it may be image-only or scanned. "
            "Returning metadata instead.]\n\n"
            + json.dumps({
                'filename': os.path.basename(path),
                'mime_type': 'application/pdf',
                'path': path,
            }, indent=2)
        )

    # Paginate by lines using read_file core for consistency. Write to a temp
    # buffer is unnecessary — render manually since content already in memory.
    lines = body.splitlines()
    total_lines = len(lines)
    start_idx = max(0, min(offset - 1, max(total_lines - 1, 0)))
    chunk_chars = 8000
    output_lines = []
    chars = 0
    end_idx = start_idx
    for i in range(start_idx, total_lines):
        line_str = f"{i + 1}: {lines[i].rstrip()}"
        if chars + len(line_str) + 1 > chunk_chars and output_lines:
            break
        output_lines.append(line_str)
        chars += len(line_str) + 1
        end_idx = i + 1
    header_line = (
        f"[PDF: {os.path.basename(path)} | {total_lines} extracted lines | "
        f"showing lines {start_idx + 1}-{end_idx}"
        + (" | text truncated at 100KB" if truncated else "")
        + "]"
    )
    content = "\n".join(output_lines)
    if end_idx < total_lines:
        remaining = total_lines - end_idx
        footer = (
            f"\n[...{remaining} lines remaining. "
            f"Call read_attachment with offset={end_idx + 1} to continue.]"
        )
        return f"{header_line}\n\n{content}{footer}"
    return f"{header_line}\n\n{content}"


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

    # If the agent operates in a remote workplace (SSH/tunnel/etc.), ensure the
    # attachment file is available on the remote filesystem.  This is needed
    # because _read_text_file routes through the execution backend when the
    # agent has a workplace, and would otherwise fail to find the file.
    try:
        from backend.tools._ensure_workplace_file import ensure_workplace_file
        workplace_path = ensure_workplace_file(resolved_path, agent)
    except (ImportError, RuntimeError) as e:
        return {"error": f"Failed to prepare attachment for workplace: {e}"}

    mime_type = (row or {}).get('mime_type')

    # Dispatch — use the workplace path for operations that route through the
    # execution backend, and the original host path for direct filesystem access.
    if _is_pdf(mime_type, resolved_path):
        return {"result": _read_pdf_text(resolved_path, offset)}

    if _is_textish(mime_type, resolved_path):
        return {"result": _read_text_file(workplace_path, offset=offset)}

    # Binary fallback — metadata only.
    return {"result": _format_metadata(row, resolved_path, workplace_path)}
