from __future__ import annotations

import base64
import logging
import os
import re
import threading
import time

from flask import Blueprint, render_template, jsonify, request, send_file

from models.db import db
from backend.audit_logger import audit

_logger = logging.getLogger(__name__)

sessions_bp = Blueprint('sessions', __name__)

# ---------------------------------------------------------------------------
# File upload helpers
# ---------------------------------------------------------------------------

_IMAGE_MIMES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}


def _human_size(size_bytes: int | None) -> str:
    """Render a byte count as a human-friendly string."""
    if size_bytes is None or size_bytes < 0:
        return '0B'
    units = ['B', 'KB', 'MB', 'GB']
    n = float(size_bytes)
    for unit in units:
        if n < 1024 or unit == units[-1]:
            if unit == 'B':
                return f"{int(n)}{unit}"
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{int(size_bytes)}B"


from backend.tools._document import (
    SUPPORTED_DOCUMENT_EXTENSIONS,
    document_category,
)

_ALLOWED_EXTS = (
    _IMAGE_EXTS
    | set(SUPPORTED_DOCUMENT_EXTENSIONS)
)


def _sanitize_filename(name: str) -> str:
    """Strip path separators and collapse to safe chars."""
    name = os.path.basename(name)
    name = re.sub(r'[^\w.\-]', '_', name)
    return name[:200] or 'upload'


def _process_upload(file_storage, agent_id: str, session_id: str,
                    external_user_id: str, channel_id: str | None):
    """Process an uploaded file. Returns dict with keys:
    image_url, text_prefix, attachment_info (filename, mime_type, is_image).
    """
    import mimetypes
    from io import BytesIO

    original_name = file_storage.filename or 'upload'
    mime_type = file_storage.content_type or mimetypes.guess_type(original_name)[0] or 'application/octet-stream'
    ext = os.path.splitext(original_name)[1].lower()
    normalized_mime = mime_type.split(';', 1)[0].lower()
    generic_mime = normalized_mime in ('application/octet-stream', 'binary/octet-stream')
    is_image = ext in _IMAGE_EXTS and (normalized_mime in _IMAGE_MIMES or generic_mime)
    if (ext in _IMAGE_EXTS or normalized_mime.startswith('image/')) and not is_image:
        raise ValueError("Unsupported image format or MIME type does not match filename")
    category = None if is_image else document_category(original_name, mime_type)
    if not is_image and not category:
        raise ValueError(
            "Unsupported document format or MIME type does not match filename"
        )

    # Read file bytes
    file_bytes = file_storage.read()
    size_bytes = len(file_bytes)

    # Save to disk
    safe_name = _sanitize_filename(original_name)
    target_dir = os.path.join('data', 'attachments', agent_id, session_id)
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, f"{time.time_ns()}_{safe_name}")
    with open(target_path, 'wb') as f:
        f.write(file_bytes)

    # Persist attachment record
    file_type = 'photo' if is_image else 'document'
    attachment_id = db.save_attachment(
        agent_id=agent_id,
        session_id=session_id,
        filename=os.path.basename(target_path),
        file_path=target_path,
        external_user_id=external_user_id,
        channel_id=channel_id,
        channel_type='web',
        original_filename=original_name,
        mime_type=mime_type,
        file_type=file_type,
        size_bytes=size_bytes,
    )

    attachment_info = {
        'filename': original_name,
        'mime_type': mime_type,
        'is_image': is_image,
        'attachment_id': attachment_id,
        'size_bytes': size_bytes,
        'file_path': target_path,
    }

    # --- Sync to remote workplace if applicable (Phase 3: proactive sync) ---
    workplace_path = None
    try:
        agent = db.get_agent(agent_id)
        if agent and (agent.get('workplace_id') or agent.get('sandbox_enabled')):
            from backend.tools._ensure_workplace_file import ensure_workplace_file
            workplace_path = ensure_workplace_file(target_path, agent)
            attachment_info['workplace_path'] = workplace_path
    except Exception as e:
        _logger.warning(
            "Failed to sync attachment to remote workplace for agent %s: %s",
            agent_id, e
        )

    # --- Image: convert to JPEG base64 for LLM vision ---
    if is_image:
        try:
            from PIL import Image
            img = Image.open(BytesIO(file_bytes))
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGB')
            # Cap dimensions for reasonable base64 size
            max_dim = 2048
            if max(img.size) > max_dim:
                img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            buf = BytesIO()
            img.save(buf, format='JPEG', quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
            image_url = f"data:image/jpeg;base64,{b64}"
        except Exception as e:
            _logger.error("Failed to convert image to base64: %s", e)
            image_url = None
        return {'image_url': image_url, 'text_prefix': None, 'attachment_info': attachment_info}

    # Documents stay metadata-only. analyze_document sends the original bytes
    # to a compatible provider; read_attachment remains available for exact text.
    return {'image_url': None, 'text_prefix': None, 'attachment_info': attachment_info}


@sessions_bp.route('/sessions')
def sessions():
    """Chat sessions dashboard"""
    return render_template('sessions.html')


@sessions_bp.route('/api/sessions')
def api_list_sessions():
    search = request.args.get('search', '').strip() or None
    limit = min(request.args.get('limit', 50, type=int), 500)
    offset = request.args.get('offset', 0, type=int)
    exclude_test = request.args.get('exclude_test', '1') != '0'
    sessions, total = db.get_all_sessions(search=search, limit=limit, offset=offset,
                                          exclude_test=exclude_test)
    # Tag sessions that are currently being processed by an agent
    from backend.agent_runtime import agent_runtime
    for s in sessions:
        s['is_active'] = agent_runtime._is_busy(s['id'])
    return jsonify({'sessions': sessions, 'total': total})


@sessions_bp.route('/api/sessions/<session_id>')
def api_get_session(session_id):
    session = db.get_session_with_details(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    limit = request.args.get('limit', 200, type=int)
    before_id = request.args.get('before_id', None, type=int)
    messages, has_more = db.get_session_messages_full(session_id, limit=limit, before_id=before_id)
    return jsonify({'session': session, 'messages': messages, 'has_more': has_more})


@sessions_bp.route('/api/sessions/<session_id>/poll')
def api_session_poll(session_id):
    """Poll for new messages since after_id."""
    after_id = request.args.get('after', 0, type=int)
    messages = db.get_new_messages(session_id, after_id)
    return jsonify({'messages': messages})


@sessions_bp.route('/api/sessions/<session_id>/reply', methods=['POST'])
def api_session_reply(session_id):
    # Support both JSON and multipart/form-data
    if request.content_type and request.content_type.startswith('multipart/form-data'):
        text = (request.form.get('text') or '').strip()
        perspective = (request.form.get('perspective') or 'A').strip()
        file = request.files.get('file')
    else:
        data = request.get_json()
        text = (data.get('text') or '').strip()
        perspective = (data.get('perspective') or 'A').strip()
        file = None

    if not text and not file:
        return jsonify({'error': 'Text or file is required'}), 400

    from backend.agent_runtime import agent_runtime

    image_url = None
    upload_meta = None
    attachment_info = None

    if file:
        if perspective != 'A':
            return jsonify({'error': 'File upload only supported in user perspective'}), 400

        # Validate extension
        ext = os.path.splitext(file.filename or '')[1].lower()
        if ext not in _ALLOWED_EXTS:
            return jsonify({'error': f'File type {ext} not supported'}), 400

        # Resolve session → agent for attachment storage
        session = db.get_session_with_details(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        agent_id = session['agent_id']

        # Check if attachments are enabled for this agent
        cfg = db.get_agent_attachment_config(agent_id)
        if not cfg.get('enabled'):
            return jsonify({'error': 'File attachments are not enabled for this agent. Enable them in agent settings.'}), 400

        max_bytes = cfg.get('max_size_mb', 20) * 1024 * 1024
        file.seek(0, os.SEEK_END)
        fsize = file.tell()
        file.seek(0)
        if fsize > max_bytes:
            return jsonify({'error': f'File too large (max {cfg.get("max_size_mb", 20)}MB)'}), 400

        try:
            result = _process_upload(
                file, agent_id, session_id,
                session.get('external_user_id', ''),
                session.get('channel_id'),
            )
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            _logger.error("Upload processing failed: %s", e, exc_info=True)
            return jsonify({'error': 'Failed to process uploaded file'}), 500

        image_url = result['image_url']
        attachment_info = result['attachment_info']
        upload_meta = {'attachment_info': attachment_info}

        # Build [Attached: ... id=N] line so agents can use read_attachment(attachment_id=N)
        att = result['attachment_info']
        info_line = (
            f"[Attached: {att['filename']} "
            f"({att['mime_type']}, {_human_size(att['size_bytes'])}) "
            f"id={att['attachment_id']} path={att['file_path']}]"
        )

        # Prepend info_line to text; for non-image files also prepend extracted content
        if result['text_prefix']:
            text = f"{info_line}\n\n{result['text_prefix']}\n\n{text}" if text else f"{info_line}\n\n{result['text_prefix']}"
        else:
            text = f"{info_line}\n\n{text}" if text else info_line

    if perspective == 'A':
        result = agent_runtime.send_as_user(session_id, text,
                                            image_url=image_url, metadata=upload_meta)
    else:
        result = agent_runtime.send_as_bot(session_id, text)

    if not result:
        return jsonify({'error': 'Session not found'}), 404

    # Build response — for slash commands, include the response text directly
    resp = {'success': True}
    if isinstance(result, str):
        # send_as_user returned the slash command response text
        resp['slash_command'] = True
        resp['response'] = result
        if text.strip().startswith('/clear'):
            resp['clear_ui'] = True
    elif text.strip().startswith('/clear'):
        resp['clear_ui'] = True
    if attachment_info:
        resp['attachment_info'] = attachment_info
    return jsonify(resp)


@sessions_bp.route('/api/sessions/<session_id>/stop', methods=['POST'])
def api_session_stop(session_id):
    """Send a stop signal to interrupt the agent's current processing loop."""
    from backend.agent_runtime import agent_runtime
    agent_runtime.request_stop(session_id)
    return jsonify({'success': True})


@sessions_bp.route('/api/sessions/<session_id>/bot', methods=['PUT'])
def api_session_toggle_bot(session_id):
    data = request.get_json()
    enabled = data.get('enabled', True)
    db.set_session_bot_enabled(session_id, enabled)
    return jsonify({'success': True, 'bot_enabled': enabled})


@sessions_bp.route('/api/sessions/<session_id>/summary')
def api_session_summary(session_id):
    """Get the conversation summary for a session."""
    summary = db.get_summary(session_id)
    if summary:
        return jsonify({'summary': summary['summary'],
                        'last_message_id': summary['last_message_id'],
                        'message_count': summary['message_count'],
                        'updated_at': summary.get('updated_at')})
    return jsonify({'summary': None})


@sessions_bp.route('/api/sessions/<session_id>/summarize', methods=['POST'])
def api_force_summarize(session_id):
    """Force a fresh summarization for the session."""
    session = db.get_session_with_details(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    agent = db.get_agent(session['agent_id'])
    if not agent:
        return jsonify({'error': 'Agent not found'}), 404
    from backend.agent_runtime import agent_runtime
    threading.Thread(
        target=agent_runtime._maybe_summarize,
        args=(agent, session_id),
        daemon=True
    ).start()
    return jsonify({'success': True})


@sessions_bp.route('/api/sessions/<session_id>', methods=['DELETE'])
def api_delete_session(session_id):
    result = db.delete_session(session_id)
    audit.log_session(user_id='admin', session_id=session_id, action='delete', ip=request.remote_addr or '')
    return jsonify({'success': result})


@sessions_bp.route('/api/sessions/clear-all', methods=['POST'])
def api_clear_all_sessions():
    """Delete all chat sessions, messages, summaries, and attachments
    across all agents."""
    db.clear_all_sessions()
    audit.log_session(user_id='admin', session_id='*', action='clear_all', ip=request.remote_addr or '')
    return jsonify({'success': True})


@sessions_bp.route('/api/attachments/clear-all', methods=['POST'])
def api_clear_all_attachments():
    """Delete every stored attachment (DB rows + on-disk files) across all
    agents and sessions, without touching chat sessions/messages."""
    deleted, freed = db.delete_all_attachments()
    return jsonify({'success': True, 'deleted': deleted, 'freed_bytes': freed})


@sessions_bp.route('/api/attachments/<int:attachment_id>/download')
def api_attachment_download(attachment_id):
    """Serve an attachment file as a downloadable file."""
    attachment = db.get_attachment(attachment_id)
    if not attachment:
        return jsonify({'error': 'Attachment not found'}), 404

    file_path = attachment.get('file_path', '')
    if not file_path or not os.path.isfile(file_path):
        return jsonify({'error': 'File unavailable'}), 404

    original_filename = attachment.get('original_filename') or os.path.basename(file_path)
    mime_type = attachment.get('mime_type') or 'application/octet-stream'

    return send_file(
        file_path,
        mimetype=mime_type,
        as_attachment=True,
        download_name=original_filename,
    )


@sessions_bp.route('/api/attachments/<int:attachment_id>/view')
def api_attachment_view(attachment_id):
    """Serve an attachment file for inline viewing (browser display)."""
    attachment = db.get_attachment(attachment_id)
    if not attachment:
        return jsonify({'error': 'Attachment not found'}), 404

    file_path = attachment.get('file_path', '')
    if not file_path or not os.path.isfile(file_path):
        return jsonify({'error': 'File unavailable'}), 404

    original_filename = attachment.get('original_filename') or os.path.basename(file_path)
    mime_type = attachment.get('mime_type') or 'application/octet-stream'

    return send_file(
        file_path,
        mimetype=mime_type,
        as_attachment=False,
        download_name=original_filename,
    )
