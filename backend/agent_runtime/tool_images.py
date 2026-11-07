"""Images returned by tools.

A tool may add ``"images": [{"mime": "image/png", "data": "<base64>", "label": "..."}]`` to a
dict result (a chart, a render preview, a screenshot). Each image is saved as an ordinary session
attachment and replaced in the result by a short reference, so the transcript stays text. When the
turn uses native images (model ``vision_supported`` and agent ``vision_enabled``), the model sees
them directly (``native_images.attach_tool_images``); otherwise it can read them with
``describe_image`` at the given path.
"""

import base64
import binascii
import logging
import os
import time
from typing import Any, Dict

_logger = logging.getLogger(__name__)

ALLOWED_MIME = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp'}
MAX_IMAGES_PER_RESULT = 4
MAX_BYTES = 4 * 1024 * 1024
# The reference key the runtime later looks for in tool messages
REF_KEY = 'tool_image_attachment_id'


def _attachment_dir(agent_id: str, session_id: str) -> str:
    from backend.agent_runtime.context import _BASE_DIR
    path = os.path.join(_BASE_DIR, 'data', 'attachments', agent_id, session_id)
    os.makedirs(path, exist_ok=True)
    return path


def store(tool_result: Dict[str, Any], agent_id: str, session_id: str, fn_name: str, db: Any) -> Dict[str, Any]:
    """Return ``tool_result`` with its ``images`` saved as attachments and replaced by references.

    Images that are not a supported type, do not decode or are too large are dropped with a note,
    so a bad image never fails the tool call.
    """
    images = tool_result.get('images')
    if not isinstance(images, list):
        return tool_result
    refs = []
    for index, image in enumerate(images[:MAX_IMAGES_PER_RESULT]):
        mime = str((image or {}).get('mime') or '') if isinstance(image, dict) else ''
        label = str(image.get('label') or '')[:200] if isinstance(image, dict) else ''
        ext = ALLOWED_MIME.get(mime)
        if not ext:
            refs.append({'skipped': f'image {index + 1}: type {mime or "unknown"} is not supported (png, jpeg, webp)'})
            continue
        try:
            data = base64.b64decode(str(image.get('data') or ''), validate=True)
        except (binascii.Error, ValueError):
            refs.append({'skipped': f'image {index + 1}: data is not valid base64'})
            continue
        if not data or len(data) > MAX_BYTES:
            refs.append({'skipped': f'image {index + 1}: empty or larger than {MAX_BYTES // (1024 * 1024)} MB'})
            continue
        filename = f'{int(time.time() * 1000)}_{fn_name}_{index + 1}.{ext}'
        path = os.path.join(_attachment_dir(agent_id, session_id), filename)
        with open(path, 'wb') as f:
            f.write(data)
        attachment_id = db.save_attachment(
            agent_id=agent_id, session_id=session_id, filename=filename, file_path=path,
            channel_type='tool', original_filename=label or filename, mime_type=mime,
            file_type='image', size_bytes=len(data),
        )
        refs.append({REF_KEY: attachment_id, 'label': label, 'path': path})
    if len(images) > MAX_IMAGES_PER_RESULT:
        refs.append({'skipped': f'{len(images) - MAX_IMAGES_PER_RESULT} more image(s): at most {MAX_IMAGES_PER_RESULT} per result'})
    result = {k: v for k, v in tool_result.items() if k != 'images'}
    result['images'] = refs
    if any(REF_KEY in r for r in refs):
        result['images_note'] = ('Images attached. If you cannot see them, call describe_image with '
                                 'the path of the image you want to read.')
    return result
