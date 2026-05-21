"""Telegram channel implementation using python-telegram-bot."""

import base64
import logging
import os
import re
import time
import threading
from typing import Dict, Any, Optional, Tuple
from backend.channels.base import BaseChannel, strip_system_tags

_logger = logging.getLogger(__name__)


def _sanitize_filename(name: str) -> str:
    """Sanitize a filename to a safe ASCII slug, max 120 chars."""
    if not name:
        return 'file'
    cleaned = re.sub(r'[^A-Za-z0-9._-]', '_', name)[:120]
    return cleaned or 'file'


def _human_size(size_bytes: Optional[int]) -> str:
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


_TG_FILE_TYPE_DEFAULT_MIME = {
    'voice': 'audio/ogg',
    'video_note': 'video/mp4',
    'sticker': 'image/webp',
    'animation': 'video/mp4',
}


_IMAGE_DOC_MIMES = frozenset({'image/jpeg', 'image/png', 'image/webp'})


def _has_image_document(message) -> bool:
    """Return True when the Telegram message carries an image-mime document.

    Image-mime documents are routed exclusively through the photo / vision
    pipeline; this helper lets the non-photo path skip them cleanly without
    relying on side-effects from a sibling branch.
    """
    doc = getattr(message, 'document', None)
    if not doc:
        return False
    return getattr(doc, 'mime_type', None) in _IMAGE_DOC_MIMES


def _detect_non_photo_attachment(message) -> Optional[Tuple[str, Optional[str], str, Optional[int], str]]:
    """Inspect a Telegram message for a non-photo attachment.

    Returns (file_id, original_filename, mime_type, size_bytes, file_type) or None.
    """
    candidates = [
        ('document', getattr(message, 'document', None)),
        ('audio', getattr(message, 'audio', None)),
        ('voice', getattr(message, 'voice', None)),
        ('video', getattr(message, 'video', None)),
        ('video_note', getattr(message, 'video_note', None)),
        ('animation', getattr(message, 'animation', None)),
        ('sticker', getattr(message, 'sticker', None)),
    ]
    for file_type, obj in candidates:
        if not obj:
            continue
        file_id = getattr(obj, 'file_id', None)
        if not file_id:
            continue
        original_filename = getattr(obj, 'file_name', None)
        mime_type = getattr(obj, 'mime_type', None) or _TG_FILE_TYPE_DEFAULT_MIME.get(file_type)
        size_bytes = getattr(obj, 'file_size', None)
        if not original_filename:
            # Synthesize a filename from the file_type for media with no name.
            ext = {
                'voice': 'ogg', 'video_note': 'mp4', 'sticker': 'webp',
                'animation': 'mp4', 'audio': 'mp3', 'video': 'mp4',
            }.get(file_type, 'bin')
            original_filename = f"{file_type}.{ext}"
        return file_id, original_filename, mime_type, size_bytes, file_type
    return None


async def _ingest_non_photo_attachment(message, context, agent_id, session_id,
                                       user_id, channel_id, db):
    """Detect, gate, download and persist a non-photo Telegram attachment.

    The helper is the single owner of the non-photo branch: it inspects the
    message exactly once, resolves the agent attachment config exactly once,
    and on rejection it sends its own reply (mirroring legacy behaviour).

    Returns a tuple ``(info_line, rejected)`` where:
      * ``(None, False)``  — no non-photo attachment present (or it is an
                              image-mime document routed to the photo branch);
                              the caller continues with the photo / text flow.
      * ``(info_line, False)`` — attachment persisted; ``info_line`` should be
                                  prepended to the user's text.
      * ``(None, True)`` — the message was rejected (gating, oversize, or
                            download failure). A user-facing reply has already
                            been sent and the caller must ``return`` early.
    """
    # Image-mime documents are owned by the photo / vision branch.
    if _has_image_document(message):
        return None, False
    non_photo = _detect_non_photo_attachment(message)
    if not non_photo:
        return None, False

    file_id, original_filename, mime_type, size_bytes, file_type = non_photo
    cfg = db.get_agent_attachment_config(agent_id)
    if not cfg['enabled'] or not cfg['supported']:
        await message.reply_text(
            "Attachments are not enabled for this assistant."
        )
        return None, True
    max_bytes = cfg['max_size_mb'] * 1024 * 1024
    if size_bytes and size_bytes > max_bytes:
        await message.reply_text(
            f"File too large (max {cfg['max_size_mb']}MB)."
        )
        return None, True
    safe = _sanitize_filename(original_filename)
    target_dir = os.path.join('data', 'attachments', agent_id, session_id)
    try:
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, f"{int(time.time())}_{safe}")
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(target_path)
    except Exception as e:
        _logger.error(
            "Failed to download attachment %s for agent %s: %s",
            file_id, agent_id, e, exc_info=True,
        )
        try:
            await message.reply_text(
                "Failed to download attachment. Please try again."
            )
        except Exception:
            pass
        return None, True
    real_size = size_bytes or (
        os.path.getsize(target_path) if os.path.isfile(target_path) else 0
    )
    attachment_id = db.save_attachment(
        agent_id=agent_id,
        session_id=session_id,
        filename=os.path.basename(target_path),
        file_path=target_path,
        external_user_id=user_id,
        channel_id=channel_id,
        channel_type='telegram',
        original_filename=original_filename,
        mime_type=mime_type,
        file_type=file_type,
        size_bytes=real_size,
        telegram_file_id=file_id,
    )
    info_line = (
        f"[Attached: {original_filename} "
        f"({mime_type or 'application/octet-stream'}, "
        f"{_human_size(real_size)}) "
        f"id={attachment_id} path={target_path}]"
    )
    return info_line, False


async def _ingest_photo(message, context, agent_id, session_id, user_id,
                        channel_id, db):
    """Handle photo / image-document messages: vision conversion + optional persist.

    The helper is the single owner of the photo branch: it derives
    ``photo_file_id`` / ``photo_size`` / ``photo_bytes_for_attachment`` exactly
    once and never relies on state set by another branch.

    Returns ``(image_url, info_line)``: either or both may be ``None``.
      * ``image_url`` is a ``data:`` URL when the agent has vision enabled and
        the photo was successfully decoded; ``None`` otherwise.
      * ``info_line`` is the ``[Attached: …]`` line emitted exactly when the
        photo was also persisted as an attachment row. This is the single
        composition point for the photo info-line.
    """
    has_photo = bool(getattr(message, 'photo', None))
    has_image_doc = _has_image_document(message)
    if not (has_photo or has_image_doc):
        return None, None

    # Derive photo identifiers exactly once.
    if has_photo:
        photo = message.photo[-1]
        photo_file_id = photo.file_id
        photo_size = getattr(photo, 'file_size', None)
        photo_mime = 'image/jpeg'
        photo_orig_name = 'photo.jpg'
        photo_file_type = 'photo'
    else:
        doc = message.document
        photo_file_id = doc.file_id
        photo_size = getattr(doc, 'file_size', None)
        photo_mime = doc.mime_type or 'image/jpeg'
        photo_orig_name = doc.file_name or 'image.jpg'
        photo_file_type = 'document'

    image_url = None
    photo_bytes_for_attachment = None

    agent = db.get_agent(agent_id)
    if agent and agent.get('vision_enabled'):
        file = await context.bot.get_file(photo_file_id)
        img_bytes = await file.download_as_bytearray()
        photo_bytes_for_attachment = bytes(img_bytes)
        # Convert to JPEG for consistent LLM input.
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(photo_bytes_for_attachment))
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        buf = BytesIO()
        img.save(buf, format='JPEG', quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        image_url = f"data:image/jpeg;base64,{b64}"

    info_line = None
    cfg = db.get_agent_attachment_config(agent_id)
    if cfg['enabled'] and cfg['supported']:
        try:
            max_bytes = cfg['max_size_mb'] * 1024 * 1024
            if photo_size and photo_size > max_bytes:
                _logger.info(
                    "Skipping photo attachment row for agent %s: "
                    "size %s exceeds %s bytes",
                    agent_id, photo_size, max_bytes,
                )
            else:
                safe = _sanitize_filename(photo_orig_name)
                target_dir = os.path.join(
                    'data', 'attachments', agent_id, session_id
                )
                os.makedirs(target_dir, exist_ok=True)
                target_path = os.path.join(
                    target_dir, f"{int(time.time())}_{safe}"
                )
                if photo_bytes_for_attachment is not None:
                    with open(target_path, 'wb') as f:
                        f.write(photo_bytes_for_attachment)
                else:
                    tg_file = await context.bot.get_file(photo_file_id)
                    await tg_file.download_to_drive(target_path)
                real_size = (
                    photo_size
                    or (os.path.getsize(target_path) if os.path.isfile(target_path) else 0)
                )
                attachment_id = db.save_attachment(
                    agent_id=agent_id,
                    session_id=session_id,
                    filename=os.path.basename(target_path),
                    file_path=target_path,
                    external_user_id=user_id,
                    channel_id=channel_id,
                    channel_type='telegram',
                    original_filename=photo_orig_name,
                    mime_type=photo_mime,
                    file_type=photo_file_type,
                    size_bytes=real_size,
                    telegram_file_id=photo_file_id,
                )
                info_line = (
                    f"[Attached: {photo_orig_name} "
                    f"({photo_mime}, {_human_size(real_size)}) "
                    f"id={attachment_id} path={target_path}]"
                )
        except Exception as e:
            _logger.error(
                "Failed to persist photo attachment for agent %s: %s",
                agent_id, e, exc_info=True,
            )

    return image_url, info_line


def _extract_name(text: str) -> str:
    """Extract a proper name from a self-introduction phrase using LLM.

    e.g. 'my name is amir' → 'Amir', 'nama saya budi' → 'Budi'.
    Falls back to the raw text (title-cased) if LLM call fails.
    """
    try:
        from backend.llm_client import llm_client
        response = llm_client.chat_completion(
            messages=[
                {"role": "system", "content": (
                    "Extract only the person's name from their message. "
                    "Reply with the name only — no other words. "
                    "Capitalize it properly (e.g. 'Amir Oktaviana'). "
                    "If the message contains no name, reply with the original message verbatim."
                )},
                {"role": "user", "content": text},
            ],
            tools=None,
            temperature=0.0,
            enable_thinking=False,
            max_tokens=20,
        )
        if response.get("success"):
            choices = response.get("response", {}).get("choices", [])
            if choices:
                name = choices[0].get("message", {}).get("content", "").strip()
                if name:
                    return name
    except Exception:
        pass
    return text.strip().title()


def _strip_markdown(text: str) -> str:
    """Remove markdown symbols (bold, italic, headers) from text for plain Telegram messages."""
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)  # headers
    text = re.sub(r'\*+', '', text)  # bold/italic
    return text


def _split_message(text: str, max_len: int = 4050) -> list:
    """Split text into chunks that fit within Telegram's 4096 char limit.

    Prefers splitting at paragraph breaks, then line breaks, then spaces.
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        # Try splitting at paragraph boundary, then line, then space
        split_at = -1
        for sep in ('\n\n', '\n', ' '):
            pos = text.rfind(sep, 0, max_len)
            if pos > 0:
                split_at = pos
                break

        if split_at <= 0:
            split_at = max_len  # hard cut

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')  # strip leading newlines from next chunk

    return chunks


class TelegramChannel(BaseChannel):
    def __init__(self, channel_id: str, agent_id: str, config: Dict[str, Any]):
        super().__init__(channel_id, agent_id, config)
        self._app = None
        self._thread = None
        self._loop = None  # the event loop owned by the polling thread
        self._approval_required_handler = None
        self._approval_resolved_handler = None

    @staticmethod
    def get_channel_type() -> str:
        return 'telegram'

    def get_system_instructions(self) -> Optional[str]:
        return (
            "IMPORTANT — Telegram Formatting Constraint:\n"
            "You are responding via Telegram which uses PLAIN TEXT only. "
            "Markdown formatting (bold, italic, code blocks, headers, bullet lists, "
            "blockquotes, inline code, links) is NOT supported and will appear as "
            "raw symbols, making your response unreadable.\n\n"
            "STRICTLY FOLLOW THESE RULES:\n"
            "- NEVER use markdown symbols: **, *, `, ```, #, -, >, [], ()\n"
            "- Use UPPERCASE for emphasis instead of bold/italic\n"
            "- Use numbered lists (1. 2. 3.) for lists\n"
            "- Use indentation with spaces for structure\n"
            "- Use plain URLs without markdown link syntax\n"
            "- Write code inline with clear labels like \"CODE:\" prefix\n"
            "- Keep responses clean and readable in plain text"
        )

    def start(self):
        _logger.info("Telegram channel %s connecting (agent: %s)...", self.channel_id, self.agent_id)
        try:
            from telegram import Update
            from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
        except ImportError:
            _logger.error("Telegram channel %s: python-telegram-bot not installed", self.channel_id)
            raise RuntimeError("python-telegram-bot not installed. Run: pip install python-telegram-bot")

        bot_token = self.config.get('bot_token', '')
        if not bot_token:
            _logger.error("Telegram channel %s: bot token is missing", self.channel_id)
            raise ValueError("Bot token is required for Telegram channel.")

        from backend.agent_runtime import agent_runtime

        channel_id = self.channel_id
        agent_id = self.agent_id

        async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not update.message:
                return

            try:
                user_id = str(update.message.chat_id)
                text = strip_system_tags(update.message.text or update.message.caption or '')
                image_url = None

                # Allowlist check with pairing-code auto-approve (mirrors WhatsApp pattern)
                from_user = update.message.from_user
                user_name = None
                if from_user:
                    # Prefer Telegram @username as the identifier; fall back to display name
                    user_name = from_user.username or ' '.join(
                        p for p in [from_user.first_name, from_user.last_name] if p
                    ) or None
                from models.db import db

                # Step 1: Fully approved user? (in allowlist AND has name set)
                if db.is_user_allowed(self.channel_id, user_id):
                    if db.needs_name(self.channel_id, user_id):
                        name_candidate = _extract_name(text) if text and text.strip() else ''
                        if name_candidate and len(name_candidate) <= 100:
                            db.set_user_display_name(self.channel_id, user_id, name_candidate)
                            await update.message.reply_text(
                                f"Thanks, {name_candidate}! You're all set. How can I help you today?"
                            )
                        elif text:
                            await update.message.reply_text(
                                "That name is too long. Please share a shorter name (max 100 characters)."
                            )
                        else:
                            await update.message.reply_text(
                                "Please tell me your name to continue (e.g. 'My name is Budi')."
                            )
                        return
                    # User is fully approved — fall through to normal processing
                else:
                    # Step 2: User NOT in allowlist — try pairing-code auto-approve
                    from backend.channels.pairing import extract_pair_code, format_pair_code as fmt_code
                    raw_code = extract_pair_code(text) if text else None
                    if raw_code:
                        pending = db.get_pending_approval_by_code(raw_code)
                        if pending:
                            if not pending.get('external_user_id'):
                                db.update_pending_user_id(pending['id'], user_id)
                            approved_user = db.approve_pending_with_name_needed(pending['id'])
                            if approved_user:
                                if db.needs_name(self.channel_id, user_id):
                                    await update.message.reply_text(
                                        "✅ You're now approved! Welcome aboard.\n\n"
                                        "Before we chat, please tell me your name (e.g. 'My name is Budi')."
                                    )
                                else:
                                    await update.message.reply_text(
                                        "✅ You're now approved! Welcome aboard. How can I help you today?"
                                    )
                            return
                        else:
                            await update.message.reply_text(
                                "❌ That pairing code is invalid or has expired. "
                                "Please ask the administrator for a new one."
                            )
                            return
                    else:
                        # No pairing code in message — check if pending approval already exists
                        existing = db.get_pending_approvals(self.channel_id)
                        already_pending = any(
                            p.get('external_user_id') == user_id for p in existing
                        )
                        if not already_pending:
                            allowed, pair_code = self._check_allowlist(user_id, user_name)
                            if not allowed and pair_code:
                                formatted = fmt_code(pair_code)
                                await update.message.reply_text(
                                    "👋 You're not yet approved to chat here. "
                                    "Please ask the administrator for a pairing code, then send it in this chat."
                                )
                        return

                # Establish session_id early — needed for attachment storage paths.
                from models.db import db
                session_id = db.get_or_create_session(agent_id, user_id, channel_id)

                # Detect message shape exactly once. The non-photo and photo
                # helpers are mutually exclusive: image-mime documents are
                # routed through `_ingest_photo` only.
                has_photo = bool(update.message.photo)
                has_image_doc = _has_image_document(update.message)

                # Non-photo attachments (documents, audio, voice, video, etc.).
                non_photo_info, rejected = await _ingest_non_photo_attachment(
                    update.message, context, agent_id, session_id,
                    user_id, channel_id, db,
                )
                if rejected:
                    return

                # Photo / image-document branch: vision conversion + optional
                # attachment persistence. `photo_info` is the single source of
                # the `[Attached: …]` line for photos — never composed twice.
                image_url, photo_info = await _ingest_photo(
                    update.message, context, agent_id, session_id,
                    user_id, channel_id, db,
                )

                # Compose `info_line` once — the two helpers are mutually
                # exclusive by message shape, so at most one is non-None.
                info_line = non_photo_info or photo_info
                if info_line:
                    text = info_line + (f"\n{text}" if text else '')

                # Drop empty updates per legacy behavior.
                if has_photo or has_image_doc:
                    if image_url is None and not text:
                        return
                elif not text:
                    return

                # Check if bot is enabled for this session
                if not db.is_session_bot_enabled(session_id, agent_id=agent_id):
                    db.add_chat_message(session_id, 'user', text or '[Image]', agent_id=agent_id)
                    return

                # Detect reply/quote: include replied message content as context
                final_text = text
                reply_to = update.message.reply_to_message
                if reply_to is not None:
                    try:
                        # Check if the replied message is from our bot
                        bot_info = await context.bot.get_me()
                        if reply_to.from_user and reply_to.from_user.id == bot_info.id:
                            # Bot message — include as context
                            replied_text = reply_to.text or reply_to.caption or ''
                            if replied_text:
                                final_text = f"[Replying to: {replied_text[:200]}]\n{text}"
                            else:
                                # Replied to a photo/document from bot
                                final_text = f"[Replying to: (media from bot)]\n{text}"
                        elif reply_to.from_user and reply_to.from_user.id == update.message.chat_id:
                            # User replying to their own previous message
                            replied_text = reply_to.text or reply_to.caption or ''
                            if replied_text:
                                final_text = f"[Replying to myself: {replied_text[:200]}]\n{text}"
                    except Exception:
                        pass  # Silently skip if we can't resolve the reply

                result = agent_runtime.handle_message(
                    agent_id, user_id, final_text, channel_id, image_url=image_url
                )
                if result.get('buffered'):
                    return  # message buffered, response will come from the first caller
                response = _strip_markdown(result.get('response') or '')
                if response and response != "(No response)":
                    # Don't quote slash commands — Telegram's reply preview would show the
                    # user's /command text, which is noisy and unnecessary.
                    is_cmd = text.lstrip().startswith('/')
                    reply_kwargs = {} if is_cmd else {'reply_to_message_id': update.message.message_id}
                    for chunk in _split_message(response):
                        await update.message.reply_text(chunk, **reply_kwargs)
                from backend.event_stream import event_stream
                event_stream.emit('message_sent', {
                    'channel_type': 'telegram',
                    'channel_id': channel_id,
                    'external_user_id': user_id,
                    'message': response,
                })
            except Exception as e:
                _logger.error("Error handling message from chat %s: %s",
                              update.message.chat_id, e, exc_info=True)
                try:
                    await update.message.reply_text(
                        "Sorry, an error occurred while processing your message. "
                        "Please try again.")
                except Exception:
                    pass

        self._app = ApplicationBuilder().token(bot_token).build()

        async def handle_error(update: object, context) -> None:
            from telegram.error import Conflict
            if isinstance(context.error, Conflict):
                _logger.warning(
                    "Telegram channel %s: bot token conflict — another bot instance is already "
                    "running with this token. Stopping polling. "
                    "Make sure only one instance uses this bot token.",
                    channel_id,
                )
                self._running = False
                # Stop polling asynchronously so we don't deadlock
                import asyncio
                asyncio.ensure_future(self._app.updater.stop())
            else:
                _logger.error("Telegram error: %s", context.error, exc_info=context.error)

        self._app.add_error_handler(handle_error)

        # Handle text, photos, and image documents (PNG, WebP)
        # Note: we intentionally do NOT exclude COMMAND filter so that
        # slash commands (/clear, /help, /summary) reach our backend handler.
        self._app.add_handler(MessageHandler(
            filters.TEXT
            | filters.PHOTO
            | filters.Document.ALL
            | filters.AUDIO
            | filters.VOICE
            | filters.VIDEO
            | filters.VIDEO_NOTE
            | filters.ANIMATION
            | filters.Sticker.ALL,
            handle_message,
        ))

        # Inline keyboard callback for approval decisions
        from telegram.ext import CallbackQueryHandler
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
            query = update.callback_query
            await query.answer()
            raw = query.data or ''
            parts = raw.split(':', 1)
            if len(parts) != 2 or parts[0] not in ('approve', 'reject'):
                return
            decision, approval_id = parts[0], parts[1]
            from backend.agent_runtime.approval import approval_registry
            # Pop the pending message BEFORE resolve so the async approval_resolved
            # event handler (fired by run_tool_loop) always sees an empty dict and
            # skips its redundant edit — prevents race between two edit_message_text calls.
            _pending_approval_msgs.pop(approval_id, None)
            success = approval_registry.resolve(approval_id, decision)
            label = 'Approved' if decision == 'approve' else 'Rejected'
            if success:
                await query.edit_message_text(f"{label} by user.")
            else:
                await query.edit_message_text("This approval has already been resolved or expired.")

        self._app.add_handler(CallbackQueryHandler(handle_callback))

        # EventStream listener: send inline keyboard when approval is needed for this channel
        from backend.event_stream import event_stream

        _pending_approval_msgs: dict = {}  # approval_id -> (chat_id, message_id)

        def _on_approval_required(data):
            if data.get('channel_id') != channel_id:
                return
            user_id = data.get('external_user_id')
            if not user_id:
                return
            approval_id = data.get('approval_id', '')
            tool_name = data.get('tool_name', '')
            info = data.get('approval_info', {})
            reasons = data.get('reasons', [])
            risk = info.get('risk_level', 'medium')
            desc = info.get('description', 'This action requires careful consideration.')
            reasons_str = ', '.join(reasons) if reasons else '-'
            tool_args = data.get('tool_args') or {}
            code_snippet = tool_args.get('script') or tool_args.get('code') or ''
            code_lang = 'bash' if 'script' in tool_args else 'python'
            if code_snippet and len(code_snippet) > 500:
                code_snippet = code_snippet[:500] + '\n... (truncated)'
            code_block = f"\n\n```{code_lang}\n{code_snippet}\n```" if code_snippet else ''
            source_agent = data.get('source_agent_name')
            header = f"\u26a0\ufe0f Approval Required(agent: {source_agent})" if source_agent else "\u26a0\ufe0f Approval Required"
            text = (
                f"{header}\n"
                f"Tool: {tool_name}\n"
                f"Risk: {risk}\n"
                f"{desc}\n"
                f"Reasons: {reasons_str}"
                f"{code_block}"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Approve", callback_data=f"approve:{approval_id}"),
                InlineKeyboardButton("Reject", callback_data=f"reject:{approval_id}"),
            ]])
            try:
                sent_msg = self._run_async(
                    self._app.bot.send_message(
                        chat_id=int(user_id), text=text, reply_markup=keyboard
                    )
                )
                _pending_approval_msgs[approval_id] = (int(user_id), sent_msg.message_id)
            except Exception as e:
                _logger.error("Failed to send approval prompt: %s", e)

        def _on_approval_resolved(data):
            if data.get('channel_id') != channel_id:
                return
            approval_id = data.get('approval_id', '')
            msg_info = _pending_approval_msgs.pop(approval_id, None)
            if not msg_info:
                return
            chat_id, message_id = msg_info
            timed_out = data.get('timed_out', False)
            decision = data.get('decision', 'reject')
            if timed_out:
                label = 'Timed out — auto-rejected.'
            elif decision == 'approve':
                label = 'Approved.'
            else:
                label = 'Rejected.'
            try:
                self._run_async(
                    self._app.bot.edit_message_text(
                        chat_id=chat_id, message_id=message_id, text=label
                    )
                )
            except Exception:
                pass

        self._approval_required_handler = _on_approval_required
        self._approval_resolved_handler = _on_approval_resolved
        event_stream.on('approval_required', _on_approval_required)
        event_stream.on('approval_resolved', _on_approval_resolved)

        # Typing status listener: send typing indicator on llm_thinking events
        _typing_last_sent: dict = {}  # external_user_id -> timestamp (debounce 3s)

        def _on_llm_thinking(data):
            if data.get('channel_id') != channel_id:
                return
            user_id = data.get('external_user_id')
            if not user_id:
                return
            now = time.time()
            last = _typing_last_sent.get(user_id, 0)
            if now - last < 3:
                return
            _typing_last_sent[user_id] = now
            try:
                self.send_typing(user_id)
            except Exception:
                pass

        self._llm_thinking_handler = _on_llm_thinking
        event_stream.on('llm_thinking', _on_llm_thinking)

        def run_polling():
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop  # save reference so send_message/send_typing can use it
            try:
                loop.run_until_complete(self._app.initialize())
                loop.run_until_complete(self._app.start())
                loop.run_until_complete(self._app.updater.start_polling())
            except Exception as exc:
                # Check for Telegram Conflict error (another instance using the same token)
                exc_type = type(exc).__name__
                exc_module = type(exc).__module__
                if 'Conflict' in exc_type or (exc_module.startswith('telegram') and 'conflict' in str(exc).lower()):
                    _logger.warning(
                        "Telegram channel %s: bot token conflict — another bot instance is already "
                        "running with this token. Polling will not start. "
                        "Make sure only one instance uses this bot token.",
                        channel_id,
                    )
                else:
                    _logger.error("Telegram channel %s: failed to start polling: %s", channel_id, exc, exc_info=True)
                self._running = False
                loop.close()
                return
            self._running = True
            _logger.info("Telegram channel %s connected and polling (agent: %s)", channel_id, agent_id)
            loop.run_forever()

        self._thread = threading.Thread(target=run_polling, daemon=True)
        self._thread.start()
        self._running = True

    def stop(self):
        if not self._running:
            return
        _logger.info("Telegram channel %s disconnecting...", self.channel_id)
        self._running = False
        from backend.event_stream import event_stream
        if self._approval_required_handler:
            event_stream.off('approval_required', self._approval_required_handler)
        if self._approval_resolved_handler:
            event_stream.off('approval_resolved', self._approval_resolved_handler)
        if self._llm_thinking_handler:
            event_stream.off('llm_thinking', self._llm_thinking_handler)
        import asyncio
        loop = self._loop
        if loop and loop.is_running():
            async def _shutdown():
                try:
                    await self._app.updater.stop()
                    await self._app.stop()
                    await self._app.shutdown()
                finally:
                    loop.stop()
            asyncio.run_coroutine_threadsafe(_shutdown(), loop)
        # Wait for the polling thread to exit (up to 10s)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        _logger.info("Telegram channel %s disconnected", self.channel_id)

    def _run_async(self, coro):
        """Run a coroutine on the bot's event loop from any thread."""
        import asyncio
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            return future.result(timeout=10)
        # Fallback: loop not ready yet (shouldn't normally happen)
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _do_send(self, external_user_id: str, text: str):
        if not self._app:
            return
        text = _strip_markdown(text)
        for chunk in _split_message(text):
            self._run_async(self._app.bot.send_message(chat_id=external_user_id, text=chunk))
        from backend.event_stream import event_stream
        event_stream.emit('message_sent', {
            'channel_type': 'telegram',
            'channel_id': self.channel_id,
            'external_user_id': external_user_id,
            'message': text,
        })

    def send_typing(self, external_user_id: str):
        if not self._app:
            return
        self._run_async(self._app.bot.send_chat_action(chat_id=external_user_id, action='typing'))
