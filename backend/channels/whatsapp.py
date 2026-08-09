"""WhatsApp channel implementation via Baileys Node.js sidecar."""

import base64
import logging
import os
import re
import secrets
import subprocess
import time
import threading
import uuid
import requests
from typing import Dict, Any, Optional
from backend.channels.base import BaseChannel, strip_system_tags
from backend.channels.whatsapp_dispatcher import WhatsAppOutboundDispatcher

_logger = logging.getLogger(__name__)
# Bridge (Node/Baileys) stdout is routed to logs/baileys.log via EVONIC_LOG_ROUTES
_bridge_logger = logging.getLogger('baileys')

_BRIDGE_DIR = os.path.join(os.path.dirname(__file__), 'whatsapp-bridge')


def _whatsapp_format(text: str) -> str:
    """Convert Markdown/rich text to WhatsApp-native conversational formatting.

    Unlike _strip_markdown (which deleted markup destructively), this formatter:
    - Converts headings to plain-text labels.
    - Converts unordered-list bullets to '•'.
    - Preserves numbered lists.
    - Converts [label](url) → "label: url".
    - Converts fenced code blocks to compact "CODE:" sections.
    - Removes unsupported inline markup (**, __, ~~, `) without harming
      punctuation, URLs, or literal content.
    - Collapses excessive blank lines and trims leading/trailing whitespace.
    - Is deterministic and safe for noncompliant LLM output.
    """
    if not text or not isinstance(text, str):
        return ""

    # ── 1. Convert fenced code blocks into compact "CODE:" sections ──────────
    #     (process before inline rules so content inside blocks is untouched)
    text = re.sub(
        r'```[a-zA-Z]*\n(.*?)```',
        lambda m: 'CODE:\n' + m.group(1).rstrip() + '\n',
        text, flags=re.DOTALL
    )

    # ── 2. Convert headings (# ## ### …) to plain-text labels ────────────────
    text = re.sub(r'^######\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^#####\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^####\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^###\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^##\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^#\s+', '', text, flags=re.MULTILINE)

    # ── 3. Convert [label](url) → "label: url" ───────────────────────────────
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1: \2', text)

    # ── 4. Convert bold (**text** or __text__) – strip markers ─────────────
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)

    # ── 5. Convert italic (_text_ or *text*) – strip markers ─────────────────
    #     Single * or _ wrapped text, but not double. Must not destroy URLs.
    text = re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'\1', text)
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'\1', text)

    # ── 6. Convert strikethrough (~~text~~) – strip markers ──────────────────
    text = re.sub(r'~~(.+?)~~', r'\1', text)

    # ── 7. Convert inline code (`text`) – strip backticks ────────────────────
    text = re.sub(r'`([^`\n]+)`', r'\1', text)

    # ── 8. Convert unordered-list markers (- or * at line start) to '•' ──────
    #     Preserves indentation via leading spaces capture.
    text = re.sub(r'^(\s*)[-*]\s+', r'\1• ', text, flags=re.MULTILINE)

    # ── 9. Collapse 3+ consecutive blank lines to at most 2 ──────────────────
    text = re.sub(r'\n{3,}', '\n\n', text)

    # ── 10. Remove leading/trailing blank lines and whitespace ───────────────
    text = text.strip()

    return text


def _split_message(text: str, max_len: int = 4096) -> list:
    """Split text into chunks within WhatsApp's message size limit."""
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
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')
    return chunks


def _read_global_setting(key: str, default: str) -> str:
    """Read a WhatsApp safe-delivery setting from the global app_settings table."""
    try:
        from models.db import db
        return db.get_setting(key, default) or default
    except Exception:
        return default


def _is_status_broadcast(sender: str, jid: str) -> bool:
    """Return whether an inbound payload represents a WhatsApp Status update."""
    return sender in {"status", "status@broadcast"} or jid == "status@broadcast"


def _is_non_conversational_broadcast(sender: str, jid: str) -> bool:
    """Return whether an inbound payload is a Status or Channel broadcast."""
    return _is_status_broadcast(sender, jid) or jid.endswith('@newsletter')


def _sanitize_attachment_filename(name: str) -> str:
    """Return a bounded path-safe filename for an inbound WhatsApp document."""
    basename = os.path.basename(str(name or '').replace('\\', '/'))
    cleaned = re.sub(r'[^A-Za-z0-9._-]', '_', basename)[:120]
    return cleaned.strip('.') or 'document'


def _decode_document_payload(document_data: Any,
                             max_bytes: int = 10 * 1024 * 1024) -> Optional[Dict[str, Any]]:
    """Validate and decode bounded bridge document data without trusting metadata."""
    if not isinstance(document_data, dict):
        return None
    encoded = document_data.get('base64')
    if not isinstance(encoded, str) or not encoded:
        return None

    # Reject oversized data before allocating the decoded byte buffer. Four
    # base64 characters encode at most three bytes, with a small padding margin.
    if len(encoded) > ((max_bytes + 2) // 3) * 4:
        _logger.warning("WhatsApp document rejected before decode: payload exceeds %s bytes",
                        max_bytes)
        return None
    try:
        document_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        _logger.warning("WhatsApp document decode failed: %s", exc)
        return None
    if not document_bytes or len(document_bytes) > max_bytes:
        return None

    declared_length = document_data.get('file_length')
    if declared_length is not None:
        try:
            if int(declared_length) != len(document_bytes):
                _logger.warning(
                    "WhatsApp document length mismatch: declared=%s actual=%s",
                    declared_length, len(document_bytes))
                return None
        except (TypeError, ValueError):
            return None

    mime_type = re.sub(
        r'[\x00-\x1f\x7f]', '',
        str(document_data.get('mimetype') or 'application/octet-stream'),
    )[:255] or 'application/octet-stream'
    return {
        'bytes': document_bytes,
        'filename': _sanitize_attachment_filename(document_data.get('filename')),
        'mime_type': mime_type,
    }


def _human_size(size_bytes: int) -> str:
    """Format an attachment size for the agent-visible attachment marker."""
    size = float(size_bytes)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{int(size)} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{size_bytes} B'


def _format_attachment_marker(attachment_info: Dict[str, Any]) -> str:
    """Build the standard agent-readable attachment marker."""
    return (
        f"[Attached: {attachment_info['original_filename']} "
        f"({attachment_info['mime_type']}, {_human_size(attachment_info['size_bytes'])}) "
        f"id={attachment_info['attachment_id']} path={attachment_info['file_path']}]"
    )


def _format_quoted_context(quoted_text=None, quoted_message=None,
                           quoted_is_bot=False, quoted_sender_name='',
                           quoted_sender='', is_group=False) -> str:
    """Render complete quoted content for an agent, including media identity."""
    details = quoted_message if isinstance(quoted_message, dict) else {}
    message_type = details.get('type') or 'text'
    content = details.get('caption') or details.get('text') or quoted_text or ''
    filename = details.get('filename') or ''
    mimetype = details.get('mimetype') or ''

    if not content and message_type == 'text' and not filename and not mimetype:
        return ''

    if quoted_is_bot:
        target = 'your message' if is_group else 'bot'
    elif is_group:
        target = quoted_sender_name or quoted_sender or 'unknown'
    else:
        target = ''

    prefix = f'Replying to {target}' if target else 'Replying to'
    if message_type == 'text' and not filename and not mimetype:
        return f'[{prefix}: "{content}"]'

    metadata = [f'quoted {message_type}']
    if filename:
        metadata.append(f'filename: "{filename}"')
    if mimetype:
        metadata.append(f'MIME type: "{mimetype}"')
    header = f'[{prefix} — {"; ".join(metadata)}]'
    if content:
        return f'{header}\n{content}\n[/Quoted message]'
    return f'{header}\n(no caption)\n[/Quoted message]'


def _wrap_group_message(text, group_name, push_name, sender,
                        quoted_text, quoted_is_bot,
                        quoted_sender_name, quoted_sender,
                        quoted_message=None) -> str:
    """Wrap a group message with sender and complete reply context."""
    group_label = f'WhatsApp group "{group_name}"' if group_name else 'WhatsApp group'
    sender_label = f'{push_name} ({sender})' if push_name else sender
    lines = [f'[{group_label} — message from {sender_label}]']
    quote_context = _format_quoted_context(
        quoted_text, quoted_message, quoted_is_bot,
        quoted_sender_name, quoted_sender, is_group=True)
    if quote_context:
        lines.append(quote_context)
    lines.append(text)
    return '\n'.join(lines)


def _reject_group_for_agent(agent, is_group: bool) -> bool:
    """dm_only agents reject every group message before further processing.

    The check is deliberately independent of @mentions, replies, and slash
    commands: a dm_only agent must not engage with group chats at all.
    """
    return bool(is_group and agent and agent.get('dm_only'))


class WhatsAppChannel(BaseChannel):
    def __init__(self, channel_id: str, agent_id: str, config: Dict[str, Any]):
        super().__init__(channel_id, agent_id, config)
        self._bridge_port = int(config.get('bridge_port', 3001))
        self._process = None
        self._approval_required_handler = None
        self._approval_resolved_handler = None
        self._llm_thinking_handler = None
        # Per-channel secret for authenticating sidecar → server callbacks
        self._callback_secret: str = secrets.token_urlsafe(32)
        # Last status pushed by the bridge ('connected' | 'qr_pending' | 'disconnected')
        self._last_bridge_status: Optional[str] = None
        # Maps session-facing external_user_id values to the exact inbound JID.
        # The alternate map retains the other WhatsApp identity namespace (PN or
        # LID) for diagnostics without changing the canonical reply target.
        self._jid_map: Dict[str, str] = {}
        self._alternate_jids: Dict[str, str] = {}
        self._load_persisted_jid_routes(config)
        # Debounce state for llm_thinking typing indicator
        self._typing_timer: Dict[str, threading.Timer] = {}
        self._typing_lock = threading.Lock()
        # Per-user suppression deadline (monotonic) — blocks late llm_thinking
        # events from re-scheduling typing right after a response was sent
        self._typing_suppress_until: Dict[str, float] = {}

        # ── Outbound dispatcher (lazy-init in start()) ──
        self._dispatcher: Optional[WhatsAppOutboundDispatcher] = None

    def _load_persisted_jid_routes(self, config: dict) -> None:
        """Restore reply JIDs learned from inbound traffic before a restart."""
        for user_id, route in (config.get('jid_routes') or {}).items():
            if not isinstance(route, dict):
                continue
            primary = route.get('primary') or ''
            alternate = route.get('alternate') or ''
            if primary:
                self._jid_map[str(user_id)] = primary
            if alternate and alternate != primary:
                self._alternate_jids[str(user_id)] = alternate

    def _remember_jid_route(self, user_id: str, primary: str,
                            alternate: str = '') -> None:
        """Cache and persist a canonical reply JID plus its alternate identity."""
        if not user_id or not primary:
            return
        self._jid_map[user_id] = primary
        if alternate and alternate != primary:
            self._alternate_jids[user_id] = alternate
        else:
            self._alternate_jids.pop(user_id, None)

        # Persist only when the learned route changed. Reading the latest config
        # first avoids clobbering route-table edits made while the channel runs.
        try:
            from models.db import db
            channel = db.get_channel(self.channel_id)
            if not channel:
                return
            config = dict(channel.get('config') or {})
            routes = dict(config.get('jid_routes') or {})
            learned = {'primary': primary}
            if alternate and alternate != primary:
                learned['alternate'] = alternate
            if routes.get(user_id) == learned:
                return
            routes[user_id] = learned
            # Bound persisted transport metadata independently from user routes.
            if len(routes) > 2000:
                routes.pop(next(iter(routes)))
            config['jid_routes'] = routes
            db.update_channel(self.channel_id, {'config': config})
            self.config = config
        except Exception as exc:
            _logger.warning("WhatsApp JID route persistence failed for channel %s: %s",
                            self.channel_id, exc)

    @staticmethod
    def _jid_namespace(jid: str) -> str:
        if not jid or '@' not in jid:
            return 'bare'
        return jid.rsplit('@', 1)[-1]

    @staticmethod
    def get_channel_type() -> str:
        return 'whatsapp'

    def get_system_instructions(self) -> Optional[str]:
        return (
            "WhatsApp response style:\n"
            "- Reply concisely, naturally, and conversationally. Avoid repetitive greetings, "
            "signatures, and unnecessary ceremony.\n"
            "- Prefer one complete combined answer over several fragmented messages.\n"
            "- Use plain text that renders reliably in WhatsApp. Avoid Markdown constructs "
            "such as heading markers, fenced code blocks, and Markdown links; use plain URLs.\n"
            "- Images and files: ALWAYS deliver them with the `send_file` tool so they arrive "
            "as attachments. NEVER embed images with HTML `<img>` tags or Markdown image "
            "embeds (`![alt](url)`) — WhatsApp does not render them; they arrive as raw text.\n"
            "- Do not claim to be human. Be transparent that you are an AI assistant when "
            "identity is relevant.\n"
            "- Preserve useful structure with short paragraphs or simple numbered items "
            "when needed."
        )

    def _resolve_agent(self, sender: str, is_group: bool, jid: str,
                       alt_sender: str = '', payload: Optional[dict] = None) -> Optional[str]:
        """Pick the agent that handles this message. The base channel is
        bound to a single agent; subclasses may route per-sender/group and
        use the raw payload for identity hints. Returning None drops the
        message silently."""
        return self.agent_id

    def _gate_sender(self, sender: str, is_group: bool, jid: str, text: str,
                     push_name: str, payload: dict) -> bool:
        """Allowlist/pairing gate — returns True when the message should be
        processed. Groups are checked by group ID, DMs by individual user ID.
        Subclasses may override (e.g. when a routing table is the allowlist)."""
        from models.db import db
        if is_group:
            group_id = jid.split('@')[0] if '@' in jid else jid
            if not db.is_user_allowed(self.channel_id, group_id):
                _logger.info("WhatsApp group not in allowlist: group=%s", group_id)
                return False
            return True

        user_name = push_name or payload.get('name') or sender

        # Step 1: Fully approved user? (in allowlist AND has name set)
        if db.is_user_allowed(self.channel_id, sender):
            if db.needs_name(self.channel_id, sender):
                # NAME COLLECTION MODE — every message is treated as a name attempt
                name_candidate = text.strip() if text else ''
                if name_candidate and len(name_candidate) <= 100:
                    db.set_user_display_name(self.channel_id, sender, name_candidate)
                    self._do_send(sender,
                        "Thanks, %s! You're all set. How can I help you today?" % name_candidate)
                elif text:
                    self._do_send(sender,
                        "That name is too long. Please share a shorter name (max 100 characters).")
                else:
                    self._do_send(sender,
                        "Please tell me your name to continue (e.g. 'My name is Budi').")
                return False
            # User is fully approved — proceed to normal processing
            return True

        # Step 2: User NOT in allowlist — try pairing-code auto-approve
        from backend.channels.pairing import extract_pair_code, format_pair_code as fmt_code
        raw_code = extract_pair_code(text) if text else None
        if raw_code:
            _logger.info("WhatsApp pairing code received from %s (channel %s)", sender, self.channel_id)
            pending = db.get_pending_approval_by_code(raw_code)
            if pending:
                if not pending.get('external_user_id'):
                    db.update_pending_user_id(pending['id'], sender)
                approved_user = db.approve_pending_with_name_needed(pending['id'])
                if approved_user:
                    if db.needs_name(self.channel_id, sender):
                        self._do_send(sender,
                            "✅ You're now approved! Welcome aboard.\n\n"
                            "Before we chat, please tell me your name (e.g. 'My name is Budi').")
                    else:
                        self._do_send(sender,
                            "✅ You're now approved! Welcome aboard. How can I help you today?")
                return False
            else:
                self._do_send(sender,
                    "❌ That pairing code is invalid or has expired. "
                    "Please ask the administrator for a new one.")
                return False
        else:
            # No pairing code in message — check if pending approval already exists
            existing = db.get_pending_approvals(self.channel_id)
            already_pending = any(
                p.get('external_user_id') == sender for p in existing
            )
            if not already_pending:
                allowed, pair_code = self._check_allowlist(sender, user_name)
                if not allowed and pair_code:
                    self._do_send(sender,
                        "👋 You're not yet approved to chat here. "
                        "Please ask the administrator for a pairing code, then send it in this chat.")
                # If open mode, user IS allowed — would have been caught above
            # If already pending, stay silent (don't spam the user)
            _logger.info("WhatsApp DM from unapproved user %s (pending=%s, channel %s)",
                         sender, already_pending, self.channel_id)
            return False

    def start(self):
        # Register EventStream handlers first (before background bridge startup)
        from backend.event_stream import event_stream

        def _on_approval_required(data):
            if not self._is_super_agent_channel():
                return
            if data.get('channel_id') != self.channel_id:
                return
            user_id = data.get('external_user_id')
            if not user_id:
                return
            approval_id = data.get('approval_id', '')
            tool_name = data.get('tool_name', '')
            info = data.get('approval_info', {})
            risk = info.get('risk_level', 'medium')
            desc = info.get('description', 'This action requires your approval.')
            source_agent = data.get('source_agent_name')
            header = f"Approval Required (agent: {source_agent})" if source_agent else "Approval Required"
            text = f"{header}\nTool: {tool_name}\nRisk: {risk}\n{desc}"
            # Include the focused snippet (window centered on the dangerous line with a
            # marker) so mobile reviewers can actually see the risky code. WhatsApp
            # interactive-button bodies are length-limited, so keep it compact.
            focus_snippet = info.get('focus_snippet') or ''
            if focus_snippet:
                if len(focus_snippet) > 700:
                    focus_snippet = focus_snippet[:700].rstrip() + '\n…'
                text += f"\n\n```{focus_snippet}```"
            try:
                self._bridge_post('/send-buttons', {
                    'to': self._jid_map.get(user_id, user_id),
                    'text': text,
                    'buttons': [
                        {'id': f'approve:{approval_id}', 'title': 'Approve'},
                        {'id': f'reject:{approval_id}', 'title': 'Reject'},
                    ],
                })
            except Exception as e:
                _logger.error("WhatsApp approval send failed: %s", e)

        def _on_approval_resolved(data):
            if not self._is_super_agent_channel():
                return
            if data.get('channel_id') != self.channel_id:
                return
            user_id = data.get('external_user_id')
            if not user_id:
                return
            decision = data.get('decision', 'reject')
            timed_out = data.get('timed_out', False)
            if timed_out:
                label = "Timed out — auto-rejected."
            elif decision == 'approve':
                label = "Approved."
            else:
                label = "Rejected."
            try:
                self._bridge_post('/send', {'to': self._jid_map.get(user_id, user_id), 'text': label})
            except Exception as e:
                _logger.error("WhatsApp approval resolution send failed: %s", e)

        def _on_llm_thinking(data):
            if data.get('channel_id') != self.channel_id:
                return
            user_id = data.get('external_user_id')
            if not user_id:
                return
            # Debounce: cancel any pending timer, fire after 3 s idle to avoid spamming
            with self._typing_lock:
                # llm_thinking events are dispatched async (event_stream thread pool)
                # and can arrive AFTER the response was sent — suppress those so
                # they don't schedule a phantom typing indicator.
                if time.monotonic() < self._typing_suppress_until.get(user_id, 0):
                    return
                existing = self._typing_timer.pop(user_id, None)
                if existing:
                    existing.cancel()

                def _fire():
                    with self._typing_lock:
                        self._typing_timer.pop(user_id, None)
                        if time.monotonic() < self._typing_suppress_until.get(user_id, 0):
                            return
                    self.send_typing(user_id)

                t = threading.Timer(3.0, _fire)
                self._typing_timer[user_id] = t
                t.start()

        self._approval_required_handler = _on_approval_required
        self._approval_resolved_handler = _on_approval_resolved
        self._llm_thinking_handler = _on_llm_thinking
        event_stream.on('approval_required', _on_approval_required)
        event_stream.on('approval_resolved', _on_approval_resolved)
        event_stream.on('llm_thinking', _on_llm_thinking)

        # ── Initialize outbound dispatcher ──
        self._dispatcher = WhatsAppOutboundDispatcher(
            self,
            settings_getter=_read_global_setting,
        )

        self._running = True

        # Start the bridge in a background thread so start() returns immediately
        threading.Thread(target=self._start_bridge, daemon=True).start()
        _logger.info("WhatsApp channel %s starting (bridge port %s)", self.channel_id, self._bridge_port)

    @property
    def bridge_port(self) -> int:
        """Local port the Baileys sidecar binds — read by the manager to avoid collisions."""
        return self._bridge_port

    def _start_bridge(self):
        """Launch the Baileys sidecar with auto-restart on unexpected exit.

        The bridge subprocess (Node.js Express + Baileys) can crash transiently
        during cold auto-start (e.g. auth state instability after forced kill).
        This method retries up to MAX_RESTARTS times with exponential backoff,
        then gives up and sets _running = False.
        """
        MAX_RESTARTS = 3
        backoff = 2  # seconds, doubles each retry
        node_modules_installed = os.path.isdir(os.path.join(_BRIDGE_DIR, 'node_modules'))
        restart_count = 0

        while restart_count <= MAX_RESTARTS:
            try:
                # Ensure npm dependencies are installed (only on first attempt)
                if not node_modules_installed:
                    _logger.info("Installing whatsapp-bridge npm dependencies...")
                    subprocess.run(
                        ['npm', 'install'],
                        cwd=_BRIDGE_DIR,
                        check=True,
                        capture_output=True,
                    )
                    node_modules_installed = True

                from config import PORT as EVONIC_PORT, APP_ROOT
                # Anchor to APP_ROOT (absolute) rather than a CWD-relative path:
                # if the server is ever launched from a different working dir
                # (e.g. symlinked release dirs), a relative path would resolve to
                # an empty auth dir and force a spurious QR re-scan.
                session_dir = os.path.join(APP_ROOT, 'data', 'whatsapp-sessions', self.channel_id)
                os.makedirs(session_dir, exist_ok=True)

                callback_url = (
                    f"http://127.0.0.1:{EVONIC_PORT}"
                    f"/api/channels/whatsapp-bridge/{self.channel_id}/callback"
                )

                env = {
                    **os.environ,
                    'PORT': str(self._bridge_port),
                    'CALLBACK_URL': callback_url,
                    'CALLBACK_SECRET': self._callback_secret,
                    'AUTH_DIR': os.path.abspath(session_dir),
                }

                # Reset cached status so probes reflect the new process until it pushes
                self._last_bridge_status = None
                self._process = subprocess.Popen(
                    ['node', os.path.join(_BRIDGE_DIR, 'index.js')],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )

                # Guard: if stop() was called during a retry sleep, kill the
                # newly spawned process immediately to avoid orphan processes.
                if not self._running:
                    _logger.info("WhatsApp bridge for channel %s stopped during startup — cleaning up", self.channel_id)
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                    self._process = None
                    return

                if restart_count == 0:
                    _logger.info("WhatsApp bridge started for channel %s on port %s",
                                 self.channel_id, self._bridge_port)
                else:
                    _logger.info("WhatsApp bridge restarted for channel %s (attempt %d/%d, port %s)",
                                 self.channel_id, restart_count, MAX_RESTARTS, self._bridge_port)

                # Consume stdout until the process dies or we're asked to stop
                for line in self._process.stdout:
                    if not self._running:
                        break
                    # INFO so bridge activity is visible (early feature — verbose monitoring).
                    # Routed to logs/baileys.log via the 'baileys' logger.
                    _bridge_logger.info("[%s] %s", self.channel_id[:8], line.decode().rstrip())

                # Reap the child process to prevent zombie accumulation.
                # The process may be already dead (stdout pipe closed) or
                # still alive if we broke out due to _running=False.
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    # Still alive — deliberate stop path; kill via stop() later
                    pass

                # If _running is False, this was a deliberate stop — exit cleanly
                if not self._running:
                    return

                # Bridge exited while _running is still True — unexpected crash
                restart_count += 1
                if restart_count <= MAX_RESTARTS:
                    _logger.warning(
                        "WhatsApp bridge exited unexpectedly for channel %s (port %s) — "
                        "restarting in %ds (%d/%d)",
                        self.channel_id, self._bridge_port, backoff, restart_count, MAX_RESTARTS,
                    )
                    time.sleep(backoff)
                    backoff *= 2
                else:
                    self._running = False
                    _logger.error(
                        "WhatsApp bridge failed to stay alive after %d restarts for channel %s",
                        MAX_RESTARTS, self.channel_id,
                    )

            except Exception as e:
                _logger.error("WhatsApp bridge error for channel %s: %s", self.channel_id, e)
                restart_count += 1
                if restart_count <= MAX_RESTARTS:
                    _logger.info("Retrying bridge startup for channel %s in %ds (%d/%d)",
                                 self.channel_id, backoff, restart_count, MAX_RESTARTS)
                    time.sleep(backoff)
                    backoff *= 2
                else:
                    self._running = False
                    _logger.error(
                        "WhatsApp bridge failed to start after %d attempts for channel %s",
                        MAX_RESTARTS + 1, self.channel_id,
                    )

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._dispatcher:
            self._dispatcher.shutdown()
            self._dispatcher = None

        from backend.event_stream import event_stream
        if self._approval_required_handler:
            event_stream.off('approval_required', self._approval_required_handler)
        if self._approval_resolved_handler:
            event_stream.off('approval_resolved', self._approval_resolved_handler)
        if self._llm_thinking_handler:
            event_stream.off('llm_thinking', self._llm_thinking_handler)

        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                _logger.error(
                    "WhatsApp bridge did not finish graceful shutdown for channel %s — forcing exit",
                    self.channel_id,
                )
                self._process.kill()
                self._process.wait(timeout=5)
        self._process = None
        _logger.info("WhatsApp channel %s stopped", self.channel_id)

    def handle_callback(self, payload: dict):
        """Process incoming message POSTed by the sidecar."""
        from backend.agent_runtime import agent_runtime
        from backend.slash_commands import parse_command
        from models.db import db
        from backend.event_stream import event_stream

        # Bridge status push — cache + broadcast, no message processing
        if payload.get('event') == 'status':
            status = payload.get('status') or 'disconnected'
            _logger.info("WhatsApp bridge status for channel %s: %s", self.channel_id, status)
            self._last_bridge_status = status
            event_stream.emit('whatsapp_bridge_status', {
                'agent_id': self.agent_id,
                'channel_id': self.channel_id,
                'status': status,
            })
            return

        if payload.get('event') == 'outbound_status':
            status = payload.get('status') or 'unknown'
            _logger.info(
                'WhatsApp outbound status: correlation_id=%s status=%s retry=%s reason=%s',
                payload.get('correlation_id'), status,
                payload.get('retry_count', 0), payload.get('reason', ''),
            )
            event_stream.emit('whatsapp_outbound_status', {
                'agent_id': self.agent_id,
                'channel_id': self.channel_id,
                **payload,
            })
            if (status == 'failed' and payload.get('terminal')
                    and payload.get('reachout_timelocked')):
                if self._dispatcher:
                    self._dispatcher.pause_for_restriction(
                        payload.get('reachout_enforcement_ends'),
                        payload.get('reachout_enforcement_type'),
                    )
                self._record_reachout_restriction(payload, db, event_stream)
            return

        # Handle button reply (approval flow)
        button_id = payload.get('button_id', '')
        if button_id:
            _logger.info("WhatsApp button reply: %s (channel %s)", button_id, self.channel_id)
            parts = button_id.split(':', 1)
            if len(parts) == 2 and parts[0] in ('approve', 'reject'):
                from backend.agent_runtime.approval import approval_registry
                approval_registry.resolve(parts[1], parts[0])
            return

        sender = payload.get('from', '')
        jid = payload.get('jid') or sender  # full WhatsApp JID for replies
        is_group = payload.get('is_group', False)
        bot_mentioned = payload.get('bot_mentioned', False)
        quoted_is_bot = payload.get('quoted_is_bot', False)
        push_name = payload.get('pushName') or ''
        group_name = payload.get('group_name') or ''
        quoted_sender = payload.get('quoted_sender') or ''
        quoted_sender_name = payload.get('quoted_sender_name') or ''

        # WhatsApp Status updates and Channel newsletters are broadcasts, not
        # direct user messages. Routing them can create synthetic conversations
        # or capture newsletter IDs as unassigned shared-channel senders.
        if _is_non_conversational_broadcast(sender, jid):
            _logger.info("WhatsApp broadcast/newsletter dropped (channel %s)",
                         self.channel_id)
            return

        # Reply through the exact namespace used by the inbound conversation.
        # Baileys may also resolve the peer's alternate PN/LID identity; retain it
        # for diagnostics and persist both identities across restarts.
        alt_jid = payload.get('alt_jid') or ''
        alt_sender = payload.get('alt_sender') or ''
        if is_group:
            self._remember_jid_route(jid.split('@')[0], jid)
        elif sender and jid:
            self._remember_jid_route(sender, jid, alt_jid)
        if not is_group and jid.endswith('@lid'):
            _logger.info(
                "WhatsApp LID DM route: primary_namespace=%s alternate_namespace=%s channel=%s",
                self._jid_namespace(jid), self._jid_namespace(alt_jid), self.channel_id)
        text = strip_system_tags(payload.get('text', ''))
        image_data = payload.get('image')
        audio_data = payload.get('audio')
        video_data = payload.get('video')
        document_data = payload.get('document')
        quoted_text = payload.get('quoted_text')
        quoted_message = payload.get('quoted_message')
        quoted_context = _format_quoted_context(
            quoted_text, quoted_message, quoted_is_bot,
            quoted_sender_name, quoted_sender, is_group=is_group)

        _logger.info(
            "WhatsApp callback: sender=%s jid=%s group=%s mentioned=%s quoted_bot=%s text=%r",
            sender, jid, is_group, bot_mentioned, quoted_is_bot,
            (text[:60] if text else ''))

        # Determine message type for debug listener
        msg_type = payload.get('type') or 'text'
        if payload.get('image'):
            msg_type = 'image'
        elif payload.get('audio'):
            msg_type = 'audio'
        elif payload.get('video'):
            msg_type = 'video'
        elif payload.get('document'):
            msg_type = 'document'
        elif payload.get('sticker'):
            msg_type = 'sticker'
        elif payload.get('location'):
            msg_type = 'location'

        # Resolve before emitting diagnostics so the listener can report the route
        # outcome while still showing messages that are ultimately dropped.
        agent_id = self._resolve_agent(sender, is_group, jid, alt_sender,
                                       payload=payload)
        from datetime import datetime, timezone
        server_timestamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        event_stream.emit('whatsapp_inbound', {
            'channel_id': self.channel_id,
            'channel_name': self.config.get('name', self.channel_id),
            'message_id': payload.get('message_id') or '',
            'sender': sender,
            'jid': jid,
            'jid_namespace': self._jid_namespace(jid),
            'alt_sender': alt_sender,
            'alt_jid': alt_jid,
            'alt_jid_namespace': self._jid_namespace(alt_jid),
            'is_group': is_group,
            'push_name': push_name,
            'group_name': group_name,
            'text': (text[:200] if text else ''),
            'text_length': len(text or ''),
            'type': msg_type,
            'content_type': payload.get('content_type') or msg_type,
            'wrapper_types': payload.get('wrapper_types') or [],
            'payload_keys': payload.get('payload_keys') or [],
            'message_timestamp': payload.get('message_timestamp'),
            'server_timestamp': server_timestamp,
            # Keep timestamp for older listener clients.
            'timestamp': server_timestamp,
            'bot_mentioned': bot_mentioned,
            'quoted': bool(quoted_message or quoted_text),
            'quoted_is_bot': quoted_is_bot,
            'quoted_sender': quoted_sender,
            'quoted_sender_name': quoted_sender_name,
            'quoted_type': (quoted_message or {}).get('type')
                if isinstance(quoted_message, dict) else '',
            'route_status': 'matched' if agent_id else 'unmatched',
            'routed_agent_id': agent_id or '',
            'reply_jid': self._jid_map.get(sender, jid) if not is_group else jid,
            'fallback_jid': self._alternate_jids.get(sender, '') if not is_group else '',
        })

        if not agent_id:
            _logger.info("WhatsApp message dropped (no route): sender=%s is_group=%s jid=%s",
                         sender, is_group, jid)
            return

        # dm_only agents reject every group message before any further processing,
        # including @mentions, replies, and slash commands.
        agent = db.get_agent(agent_id)
        if _reject_group_for_agent(agent, is_group):
            _logger.info("WhatsApp group message dropped (agent dm_only): agent=%s sender=%s text=%s",
                         agent_id, sender, text[:80] if text else "")
            return

        # In groups, only respond when @mentioned or when user replies to a bot message
        if is_group and not bot_mentioned and not quoted_is_bot:
            _logger.info("WhatsApp group message dropped (not mentioned): sender=%s text=%s", sender, text[:80] if text else "")
            return

        # Strip the @mention tag from the message text
        if bot_mentioned and text:
            text = re.sub(r'@\d+', '', text).strip()

        # Allowlist check — groups use group ID, DMs use individual user ID
        if not self._gate_sender(sender, is_group, jid, text, push_name, payload):
            return

        image_url = None
        video_url = None
        image_bytes = None  # decoded original bytes, persisted as attachment below
        audio_bytes = None  # decoded original bytes, persisted as attachment below
        audio_mime = None
        document = _decode_document_payload(document_data)

        if image_data:
            try:
                image_bytes = base64.b64decode(image_data['base64'])
            except Exception as e:
                _logger.error("WhatsApp image decode failed: %s", e)
            if agent and agent.get('vision_enabled'):
                if image_bytes:
                    try:
                        from io import BytesIO
                        from PIL import Image
                        img = Image.open(BytesIO(image_bytes))
                        if img.mode in ('RGBA', 'LA', 'P'):
                            img = img.convert('RGB')
                        buf = BytesIO()
                        img.save(buf, format='JPEG', quality=85)
                        b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
                        image_url = f"data:image/jpeg;base64,{b64}"
                    except Exception as e:
                        _logger.error("WhatsApp image conversion failed: %s", e)
            if not text:
                text = '[Image]'

        if audio_data:
            # Audio is attachment-only — agents listen to it via the
            # transcribe_audio tool instead of inline multimodal input.
            try:
                audio_bytes = base64.b64decode(audio_data['base64'])
                audio_mime = audio_data.get('mimetype', 'audio/ogg')
            except Exception as e:
                _logger.error("WhatsApp audio decode failed: %s", e)
            if not text:
                text = '[Audio]'

        if video_data:
            if agent and agent.get('video_enabled'):
                try:
                    raw = base64.b64decode(video_data['base64'])
                    mime = video_data.get('mimetype', 'video/mp4')
                    b64 = base64.b64encode(raw).decode('utf-8')
                    video_url = f"data:{mime};base64,{b64}"
                except Exception as e:
                    _logger.error("WhatsApp video conversion failed: %s", e)
            elif not text:
                text = '[Video]'

        if document and not text:
            text = '[Document]'
        elif payload.get('document_download_failed') and not text:
            text = '[Document download failed]'

        if not text and not image_url and not video_url and not quoted_context:
            _logger.info("WhatsApp message dropped (no usable content): sender=%s", sender)
            return

        # Keep group slash commands raw so the runtime can detect them before
        # adding the sender context that normal group messages require.
        group_command = is_group and parse_command(text)

        # Prepend group/sender context (groups) or reply context (DMs).
        final_text = text
        if is_group and not group_command:
            final_text = _wrap_group_message(
                text, group_name, push_name, sender,
                quoted_text, quoted_is_bot,
                quoted_sender_name, quoted_sender, quoted_message)
        elif not is_group and quoted_context:
            final_text = f"{quoted_context}\n{text}"

        # For group messages, anchor the session to the group ID so all
        # participants share a single session.  Individual DMs keep the
        # sender as the external_user_id.
        if is_group:
            group_id = jid.split('@')[0] if '@' in jid else jid
            session_user_id = group_id
            # Map group_id → sender JID so that _do_send (including the
            # buffered worker path where external_user_id is the group_id)
            # can resolve the correct individual JID for replies.
            self._jid_map[group_id] = jid
        else:
            session_user_id = sender

        session_id = db.get_or_create_session(agent_id, session_user_id, self.channel_id)

        # Persist media to disk so attachment tools can access the original bytes.
        attachment_info = None
        if image_bytes:
            attachment_info = self._save_image_attachment(
                session_id, sender, image_bytes,
                image_data.get('mimetype') or 'image/jpeg', agent_id=agent_id)
        elif audio_bytes:
            attachment_info = self._save_audio_attachment(
                session_id, sender, audio_bytes, audio_mime or 'audio/ogg',
                agent_id=agent_id)
        elif document:
            attachment_info = self._save_document_attachment(
                session_id, sender, document['bytes'], document['mime_type'],
                document['filename'], agent_id=agent_id)

        # Append the standard marker only after persistence succeeds. This keeps
        # paths and attachment IDs truthful and makes captionless PDFs usable.
        if attachment_info and document:
            marker = _format_attachment_marker(attachment_info)
            final_text = f"{final_text}\n{marker}" if final_text else marker

        if not db.is_session_bot_enabled(session_id, agent_id=agent_id):
            _logger.info("WhatsApp message stored only — bot disabled for session %s (sender=%s)",
                         session_id, sender)
            db.add_chat_message(session_id, 'user', final_text or text or '[Attachment]',
                                agent_id=agent_id)
            return

        _logger.info("WhatsApp message received from %s (channel %s)", sender, self.channel_id)
        inbound_metadata = {"channel_message_id": str(payload.get("message_id") or "")}
        if attachment_info:
            inbound_metadata["attachment_info"] = attachment_info
        result = agent_runtime.handle_message(
            agent_id, session_user_id, final_text, self.channel_id,
            image_url=image_url, video_url=video_url,
            metadata=inbound_metadata,
        )
        if result.get('buffered'):
            _logger.info("WhatsApp message buffered for %s (session %s)", sender, session_id)
            return

        # Cancel any pending debounced typing timer so it doesn't fire
        # after the response is sent (would show a phantom "typing" indicator).
        self._clear_typing(sender)

        response = result.get('response') or ''
        if response and response != "(No response)":
            # Use the session identity for delivery. Group slash commands need
            # the group recipient rather than the participant who issued them.
            response_recipient = session_user_id if is_group else sender
            self.send_message(response_recipient, response, session_id=session_id)
        else:
            # No message will follow — actively clear any composing presence
            # shown during the thinking phase.
            self.send_typing(sender, state='paused')

        event_stream.emit('message_sent', {
            'channel_type': 'whatsapp',
            'channel_id': self.channel_id,
            'external_user_id': sender,
            'message': response,
        })

    def _save_image_attachment(self, session_id: str, external_user_id: str,
                               image_bytes: bytes, mime_type: str,
                               agent_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Persist an incoming image to disk and return attachment_info.

        Mirrors Telegram's _ingest_photo(): honors the agent attachment config
        (enabled + max size) and records the file via db.save_attachment().
        Returns None when persistence is disabled, over-limit, or fails.
        """
        from models.db import db
        agent_id = agent_id or self.agent_id
        try:
            cfg = db.get_agent_attachment_config(agent_id)
            if not cfg.get('enabled'):
                return None
            max_bytes = cfg.get('max_size_mb', 10) * 1024 * 1024
            if len(image_bytes) > max_bytes:
                _logger.info(
                    "Skipping WhatsApp image attachment for agent %s: "
                    "size %s exceeds %s bytes",
                    agent_id, len(image_bytes), max_bytes)
                return None
            ext = {
                'image/jpeg': '.jpg', 'image/png': '.png',
                'image/webp': '.webp', 'image/gif': '.gif',
            }.get(mime_type, '.jpg')
            filename = f"{int(time.time())}_whatsapp{ext}"
            target_dir = os.path.join('data', 'attachments', agent_id, session_id)
            os.makedirs(target_dir, exist_ok=True)
            file_path = os.path.join(target_dir, filename)
            with open(file_path, 'wb') as f:
                f.write(image_bytes)
            attachment_id = db.save_attachment(
                agent_id=agent_id,
                session_id=session_id,
                filename=filename,
                file_path=file_path,
                external_user_id=external_user_id,
                channel_id=self.channel_id,
                channel_type='whatsapp',
                original_filename=filename,
                mime_type=mime_type,
                file_type='photo',
                size_bytes=len(image_bytes),
            )
            _logger.info("WhatsApp image saved as attachment %s (%d bytes): %s",
                         attachment_id, len(image_bytes), file_path)
            return {
                'attachment_id': attachment_id,
                'filename': filename,
                'mime_type': mime_type,
                'size_bytes': len(image_bytes),
                'is_image': True,
                'file_path': file_path,
            }
        except Exception as e:
            _logger.error("Failed to persist WhatsApp image attachment: %s", e, exc_info=True)
            return None

    def _save_audio_attachment(self, session_id: str, external_user_id: str,
                               audio_bytes: bytes, mime_type: str,
                               agent_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Persist an incoming voice/audio message to disk and return attachment_info.

        Mirrors _save_image_attachment(): honors the agent attachment config
        (enabled + max size) and records the file via db.save_attachment().
        The agent listens to the file via the transcribe_audio tool.
        Returns None when persistence is disabled, over-limit, or fails.
        """
        from models.db import db
        agent_id = agent_id or self.agent_id
        try:
            cfg = db.get_agent_attachment_config(agent_id)
            if not cfg.get('enabled'):
                return None
            max_bytes = cfg.get('max_size_mb', 10) * 1024 * 1024
            if len(audio_bytes) > max_bytes:
                _logger.info(
                    "Skipping WhatsApp audio attachment for agent %s: "
                    "size %s exceeds %s bytes",
                    agent_id, len(audio_bytes), max_bytes)
                return None
            ext = {
                'audio/ogg': '.ogg', 'audio/ogg; codecs=opus': '.ogg',
                'audio/mpeg': '.mp3', 'audio/mp4': '.m4a',
                'audio/wav': '.wav', 'audio/webm': '.webm',
            }.get(mime_type, '.ogg')
            filename = f"{int(time.time())}_whatsapp_voice{ext}"
            target_dir = os.path.join('data', 'attachments', agent_id, session_id)
            os.makedirs(target_dir, exist_ok=True)
            file_path = os.path.join(target_dir, filename)
            with open(file_path, 'wb') as f:
                f.write(audio_bytes)
            attachment_id = db.save_attachment(
                agent_id=agent_id,
                session_id=session_id,
                filename=filename,
                file_path=file_path,
                external_user_id=external_user_id,
                channel_id=self.channel_id,
                channel_type='whatsapp',
                original_filename=filename,
                mime_type=mime_type,
                file_type='voice',
                size_bytes=len(audio_bytes),
            )
            _logger.info("WhatsApp audio saved as attachment %s (%d bytes): %s",
                         attachment_id, len(audio_bytes), file_path)
            return {
                'attachment_id': attachment_id,
                'filename': filename,
                'mime_type': mime_type,
                'size_bytes': len(audio_bytes),
                'file_path': file_path,
            }
        except Exception as e:
            _logger.error("Failed to persist WhatsApp audio attachment: %s", e, exc_info=True)
            return None

    def _save_document_attachment(self, session_id: str, external_user_id: str,
                                  document_bytes: bytes, mime_type: str,
                                  original_filename: str,
                                  agent_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Persist a validated inbound WhatsApp document as an Evonic attachment."""
        from models.db import db
        agent_id = agent_id or self.agent_id
        try:
            cfg = db.get_agent_attachment_config(agent_id)
            if not cfg.get('enabled'):
                _logger.info("Skipping WhatsApp document for agent %s: attachments disabled",
                             agent_id)
                return None
            max_bytes = cfg.get('max_size_mb', 10) * 1024 * 1024
            if len(document_bytes) > max_bytes:
                _logger.info(
                    "Skipping WhatsApp document for agent %s: size %s exceeds %s bytes",
                    agent_id, len(document_bytes), max_bytes)
                return None

            safe_name = _sanitize_attachment_filename(original_filename)
            filename = f"{int(time.time())}_{safe_name}"
            target_dir = os.path.join('data', 'attachments', agent_id, session_id)
            os.makedirs(target_dir, exist_ok=True)
            file_path = os.path.join(target_dir, filename)
            with open(file_path, 'wb') as handle:
                handle.write(document_bytes)
            attachment_id = db.save_attachment(
                agent_id=agent_id,
                session_id=session_id,
                filename=filename,
                file_path=file_path,
                external_user_id=external_user_id,
                channel_id=self.channel_id,
                channel_type='whatsapp',
                original_filename=safe_name,
                mime_type=mime_type,
                file_type='document',
                size_bytes=len(document_bytes),
            )
            _logger.info("WhatsApp document saved as attachment %s (%d bytes): %s",
                         attachment_id, len(document_bytes), file_path)
            return {
                'attachment_id': attachment_id,
                'filename': filename,
                'original_filename': safe_name,
                'mime_type': mime_type,
                'size_bytes': len(document_bytes),
                'file_path': file_path,
            }
        except Exception as exc:
            _logger.error("Failed to persist WhatsApp document attachment: %s",
                          exc, exc_info=True)
            return None

    def _clear_typing(self, external_user_id: str):
        """Cancel any pending typing debounce timer and suppress late
        llm_thinking events (dispatched async, they can outlive the turn)
        so no phantom composing fires around an outbound send."""
        with self._typing_lock:
            pending = self._typing_timer.pop(external_user_id, None)
            if pending:
                pending.cancel()
            self._typing_suppress_until[external_user_id] = time.monotonic() + 10.0

    def send_typing(self, external_user_id: str, state: str = 'composing'):
        """Send composing/paused presence to the given user."""
        to = self._jid_map.get(external_user_id, external_user_id)
        try:
            self._bridge_post('/typing', {'to': to, 'state': state})
        except Exception as e:
            _logger.warning("WhatsApp typing indicator failed for %s: %s", external_user_id, e)

    def send_message_buffered(self, external_user_id: str, text: str,
                              session_id: str = None):
        """Suppress intermediate agent output; WhatsApp delivers final responses only."""
        _logger.debug(
            "Suppressing WhatsApp intermediate output for channel %s", self.channel_id)

    def send_message(self, external_user_id: str, text: str,
                     session_id: str = None):
        """Queue final output and absorb pending intermediate messages."""
        if self._dispatcher:
            self._dispatcher.enqueue(
                external_user_id, text, session_id=session_id, is_final=True)
            return
        super().send_message(external_user_id, text, session_id=session_id)

    def get_qr(self) -> dict:
        """Fetch QR code data from the bridge."""
        try:
            resp = requests.get(f"http://127.0.0.1:{self._bridge_port}/qr", timeout=5)
            return resp.json()
        except Exception as e:
            return {'status': 'disconnected', 'error': str(e)}

    def get_bridge_status(self) -> dict:
        """Return live bridge status, falling back to the last valid push.

        Status callbacks can be missed or arrive during a transient reconnect, so
        the cached value must not permanently override the sidecar's current
        state.  A failed probe is non-destructive: retain the last known status
        rather than turning a temporary HTTP failure into a false disconnect.
        """
        try:
            resp = requests.get(f"http://127.0.0.1:{self._bridge_port}/status", timeout=5)
            resp.raise_for_status()
            live_status = resp.json().get('status')
            if live_status in ('connected', 'qr_pending', 'disconnected'):
                self._last_bridge_status = live_status
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            pass

        return {'status': self._last_bridge_status or 'disconnected'}

    def _do_send(self, external_user_id: str, text: str,
                 session_id: Optional[str] = None,
                 _inter_chunk_seconds: float = 0.0):
        # Prefer the exact inbound JID, including @lid. Persisted routes restore
        # this mapping for delayed agent/tool sends after a process restart.
        to = self._jid_map.get(external_user_id, external_user_id)
        alternate_jid = self._alternate_jids.get(external_user_id)
        from_map = external_user_id in self._jid_map
        _logger.info(
            "WhatsApp outbound route: primary_namespace=%s alternate_namespace=%s "
            "persisted=%s channel=%s",
            self._jid_namespace(to), self._jid_namespace(alternate_jid or ''),
            from_map, self.channel_id)
        # Format text for WhatsApp (already formatted by dispatcher; safe no-op
        # for bypass calls like pairing/approval failures).
        text = _whatsapp_format(text)
        # Every send path ends here — clear typing state so no phantom indicator
        # survives the send.
        self._clear_typing(external_user_id)
        chunks = _split_message(text)
        for i, chunk in enumerate(chunks):
            # Inter-chunk pacing: insert a short gap between 4096-char splits
            # so one answer is not emitted as a zero-gap burst.
            if i > 0 and _inter_chunk_seconds > 0:
                time.sleep(_inter_chunk_seconds)
            correlation_id = uuid.uuid4().hex
            payload = {
                'to': to,
                'text': chunk,
                'correlation_id': correlation_id,
            }
            if session_id:
                payload['session_id'] = session_id
            if self._bridge_send_retry(payload, external_user_id):
                _logger.info(
                    "WhatsApp outbound accepted: correlation_id=%s channel=%s",
                    correlation_id, self.channel_id)
        # Actively clear any lingering composing presence on the recipient
        self.send_typing(external_user_id, state='paused')
        from backend.event_stream import event_stream
        event_stream.emit('message_sent', {
            'channel_type': 'whatsapp',
            'channel_id': self.channel_id,
            'external_user_id': external_user_id,
            'message': text,
        })

    def _do_send_file(self, external_user_id: str, file_path: str,
                      caption: Optional[str] = None,
                      mime_type: Optional[str] = None) -> bool:
        """Send a file to a WhatsApp user via the bridge.

        Returns True on success, False on failure (missing file, too large, etc).
        """
        # 1. Validate file exists and is readable
        if not os.path.isfile(file_path) or not os.access(file_path, os.R_OK):
            _logger.error("File not found or not readable: %s", file_path)
            return False

        # 2. Check WhatsApp file size limit: 100 MB for documents
        file_size = os.path.getsize(file_path)
        max_size = 100 * 1024 * 1024  # 100 MB
        if file_size > max_size:
            _logger.error("File too large: %s bytes (max %s)", file_size, max_size)
            return False

        # 3. Resolve JID
        to = self._jid_map.get(external_user_id, external_user_id)

        # 4. Strip markdown from caption (WhatsApp uses plain text)
        if caption:
            caption = _whatsapp_format(caption)

        # 5. Submit through the bridge delivery lifecycle. A successful HTTP
        # response means Baileys accepted the attachment; delivery is confirmed
        # later through whatsapp_outbound_status callbacks.
        correlation_id = uuid.uuid4().hex
        self._clear_typing(external_user_id)
        try:
            result = self._bridge_post('/send-file', {
                'to': to,
                'filePath': file_path,
                'caption': caption,
                'mimeType': mime_type,
                'correlation_id': correlation_id,
            })
            status = result.get('status')
            if status != 'accepted':
                _logger.error(
                    "WhatsApp file was not accepted for %s: status=%s correlation_id=%s",
                    external_user_id, status, correlation_id)
                return False
            _logger.info(
                "WhatsApp file accepted for %s (channel %s): correlation_id=%s "
                "message_id=%s file=%s",
                external_user_id, self.channel_id, correlation_id,
                result.get('message_id'), file_path)
        except Exception as e:
            _logger.error("WhatsApp file send failed to %s: %s", external_user_id, e)
            return False
        finally:
            self.send_typing(external_user_id, state='paused')

        # 6. Report queue acceptance without claiming confirmed delivery.
        from backend.event_stream import event_stream
        event_stream.emit('message_sent', {
            'channel_type': 'whatsapp',
            'channel_id': self.channel_id,
            'external_user_id': external_user_id,
            'message': f"[File: {os.path.basename(file_path)}]",
            'status': 'accepted',
            'correlation_id': correlation_id,
            'message_id': result.get('message_id'),
        })
        return True

    def _bridge_send_retry(self, payload: dict, external_user_id: str,
                           max_attempts: int = 4, delay: float = 3.0) -> bool:
        """POST /send, retrying while the bridge is momentarily not connected.

        A 503 (bridge reports not-connected) or a connection error means the
        message was NOT sent — so retrying is duplicate-safe. The bridge
        normally reconnects within a few seconds (creds are preserved), so a
        short bounded retry keeps replies from being silently lost during a
        transient reconnect — the root cause of "agent replied but nothing
        arrived in WhatsApp". Read timeouts are NOT retried (the send may have
        gone through, and retrying could duplicate the message).
        """
        for attempt in range(1, max_attempts + 1):
            try:
                self._bridge_post('/send', payload)
                return True
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                if status == 503 and attempt < max_attempts:
                    _logger.warning(
                        "WhatsApp bridge not connected (503) sending to %s — retry %d/%d",
                        external_user_id, attempt, max_attempts - 1)
                    time.sleep(delay)
                    continue
                _logger.error("WhatsApp send failed to %s: %s", external_user_id, e)
                return False
            except requests.exceptions.ConnectionError as e:
                if attempt < max_attempts:
                    _logger.warning(
                        "WhatsApp bridge connection error sending to %s — retry %d/%d: %s",
                        external_user_id, attempt, max_attempts - 1, e)
                    time.sleep(delay)
                    continue
                _logger.error("WhatsApp send failed to %s: %s", external_user_id, e)
                return False
            except Exception as e:
                _logger.error("WhatsApp send failed to %s: %s", external_user_id, e)
                return False
        return False

    def _record_reachout_restriction(self, payload: dict, db, event_stream) -> None:
        """Persist a deduplicated restriction warning in the originating session."""
        session_id = payload.get('session_id')
        if not session_id or self.get_channel_type() not in ('whatsapp', 'whatsapp_shared'):
            return
        session = db.get_session_with_details(session_id)
        if not session:
            _logger.warning('Ignoring WhatsApp restriction callback for unknown session %s', session_id)
            return
        if session.get('channel_id') != self.channel_id:
            _logger.warning('Ignoring WhatsApp restriction callback for channel mismatch: %s', session_id)
            return
        session_agent_id = session.get('agent_id')
        if (self.get_channel_type() == 'whatsapp'
                and session_agent_id != self.agent_id):
            _logger.warning('Ignoring WhatsApp restriction callback for agent mismatch: %s', session_id)
            return
        if not session_agent_id:
            _logger.warning('Ignoring WhatsApp restriction callback without session agent: %s', session_id)
            return
        enforcement_type = payload.get('reachout_enforcement_type') or 'unknown enforcement'
        ends = payload.get('reachout_enforcement_ends') or 'an unknown time'
        restriction_key = f'{enforcement_type}|{ends}'
        content = (
            '[SYSTEM/whatsapp-restriction] WhatsApp sending is temporarily restricted '
            f'for this account ({enforcement_type}). Sending may resume after {ends}.'
        )
        for message in db.get_session_messages(session_id, limit=100, agent_id=session_agent_id):
            metadata = message.get('metadata') or {}
            if metadata.get('whatsapp_restriction_key') == restriction_key:
                return
        metadata = {
            'whatsapp_restriction_key': restriction_key,
            'reachout_enforcement_type': enforcement_type,
            'reachout_enforcement_ends': ends,
            'correlation_id': payload.get('correlation_id'),
        }
        db.add_chat_message(session_id, 'system', content, agent_id=session_agent_id,
                            metadata=metadata)
        _logger.warning(
            'WhatsApp reach-out restriction in session %s: type=%s ends=%s',
            session_id, enforcement_type, ends,
        )
        event_stream.emit('whatsapp_restriction_warning', {
            'agent_id': session_agent_id,
            'channel_id': self.channel_id,
            'session_id': session_id,
            'content': content,
            'metadata': metadata,
        })

    def _bridge_post(self, path: str, payload: dict):
        resp = requests.post(
            f"http://127.0.0.1:{self._bridge_port}{path}",
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
