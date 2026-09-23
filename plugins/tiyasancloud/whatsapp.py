"""WhatsApp on the shared TiyasanCloud number.

The customer never types a phone number, never scans a QR and never opens a
WhatsApp session. After their agent is set up, the plugin:

1. picks the active ``whatsapp_shared`` channel (the one an operator connected
   once) and reads its number from the channel's WhatsApp session;
2. returns a ``wa.me`` link to that number with a ready-made, plain-language
   message that carries a short code;
3. when the customer taps the link and presses send, the shared channel drops
   the unknown sender into its inbox; the worker below finds the code there,
   routes the sender's number to the agent and says hello back.

So the customer's number comes from the message they send, and pairing needs
no change to Evonic's core.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import defaultdict
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .common import (ApiError, agent_not_found, config, config_int, in_minutes, iso, now, valid_phone)
from . import store

_logger = logging.getLogger(__name__)

MAX_CODES_PER_HOUR = 5
FAIL_TRIES, FAIL_WINDOW_MIN, FAIL_BLOCK_MIN = 5, 15, 15
HINT_EVERY_HOURS = 24

# Written for people who are not technical at all: no "pairing", no "verify",
# no "code" jargon beyond "kode saya", and the next step is always spelled out.
MESSAGES = {
    "id": {
        "start": "Halo! 👋 Saya mau mulai ngobrol dengan {agent} lewat WhatsApp.\n\nKode saya: {code}",
        "linked": ("Hore, sudah tersambung! 🎉 Mulai sekarang kamu bisa ngobrol dengan {agent} di sini.\n\n"
                   "Coba kirim pesan seperti: \"Tolong buatkan website untuk usaha saya.\""),
        "expired": ("Maaf, pesan ini sudah kedaluwarsa. 🙏\n\n"
                    "Buka lagi halaman Tiyasan kamu, lalu tekan tombol WhatsApp sekali lagi ya."),
        "hint": ("Halo! 👋 Nomor ini belum tersambung dengan asisten Tiyasan mana pun.\n\n"
                 "Buka halaman Tiyasan kamu, lalu tekan tombol WhatsApp di sana. Nanti pesannya sudah "
                 "disiapkan, kamu tinggal tekan kirim."),
        "taken": ("Nomor WhatsApp ini sudah tersambung ke akun Tiyasan lain. 🙏\n\n"
                  "Kalau menurutmu ini keliru, balas pesan ini dan tim kami akan bantu."),
        "full": ("Maaf, {agent} sudah tersambung dengan nomor WhatsApp lain. 🙏\n\n"
                 "Kalau mau pakai nomor ini, lepas dulu nomor yang lama di halaman Tiyasan kamu."),
    },
    "en": {
        "start": "Hi! 👋 I'd like to start chatting with {agent} on WhatsApp.\n\nMy code: {code}",
        "linked": ("You're all set! 🎉 From now on you can chat with {agent} right here.\n\n"
                   "Try sending something like: \"Please make a website for my business.\""),
        "expired": ("Sorry, this message has expired. 🙏\n\n"
                    "Open your Tiyasan page again and tap the WhatsApp button once more."),
        "hint": ("Hi! 👋 This number isn't connected to a Tiyasan assistant yet.\n\n"
                 "Open your Tiyasan page and tap the WhatsApp button there. The message will be ready, "
                 "you just press send."),
        "taken": ("This WhatsApp number is already connected to another Tiyasan account. 🙏\n\n"
                  "If you think that's a mistake, reply here and our team will help."),
        "full": ("Sorry, {agent} is already connected to another WhatsApp number. 🙏\n\n"
                 "To use this number, remove the old one on your Tiyasan page first."),
    },
}


def _locale_of(agent_id: Optional[str]) -> str:
    locale = (store.get_meta(agent_id).get("locale") if agent_id else None) or "id"
    return locale if locale in MESSAGES else "id"


def _text(agent_id: Optional[str], key: str, **fields: Any) -> str:
    return MESSAGES[_locale_of(agent_id)][key].format(**fields)


def _agent_name(agent_id: str) -> str:
    from models.db import db
    return (db.get_agent(agent_id) or {}).get("name") or ("asisten saya" if _locale_of(agent_id) == "id" else "my assistant")


# ------------------------------------------------------------------ channels

def shared_channels() -> List[Dict[str, Any]]:
    """Enabled whatsapp_shared channels (just SHARED_CHANNEL_ID when it is set)."""
    from models.db import db
    pinned = str(config().get("SHARED_CHANNEL_ID") or "").strip()
    if pinned:
        row = db.get_channel(pinned)
        return [row] if row and row.get("type") == "whatsapp_shared" else []
    rows = db.get_shared_channels() or []
    return [row for row in rows if row.get("type") == "whatsapp_shared" and row.get("enabled", 1)]


def channel_status(cid: Optional[str]) -> str:
    """connected | disconnected | not_running."""
    if not cid:
        return "not_running"
    try:
        from backend.channels.registry import channel_manager
        if not channel_manager.is_running(cid):
            return "not_running"
        instance = channel_manager.get_channel_instance(cid)
        status = (instance.get_bridge_status() or {}).get("status") if instance else None
        return "connected" if status == "connected" else "disconnected"
    except Exception:
        return "not_running"


def active_channel() -> Dict[str, Any]:
    """The shared channel to send new customers to: the first connected one."""
    channels = shared_channels()
    for row in channels:
        if channel_status(row["id"]) == "connected":
            return row
    if not channels:
        raise ApiError(503, "whatsapp_unavailable", "there is no shared WhatsApp channel yet")
    raise ApiError(503, "whatsapp_unavailable", "the shared WhatsApp number is not connected right now")


def channel_number(cid: str) -> str:
    """The shared number, read from the channel's WhatsApp session.

    Baileys keeps the logged-in account in ``creds.json`` (``me.id`` looks like
    ``6281100000000:12@s.whatsapp.net``). SHARED_NUMBER overrides it.
    """
    override = str(config().get("SHARED_NUMBER") or "").strip()
    if override:
        return override
    try:
        from config import BASE_DIR
        path = os.path.join(BASE_DIR, "data", "whatsapp-sessions", cid, "creds.json")
        with open(path, encoding="utf-8") as handle:
            me_id = str((json.load(handle).get("me") or {}).get("id") or "")
    except (OSError, ValueError):
        return ""
    return valid_phone(me_id.split("@")[0].split(":")[0]) or ""


def _channel_row(cid: str) -> Dict[str, Any]:
    from models.db import db
    row = db.get_channel(cid)
    if not row:
        raise ApiError(503, "whatsapp_unavailable", "the shared WhatsApp channel no longer exists")
    return row


def _routes(cid: str) -> Dict[str, str]:
    return dict((_channel_row(cid).get("config") or {}).get("routes") or {})


def _save_routes(cid: str, routes: Dict[str, str]) -> None:
    from models.db import db
    row = _channel_row(cid)
    channel_config = dict(row.get("config") or {})
    channel_config["routes"] = routes
    # The routing table is the allowlist; never widen it by accident.
    channel_config.setdefault("access_mode", "assigned_only")
    db.update_channel(cid, {"config": channel_config})


def _send(cid: str, to: str, text: str) -> None:
    try:
        from backend.channels.registry import channel_manager
        instance = channel_manager.get_channel_instance(cid)
        if instance:
            instance.send_message(to, text)
    except Exception:
        _logger.exception("TiyasanCloud: cannot reply on channel %s", cid)


# --------------------------------------------------------------------- codes

# Evonic's unambiguous alphabet (no 0/O, 1/I/L). Codes grant access to an
# agent, so they come from `secrets`, not Evonic's `random.choices`.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _generate_code() -> str:
    import secrets
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


def chat_link(agent_id: str, *, reuse: bool = False) -> Dict[str, Any]:
    """The "chat on WhatsApp" link for an agent.

    ``reuse=True`` hands back the agent's still-valid link instead of making a
    new one (re-running setup must not burn the hourly allowance).
    """
    from models.db import db
    if not db.get_agent(agent_id):
        raise agent_not_found(agent_id)

    if reuse:
        pending = store.pending_code(agent_id)
        if pending and channel_status(pending["channel_id"]) == "connected":
            return link_payload(agent_id, pending["code"], pending["expires_at"], pending["channel_id"])

    if len(store.links_of(agent_id)) >= config_int("MAX_LINKED_PER_AGENT"):
        raise ApiError(409, "link_limit", "this agent already has the maximum number of linked numbers")

    from datetime import timedelta
    if store.codes_issued_since(agent_id, iso(now() - timedelta(hours=1))) >= MAX_CODES_PER_HOUR:
        raise ApiError(429, "rate_limited", "too many links for this agent; try again later")

    channel = active_channel()
    code = _generate_code()
    expires_at = iso(in_minutes(config_int("PAIR_CODE_TTL_MINUTES")))
    store.add_pair_code(code, agent_id, channel["id"], expires_at)
    return link_payload(agent_id, code, expires_at, channel["id"])


# Evonic keeps the first 200 characters of an unknown sender's message in the
# inbox; the code sits at the end, so the message must stay well under that.
MAX_MESSAGE_CHARS = 180


def link_payload(agent_id: str, code: str, expires_at: str, cid: str) -> Dict[str, Any]:
    number = channel_number(cid)
    agent = _agent_name(agent_id)
    message = _text(agent_id, "start", agent=agent, code=code)
    if len(message) > MAX_MESSAGE_CHARS:
        agent = agent[: max(8, len(agent) - (len(message) - MAX_MESSAGE_CHARS) - 1)].rstrip() + "…"
        message = _text(agent_id, "start", agent=agent, code=code)
    encoded = quote(message)
    return {
        "number": number,
        "message": message,
        "wa_link": f"https://wa.me/{number}?text={encoded}" if number else None,
        "api_link": f"https://api.whatsapp.com/send?phone={number}&text={encoded}" if number else None,
        "code": code,
        "expires_at": expires_at,
    }


# ------------------------------------------------------------------- status

def status(agent_id: str) -> Dict[str, Any]:
    links = store.links_of(agent_id)
    pending = store.pending_code(agent_id)
    cid = (links[0]["channel_id"] if links else None) or (pending or {}).get("channel_id") \
        or next((c["id"] for c in shared_channels()), None)
    return {
        "number": channel_number(cid) if cid else "",
        "channel_status": channel_status(cid),
        "linked_users": [
            {"phone": row["phone"], "name": row.get("name"), "linked_at": row["linked_at"]} for row in links
        ],
        "pending": (link_payload(agent_id, pending["code"], pending["expires_at"], pending["channel_id"])
                    if pending else None),
    }


# -------------------------------------------------------------- link/unlink

def link(agent_id: str, cid: str, phone: str, name: Optional[str], alias: Optional[str] = None) -> None:
    """Route the number (and its WhatsApp LID, when the chat uses one) to the agent."""
    routes = _routes(cid)
    for key in {phone, alias} - {None, ""}:
        routes[key] = agent_id
    _save_routes(cid, routes)
    store.add_link(phone, agent_id, cid, name, alias)


def unlink(agent_id: str, phone_raw: str) -> None:
    phone = valid_phone(phone_raw)
    if not phone:
        raise ApiError(422, "invalid_phone", "phone must be E.164 digits without '+'")
    row = store.get_link(phone)
    if not row or row["agent_id"] != agent_id:
        raise ApiError(404, "not_found", "this number is not linked to this agent")
    try:
        routes = _routes(row["channel_id"])
        if any([routes.pop(key, None) for key in {phone, row.get("alias")} - {None, ""}]):
            _save_routes(row["channel_id"], routes)
    except ApiError:
        pass  # channel gone: nothing left to route
    store.remove_link(phone)
    from . import callbacks
    callbacks.send(agent_id, "whatsapp_unlinked", {"phone": phone})


# -------------------------------------------------------------- redeem loop

_KEYWORD_RE = re.compile(
    r"\b(?:KODE(?:\s+SAYA)?|(?:MY\s+)?CODE|PAIR)\s*[:\-]?\s*([A-Z0-9]{3})-?([A-Z0-9]{3})\b")
_BARE_RE = re.compile(r"^([A-Z0-9]{3})-?([A-Z0-9]{3})$")


def _extract_code(text: str) -> Optional[str]:
    """The code from our own message ("Kode saya: K7Q2XP"), or a message that is
    only the code.

    Stricter than Evonic's extractor on purpose: that one takes any 6-letter
    word ("THANKS"), which would answer ordinary chat with an error.
    """
    upper = (text or "").strip().upper()
    match = _KEYWORD_RE.search(upper) or _BARE_RE.match(upper)
    if not match:
        return None
    code = match.group(1) + match.group(2)
    return code if all(ch in CODE_ALPHABET for ch in code) else None


def redeem_from_inbox() -> int:
    """Pair every sender whose message carries a live code. Returns the count.

    Safe to run on a timer. Other messages stay in the inbox so the operator can
    still assign them by hand; their sender gets one friendly pointer a day.
    """
    from models.db import db
    linked_count = 0
    for channel in shared_channels():
        cid = channel["id"]
        for entry in db.get_inbox(cid):
            if entry.get("is_group"):
                continue
            # WhatsApp may address the chat by LID; then alt_user_id is the phone.
            sender = str(entry.get("external_user_id") or "")
            phone = valid_phone(entry.get("alt_user_id")) or valid_phone(sender)
            text = str(entry.get("last_message") or "")
            if not sender or not phone or not text:
                continue
            code = _extract_code(text)
            if not code:
                if not store.is_blocked(phone) and store.hint_due(sender, HINT_EVERY_HOURS):
                    _send(cid, sender, MESSAGES["id"]["hint"])
                continue
            # A message with a code is handled exactly once, whatever the outcome.
            db.delete_inbox_entry(entry["id"])
            if store.is_blocked(phone):
                continue
            record = store.get_pair_code(code)
            live = (record and not record.get("used_at") and record["expires_at"] > iso(now())
                    and record["channel_id"] == cid)
            if not live:
                if not store.record_failure(phone, FAIL_TRIES, FAIL_WINDOW_MIN, FAIL_BLOCK_MIN):
                    _send(cid, sender, _text(record["agent_id"] if record else None, "expired"))
                continue
            agent_id = record["agent_id"]
            owner = store.link_owner(phone)
            if owner and owner != agent_id:
                _send(cid, sender, _text(agent_id, "taken"))
                continue
            # The limit is checked again here: a link issued earlier may be used
            # after another number was connected.
            if len(store.links_of(agent_id)) >= config_int("MAX_LINKED_PER_AGENT"):
                _send(cid, sender, _text(agent_id, "full", agent=_agent_name(agent_id)))
                continue
            name = entry.get("push_name") or None
            link(agent_id, cid, phone, name, sender if sender != phone else None)
            store.mark_code_used(code, phone)
            store.clear_failures(phone)
            _send(cid, sender, _text(agent_id, "linked", agent=_agent_name(agent_id)))
            from . import callbacks
            callbacks.send(agent_id, "whatsapp_linked", {"phone": phone, "name": name})
            linked_count += 1
    return linked_count


class PairingWorker:
    """Background timer: redeem codes, switch on newly registered agents once
    their connector is up, and watch the shared channels."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_status: Dict[str, str] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tiyasancloud-pairing", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                redeem_from_inbox()
                from .agents import activate_ready
                activate_ready()
                self.watch_channels()
                store.purge_expired_codes()
            except Exception:
                _logger.exception("TiyasanCloud pairing worker failed")
            self._stop.wait(max(1, config_int("PAIRING_POLL_SECONDS")))

    def watch_channels(self) -> None:
        """Tell the accounts on a shared number once when it drops."""
        by_channel: Dict[str, set] = defaultdict(set)
        for row in store.links_of_all():
            by_channel[row["channel_id"]].add(row["agent_id"])
        from . import callbacks
        for cid, agent_ids in by_channel.items():
            status_now = channel_status(cid)
            previous = self._last_status.get(cid, "connected")
            self._last_status[cid] = status_now
            if status_now == previous or status_now == "connected":
                continue
            for agent_id in agent_ids:
                callbacks.send(agent_id, "whatsapp_channel_down", {"channel_status": status_now})
