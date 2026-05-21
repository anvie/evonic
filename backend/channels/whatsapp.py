"""WhatsApp channel implementation via Baileys Node.js sidecar."""

import base64
import logging
import os
import re
import secrets
import subprocess
import time
import threading
import requests
from typing import Dict, Any, Optional
from backend.channels.base import BaseChannel, strip_system_tags

_logger = logging.getLogger(__name__)

_BRIDGE_DIR = os.path.join(os.path.dirname(__file__), 'whatsapp-bridge')


def _strip_markdown(text: str) -> str:
    """Remove markdown symbols from text for plain WhatsApp messages."""
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*+', '', text)
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
        # Maps external_user_id (bare number) → full WhatsApp JID for reliable replies
        self._jid_map: Dict[str, str] = {}
        # Debounce state for llm_thinking typing indicator
        self._typing_timer: Dict[str, threading.Timer] = {}
        self._typing_lock = threading.Lock()

    @staticmethod
    def get_channel_type() -> str:
        return 'whatsapp'

    def get_system_instructions(self) -> Optional[str]:
        return (
            "IMPORTANT — WhatsApp Formatting Constraint:\n"
            "You are responding via WhatsApp which uses PLAIN TEXT only. "
            "Markdown formatting (bold, italic, code blocks, headers, bullet lists) "
            "is NOT supported and will appear as raw symbols.\n\n"
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
        # Register EventStream handlers first (before background bridge startup)
        from backend.event_stream import event_stream

        def _on_approval_required(data):
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
                existing = self._typing_timer.pop(user_id, None)
                if existing:
                    existing.cancel()

                def _fire():
                    with self._typing_lock:
                        self._typing_timer.pop(user_id, None)
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

        self._running = True

        # Start the bridge in a background thread so start() returns immediately
        threading.Thread(target=self._start_bridge, daemon=True).start()
        _logger.info("WhatsApp channel %s starting (bridge port %s)", self.channel_id, self._bridge_port)

    def _start_bridge(self):
        """Launch the Baileys sidecar (runs in background thread)."""
        try:
            # Ensure npm dependencies are installed
            node_modules = os.path.join(_BRIDGE_DIR, 'node_modules')
            if not os.path.isdir(node_modules):
                _logger.info("Installing whatsapp-bridge npm dependencies...")
                subprocess.run(
                    ['npm', 'install'],
                    cwd=_BRIDGE_DIR,
                    check=True,
                    capture_output=True,
                )

            from config import PORT as EVONIC_PORT
            session_dir = os.path.join('data', 'whatsapp-sessions', self.channel_id)
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

            self._process = subprocess.Popen(
                ['node', os.path.join(_BRIDGE_DIR, 'index.js')],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            _logger.info("WhatsApp bridge started for channel %s on port %s", self.channel_id, self._bridge_port)

            for line in self._process.stdout:
                if not self._running:
                    break
                _logger.debug("[bridge] %s", line.decode().rstrip())
            # If we exit the loop while still running, the bridge process died unexpectedly
            if self._running:
                self._running = False
                _logger.warning("WhatsApp bridge process exited unexpectedly for channel %s (port %s)",
                               self.channel_id, self._bridge_port)
        except Exception as e:
            _logger.error("WhatsApp bridge failed to start for channel %s: %s", self.channel_id, e)

    def stop(self):
        if not self._running:
            return
        self._running = False

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
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
        _logger.info("WhatsApp channel %s stopped", self.channel_id)

    def handle_callback(self, payload: dict):
        """Process incoming message POSTed by the sidecar."""
        from backend.agent_runtime import agent_runtime
        from models.db import db
        from backend.event_stream import event_stream

        # Handle button reply (approval flow)
        button_id = payload.get('button_id', '')
        if button_id:
            parts = button_id.split(':', 1)
            if len(parts) == 2 and parts[0] in ('approve', 'reject'):
                from backend.agent_runtime.approval import approval_registry
                approval_registry.resolve(parts[1], parts[0])
            return

        sender = payload.get('from', '')
        jid = payload.get('jid') or sender  # full WhatsApp JID for replies
        if sender and jid:
            self._jid_map[sender] = jid
        text = strip_system_tags(payload.get('text', ''))
        image_data = payload.get('image')
        quoted_text = payload.get('quoted_text')

        # Allowlist check with pairing-code auto-approve for WhatsApp.
        user_name = payload.get('pushName') or payload.get('name') or sender

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
                    return
                else:
                    self._do_send(sender,
                        "❌ That pairing code is invalid or has expired. "
                        "Please ask the administrator for a new one.")
                    return
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
                return

        image_url = None

        if image_data:
            agent = db.get_agent(self.agent_id)
            if agent and agent.get('vision_enabled'):
                try:
                    raw = base64.b64decode(image_data['base64'])
                    from io import BytesIO
                    from PIL import Image
                    img = Image.open(BytesIO(raw))
                    if img.mode in ('RGBA', 'LA', 'P'):
                        img = img.convert('RGB')
                    buf = BytesIO()
                    img.save(buf, format='JPEG', quality=85)
                    b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
                    image_url = f"data:image/jpeg;base64,{b64}"
                except Exception as e:
                    _logger.error("WhatsApp image conversion failed: %s", e)
            elif not text:
                return

        if not text and not image_url:
            return

        # Prepend reply context
        final_text = text
        if quoted_text:
            final_text = f"[Replying to: {quoted_text[:200]}]\n{text}"

        session_id = db.get_or_create_session(self.agent_id, sender, self.channel_id)
        if not db.is_session_bot_enabled(session_id, agent_id=self.agent_id):
            db.add_chat_message(session_id, 'user', text or '[Image]', agent_id=self.agent_id)
            return

        _logger.info("WhatsApp message received from %s (channel %s)", sender, self.channel_id)
        result = agent_runtime.handle_message(
            self.agent_id, sender, final_text, self.channel_id, image_url=image_url
        )
        if result.get('buffered'):
            return

        response = _strip_markdown(result.get('response') or '')
        if response and response != "(No response)":
            # Human-like typing delay relative to response length
            _TYPING_SPEED = 15   # chars/sec
            _MIN_DELAY = 1.0     # seconds
            _MAX_DELAY = 8.0     # seconds
            _TYPING_REFRESH = 5.0  # re-send composing every N seconds during delay

            delay = max(_MIN_DELAY, min(len(response) / _TYPING_SPEED, _MAX_DELAY))
            self.send_typing(sender)
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                time.sleep(min(_TYPING_REFRESH, remaining))
                if time.monotonic() < deadline:
                    self.send_typing(sender)

            for chunk in _split_message(response):
                self._do_send(sender, chunk)

        event_stream.emit('message_sent', {
            'channel_type': 'whatsapp',
            'channel_id': self.channel_id,
            'external_user_id': sender,
            'message': response,
        })

    def send_typing(self, external_user_id: str):
        """Send composing presence to the given user."""
        to = self._jid_map.get(external_user_id, external_user_id)
        try:
            self._bridge_post('/typing', {'to': to})
        except Exception as e:
            _logger.warning("WhatsApp typing indicator failed for %s: %s", external_user_id, e)

    def get_qr(self) -> dict:
        """Fetch QR code data from the bridge."""
        try:
            resp = requests.get(f"http://127.0.0.1:{self._bridge_port}/qr", timeout=5)
            return resp.json()
        except Exception as e:
            return {'status': 'disconnected', 'error': str(e)}

    def get_bridge_status(self) -> dict:
        """Fetch bridge connection status."""
        try:
            resp = requests.get(f"http://127.0.0.1:{self._bridge_port}/status", timeout=5)
            return resp.json()
        except Exception:
            return {'status': 'disconnected'}

    def _do_send(self, external_user_id: str, text: str):
        # Resolve full JID from map; fall back to external_user_id as-is
        to = self._jid_map.get(external_user_id, external_user_id)
        text = _strip_markdown(text)
        for chunk in _split_message(text):
            try:
                self._bridge_post('/send', {'to': to, 'text': chunk})
                _logger.info("WhatsApp message sent to %s (channel %s)", external_user_id, self.channel_id)
            except Exception as e:
                _logger.error("WhatsApp send failed to %s: %s", external_user_id, e)
        from backend.event_stream import event_stream
        event_stream.emit('message_sent', {
            'channel_type': 'whatsapp',
            'channel_id': self.channel_id,
            'external_user_id': external_user_id,
            'message': text,
        })

    def _bridge_post(self, path: str, payload: dict):
        resp = requests.post(
            f"http://127.0.0.1:{self._bridge_port}{path}",
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
