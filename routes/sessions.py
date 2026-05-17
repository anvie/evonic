import base64
import io
import os
import threading
import uuid

from flask import Blueprint, render_template, jsonify, request, send_file

from models.db import db

sessions_bp = Blueprint('sessions', __name__)

# Allowed file types for web upload
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'pdf', 'docx', 'txt', 'csv', 'md', 'json', 'xml', 'html', 'css', 'js', 'py', 'sh', 'yaml', 'yml', 'toml', 'log', 'sql', 'zip', 'tar', 'gz'}
ALLOWED_MIME_TYPES = {
    'image/png', 'image/jpeg', 'image/gif', 'image/webp',
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'text/plain', 'text/csv', 'text/markdown', 'text/xml', 'text/html', 'text/css', 'text/javascript',
    'text/x-python', 'text/x-shellscript', 'text/x-yaml', 'text/x-toml',
    'application/json',
    'application/zip', 'application/x-tar', 'application/gzip',
}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB per file
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'attachments', 'web')


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
    return jsonify({'sessions': sessions, 'total': total})


@sessions_bp.route('/api/sessions/<session_id>')
def api_get_session(session_id):
    session = db.get_session_with_details(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    messages = db.get_session_messages_full(session_id)
    return jsonify({'session': session, 'messages': messages})


@sessions_bp.route('/api/sessions/<session_id>/poll')
def api_session_poll(session_id):
    """Poll for new messages since after_id."""
    after_id = request.args.get('after', 0, type=int)
    messages = db.get_new_messages(session_id, after_id)
    return jsonify({'messages': messages})


@sessions_bp.route('/api/sessions/<session_id>/reply', methods=['POST'])
def api_session_reply(session_id):
    data = request.get_json()
    text = (data.get('text') or '').strip()
    file_ids = data.get('file_ids')  # list of file refs from upload
    perspective = (data.get('perspective') or 'B').strip()
    from backend.agent_runtime import agent_runtime
    if perspective == 'A':
        ok = agent_runtime.send_as_user(session_id, text, file_ids=file_ids)
    else:
        ok = agent_runtime.send_as_bot(session_id, text)
    if not ok:
        return jsonify({'error': 'Session not found'}), 404
    # Signal the frontend to clear the UI for /clear commands
    is_clear = text.strip().startswith('/clear') if perspective == 'A' else False
    resp = {'success': True}
    if is_clear:
        resp['clear_ui'] = True
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
    db.delete_session(session_id)
    return jsonify({'success': True})


@sessions_bp.route('/api/sessions/clear-all', methods=['POST'])
def api_clear_all_sessions():
    """Delete all chat sessions, messages, summaries, and attachments
    across all agents."""
    db.clear_all_sessions()
    return jsonify({'success': True})


@sessions_bp.route('/api/attachments/clear-all', methods=['POST'])
def api_clear_all_attachments():
    """Delete every stored attachment (DB rows + on-disk files) across all
    agents and sessions, without touching chat sessions/messages."""
    deleted, freed = db.delete_all_attachments()
    return jsonify({'success': True, 'deleted': deleted, 'freed_bytes': freed})


# ─── File Upload ────────────────────────────────────────────────────────────

def _safe_filename(name):
    """Sanitize a filename to a safe ASCII slug, max 120 chars."""
    if not name:
        return 'file'
    cleaned = ''.join(c if c.isalnum() or c in '._-' else '_' for c in name)[:120]
    return cleaned or 'file'


@sessions_bp.route('/api/sessions/<session_id>/upload', methods=['POST'])
def api_session_upload(session_id):
    """Upload one or more files to a session. Accepts multipart/form-data.

    Returns list of file references with attachment_id, filename, mime_type,
    url (for download), and for images: a base64 data_url.
    """
    session = db.get_session_with_details(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': 'No files provided'}), 400

    agent_id = session['agent_id']
    results = []

    for f in files:
        filename = f.filename or 'unnamed'
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

        # Validate extension
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify({
                'error': f'File type not allowed: {ext}',
                'rejected': filename,
            }), 400

        # Read file data
        data = f.read()
        if len(data) > MAX_FILE_SIZE:
            return jsonify({
                'error': f'File too large: {filename} ({len(data)} bytes > {MAX_FILE_SIZE})',
                'rejected': filename,
            }), 400

        # Validate MIME type
        mime_type = f.content_type or 'application/octet-stream'
        if mime_type not in ALLOWED_MIME_TYPES:
            return jsonify({
                'error': f'MIME type not allowed: {mime_type}',
                'rejected': filename,
            }), 400

        # Save file to disk
        safe_name = _safe_filename(filename)
        file_uuid = str(uuid.uuid4())
        storage_dir = os.path.join(UPLOAD_DIR, agent_id, session_id)
        os.makedirs(storage_dir, exist_ok=True)
        file_path = os.path.join(storage_dir, f"{file_uuid}_{safe_name}")

        with open(file_path, 'wb') as fout:
            fout.write(data)

        # Save to DB
        attachment_id = db.save_attachment(
            agent_id=agent_id,
            session_id=session_id,
            filename=safe_name,
            file_path=file_path,
            external_user_id=session['external_user_id'],
            channel_id=session.get('channel_id'),
            channel_type='web',
            original_filename=filename,
            mime_type=mime_type,
            file_type=ext,
            size_bytes=len(data),
        )

        # Build reference
        ref = {
            'attachment_id': attachment_id,
            'filename': filename,
            'mime_type': mime_type,
            'url': f'/uploads/{session_id}/{safe_name}',
            'size': len(data),
        }

        # For images, also include a base64 data URL
        is_image = mime_type.startswith('image/')
        if is_image:
            b64 = base64.b64encode(data).decode()
            ref['data_url'] = f'data:{mime_type};base64,{b64}'

        results.append(ref)

    return jsonify({'files': results}), 200


@sessions_bp.route('/uploads/<session_id>/<path:filename>')
def serve_upload(session_id, filename):
    """Serve an uploaded file. Only the session owner can access."""
    session = db.get_session_with_details(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    agent_id = session['agent_id']
    safe = os.path.basename(filename)  # prevent path traversal
    file_path = os.path.join(UPLOAD_DIR, agent_id, session_id, safe)

    if not os.path.isfile(file_path):
        return jsonify({'error': 'File not found'}), 404

    return send_file(file_path, mimetype=None, as_attachment=False)
