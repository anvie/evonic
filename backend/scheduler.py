"""
Global Scheduler — APScheduler wrapper with SQLite persistence and EventStream integration.

Usage:
    from backend.scheduler import scheduler

    scheduler.start()  # call once at app boot

    # Create a one-shot reminder
    scheduler.create_schedule(
        name='Remind standup',
        owner_type='agent', owner_id='agent-1',
        trigger_type='date',
        trigger_config={'run_date': '2026-04-21T09:00:00'},
        action_type='agent_message',
        action_config={'agent_id': 'agent-1', 'message': 'Time for standup!'},
    )

    # Create a recurring interval job
    scheduler.create_schedule(
        name='Health check',
        owner_type='plugin', owner_id='monitor',
        trigger_type='interval',
        trigger_config={'minutes': 5},
        action_type='emit_event',
        action_config={'event_name': 'health_check', 'payload': {}},
    )
"""

import logging
import time
import uuid
import threading
import requests as http_lib
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.events import EVENT_JOB_MISSED, EVENT_JOB_EXECUTED

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self):
        self._scheduler = BackgroundScheduler(daemon=True)
        self._started = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Load persisted schedules from DB and start the APScheduler."""
        if self._started:
            return
        self._started = True
        self._scheduler.add_listener(self._on_job_event,
                                     EVENT_JOB_EXECUTED | EVENT_JOB_MISSED)
        self._scheduler.start()
        self._load_from_db()
        # Built-in: nightly attachment cleanup (rows + files older than 7 days).
        try:
            self._scheduler.add_job(
                self._cleanup_expired_attachments,
                CronTrigger(hour=3, minute=0),
                id='builtin:attachments_cleanup',
                replace_existing=True,
            )
        except Exception as e:  # pragma: no cover - defensive guard
            log.warning("Failed to register attachments cleanup job: %s", e)
        log.info("Started with %d jobs", len(self._scheduler.get_jobs()))

    def _cleanup_expired_attachments(self):
        """Daily housekeeping: delete attachment rows + files older than 7 days."""
        try:
            from models.db import db
            deleted, freed = db.cleanup_expired_attachments(max_age_days=7)
            if deleted:
                log.info(
                    "Attachments cleanup: deleted %d rows, freed %d bytes",
                    deleted, freed,
                )
        except Exception as e:
            log.error("Attachments cleanup failed: %s", e, exc_info=True)

    def shutdown(self):
        """Gracefully shut down the scheduler."""
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False
            log.info("Shut down")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_schedule(self, name: str, owner_type: str, owner_id: str,
                        trigger_type: str, trigger_config: dict,
                        action_type: str, action_config: dict,
                        max_runs: int = None, metadata: dict = None) -> dict:
        """Create a new schedule, persist to DB, and register with APScheduler."""
        schedule_id = str(uuid.uuid4())[:8]

        # For one-shot date triggers, enforce max_runs=1
        if trigger_type == 'date' and max_runs is None:
            max_runs = 1

        from models.db import db
        db.create_schedule(
            schedule_id=schedule_id, name=name,
            owner_type=owner_type, owner_id=owner_id,
            trigger_type=trigger_type, trigger_config=trigger_config,
            action_type=action_type, action_config=action_config,
            max_runs=max_runs, metadata=metadata,
        )

        self._register_job(schedule_id, trigger_type, trigger_config)
        self._update_next_run(schedule_id)
        self._emit('schedule_created', {
            'schedule_id': schedule_id, 'name': name,
            'owner_type': owner_type, 'owner_id': owner_id,
        })

        return db.get_schedule(schedule_id)

    def cancel_schedule(self, schedule_id: str, owner_id: str = None) -> bool:
        """Cancel and delete a schedule. If owner_id is given, enforce ownership."""
        from models.db import db
        schedule = db.get_schedule(schedule_id)
        if not schedule:
            return False
        if owner_id and schedule['owner_id'] != owner_id:
            return False

        self._remove_job(schedule_id)
        db.delete_schedule_logs(schedule_id)
        db.delete_schedule(schedule_id)
        self._emit('schedule_cancelled', {
            'schedule_id': schedule_id, 'name': schedule['name'],
            'owner_type': schedule['owner_type'], 'owner_id': schedule['owner_id'],
        })
        return True

    def list_schedules(self, owner_type: str = None, owner_id: str = None,
                       enabled_only: bool = False) -> list:
        from models.db import db
        schedules = db.get_schedules(owner_type=owner_type, owner_id=owner_id,
                                     enabled_only=enabled_only)
        job_map = self._build_next_run_map() if self._started else {}
        return [self._enrich_next_run(s, job_map) for s in schedules]

    def get_schedule(self, schedule_id: str) -> Optional[dict]:
        from models.db import db
        s = db.get_schedule(schedule_id)
        return self._enrich_next_run(s) if s else None

    def toggle_schedule(self, schedule_id: str) -> Optional[dict]:
        """Toggle enabled/disabled state."""
        from models.db import db
        schedule = db.get_schedule(schedule_id)
        if not schedule:
            return None
        new_state = 0 if schedule['enabled'] else 1
        db.update_schedule(schedule_id, enabled=new_state)
        if new_state:
            self._register_job(schedule_id, schedule['trigger_type'],
                               schedule['trigger_config'])
            self._update_next_run(schedule_id)
        else:
            self._remove_job(schedule_id)
            db.update_schedule(schedule_id, next_run_at=None)
        return db.get_schedule(schedule_id)

    def run_now(self, schedule_id: str) -> bool:
        """Trigger a schedule immediately (out-of-band)."""
        from models.db import db
        schedule = db.get_schedule(schedule_id)
        if not schedule:
            return False
        self._execute_action(schedule_id)
        return True

    def cleanup_once_schedules(self) -> int:
        """Cancel and delete all executed one-shot (date-triggered) schedules.

        Returns the number of schedules cleaned up.
        """
        from models.db import db
        schedules = db.get_schedules()
        cleaned = 0
        for s in schedules:
            if s['trigger_type'] == 'date' and s['run_count'] > 0:
                self.cancel_schedule(s['id'])
                cleaned += 1
        if cleaned:
            log.info("Cleaned up %d executed once schedules", cleaned)
        return cleaned

    # ------------------------------------------------------------------
    # Internal: Job registration
    # ------------------------------------------------------------------

    def _build_trigger(self, trigger_type: str, trigger_config: dict):
        if trigger_type == 'cron':
            return CronTrigger(**trigger_config)
        elif trigger_type == 'interval':
            return IntervalTrigger(**trigger_config)
        elif trigger_type == 'date':
            return DateTrigger(**trigger_config)
        else:
            raise ValueError(f"Unknown trigger_type: {trigger_type}")

    def _register_job(self, schedule_id: str, trigger_type: str, trigger_config: dict):
        """Register (or replace) an APScheduler job for this schedule."""
        try:
            trigger = self._build_trigger(trigger_type, trigger_config)
            self._scheduler.add_job(
                self._execute_action,
                trigger=trigger,
                args=[schedule_id],
                id=schedule_id,
                replace_existing=True,
                misfire_grace_time=60,
            )
        except Exception as e:
            log.error("Failed to register job %s: %s", schedule_id, e)

    def _remove_job(self, schedule_id: str):
        try:
            self._scheduler.remove_job(schedule_id)
        except Exception:
            pass  # job may not exist in APScheduler

    def _update_next_run(self, schedule_id: str):
        """Update next_run_at from APScheduler's computed next fire time."""
        from models.db import db
        try:
            job = self._scheduler.get_job(schedule_id)
            if job and job.next_run_time:
                db.update_schedule(schedule_id,
                                   next_run_at=job.next_run_time.isoformat())
            else:
                db.update_schedule(schedule_id, next_run_at=None)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal: Load from DB on startup
    # ------------------------------------------------------------------

    def _load_from_db(self):
        """Reload all enabled schedules from DB into APScheduler."""
        from models.db import db
        schedules = db.get_schedules(enabled_only=True)
        loaded = 0
        for s in schedules:
            try:
                # Skip expired one-shot schedules
                if s['trigger_type'] == 'date':
                    run_date = s['trigger_config'].get('run_date', '')
                    if run_date and run_date < datetime.now().isoformat():
                        db.update_schedule(s['id'], enabled=0)
                        continue

                self._register_job(s['id'], s['trigger_type'], s['trigger_config'])
                self._update_next_run(s['id'])
                loaded += 1
            except Exception as e:
                log.error("Failed to load schedule %s (%s): %s",
                          s['id'], s['name'], e)
        log.info("Loaded %d/%d schedules from DB", loaded, len(schedules))

    # ------------------------------------------------------------------
    # Internal: Action execution
    # ------------------------------------------------------------------

    def _execute_action(self, schedule_id: str):
        """Called by APScheduler when a job fires."""
        from models.db import db

        schedule = db.get_schedule(schedule_id)
        if not schedule or not schedule['enabled']:
            return

        action_type = schedule['action_type']
        action_config = schedule['action_config']
        fired_at = datetime.now().isoformat()

        status = 'success'
        error_message = None
        action_summary = None
        start_ms = time.monotonic()

        try:
            if action_type == 'emit_event':
                self._action_emit_event(action_config)
                action_summary = f"Emitted event '{action_config.get('event_name', '?')}'"
            elif action_type == 'agent_message':
                self._action_agent_message(action_config)
                action_summary = f"Sent message to agent '{action_config.get('agent_id', '?')}'"
            elif action_type == 'webhook':
                status_code = self._action_webhook(action_config)
                method = action_config.get('method', 'POST').upper()
                url = action_config.get('url', '')
                action_summary = f"{method} {url} -> {status_code}"
            else:
                log.warning("Unknown action_type '%s' for %s",
                            action_type, schedule_id)
                return
        except Exception as e:
            log.error("Action failed for %s (%s): %s",
                      schedule_id, schedule['name'], e)
            status = 'error'
            error_message = str(e)
            action_summary = action_summary or f"Failed to execute {action_type}"

        duration_ms = int((time.monotonic() - start_ms) * 1000)

        # Persist execution log
        db.create_schedule_log(
            log_id=str(uuid.uuid4()),
            schedule_id=schedule_id,
            executed_at=fired_at,
            duration_ms=duration_ms,
            status=status,
            action_type=action_type,
            action_summary=action_summary,
            error_message=error_message,
        )
        db.cleanup_old_schedule_logs(schedule_id, keep=100)

        # Update run stats
        new_count = schedule['run_count'] + 1
        updates = {'last_run_at': fired_at, 'run_count': new_count}

        # Auto-disable if max_runs reached
        if schedule['max_runs'] and new_count >= schedule['max_runs']:
            updates['enabled'] = 0
            self._remove_job(schedule_id)
        else:
            self._update_next_run(schedule_id)

        db.update_schedule(schedule_id, **updates)

        # Emit schedule_fired event
        self._emit('schedule_fired', {
            'schedule_id': schedule_id, 'name': schedule['name'],
            'owner_type': schedule['owner_type'],
            'owner_id': schedule['owner_id'],
            'action_type': action_type, 'fired_at': fired_at,
        })

    def _action_emit_event(self, config: dict):
        from backend.event_stream import event_stream
        event_name = config.get('event_name', 'schedule_custom')
        payload = config.get('payload', {})
        event_stream.emit(event_name, payload)

    def _action_agent_message(self, config: dict):
        from backend.agent_runtime import agent_runtime
        from backend.channels.registry import channel_manager
        from models.db import db as main_db

        agent_id = config['agent_id']
        message = config['message']
        channel_id = config.get('channel_id')
        external_user_id = config.get('external_user_id', '__scheduler__')

        # If the schedule was created without proper routing (external_user_id
        # defaults to '__scheduler__'), try to resolve the real human user from
        # the agent's most recent active session.  This prevents reminders from
        # landing in a ghost session where the user never sees them.
        if external_user_id == '__scheduler__':
            human_session = main_db.get_latest_human_session(agent_id)
            if human_session:
                external_user_id = human_session['external_user_id']
                channel_id = channel_id or human_session.get('channel_id')
                log.info(
                    "Resolved agent_message routing: agent=%s -> user=%s channel=%s",
                    agent_id, external_user_id, channel_id or 'none',
                )

        # If we resolved a real user with a channel, deliver the message
        # directly — bypass the LLM.  The message was already composed by the
        # agent at schedule-creation time; re-running the LLM just risks the
        # response getting lost in a system-user session (see #217 follow-up).
        if external_user_id != '__scheduler__' and channel_id:
            session_id = main_db.get_or_create_session(
                agent_id, external_user_id, channel_id)
            main_db.add_chat_message(
                session_id, 'assistant', message, agent_id=agent_id)

            # Push via channel (Telegram, etc.) so the user sees it immediately
            instance = channel_manager._active.get(channel_id)
            if instance and instance.is_running:
                try:
                    instance.send_message(external_user_id, message)
                    log.info(
                        "Delivered agent_message directly: agent=%s user=%s "
                        "session=%s", agent_id, external_user_id, session_id,
                    )
                except Exception as e:
                    log.error(
                        "Failed to send agent_message via channel %s: %s",
                        channel_id, e,
                    )
            return

        # Fallback: no real user/channel resolved — use the old LLM path.
        # The agent will process the message in a __scheduler__ session, but
        # the response may not reach the user if no channel is associated.
        log.warning(
            "agent_message falling back to handle_message (no real user "
            "resolved): agent=%s external_user_id=%s channel_id=%s",
            agent_id, external_user_id, channel_id or 'none',
        )
        agent_runtime.handle_message(
            agent_id=agent_id,
            external_user_id=external_user_id,
            message=message,
            channel_id=channel_id,
        )

    def _action_webhook(self, config: dict) -> int:
        method = config.get('method', 'POST').upper()
        url = config['url']
        headers = config.get('headers', {})
        body = config.get('body')
        timeout = config.get('timeout', 30)
        resp = http_lib.request(method, url, headers=headers, json=body,
                                timeout=timeout)
        log.info("Webhook %s %s -> %d", method, url, resp.status_code)
        return resp.status_code

    # ------------------------------------------------------------------
    # Internal: APScheduler event listener
    # ------------------------------------------------------------------

    def _on_job_event(self, event):
        """Update next_run_at in DB after every execution or misfire."""
        schedule_id = event.job_id
        try:
            self._update_next_run(schedule_id)
            if event.code == EVENT_JOB_MISSED:
                log.warning("Job %s misfired at %s — next_run_at updated",
                            schedule_id, event.scheduled_run_time)
        except Exception as e:
            log.debug("_on_job_event error for %s: %s", schedule_id, e)

    # ------------------------------------------------------------------
    # Internal: Enrich schedule dict with live APScheduler next_run_time
    # ------------------------------------------------------------------

    def _build_next_run_map(self) -> dict:
        """Build a {schedule_id: next_run_time_iso} dict via a single get_jobs() call."""
        job_map = {}
        try:
            for job in self._scheduler.get_jobs():
                if job.next_run_time:
                    job_map[job.id] = job.next_run_time.isoformat()
        except Exception:
            pass
        return job_map

    def _enrich_next_run(self, schedule: dict, job_map: dict = None) -> dict:
        """Overlay live APScheduler next_run_time onto next_run_at, if available."""
        if not self._started:
            return schedule
        try:
            if job_map is not None:
                next_iso = job_map.get(schedule['id'])
                if next_iso:
                    schedule = dict(schedule)
                    schedule['next_run_at'] = next_iso
            else:
                job = self._scheduler.get_job(schedule['id'])
                if job and job.next_run_time:
                    schedule = dict(schedule)
                    schedule['next_run_at'] = job.next_run_time.isoformat()
        except Exception:
            pass
        return schedule

    # ------------------------------------------------------------------
    # Internal: Event emission helper
    # ------------------------------------------------------------------

    def _emit(self, event_name: str, data: dict):
        try:
            from backend.event_stream import event_stream
            event_stream.emit(event_name, data)
        except Exception as e:
            log.error("Failed to emit %s: %s", event_name, e)


# Module-level singleton
scheduler = Scheduler()
