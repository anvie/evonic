"""Signed callbacks to the console.

Events are queued first and delivered by a worker, so a slow or unreachable
console never blocks a WhatsApp reply or an agent turn. Each request carries an
HMAC-SHA256 signature over the raw body, keyed with ACCESS_TOKEN.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from datetime import timedelta
from typing import Any, Dict, Optional

from .common import config, iso, now
from . import store

_logger = logging.getLogger(__name__)

# Retry schedule from the spec: 10 s, 1 min, 10 min, then give up.
RETRY_DELAYS = (10, 60, 600)
TIMEOUT_SECONDS = 10


def sign(body: str, timestamp: str) -> str:
    secret = str(config().get("ACCESS_TOKEN") or "").encode("utf-8")
    digest = hmac.new(secret, f"{timestamp}.{body}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def send(agent_id: str, event_type: str, data: Dict[str, Any]) -> Optional[str]:
    """Queue an event for the agent's callback_url. Returns the event id."""
    url = (store.get_meta(agent_id) or {}).get("callback_url")
    if not url:
        return None
    event_id = store.new_event_id()
    body = json.dumps({
        "id": event_id,  # the console drops duplicates by this id
        "type": event_type,
        "agent_id": agent_id,
        "occurred_at": iso(now()),
        "data": data,
    })
    store.queue_callback(event_id, agent_id, url, body)
    return event_id


def deliver_due() -> int:
    """Send every queued callback that is due. Returns how many were delivered."""
    try:
        import requests
    except Exception:
        return 0
    delivered = 0
    for row in store.due_callbacks():
        timestamp = str(int(now().timestamp()))
        headers = {
            "content-type": "application/json",
            "X-Tiyasan-Timestamp": timestamp,
            "X-Tiyasan-Signature": sign(row["body"], timestamp),
        }
        try:
            response = requests.post(row["url"], data=row["body"].encode("utf-8"),
                                     headers=headers, timeout=TIMEOUT_SECONDS)
            ok = 200 <= response.status_code < 300
        except Exception:
            ok = False
        if ok:
            store.drop_callback(row["id"])
            delivered += 1
            continue
        attempts = int(row["attempts"]) + 1
        if attempts > len(RETRY_DELAYS):
            _logger.warning("TiyasanCloud callback %s gave up after %s attempts", row["id"], attempts)
            store.drop_callback(row["id"])
            continue
        delay = RETRY_DELAYS[attempts - 1]
        store.defer_callback(row["id"], attempts, iso(now() + timedelta(seconds=delay)))
    return delivered


class CallbackWorker:
    def __init__(self, interval: int = 5) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tiyasancloud-callbacks", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                deliver_due()
            except Exception:
                _logger.exception("TiyasanCloud callback worker failed")
            self._stop.wait(self.interval)
