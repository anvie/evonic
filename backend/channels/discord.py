"""Discord channel implementation using discord.py.

Listens for direct messages and for guild messages that @mention the bot,
routes them through the agent runtime, and replies in the originating channel.

Mirrors the structure of ``backend/channels/telegram.py`` (async bot running in
its own event loop on a daemon thread). Notable Discord-specific differences:

* Discord renders Markdown, so responses are NOT stripped of formatting.
* Discord messages are capped at 2000 characters (Telegram allows 4096).
* Reading message text requires the privileged *Message Content Intent*, which
  must be enabled in the Discord Developer Portal for the bot to work.
"""

import base64
import logging
import os
import re
import time
import threading
from typing import Dict, Any, Optional, List, Tuple

from backend.channels.base import BaseChannel, strip_system_tags

_logger = logging.getLogger(__name__)

# Discord hard limit is 2000 chars; leave headroom for safety.
_DISCORD_MAX_LEN = 1900
# Non-boosted guild upload limit. Files larger than this are rejected.
_DISCORD_MAX_FILE_BYTES = 8 * 1024 * 1024
# Multimodal size guards (mirror Telegram channel).
_VIDEO_MAX_BYTES = 20 * 1024 * 1024

# Matches a bot mention (<@id> or <@!id>) so it can be stripped from guild text.
_MENTION_RE = re.compile(r'<@!?(\d+)>')


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


def _split_message(text: str, max_len: int = _DISCORD_MAX_LEN) -> List[str]:
    """Split text into chunks that fit within Discord's 2000 char limit.

    Prefers splitting at paragraph breaks, then line breaks, then spaces.
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        split_at = -1
        for sep in ('\n\n', '\n', ' '):
            pos = text.rfind(sep, 0, max_len)
            if pos > 0:
                split_at = pos
                break

        if split_at <= 0:
            split_at = max_len  # hard cut

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')

    return chunks


def _extract_name(text: str) -> str:
    """Extract a proper name from a self-introduction phrase using the LLM.

    e.g. 'my name is amir' -> 'Amir', 'nama saya budi' -> 'Budi'.
    Falls back to the raw text (title-cased) if the LLM call fails.
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


def _strip_bot_mention(content: str) -> str:
    """Remove all <@id> / <@!id> mentions from guild message content."""
    return _MENTION_RE.sub('', content or '').strip()


def _should_handle(is_dm: bool, mentioned: bool) -> bool:
    """Decide whether an inbound message should be processed.

    DMs are always handled; guild messages only when the bot is @mentioned.
    Factored out as a pure function so the gating logic is unit-testable
    without a live Discord client.
    """
    return bool(is_dm or mentioned)


class DiscordChannel(BaseChannel):
    def __init__(self, channel_id: str, agent_id: str, config: Dict[str, Any]):
        super().__init__(channel_id, agent_id, config)
        self._client = None
        self._thread = None
        self._loop = None  # event loop owned by the bot thread
        # external_user_id -> discord.abc.Messageable (DM or guild channel) where
        # the user last spoke, so buffered/bot-initiated sends land in the right place.
        self._reply_targets: Dict[str, Any] = {}
        self._approval_required_handler = None
        self._approval_resolved_handler = None
        self._llm_thinking_handler = None
        # approval_id -> discord.Message (the prompt we sent, for later edit)
        self._pending_approval_msgs: Dict[str, Any] = {}

    @staticmethod
    def get_channel_type() -> str:
        return 'discord'

    def get_system_instructions(self) -> Optional[str]:
        return (
            "You are responding via Discord. Discord supports Markdown, so you may "
            "use **bold**, *italic*, `inline code`, and ```code blocks```. Keep each "
            "message under 2000 characters; very long answers are split automatically.\n"
            "- Images and files: ALWAYS deliver them with the `send_file` tool so they arrive "
            "as attachments. NEVER embed images with HTML `<img>` tags or Markdown image "
            "embeds (`![alt](url)`) - Discord does not render them in chat; they arrive as raw text."
        )

    # ------------------------------------------------------------------ lifecycle

    def start(self):
        _logger.info("Discord channel %s connecting (agent: %s)...", self.channel_id, self.agent_id)
        try:
            import discord
        except ImportError:
            _logger.error("Discord channel %s: discord.py not installed", self.channel_id)
            raise RuntimeError("discord.py not installed. Run: pip install discord.py")

        bot_token = self.config.get('bot_token', '')
        if not bot_token:
            _logger.error("Discord channel %s: bot token is missing", self.channel_id)
            raise ValueError("Bot token is required for Discord channel.")

        intents = discord.Intents.default()
        intents.message_content = True  # privileged — must be enabled in the Dev Portal
        client = discord.Client(intents=intents)
        self._client = client

        channel_id = self.channel_id
        agent_id = self.agent_id

        @client.event
        async def on_ready():
            self._running = True
            _logger.info(
                "Discord channel %s connected as %s (agent: %s)",
                channel_id, getattr(client.user, 'name', '?'), agent_id,
            )

        @client.event
        async def on_message(message):
            # Ignore our own messages and other bots.
            if message.author.bot or (client.user and message.author.id == client.user.id):
                return

            is_dm = message.guild is None
            mentioned = bool(client.user and client.user in message.mentions)
            if not _should_handle(is_dm, mentioned):
                return

            try:
                await self._handle_message(message, is_dm)
            except Exception as e:
                _logger.error(
                    "Discord channel %s: error handling message from %s: %s",
                    channel_id, message.author.id, e, exc_info=True,
                )
                try:
                    await message.channel.send(
                        "Sorry, an error occurred while processing your message. "
                        "Please try again."
                    )
                except Exception:
                    pass

        self._register_event_listeners()

        def run_client():
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                # client.start() runs until the client is closed.
                loop.run_until_complete(client.start(bot_token))
            except Exception as exc:
                from discord.errors import LoginFailure, PrivilegedIntentsRequired
                if isinstance(exc, PrivilegedIntentsRequired):
                    _logger.error(
                        "Discord channel %s: the Message Content Intent is not enabled. "
                        "Enable it under Bot → Privileged Gateway Intents in the Discord "
                        "Developer Portal, then restart the channel.",
                        channel_id,
                    )
                elif isinstance(exc, LoginFailure):
                    _logger.error(
                        "Discord channel %s: login failed — the bot token is invalid.",
                        channel_id,
                    )
                else:
                    _logger.error(
                        "Discord channel %s: client stopped: %s", channel_id, exc, exc_info=True,
                    )
            finally:
                self._running = False
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                loop.close()

        self._thread = threading.Thread(target=run_client, daemon=True)
        self._thread.start()
        self._running = True

    def stop(self):
        if not self._running and not self._thread:
            return
        _logger.info("Discord channel %s disconnecting...", self.channel_id)
        self._running = False
        self._unregister_event_listeners()

        import asyncio
        loop = self._loop
        client = self._client
        if loop and client and loop.is_running():
            asyncio.run_coroutine_threadsafe(client.close(), loop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        _logger.info("Discord channel %s disconnected", self.channel_id)

    def _run_async(self, coro):
        """Run a coroutine on the bot's event loop from any thread."""
        import asyncio
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            return future.result(timeout=15)
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    # ------------------------------------------------------------- inbound message

    async def _handle_message(self, message, is_dm: bool):
        from models.db import db
        from backend.channels.pairing import extract_pair_code

        channel_id = self.channel_id
        agent_id = self.agent_id
        user_id = str(message.author.id)

        # Remember where to reply (DM or guild channel) for buffered/initiated sends.
        self._reply_targets[user_id] = message.channel

        raw = message.content or ''
        if not is_dm:
            raw = _strip_bot_mention(raw)
        text = strip_system_tags(raw)

        user_name = getattr(message.author, 'display_name', None) or message.author.name or None

        # Auto-populate display name from the Discord profile if not yet set.
        if user_name:
            current_name = db.get_user_display_name(channel_id, user_id)
            if current_name == 'unknown' or current_name == user_id:
                db.set_user_display_name(channel_id, user_id, user_name)

        # Step 1: Fully approved user? (in allowlist AND has name set)
        if db.is_user_allowed(channel_id, user_id):
            if db.needs_name(channel_id, user_id):
                name_candidate = _extract_name(text) if text and text.strip() else ''
                if name_candidate and len(name_candidate) <= 100:
                    db.set_user_display_name(channel_id, user_id, name_candidate)
                    await message.channel.send(
                        f"Thanks, {name_candidate}! You're all set. How can I help you today?"
                    )
                elif text:
                    await message.channel.send(
                        "That name is too long. Please share a shorter name (max 100 characters)."
                    )
                else:
                    await message.channel.send(
                        "Please tell me your name to continue (e.g. 'My name is Budi')."
                    )
                return
            # Approved — fall through to normal processing.
        else:
            # Step 2: Not in allowlist — try pairing-code auto-approve.
            raw_code = extract_pair_code(text) if text else None
            if raw_code:
                pending = db.get_pending_approval_by_code(raw_code)
                if pending:
                    if not pending.get('external_user_id'):
                        db.update_pending_user_id(pending['id'], user_id)
                    approved_user = db.approve_pending_with_name_needed(pending['id'])
                    if approved_user:
                        if db.needs_name(channel_id, user_id):
                            await message.channel.send(
                                "✅ You're now approved! Welcome aboard.\n\n"
                                "Before we chat, please tell me your name (e.g. 'My name is Budi')."
                            )
                        else:
                            await message.channel.send(
                                "✅ You're now approved! Welcome aboard. How can I help you today?"
                            )
                    return
                await message.channel.send(
                    "❌ That pairing code is invalid or has expired. "
                    "Please ask the administrator for a new one."
                )
                return
            # No pairing code — create a pending approval once and prompt the user.
            existing = db.get_pending_approvals(channel_id)
            already_pending = any(p.get('external_user_id') == user_id for p in existing)
            if not already_pending:
                allowed, pair_code = self._check_allowlist(user_id, user_name)
                if not allowed and pair_code:
                    await message.channel.send(
                        "👋 You're not yet approved to chat here. "
                        "Please ask the administrator for a pairing code, then send it in this chat."
                    )
            return

        # Establish session early — needed for attachment storage paths.
        session_id = db.get_or_create_session(agent_id, user_id, channel_id)

        # Media / attachment ingestion.
        image_url, video_url, info_lines = await self._ingest_attachments(
            message, agent_id, session_id, user_id, channel_id, db,
        )
        if info_lines:
            prefix = "\n".join(info_lines)
            text = prefix + (f"\n{text}" if text else '')

        has_any_media = image_url or video_url
        if has_any_media and not text:
            text = '[Image]' if image_url else '[Video]'
        elif not text and not has_any_media:
            return

        # Respect the per-session bot toggle.
        if not db.is_session_bot_enabled(session_id, agent_id=agent_id):
            db.add_chat_message(session_id, 'user', text or '[Image]', agent_id=agent_id)
            return

        # Include the replied-to bot message as context, when present.
        final_text = text
        ref = getattr(message, 'reference', None)
        if ref is not None:
            resolved = getattr(ref, 'resolved', None)
            try:
                if resolved is not None and getattr(resolved, 'author', None) \
                        and client_is_self(self._client, resolved.author):
                    replied_text = resolved.content or ''
                    if replied_text:
                        final_text = f"[Replying to: {replied_text[:200]}]\n{text}"
                    else:
                        final_text = f"[Replying to: (media from bot)]\n{text}"
            except Exception:
                pass

        from backend.agent_runtime import agent_runtime
        result = agent_runtime.handle_message(
            agent_id, user_id, final_text, channel_id,
            image_url=image_url, video_url=video_url,
            metadata={"channel_message_id": str(message.id)},
        )
        if result.get('buffered'):
            return  # response will be delivered by the buffering path

        response = result.get('response') or ''
        if response and response != "(No response)":
            first = True
            for chunk in _split_message(response):
                kwargs = {'reference': message} if first else {}
                first = False
                try:
                    await message.channel.send(chunk, **kwargs)
                except Exception:
                    # The referenced message may have been deleted — retry plain.
                    await message.channel.send(chunk)

        from backend.event_stream import event_stream
        event_stream.emit('message_sent', {
            'channel_type': 'discord',
            'channel_id': channel_id,
            'external_user_id': user_id,
            'message': response,
        })

    async def _ingest_attachments(self, message, agent_id, session_id, user_id,
                                  channel_id, db) -> Tuple[Optional[str], Optional[str],
                                                           List[str]]:
        """Download Discord attachments: build multimodal data URLs and persist rows.

        Audio is attachment-only — agents listen to it via the transcribe_audio
        tool instead of inline multimodal input.

        Returns ``(image_url, video_url, info_lines)``.
        """
        attachments = list(getattr(message, 'attachments', []) or [])
        if not attachments:
            return None, None, []

        image_url = None
        video_url = None
        info_lines: List[str] = []

        agent = db.get_agent(agent_id)
        cfg = db.get_agent_attachment_config(agent_id)
        max_bytes = cfg['max_size_mb'] * 1024 * 1024

        for att in attachments:
            content_type = (getattr(att, 'content_type', None) or '').split(';')[0].strip()
            original_filename = getattr(att, 'filename', None) or 'file'
            size_bytes = getattr(att, 'size', None)
            is_image = content_type.startswith('image/')
            is_audio = content_type.startswith('audio/')
            is_video = content_type.startswith('video/')

            # Multimodal conversion (first matching attachment of each kind wins).
            try:
                if is_image and agent and agent.get('vision_enabled') and image_url is None:
                    data = await att.read()
                    image_url = self._to_jpeg_data_url(data)
                elif is_video and agent and agent.get('video_enabled') and video_url is None:
                    if not size_bytes or size_bytes <= _VIDEO_MAX_BYTES:
                        data = await att.read()
                        b64 = base64.b64encode(data).decode('utf-8')
                        video_url = f"data:{content_type or 'video/mp4'};base64,{b64}"
            except Exception as e:
                _logger.error(
                    "Discord channel %s: failed to read attachment %s: %s",
                    channel_id, original_filename, e, exc_info=True,
                )

            # Persist the attachment row when attachments are enabled and within size.
            if not cfg['enabled']:
                continue
            if size_bytes and size_bytes > max_bytes:
                _logger.info(
                    "Discord channel %s: skipping attachment %s (size %s exceeds %s bytes)",
                    channel_id, original_filename, size_bytes, max_bytes,
                )
                continue
            try:
                safe = _sanitize_filename(original_filename)
                target_dir = os.path.join('data', 'attachments', agent_id, session_id)
                os.makedirs(target_dir, exist_ok=True)
                target_path = os.path.join(target_dir, f"{int(time.time())}_{safe}")
                await att.save(target_path)
                real_size = size_bytes or (
                    os.path.getsize(target_path) if os.path.isfile(target_path) else 0
                )
                file_type = (
                    'photo' if is_image else
                    'audio' if is_audio else
                    'video' if is_video else 'document'
                )
                attachment_id = db.save_attachment(
                    agent_id=agent_id,
                    session_id=session_id,
                    filename=os.path.basename(target_path),
                    file_path=target_path,
                    external_user_id=user_id,
                    channel_id=channel_id,
                    channel_type='discord',
                    original_filename=original_filename,
                    mime_type=content_type or 'application/octet-stream',
                    file_type=file_type,
                    size_bytes=real_size,
                )
                info_line = (
                    f"[Attached: {original_filename} "
                    f"({content_type or 'application/octet-stream'}, "
                    f"{_human_size(real_size)}) id={attachment_id} path={target_path}]"
                )
                if is_audio and agent and agent.get('audio_enabled'):
                    info_line += "\nUse the `transcribe_audio` tool to listen to this audio."
                info_lines.append(info_line)
            except Exception as e:
                _logger.error(
                    "Discord channel %s: failed to persist attachment %s: %s",
                    channel_id, original_filename, e, exc_info=True,
                )

        return image_url, video_url, info_lines

    @staticmethod
    def _to_jpeg_data_url(data: bytes) -> str:
        """Convert image bytes to a JPEG base64 data URL for vision input."""
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(data))
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        buf = BytesIO()
        img.save(buf, format='JPEG', quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        return f"data:image/jpeg;base64,{b64}"

    # ------------------------------------------------------------------- outbound

    async def _resolve_target(self, external_user_id: str):
        """Return a Messageable for a user: their last channel, else a DM channel."""
        target = self._reply_targets.get(external_user_id)
        if target is not None:
            return target
        if not self._client:
            return None
        user = self._client.get_user(int(external_user_id))
        if user is None:
            user = await self._client.fetch_user(int(external_user_id))
        return user

    def _do_send(self, external_user_id: str, text: str):
        if not self._client:
            return

        async def _send():
            target = await self._resolve_target(external_user_id)
            if target is None:
                raise RuntimeError(f"Cannot resolve Discord target for {external_user_id}")
            for chunk in _split_message(text):
                await target.send(chunk)

        self._run_async(_send())
        from backend.event_stream import event_stream
        event_stream.emit('message_sent', {
            'channel_type': 'discord',
            'channel_id': self.channel_id,
            'external_user_id': external_user_id,
            'message': text,
        })

    def _do_send_file(self, external_user_id: str, file_path: str,
                      caption: Optional[str] = None, mime_type: Optional[str] = None) -> bool:
        if not self._client:
            return False
        if not os.path.isfile(file_path):
            _logger.error("File not found for sending: %s", file_path)
            return False
        if not os.access(file_path, os.R_OK):
            _logger.error("File not readable: %s", file_path)
            return False
        file_size = os.path.getsize(file_path)
        if file_size > _DISCORD_MAX_FILE_BYTES:
            _logger.error(
                "File too large for Discord: %s (%d bytes, limit %d)",
                file_path, file_size, _DISCORD_MAX_FILE_BYTES,
            )
            return False

        async def _send():
            import discord
            target = await self._resolve_target(external_user_id)
            if target is None:
                raise RuntimeError(f"Cannot resolve Discord target for {external_user_id}")
            with open(file_path, 'rb') as fh:
                await target.send(content=caption or None,
                                  file=discord.File(fh, filename=os.path.basename(file_path)))

        try:
            self._run_async(_send())
        except Exception as e:
            _logger.error("Failed to send file %s to %s: %s",
                          file_path, external_user_id, e, exc_info=True)
            return False

        from backend.event_stream import event_stream
        filename = os.path.basename(file_path)
        event_stream.emit('message_sent', {
            'channel_type': 'discord',
            'channel_id': self.channel_id,
            'external_user_id': external_user_id,
            'message': f"[FILE] {filename} (with caption)" if caption else f"[FILE] {filename}",
        })
        _logger.info("Sent file %s to %s via Discord", filename, external_user_id)
        return True

    def send_typing(self, external_user_id: str):
        if not self._client:
            return

        async def _typing():
            target = await self._resolve_target(external_user_id)
            if target is None:
                return
            async with target.typing():
                pass

        try:
            self._run_async(_typing())
        except Exception:
            pass

    # ----------------------------------------------------------- approval / events

    def _register_event_listeners(self):
        from backend.event_stream import event_stream

        channel_id = self.channel_id
        _typing_last_sent: Dict[str, float] = {}

        def _on_approval_required(data):
            if not self._is_super_agent_channel():
                return
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
            code_lang = 'bash' if 'script' in tool_args else 'python'
            # Prefer the focused snippet (window centered on the dangerous line with a
            # marker) so the risky code is always visible even when the full script is
            # long. Fall back to head-truncation only when no focus snippet is present.
            code_snippet = info.get('focus_snippet') or ''
            if not code_snippet:
                code_snippet = tool_args.get('script') or tool_args.get('code') or ''
                if code_snippet and len(code_snippet) > 500:
                    code_snippet = code_snippet[:500] + '\n... (truncated)'
            code_block = f"\n```{code_lang}\n{code_snippet}\n```" if code_snippet else ''
            source_agent = data.get('source_agent_name')
            header = (f"⚠️ Approval Required (agent: {source_agent})"
                      if source_agent else "⚠️ Approval Required")
            text = (
                f"{header}\n"
                f"Tool: {tool_name}\n"
                f"Risk: {risk}\n"
                f"{desc}\n"
                f"Reasons: {reasons_str}"
                f"{code_block}"
            )
            try:
                self._send_approval_prompt(user_id, approval_id, text)
            except Exception as e:
                _logger.error("Failed to send Discord approval prompt: %s", e)

        def _on_approval_resolved(data):
            if not self._is_super_agent_channel():
                return
            if data.get('channel_id') != channel_id:
                return
            approval_id = data.get('approval_id', '')
            msg = self._pending_approval_msgs.pop(approval_id, None)
            if msg is None:
                return
            timed_out = data.get('timed_out', False)
            decision = data.get('decision', 'reject')
            if timed_out:
                label = 'Timed out — auto-rejected.'
            elif decision == 'approve':
                label = 'Approved.'
            else:
                label = 'Rejected.'
            try:
                self._run_async(msg.edit(content=label, view=None))
            except Exception:
                pass

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

        self._approval_required_handler = _on_approval_required
        self._approval_resolved_handler = _on_approval_resolved
        self._llm_thinking_handler = _on_llm_thinking
        event_stream.on('approval_required', _on_approval_required)
        event_stream.on('approval_resolved', _on_approval_resolved)
        event_stream.on('llm_thinking', _on_llm_thinking)

    def _unregister_event_listeners(self):
        from backend.event_stream import event_stream
        if self._approval_required_handler:
            event_stream.off('approval_required', self._approval_required_handler)
        if self._approval_resolved_handler:
            event_stream.off('approval_resolved', self._approval_resolved_handler)
        if self._llm_thinking_handler:
            event_stream.off('llm_thinking', self._llm_thinking_handler)

    def _send_approval_prompt(self, external_user_id: str, approval_id: str, text: str):
        """Send an approval message with Approve/Reject buttons; track it for editing."""
        import discord

        pending = self._pending_approval_msgs

        class _ApprovalView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=None)

            @discord.ui.button(label='Approve', style=discord.ButtonStyle.success)
            async def approve(self, interaction, button):
                from backend.agent_runtime.approval import approval_registry
                pending.pop(approval_id, None)
                ok = approval_registry.resolve(approval_id, 'approve')
                msg = 'Approved by user.' if ok else 'This approval has already been resolved or expired.'
                await interaction.response.edit_message(content=msg, view=None)

            @discord.ui.button(label='Reject', style=discord.ButtonStyle.danger)
            async def reject(self, interaction, button):
                from backend.agent_runtime.approval import approval_registry
                pending.pop(approval_id, None)
                ok = approval_registry.resolve(approval_id, 'reject')
                msg = 'Rejected by user.' if ok else 'This approval has already been resolved or expired.'
                await interaction.response.edit_message(content=msg, view=None)

        async def _send():
            target = await self._resolve_target(external_user_id)
            if target is None:
                return None
            return await target.send(content=text, view=_ApprovalView())

        sent = self._run_async(_send())
        if sent is not None:
            self._pending_approval_msgs[approval_id] = sent


def client_is_self(client, author) -> bool:
    """Return True when ``author`` is the bot's own user."""
    try:
        return bool(client and client.user and author.id == client.user.id)
    except Exception:
        return False
