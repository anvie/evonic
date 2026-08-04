"""
Kanban Board Plugin — Flask Route Handlers

Provides:
- GET /board/kanban → Kanban board UI
- GET /api/kanban/tasks → List all tasks
- POST /api/kanban/tasks → Create task
- PUT /api/kanban/tasks/<int:task_id> → Update task
- DELETE /api/kanban/tasks/<int:task_id> → Delete task
- POST /api/kanban/agent/check → Force immediate agent scan
- GET  /api/kanban/agent/propose → Propose task→agent assignment plan
- POST /api/kanban/agent/auto_assign → LLM-powered task→agent matching
- POST /api/kanban/agent/assign → Confirm and execute batch assignment

Agent Access Control:
- No agent ID = regular UI user → full access
- Super agent ID → full access
- Other agent IDs → read-all, write only tasks assigned to them
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from flask import Blueprint, render_template, jsonify, request, send_file
from werkzeug.utils import secure_filename

from plugins.kanban.db import ATTACHMENTS_DIR, kanban_db

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SUPER_AGENT_ID = 'super'

# --- Attachment upload policy -------------------------------------------------
ALLOWED_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
ALLOWED_IMAGE_MIMES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


def _now():
    return datetime.now(timezone.utc).isoformat()


def _get_super_agent_id():
    from backend.plugin_manager import plugin_manager
    cfg = plugin_manager.get_plugin_config('kanban')
    return cfg.get('SUPER_AGENT_ID', DEFAULT_SUPER_AGENT_ID) or DEFAULT_SUPER_AGENT_ID


def _get_request_agent_id():
    return request.headers.get('X-Agent-Id', '').strip() or None


def _get_owner_name():
    try:
        from models.db import db
        return db.get_setting('owner_name') or 'UI User'
    except Exception:
        return 'UI User'


def _attachment_file_path(task_id, stored_name: str) -> str:
    """Absolute path of a stored attachment file on disk."""
    return os.path.join(ATTACHMENTS_DIR, f'task_{task_id}', stored_name)


def _attachment_with_url(att: dict) -> dict:
    """Return an attachment dict enriched with its public file URL."""
    att = dict(att)
    att['url'] = f'/api/kanban/attachments/{att["id"]}/file'
    return att


def _comment_with_attachments(comment: dict) -> dict:
    comment = dict(comment)
    comment['attachments'] = [
        _attachment_with_url(att)
        for att in kanban_db.get_attachments_for_comment(comment['id'])
    ]
    return comment


def _task_with_attachments(task: dict) -> dict:
    """Return a task dict enriched with its attachment list (with URLs)."""
    task = dict(task)
    task['attachments'] = [_attachment_with_url(a) for a in kanban_db.get_attachments(task['id'])]
    return task


def _remove_attachment_file(att: dict) -> None:
    """Best-effort removal of an attachment's file and its (now possibly empty) task dir."""
    try:
        path = _attachment_file_path(att.get('task_id'), att.get('stored_name'))
        if path and os.path.isfile(path):
            os.remove(path)
        task_dir = os.path.dirname(path)
        if task_dir and os.path.isdir(task_dir) and not os.listdir(task_dir):
            os.rmdir(task_dir)
    except OSError:
        pass


def _is_super_agent(agent_id):
    return agent_id and agent_id == _get_super_agent_id()


def _check_write_access(task, action='edit'):
    """Check if the requester can perform the given action.

    action: 'create', 'edit', or 'delete'
    Returns (allowed: bool, error_message: str|None)
    """
    agent_id = _get_request_agent_id()
    if not agent_id or _is_super_agent(agent_id):
        return True, None

    # All permission checks now read from skill config
    action_to_setting = {
        'create': 'create_task_super_only',
        'edit': 'edit_task_super_only',
        'delete': 'delete_task_super_only',
    }
    setting_name = action_to_setting.get(action)
    if setting_name:
        try:
            from backend.skills_manager import skills_manager
            config = skills_manager.get_skill_config('kanban')
            super_only = bool(config.get(setting_name, True))
        except Exception:
            super_only = True  # fail closed
        if super_only:
            if action == 'create':
                return False, 'Forbidden: only the super agent can create tasks'
            if action == 'edit':
                return False, 'Forbidden: only the super agent can edit tasks'
            if action == 'delete':
                return False, 'Forbidden: only the super agent can delete tasks'

    if action == 'create':
        return True, None

    if task is None:
        return False, 'Forbidden'
    if task.get('assignee') == agent_id:
        return True, None
    return False, 'Forbidden: you are not the assignee of this task'


def _emit(event_name, task):
    try:
        from backend.event_stream import event_stream
        event_stream.emit(event_name, {'task': task})
    except Exception:
        pass


def _enhance_completion(messages):
    """Run Kanban enhancement with the global default model and its fallback."""
    from backend.llm_client import LLMClient, get_llm_client
    from models.db import db

    completion_kwargs = {
        'messages': messages,
        'temperature': 0.3,
        'enable_thinking': False,
        'max_tokens': 4096,
    }
    result = get_llm_client().chat_completion(**completion_kwargs)
    if result.get('success'):
        return result

    fallback_id = db.get_setting('default_model_fallback_id', '')
    fallback_model = db.get_model_by_id(fallback_id) if fallback_id else None
    if not fallback_model or not fallback_model.get('enabled', True):
        return result

    return LLMClient(model_config=fallback_model).chat_completion(**completion_kwargs)


def _extract_reply(result):
    """Extract the text reply from an _enhance_completion result.

    Handles the nested {response: {choices: ...}} structure, falls back to
    reasoning_content when content is empty, and strips thinking tags.
    Returns None when no usable reply is present.
    """
    inner = result.get("response", result)
    choices = inner.get("choices", [])
    if not choices:
        return None
    msg = choices[0].get("message", {})
    reply = (msg.get("content") or "").strip()
    if not reply:
        reply = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    if not reply:
        return None
    if "<think" in reply or "<reasoning" in reply:
        reply = re.sub(r"<(?:think|thinking)>.*?</(?:think|thinking)>", "", reply, flags=re.DOTALL)
        reply = re.sub(r"<reasoning>.*?</reasoning>", "", reply, flags=re.DOTALL)
        reply = reply.strip()
    return reply or None


def _parse_enhance_reply(reply):
    """Parse the ---TITLE--- / ---DESCRIPTION--- / ---END--- delimiter format.

    Returns (title, description) or None when the format is invalid.
    """
    title_match = re.search(
        r"^\s*---TITLE---\s*\n(.*?)(?=\n\s*---DESCRIPTION---)",
        reply,
        re.DOTALL | re.MULTILINE,
    )
    desc_match = re.search(
        r"^\s*---DESCRIPTION---\s*\n(.*?)(?:\n\s*---END---\s*$|\Z)",
        reply,
        re.DOTALL | re.MULTILINE,
    )
    if not title_match or not desc_match:
        return None
    title = title_match.group(1).strip()
    desc = desc_match.group(1).strip()
    if not title or not desc:
        return None
    return title, desc


# Indonesian stopwords used by _looks_non_english as a lightweight guard against
# the LLM echoing non-English (e.g. Indonesian) input back verbatim.
_ID_STOPWORDS = frozenset(
    "yang untuk dengan dan dari agar supaya bisa dapat akan tidak jika pada ke di ini itu "
    "juga sudah telah atau karena namun tetapi sesudah sebelum ketika saat sebagai secara "
    "terhadap tentang antara bagi oleh membuat menjadi lebih sangat harus perlu ingin mau "
    "ada adalah tersebut kamu saya kita kami anda sehingga maka pun ya belum pernah lagi "
    "masih sedang memang walaupun meskipun kecuali tanpa hingga sampai sebab akibat "
    "bagaimana mengapa kapan".split()
)


def _looks_non_english(text, threshold=0.15):
    """Heuristic: text is probably not English when a large share of its words
    are common Indonesian stopwords (the observed failure mode for this endpoint)."""
    words = re.findall(r"[a-zA-Z]+", (text or "").lower())
    if not words:
        return False
    return sum(1 for w in words if w in _ID_STOPWORDS) / len(words) >= threshold


_ENGLISH_TRANSLATION_PROMPT = (
    "You are a translator. Rewrite the following task title and description in clear, "
    "natural English, preserving the exact meaning. Output ONLY the translated text in "
    "this exact format:\n"
    "---TITLE---\n<title in English>\n"
    "---DESCRIPTION---\n<description in English>\n"
    "---END---\n"
    "Do NOT include any text before ---TITLE--- or after ---END---."
)


def create_blueprint():
    bp = Blueprint('kanban', __name__, template_folder=os.path.join(PLUGIN_DIR, 'templates'))

    @bp.route('/board/kanban')
    def kanban_page():
        return render_template('kanban.html', super_agent_id=_get_super_agent_id())

    @bp.route('/api/kanban/tasks', methods=['GET'])
    def kanban_api_get():
        tasks = kanban_db.get_all()
        agent_id = _get_request_agent_id()
        mine = request.args.get('mine', '').lower() in ('1', 'true')
        if mine and agent_id and not _is_super_agent(agent_id):
            tasks = [t for t in tasks if t.get('assignee') == agent_id]
        # Enrich tasks with dependency info
        all_deps = kanban_db.get_all_dependencies()
        done_ids = {t['id'] for t in kanban_db.get_all() if t.get('status') == 'done'}
        for task in tasks:
            tid = task['id']
            deps = all_deps.get(tid, [])
            task['deps'] = deps
            task['deps_met'] = all(d in done_ids for d in deps)
        tasks = [_task_with_attachments(t) for t in tasks]
        return jsonify({'tasks': tasks})

    @bp.route('/api/kanban/tasks/available-deps', methods=['GET'])
    def kanban_api_available_deps():
        """Return tasks eligible as dependencies (todo or in-progress), excluding a given task."""
        exclude_id = request.args.get('exclude', type=int)
        tasks = kanban_db.get_all()
        result = [
            {'id': t['id'], 'title': t['title'], 'status': t['status']}
            for t in tasks
            if t.get('status') in ('todo', 'in-progress') and t['id'] != exclude_id
        ]
        return jsonify({'tasks': result})

    @bp.route('/api/kanban/tasks', methods=['POST'])
    def kanban_api_create():
        is_allowed, error = _check_write_access(None, action='create')
        if not is_allowed:
            return jsonify({'error': error}), 403

        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        title = data.get('title', '').strip()
        if not title:
            return jsonify({'error': 'Title is required'}), 400

        # Validate assignee matches a real agent (prevents names being stored as IDs)
        assignee_raw = data.get('assignee')
        if assignee_raw and assignee_raw.strip():
            from models.db import db as main_db
            if not main_db.get_agent(assignee_raw.strip()):
                return jsonify({'error': f'Agent "{assignee_raw}" not found. Use an agent ID, not a display name.'}), 400

        now = _now()
        new_task = {
            'title': title,
            'description': data.get('description', '').strip(),
            'status': data.get('status', 'todo'),
            'priority': data.get('priority', 'low'),
            'assignee': data.get('assignee') or None,
            'completed_at': None,
            'created_at': now,
            'updated_at': now,
        }
        task = kanban_db.create(new_task)
        kanban_db.log_task_created(task['id'])
        # Handle dependencies
        deps = data.get('deps')
        if deps and isinstance(deps, list):
            try:
                kanban_db.set_dependencies(task['id'], deps)
            except ValueError as e:
                kanban_db.delete(task['id'])
                return jsonify({'error': str(e)}), 400
        task['deps'] = kanban_db.get_dependencies(task['id'])
        task['deps_met'] = not kanban_db.has_unmet_dependencies(task['id'])
        task = _task_with_attachments(task)
        _emit('kanban_task_created', task)
        return jsonify({'task': task}), 201

    # ─── Enhance (LLM) ─────────────────────────────────────────────

    @bp.route('/api/kanban/enhance', methods=['POST'])
    def kanban_api_enhance():
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        description = data.get('description', '').strip()
        if not description:
            return jsonify({'error': 'Description is required'}), 400

        system_prompt = (
            "You are a professional task-writing assistant. Your job is to:\n"
            "1. Enhance the user's task description to be more detailed, structured, and actionable, but not too much, simple and lean but descriptive.\n"
            "2. Generate a concise, descriptive title for the task.\n"
            "3. ALWAYS write the enhanced title and description in English, regardless of the language of the user's input. "
            "If the input is written in Indonesian or another language, translate and reformulate its meaning into clear, natural English.\n"
            "Return your answer in this exact format:\n"
            "---TITLE---\n"
            "<your English title here>\n"
            "---DESCRIPTION---\n"
            "<your enhanced English description here>\n"
            "---END---\n"
            "Do NOT include any text before ---TITLE--- or after ---END---.\n"
            "All of your output must be in English."
        )

        user_prompt = f"Title: {data.get('title', '').strip() or '(not provided)'}\n\nDescription: {description}"

        result = _enhance_completion([
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ])

        # Check for API-level errors
        if not result.get('success'):
            err_type = result.get('error_type', 'unknown')
            err_detail = result.get('error_detail', str(result))
            print(f"[ENHANCE] LLM call failed: error_type={err_type} detail={err_detail[:300]}")
            if err_type == 'generation_timeout':
                return jsonify({'error': 'LLM ran out of tokens. Please try a shorter description.'}), 500
            return jsonify({'error': 'LLM API error'}), 500

        reply = _extract_reply(result)
        if not reply:
            print("[ENHANCE] no usable reply in result")
            return jsonify({'error': 'LLM returned no choices'}), 500

        parsed = _parse_enhance_reply(reply)
        if not parsed:
            print(f"[ENHANCE] delimiter parse failed, reply preview: {reply[:500]}")
            return jsonify({'error': 'Failed to parse LLM response'}), 500

        enhanced_title, enhanced_desc = parsed

        # Enforce English output: if the enhanced fields are not English (e.g. the model
        # echoed Indonesian input), run a dedicated translation pass.
        if _looks_non_english(enhanced_title) or _looks_non_english(enhanced_desc):
            print("[ENHANCE] output not English, running translation pass")
            translation = _enhance_completion([
                {'role': 'system', 'content': _ENGLISH_TRANSLATION_PROMPT},
                {
                    'role': 'user',
                    'content': f"---TITLE---\n{enhanced_title}\n---DESCRIPTION---\n{enhanced_desc}",
                },
            ])
            translated_reply = _extract_reply(translation) if translation.get('success') else None
            reparsed = _parse_enhance_reply(translated_reply) if translated_reply else None
            if reparsed:
                enhanced_title, enhanced_desc = reparsed

        return jsonify({'title': enhanced_title, 'description': enhanced_desc})

    @bp.route('/api/kanban/tasks/<int:task_id>', methods=['PUT'])
    def kanban_api_update(task_id):
        task = kanban_db.get(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        is_allowed, error = _check_write_access(task)
        if not is_allowed:
            return jsonify({'error': error}), 403

        # Guard: prevent assignee changes on done or archived tasks
        if 'assignee' in data:
            if task.get('status') == 'done' or task.get('archived_at'):
                return jsonify({'error': 'Cannot change assignee on a completed or archived task.'}), 400

        # Validate assignee matches a real agent
        if 'assignee' in data and data['assignee']:
            from models.db import db as main_db
            if not main_db.get_agent(data['assignee'].strip()):
                return jsonify({'error': f'Agent "{data["assignee"]}" not found. Use an agent ID, not a display name.'}), 400

        updatable = ['title', 'description', 'status', 'priority', 'assignee', 'completed_at']
        fields = {k: data[k] for k in updatable if k in data}

        if data.get('status') == 'done' and not task.get('completed_at'):
            fields['completed_at'] = _now()

        if data.get('status') == 'in-progress' and not task.get('started_at'):
            fields['started_at'] = _now()

        changes = []
        if 'status' in fields and fields['status'] != task.get('status'):
            changes.append(f"status: {task.get('status')} → {fields['status']}")
            kanban_db.log_task_status_change(task_id, task.get('status'), fields['status'])
        if 'title' in fields and fields['title'] != task.get('title'):
            changes.append(f"title: {task.get('title')} → {fields['title']}")
        if 'description' in fields and fields['description'] != task.get('description'):
            changes.append("description updated")
        if 'priority' in fields and fields['priority'] != task.get('priority'):
            changes.append(f"priority: {task.get('priority')} → {fields['priority']}")
        if 'assignee' in fields:
            changes.append(f"assignee: {task.get('assignee')} → {fields['assignee']}")
        if changes:
            kanban_db.log_task_updated(task_id, ', '.join(changes))

        fields['updated_at'] = _now()
        updated = kanban_db.update(task_id, fields)
        # Handle dependencies if provided
        if 'deps' in data:
            deps = data['deps']
            if not isinstance(deps, list):
                return jsonify({'error': "'deps' must be a list of task IDs"}), 400
            try:
                kanban_db.set_dependencies(task_id, deps)
            except ValueError as e:
                return jsonify({'error': str(e)}), 400
        updated['deps'] = kanban_db.get_dependencies(task_id)
        updated['deps_met'] = not kanban_db.has_unmet_dependencies(task_id)
        updated = _task_with_attachments(updated)
        _emit('kanban_task_updated', updated)
        return jsonify({'task': updated})

    # ─── Comments ──────────────────────────────────────────────────────────────

    @bp.route('/api/kanban/tasks/<int:task_id>/comments', methods=['POST'])
    def kanban_api_add_comment(task_id):
        task = kanban_db.get(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        is_allowed, error = _check_write_access(task)
        if not is_allowed:
            return jsonify({'error': error}), 403

        if request.mimetype == 'application/json':
            data = request.get_json(silent=True) or {}
            content = (data.get('content') or '').strip()
            files = []
        else:
            content = (request.form.get('content') or '').strip()
            files = [f for f in request.files.getlist('files') if f and f.filename]
        if not content and not files:
            return jsonify({'error': 'Content or at least one attachment is required'}), 400

        validated = []
        for f in files:
            filename = f.filename or ''
            ext = os.path.splitext(filename)[1].lower()
            mime = (f.mimetype or '').lower()
            if ext not in ALLOWED_IMAGE_EXTENSIONS or (
                mime and mime not in ALLOWED_IMAGE_MIMES and mime != 'application/octet-stream'
            ):
                return jsonify({'error': f'Invalid file type: "{filename}". Only image files are supported.'}), 400
            f.stream.seek(0, os.SEEK_END)
            size = f.stream.tell()
            f.stream.seek(0)
            if size > MAX_FILE_SIZE:
                return jsonify({'error': f'File too large: "{filename}"'}), 400
            validated.append((f, filename, mime, size))

        agent_id = _get_request_agent_id()
        author = agent_id or _get_owner_name()
        comment = kanban_db.add_comment(task_id, content, author)
        if validated:
            task_dir = os.path.join(ATTACHMENTS_DIR, f'task_{task_id}')
            os.makedirs(task_dir, exist_ok=True)
            for f, filename, mime, size in validated:
                stored_name = f'{uuid.uuid4().hex}_{secure_filename(filename)}'
                f.save(os.path.join(task_dir, stored_name))
                kanban_db.add_attachment(
                    task_id, filename, stored_name, mime, size, author, comment_id=comment['id']
                )
        comment = _comment_with_attachments(comment)
        kanban_db.add_activity(task_id, 'commented', f'Comment added by {author}')
        return jsonify({'comment': comment}), 201

    @bp.route('/api/kanban/tasks/<int:task_id>/comments', methods=['GET'])
    def kanban_api_get_comments(task_id):
        task = kanban_db.get(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        comments = [_comment_with_attachments(c) for c in kanban_db.get_comments(task_id)]
        return jsonify({'comments': comments})

    @bp.route('/api/kanban/comments/<int:comment_id>', methods=['DELETE'])
    def kanban_api_delete_comment(comment_id):
        comment = kanban_db.get_comment(comment_id)
        if not comment:
            return jsonify({'error': 'Comment not found'}), 404

        task = kanban_db.get(comment['task_id'])
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        # Check write access on the parent task
        is_allowed, error = _check_write_access(task)
        if not is_allowed:
            return jsonify({'error': error}), 403

        # Only the comment author or super agent can delete
        agent_id = _get_request_agent_id()
        owner_name = _get_owner_name()
        is_owner = (comment.get('author') == agent_id or comment.get('author') == owner_name)
        if not is_owner and not (agent_id and _is_super_agent(agent_id)):
            return jsonify({'error': 'You can only delete your own comments'}), 403

        for att in kanban_db.delete_attachments_for_comment(comment_id):
            _remove_attachment_file(att)
        deleted = kanban_db.delete_comment(comment_id)
        if not deleted:
            return jsonify({'error': 'Failed to delete comment'}), 500

        kanban_db.add_activity(comment['task_id'], 'comment_deleted',
                               f'Comment by {comment.get("author", "unknown")} deleted')
        return jsonify({'success': True})


    # ─── Activity Log ──────────────────────────────────────────────────────────

    @bp.route('/api/kanban/tasks/<int:task_id>/activity', methods=['GET'])
    def kanban_api_get_activity(task_id):
        task = kanban_db.get(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        activity = kanban_db.get_activity(task_id)
        return jsonify({'activity': activity})

    # --- Process Recorder ---

    @bp.route('/api/kanban/tasks/<int:task_id>/process', methods=['GET'])
    def kanban_api_get_process(task_id):
        task = kanban_db.get(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        # Check if feature is enabled
        try:
            from backend.plugin_manager import plugin_manager
            cfg = plugin_manager.get_plugin_config('kanban')
        except Exception:
            cfg = {}
        if not cfg.get('ENABLE_PROCESS_RECORDER', False):
            return jsonify({
                'enabled': False,
                'message': 'Process recorder is disabled. You can enable it in the Kanban plugin settings.',
                'settings_url': '/plugins/kanban',
            })

        log = kanban_db.get_process_log(task_id)
        if not log:
            return jsonify({'enabled': True, 'messages': None})
        return jsonify({
            'enabled': True,
            'messages': log.get('messages', []),
            'agent_id': log.get('agent_id'),
            'session_id': log.get('session_id'),
            'recorded_at': log.get('created_at'),
        })

    @bp.route('/api/kanban/tasks/<int:task_id>', methods=['DELETE'])
    def kanban_api_delete(task_id):
        task = kanban_db.get(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        agent_id = _get_request_agent_id()
        if agent_id and not _is_super_agent(agent_id):
            return jsonify({'error': 'Forbidden: only super agent or UI can delete tasks'}), 403

        # Remove attachment files from disk before deleting the task row.
        for att in kanban_db.delete_attachments_for_task(task_id):
            _remove_attachment_file(att)
        kanban_db.delete(task_id)
        return jsonify({'success': True})

    # ──────── Attachments ───────────────────────────────────────────────────────

    @bp.route('/api/kanban/tasks/<int:task_id>/attachments', methods=['GET'])
    def kanban_api_list_attachments(task_id):
        task = kanban_db.get(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        attachments = [_attachment_with_url(a) for a in kanban_db.get_attachments(task_id)]
        return jsonify({'attachments': attachments})

    @bp.route('/api/kanban/tasks/<int:task_id>/attachments', methods=['POST'])
    def kanban_api_upload_attachments(task_id):
        task = kanban_db.get(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        is_allowed, error = _check_write_access(task)
        if not is_allowed:
            return jsonify({'error': error}), 403

        files = request.files.getlist('files')
        files = [f for f in files if f and f.filename]
        if not files:
            return jsonify({'error': 'No files selected. Please choose at least one image file.'}), 400

        agent_id = _get_request_agent_id()
        uploaded_by = agent_id or _get_owner_name()
        task_dir = os.path.join(ATTACHMENTS_DIR, f'task_{task_id}')

        # Validate everything first so a bad file never leaves partial uploads behind.
        validated = []
        for f in files:
            filename = f.filename or ''
            ext = os.path.splitext(filename)[1].lower()
            mime = (f.mimetype or '').lower()
            ext_ok = ext in ALLOWED_IMAGE_EXTENSIONS
            mime_ok = mime in ALLOWED_IMAGE_MIMES or mime in ('', 'application/octet-stream')
            if not (ext_ok and mime_ok):
                return jsonify({
                    'error': f'Invalid file type: "{filename}". Only image files are supported (PNG, JPG, JPEG, GIF, WEBP, BMP).'
                }), 400
            f.stream.seek(0, os.SEEK_END)
            size = f.stream.tell()
            f.stream.seek(0)
            if size > MAX_FILE_SIZE:
                return jsonify({
                    'error': f'File too large: "{filename}" ({size // (1024 * 1024)} MB). Maximum size is {MAX_FILE_SIZE // (1024 * 1024)} MB per file.'
                }), 400
            validated.append((f, filename, ext, mime, size))

        os.makedirs(task_dir, exist_ok=True)
        saved = []
        for f, filename, ext, mime, size in validated:
            stored_name = f'{uuid.uuid4().hex}_{secure_filename(filename)}'
            f.save(os.path.join(task_dir, stored_name))
            att = kanban_db.add_attachment(task_id, filename, stored_name, mime, size, uploaded_by)
            if att:
                kanban_db.add_activity(task_id, 'attachment_added', f'{filename} uploaded by {uploaded_by}')
                saved.append(_attachment_with_url(att))

        if not saved:
            return jsonify({'error': 'No valid image files were uploaded.'}), 400
        return jsonify({'attachments': saved}), 201

    @bp.route('/api/kanban/attachments/<int:attachment_id>/file', methods=['GET'])
    def kanban_api_get_attachment_file(attachment_id):
        att = kanban_db.get_attachment(attachment_id)
        if not att:
            return jsonify({'error': 'Attachment not found'}), 404
        path = _attachment_file_path(att['task_id'], att['stored_name'])
        if not os.path.isfile(path):
            return jsonify({'error': 'Attachment file is missing on disk'}), 404
        return send_file(
            path,
            mimetype=att.get('mime_type') or 'application/octet-stream',
            as_attachment=False,
            download_name=att['filename'],
        )

    @bp.route('/api/kanban/attachments/<int:attachment_id>', methods=['DELETE'])
    def kanban_api_delete_attachment(attachment_id):
        att = kanban_db.get_attachment(attachment_id)
        if not att:
            return jsonify({'error': 'Attachment not found'}), 404

        task = kanban_db.get(att['task_id'])
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        is_allowed, error = _check_write_access(task)
        if not is_allowed:
            return jsonify({'error': error}), 403

        deleted = kanban_db.delete_attachment(attachment_id)
        if deleted:
            _remove_attachment_file(deleted)
            kanban_db.add_activity(
                att['task_id'],
                'attachment_removed',
                f'{deleted["filename"]} removed by {_get_request_agent_id() or _get_owner_name()}',
            )
        return jsonify({'success': True})

    # ───────── Archive ─────────

    @bp.route('/api/kanban/tasks/<int:task_id>/archive', methods=['POST'])
    def kanban_api_archive(task_id):
        task = kanban_db.get(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        is_allowed, error = _check_write_access(task)
        if not is_allowed:
            return jsonify({'error': error}), 403

        updated = kanban_db.archive_task(task_id)
        kanban_db.log_task_updated(task_id, 'task archived')
        _emit('kanban_task_updated', updated)
        return jsonify({'task': updated})

    @bp.route('/api/kanban/tasks/<int:task_id>/unarchive', methods=['POST'])
    def kanban_api_unarchive(task_id):
        task = kanban_db.get(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        is_allowed, error = _check_write_access(task)
        if not is_allowed:
            return jsonify({'error': error}), 403

        updated = kanban_db.unarchive_task(task_id)
        kanban_db.log_task_updated(task_id, 'task unarchived')
        _emit('kanban_task_updated', updated)
        return jsonify({'task': updated})

    @bp.route('/api/kanban/archived', methods=['GET'])
    def kanban_api_get_archived():
        tasks = [_task_with_attachments(t) for t in kanban_db.get_archived()]
        return jsonify({'tasks': tasks})

    @bp.route('/api/kanban/archived/count', methods=['GET'])
    def kanban_api_archived_count():
        count = kanban_db.count_archived()
        return jsonify({'count': count})

    @bp.route('/api/kanban/archived/incomplete/count', methods=['GET'])
    def kanban_api_archived_incomplete_count():
        count = kanban_db.get_archived_incomplete_count()
        return jsonify({'count': count})

    @bp.route('/api/kanban/archived/clear', methods=['DELETE'])
    def kanban_api_clear_archived():
        agent_id = _get_request_agent_id()
        if agent_id and not _is_super_agent(agent_id):
            return jsonify({'error': 'Forbidden: only super agent or UI can clear archived tasks'}), 403

        count = kanban_db.clear_archived()
        return jsonify({'count': count})

    # ─── Trigger Task ─── (Task #36)

    @bp.route('/api/kanban/tasks/<int:task_id>/trigger', methods=['POST'])
    def kanban_api_trigger(task_id):
        task = kanban_db.get(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        data = request.get_json() or {}
        agent_id = (data.get('agent_id') or '').strip() or None

        # If no agent_id provided, use existing assignee
        if not agent_id:
            agent_id = task.get('assignee')

        if not agent_id:
            return jsonify({'error': 'No assignee set for this task. Please assign an agent first.'}), 400

        # Update task with the agent_id if it was provided in the request
        if data.get('agent_id') and data['agent_id'] != task.get('assignee'):
            fields = {'assignee': agent_id, 'updated_at': _now()}
            kanban_db.update(task_id, fields)
            kanban_db.log_task_updated(task_id, f'assignee: {task.get("assignee")} → {agent_id}')
            task['assignee'] = agent_id

        # Block trigger if task has unmet dependencies
        try:
            if kanban_db.has_unmet_dependencies(task_id):
                unmet = kanban_db.get_unmet_dependencies(task_id)
                blocking = ', '.join(f"#{t['id']} '{t['title']}'" for t in unmet)
                return jsonify({
                    'error': (
                        f"Task #{task_id} has unmet dependencies. "
                        f"The following tasks must be completed first: {blocking}."
                    )
                }), 409
        except Exception as dep_exc:
            return jsonify({
                'error': (
                    f"Task #{task_id} — dependency check failed. "
                    f"The system could not verify whether dependencies are met. "
                    f"Please try again later."
                )
            }), 500

        # Verify agent exists before attempting to trigger
        from models.db import db as main_db
        if not main_db.get_agent(agent_id):
            return jsonify({'error': f'Agent "{agent_id}" not found. The task assignee may be invalid — use an agent ID, not a display name.'}), 400

        # Trigger the agent using the same notify path as the kanban scheduler
        try:
            from plugins.kanban.handler import _notify_agent, _load_config
            cfg = _load_config()
            channel_type = cfg.get('CHANNEL_TYPE', 'telegram')
            result = _notify_agent(agent_id, task, channel_type, force=True, force_delay=True)
            if not result.get('success'):
                reason = result.get('reason', 'unknown')
                error_messages = {
                    'no_skill': f'Agent {agent_id} does not have the kanban skill installed.',
                    'no_session': f'Agent {agent_id} has no active channel session.',
                    'busy': f'Agent {agent_id} is busy with another task.',
                    'deduplicated': f'Notification for agent {agent_id} was deduplicated.',
                    'delayed': f'Notification for agent {agent_id} is delayed by config.',
                }
                return jsonify({'error': error_messages.get(reason, f'Failed to trigger agent {agent_id}.')}), 409
        except Exception as e:
            return jsonify({'error': f'Failed to trigger agent: {e}'}), 500

        return jsonify({
            'status': 'success',
            'task_id': task_id,
            'agent_id': agent_id,
            'message': f'Task #{task_id} triggered for agent {agent_id}'
        })

    # ─── Notifier pause/resume ────────────────────────────────────────────────

    @bp.route('/api/kanban/notifier/status', methods=['GET'])
    def kanban_notifier_status():
        """Get current notifier paused state."""
        try:
            from plugins.kanban.handler import _is_notifier_paused
            return jsonify({'paused': _is_notifier_paused()})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @bp.route('/api/kanban/notifier/toggle', methods=['POST'])
    def kanban_notifier_toggle():
        """Toggle the notifier paused state. Request body: {"paused": true/false}"""
        try:
            from plugins.kanban.handler import _is_notifier_paused, _set_notifier_paused
            data = request.get_json() or {}
            if 'paused' in data:
                new_state = bool(data['paused'])
            else:
                new_state = not _is_notifier_paused()
            _set_notifier_paused(new_state)
            return jsonify({'paused': new_state})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ─── Agent scan / assignment routes ──────────────────────────────────────

    @bp.route('/api/kanban/agent/check', methods=['POST'])
    def kanban_agent_check():
        """Force an immediate scan for eligible agents."""
        try:
            from plugins.kanban.handler import _scan_and_notify
            scan_results = _scan_and_notify()
            return jsonify({
                'success': True,
                'message': 'Check complete',
                'notified': scan_results.get('notified', 0),
                'failed': scan_results.get('failed', 0),
                'details': scan_results.get('details', []),
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @bp.route('/api/kanban/agent/propose', methods=['GET'])
    def kanban_agent_propose():
        """Return a proposed round-robin assignment plan for unassigned todo tasks."""
        try:
            from plugins.kanban.handler import _load_config, _get_kanban_skill_agents
            config = _load_config()
            eligible = _get_kanban_skill_agents()

            if not eligible:
                return jsonify({'proposals': [], 'eligible_agents': []})

            tasks = kanban_db.get_all()
            unassigned = [
                t for t in tasks
                if t.get('status') == 'todo' and not t.get('assignee')
            ]

            proposals = []
            for i, task in enumerate(unassigned):
                proposals.append({
                    'task_id': task['id'],
                    'title': task['title'],
                    'description': task.get('description', ''),
                    'priority': task.get('priority', 'low'),
                    'proposed_agent': eligible[i % len(eligible)],
                })

            return jsonify({'proposals': proposals, 'eligible_agents': eligible})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @bp.route('/api/kanban/eligible-agents', methods=['GET'])
    def kanban_eligible_agents():
        """Return the list of eligible agents (id + name) — agents with kanban skill."""
        try:
            from plugins.kanban.handler import _get_kanban_skill_agents
            from models.db import db as main_db

            eligible_ids = _get_kanban_skill_agents()

            if not eligible_ids:
                return jsonify({'agents': []})

            eligible_set = set(eligible_ids)
            all_agents = main_db.get_agents()
            agents = [
                {'id': a['id'], 'name': a.get('name', a['id']), 'avatar_path': a.get('avatar_path', '')}
                for a in all_agents
                if a['id'] in eligible_set and a.get('enabled', 1)
            ]
            # Sort by name for consistent presentation
            agents.sort(key=lambda a: a['name'].lower())

            return jsonify({'agents': agents})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @bp.route('/api/kanban/all-agents', methods=['GET'])
    def kanban_all_agents():
        """Return ALL agents (id + name + has_kanban flag) for the assignee dropdown."""
        try:
            from models.db import db as main_db

            all_agents = main_db.get_agents()
            try:
                from plugins.kanban.handler import _get_kanban_skill_agents
                eligible_ids = set(_get_kanban_skill_agents())
            except Exception:
                eligible_ids = set()
            # Exclude disabled agents from the assignment dropdown
            agents = [
                {
                    'id': a['id'],
                    'name': a.get('name', a['id']),
                    'has_kanban': a['id'] in eligible_ids,
                    'avatar_path': a.get('avatar_path', ''),
                }
                for a in all_agents
                if a.get('enabled', 1)
            ]
            agents.sort(key=lambda a: a['name'].lower())
            return jsonify({'agents': agents})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @bp.route('/api/kanban/agent/auto_assign', methods=['POST'])
    def kanban_agent_auto_assign():
        """Use LLM to match unassigned tasks to best-fit eligible agents."""
        import re
        try:
            from plugins.kanban.handler import _load_config, _get_kanban_skill_agents
            from backend.llm_client import get_llm_client
            from models.db import db as main_db

            config = _load_config()
            eligible = _get_kanban_skill_agents()
            if not eligible:
                return jsonify({'error': 'No agents with kanban skill found'}), 400

            data = request.get_json()
            if not data or 'tasks' not in data:
                return jsonify({'error': 'tasks field required'}), 400
            tasks = data['tasks']
            if not tasks:
                return jsonify({'error': 'No tasks provided'}), 400

            eligible_set = set(eligible)
            all_agents = main_db.get_agents()
            agents_info = [
                a for a in all_agents if a['id'] in eligible_set
            ]

            agent_lines = '\n'.join(
                f'- ID: "{a["id"]}", Name: "{a.get("name", a["id"])}", Description: "{(a.get("description") or "").strip()}"'
                for a in agents_info
            )
            task_lines = '\n'.join(
                f'- ID: {t["task_id"]}, Title: "{t["title"]}", Description: "{(t.get("description") or "").strip()}", Priority: {t["priority"]}'
                for t in tasks
            )

            messages = [
                {
                    'role': 'system',
                    'content': (
                        'You are a task assignment assistant. '
                        'Match each task to the single best-fit agent based on the agent\'s description and expertise. '
                        'Return ONLY a JSON object mapping task_id (as string) to agent_id (as string). '
                        'No explanation, no markdown, no extra text — just the raw JSON object.'
                    ),
                },
                {
                    'role': 'user',
                    'content': f'## Agents\n{agent_lines}\n\n## Tasks\n{task_lines}\n\nRespond with a JSON object mapping each task_id to the best agent_id.',
                },
            ]

            result = get_llm_client().chat_completion(
                messages=messages,
                temperature=0,
                enable_thinking=False,
                max_tokens=2048,
            )
            if not result.get('success'):
                err = result.get('error_detail') or 'LLM call failed'
                return jsonify({'error': err}), 500

            choices = (result.get('response') or {}).get('choices') or []
            if not choices:
                return jsonify({'error': 'Empty LLM response'}), 500
            raw = choices[0].get('message', {}).get('content', '').strip()

            # Strip markdown code fences if present
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw).strip()

            # Extract first {...} block
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if not m:
                return jsonify({'error': 'LLM did not return valid JSON'}), 500
            try:
                mapping = json.loads(m.group(0))
            except json.JSONDecodeError:
                return jsonify({'error': 'Failed to parse LLM JSON response'}), 500

            # Validate and build assignments, fallback to first eligible agent
            fallback = eligible[0]
            task_ids = {str(t['task_id']) for t in tasks}
            assignments = {}
            for t in tasks:
                tid = str(t['task_id'])
                agent_id = mapping.get(tid)
                if agent_id not in eligible_set:
                    agent_id = fallback
                assignments[tid] = agent_id

            return jsonify({'assignments': assignments})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @bp.route('/api/kanban/agent/assign', methods=['POST'])
    def kanban_agent_assign():
        """Confirm batch assignment of tasks to agents, then notify each agent."""
        try:
            from plugins.kanban.handler import _load_config, _notify_agent

            data = request.get_json()
            if not data or 'assignments' not in data:
                return jsonify({'error': 'assignments field required'}), 400

            assignments = data['assignments']
            if not isinstance(assignments, list):
                return jsonify({'error': 'assignments must be a list'}), 400

            config = _load_config()
            channel_type = config.get('CHANNEL_TYPE', 'telegram')
            now = _now()
            assigned_count = 0

            from models.db import db as main_db

            for item in assignments:
                task_id = item.get('task_id')
                agent_id = item.get('agent_id', '').strip()
                if not task_id or not agent_id:
                    continue

                # Validate agent exists
                if not main_db.get_agent(agent_id):
                    continue

                task = kanban_db.get(task_id)
                if not task:
                    continue

                updated = kanban_db.update(task_id, {'assignee': agent_id, 'updated_at': now})
                if updated:
                    kanban_db.log_task_updated(task_id, f'assignee: {task.get("assignee")} → {agent_id}')
                    _emit('kanban_task_updated', updated)
                    try:
                        _notify_agent(agent_id, updated, channel_type)
                    except Exception:
                        pass
                    assigned_count += 1

            return jsonify({'success': True, 'assigned': assigned_count})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    return bp
