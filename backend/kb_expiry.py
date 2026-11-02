"""Optional lifetime for knowledge-base documents.

A doc whose frontmatter has ``expires: <date or date-time>`` is deleted once that moment has passed; a doc without it
is never deleted. Anything that writes docs can use it for information that goes stale (an advisory, a price, a
schedule): ``evomem_writer.upsert_doc(..., expires=...)``. A doc that other docs link to is kept and loses its expiry,
so no link is left dangling (see ``evomem_writer.sweep_expired``).

This module runs that sweep for every agent, once at start and then hourly.
"""

import os
import threading

from backend.logging_config import get_logger

_logger = get_logger(__name__)

INTERVAL_SECONDS = 3600
_thread = None
_stop = threading.Event()
_lock = threading.Lock()


def run_once() -> dict:
    """Sweep every agent's knowledge base. Returns ``{agent_id: {'deleted': [...], 'kept': [...]}}`` for agents
    where something happened."""
    from backend.agent_runtime import evomem_writer
    from backend.agent_runtime.evomem_client import _get_evomem_dir
    from models.db import db
    done = {}
    for agent in db.get_agents():
        agent_id = agent['id']
        try:
            if not os.path.isdir(_get_evomem_dir(agent_id)):
                continue
            result = evomem_writer.sweep_expired(agent_id)
            if result['deleted'] or result['kept']:
                done[agent_id] = result
                _logger.info("kb_expiry[%s]: deleted %d, kept %d (linked) expired doc(s)",
                             agent_id, len(result['deleted']), len(result['kept']))
        except Exception:
            _logger.warning("kb_expiry[%s]: sweep failed", agent_id, exc_info=True)
    return done


def start(interval: float = INTERVAL_SECONDS) -> bool:
    """Start the periodic sweep (idempotent). Returns True if it was started."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop.clear()

        def _loop():
            while not _stop.wait(0):
                try:
                    run_once()
                except Exception:
                    _logger.warning("kb_expiry: sweep failed", exc_info=True)
                if _stop.wait(interval):
                    break

        _thread = threading.Thread(target=_loop, name="kb-expiry", daemon=True)
        _thread.start()
    _logger.info("KB expiry sweep started (interval=%ss)", interval)
    return True


def stop() -> None:
    _stop.set()
