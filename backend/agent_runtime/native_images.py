"""Feed recent image attachments straight to vision-capable models.

Rule: a model with ``vision_supported`` and an agent whose ``vision_enabled``
flag is on sees the image natively -- the attachment is attached as an
``image_url`` part on a *copy* of the messages right before the provider call.
Models without ``vision_supported``, and agents with vision switched off, never
get the image bytes: they read it through the ``describe_image`` tool instead,
and only they receive the ``describe_image`` hint in the attachment note.

The canonical transcript is never changed, so history keeps only the textual
attachment note (``Attachment ID: N``); older images (beyond
:data:`MAX_USER_TURNS_BACK` user turns) are not attached and can still be
fetched with ``describe_image`` / ``read_attachment``.
"""

import base64
import io
import logging
import os
import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)

MAX_USER_TURNS_BACK = 3      # only images from the most recent user turns
MAX_IMAGES = 4               # per request
MAX_EDGE = 1568              # longest side after downscale
MAX_BYTES = 4 * 1024 * 1024  # encoded size budget per image

_ATTACHMENT_ID = re.compile(r'^Attachment ID:\s*(\d+)\s*$', re.MULTILINE)
_cache: 'OrderedDict[tuple, str]' = OrderedDict()
_CACHE_SIZE = 16


def native_mode_enabled(model_config: Optional[Dict[str, Any]],
                        vision_enabled: Optional[bool] = True) -> bool:
    """True when images must be attached natively instead of read via ``describe_image``.

    Both switches must be on: the model must report ``vision_supported`` and the
    agent must have ``vision_enabled`` set. ``vision_enabled`` defaults to True
    (and a missing agent key lowers to True) so legacy agent records keep the
    existing behaviour.
    """
    if not (model_config or {}).get('vision_supported'):
        return False
    return True if vision_enabled is None else bool(vision_enabled)


def native_images_for_turn(agent: Optional[Dict[str, Any]],
                           model_config: Optional[Dict[str, Any]]) -> bool:
    """Resolve the per-turn native-image decision from the agent and its model.

    Single entry point for every call site that must agree on how images are
    delivered: the agent's ``vision_enabled`` flag (absent on legacy records)
    gates the model's ``vision_supported`` capability.
    """
    return native_mode_enabled(
        model_config,
        vision_enabled=(agent or {}).get('vision_enabled', True),
    )


def _data_url(path: str, mime: str) -> Optional[str]:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (path, stat.st_mtime_ns, stat.st_size)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    try:
        from PIL import Image
        with Image.open(path) as img:
            img.load()
            if max(img.size) > MAX_EDGE:
                img.thumbnail((MAX_EDGE, MAX_EDGE))
            fmt = 'PNG' if img.mode in ('RGBA', 'LA', 'P') else 'JPEG'
            if fmt == 'JPEG' and img.mode != 'RGB':
                img = img.convert('RGB')
            buf = io.BytesIO()
            img.save(buf, format=fmt, **({'quality': 88} if fmt == 'JPEG' else {}))
        data, out_mime = buf.getvalue(), 'image/' + fmt.lower()
    except Exception:
        _logger.warning('Could not decode image attachment %s', path, exc_info=True)
        return None
    if len(data) > MAX_BYTES:
        return None
    url = f'data:{out_mime};base64,' + base64.b64encode(data).decode('ascii')
    _cache[key] = url
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return url


def _message_text(message: Dict[str, Any]) -> str:
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(p.get('text', '') for p in content
                         if isinstance(p, dict) and p.get('type') == 'text')
    return ''


DESCRIBE_HINT = 'Use the `describe_image` tool to view and analyze this image.'


def _strip_hint(text: str) -> str:
    # Drop the hint together with the newline that introduced it.
    return re.sub(r'\n?' + re.escape(DESCRIBE_HINT), '', text)


def strip_describe_image_hints(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return ``messages`` without the ``describe_image`` hint lines.

    Used for vision-capable models, where the image is already attached natively
    and the hint would tell the model to read a second model's summary instead of
    looking at the image. Unchanged messages are returned as the same objects, so
    callers keep sharing state with the canonical transcript.
    """
    result = []
    for message in messages:
        content = message.get('content')
        if isinstance(content, str):
            if DESCRIBE_HINT in content:
                result.append({**message, 'content': _strip_hint(content)})
            else:
                result.append(message)
        elif isinstance(content, list):
            parts = []
            changed = False
            for part in content:
                if (isinstance(part, dict) and part.get('type') == 'text'
                        and DESCRIBE_HINT in (part.get('text') or '')):
                    parts.append({**part, 'text': _strip_hint(part['text'])})
                    changed = True
                else:
                    parts.append(part)
            result.append({**message, 'content': parts} if changed else message)
        else:
            result.append(message)
    return result


def attach_native_images(messages: List[Dict[str, Any]], session_id: str, agent_id: str,
                         db: Any) -> List[Dict[str, Any]]:
    """Return ``messages`` with recent image attachments added as ``image_url`` parts.

    Only attachment IDs that the database confirms belong to this session and
    agent are read, never paths quoted in message text.
    """
    try:
        owned = {str(r['id']): r for r in db.list_session_attachments(session_id, agent_id)}
    except Exception:
        _logger.warning('Image attachment lookup failed for session %s', session_id, exc_info=True)
        return messages

    result = list(messages)
    budget = MAX_IMAGES
    user_turns = 0
    for index in range(len(result) - 1, -1, -1):
        message = result[index]
        if message.get('role') != 'user':
            continue
        user_turns += 1
        if user_turns > MAX_USER_TURNS_BACK or budget <= 0:
            break
        content = message.get('content')
        if isinstance(content, list) and any(
                isinstance(p, dict) and p.get('type') == 'image_url' for p in content):
            continue
        parts = []
        for attachment_id in _ATTACHMENT_ID.findall(_message_text(message)):
            record = owned.get(attachment_id)
            if not record or not str(record.get('mime_type') or '').startswith('image/'):
                continue
            path = record.get('file_path') or ''
            if path and not os.path.isabs(path):
                from backend.agent_runtime.context import _BASE_DIR
                path = os.path.abspath(os.path.join(_BASE_DIR, path))
            url = _data_url(path, record['mime_type']) if os.path.isfile(path) else None
            if url and budget > 0:
                parts.append({'type': 'image_url', 'image_url': {'url': url}})
                budget -= 1
        if parts:
            text = _message_text(message)
            result[index] = {**message, 'content': [{'type': 'text', 'text': text}, *parts]}
    return result
