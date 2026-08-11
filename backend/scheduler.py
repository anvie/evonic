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
        action_type='static_message',
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

import os
import logging
import json
import time
import uuid
import threading
import requests as http_lib
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.events import EVENT_JOB_MISSED, EVENT_JOB_EXECUTED

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self):
        self._timezone = os.getenv("EVONIC_TIMEZONE", "Asia/Jakarta")
        self._kb_organizer_hour, self._kb_organizer_minute = (
            self._parse_kb_organizer_time()
        )
        self._scheduler = BackgroundScheduler(daemon=True, timezone=self._timezone)
        self._started = False
        self._lock = threading.Lock()

    @staticmethod
    def _parse_kb_organizer_time_value(value: str) -> tuple[int, int]:
        """Parse a HH:MM value, raising ValueError for invalid values."""
        try:
            hour_text, minute_text = value.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
            return hour, minute
        except (ValueError, TypeError):
            raise ValueError("must be in HH:MM format") from None

    @classmethod
    def _parse_kb_organizer_time(cls) -> tuple[int, int]:
        """Return the persisted or environment KB Janitor time, defaulting to 03:00."""
        default = os.getenv("EVOMEM_KB_ORGANIZER_NIGHTLY_TIME", "03:00").strip()
        try:
            from models.db import db
            value = db.get_setting("kb_organizer_nightly_time", default).strip()
        except Exception as e:  # pragma: no cover - scheduler must still initialize
            log.warning("Failed to read KB Janitor schedule setting: %s", e)
            value = default
        try:
            return cls._parse_kb_organizer_time_value(value)
        except ValueError:
            log.warning("Invalid KB Janitor schedule %r; using 03:00", value)
            return 3, 0

    def refresh_kb_organizer_schedule(self, value: str) -> None:
        """Replace the running KB Janitor job with a validated daily schedule."""
        hour, minute = self._parse_kb_organizer_time_value(value)
        self._kb_organizer_hour, self._kb_organizer_minute = hour, minute
        if not self._started:
            return
        self._scheduler.add_job(
            self._sefton_tidy_all,
            CronTrigger(hour=hour, minute=minute, timezone=self._timezone),
            id="builtin:sefton_tidy",
            replace_existing=True,
            misfire_grace_time=3600,
        )

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
                CronTrigger(hour=3, minute=0, timezone=self._timezone),
                id='builtin:attachments_cleanup',
                replace_existing=True,
                misfire_grace_time=3600,
            )
        except Exception as e:  # pragma: no cover - defensive guard
            log.warning("Failed to register attachments cleanup job: %s", e)
        # Built-in: remove expired unassigned shared-channel senders hourly.
        try:
            self._scheduler.add_job(
                self._cleanup_expired_shared_inbox,
                IntervalTrigger(hours=1, timezone=self._timezone),
                id='builtin:shared_inbox_cleanup',
                replace_existing=True,
                misfire_grace_time=3600,
            )
        except Exception as e:  # pragma: no cover - defensive guard
            log.warning("Failed to register shared inbox cleanup job: %s", e)
        # Built-in: SEFTON nightly agentic tidy for all sefton-mode agents.
        try:
            self._scheduler.add_job(
                self._sefton_tidy_all,
                CronTrigger(
                    hour=self._kb_organizer_hour,
                    minute=self._kb_organizer_minute,
                    timezone=self._timezone,
                ),
                id='builtin:sefton_tidy',
                replace_existing=True,
                misfire_grace_time=3600,
            )
        except Exception as e:  # pragma: no cover
            log.warning("Failed to register sefton tidy job: %s", e)
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

    def _cleanup_expired_shared_inbox(self):
        """Hourly housekeeping for globally expired unassigned senders."""
        try:
            from routes.settings import _shared_inbox_retention_hours
            from models.db import db
            deleted = db.cleanup_expired_inbox_entries(
                _shared_inbox_retention_hours())
            if deleted:
                log.info("Shared inbox cleanup: deleted %d expired sender(s)", deleted)
        except Exception as e:
            log.error("Shared inbox cleanup failed: %s", e, exc_info=True)

    def _sefton_tidy_all(self):
        """Nightly SEFTON tidy: run KB Janitor for sefton-mode agents whose
        last KB filing was within the last 24h."""
        try:
            from models.db import db
            from backend.agent_runtime.memory_manager import (
                resolve_kb_organizer_mode, sefton_tidy_agent,
                _load_sefton_last_filing,
            )
            agents = db.get_agents()
            sefton_agents = [a for a in agents
                             if resolve_kb_organizer_mode(a) == 'sefton']
            if not sefton_agents:
                return

            cutoff = time.time() - 24 * 3600
            log.info("SEFTON tidy: %d sefton agent(s), cutoff %s",
                     len(sefton_agents),
                     datetime.fromtimestamp(cutoff, timezone.utc).isoformat())

            for agent in sefton_agents:
                if not agent.get('enabled'):
                    log.info("SEFTON tidy [%s]: skipped (agent disabled)", agent['id'])
                    continue
                last_filing = _load_sefton_last_filing(agent['id'])
                if last_filing <= 0:
                    log.info("SEFTON tidy [%s]: skipped (never filed)", agent['id'])
                    continue
                if last_filing < cutoff:
                    log.info("SEFTON tidy [%s]: skipped (last filing %s)",
                             agent['id'],
                             datetime.fromtimestamp(last_filing, timezone.utc).isoformat())
                    continue
                try:
                    result = sefton_tidy_agent(agent['id'])
                    log.info("SEFTON tidy [%s]: %s", agent['id'], result)
                except Exception as e:
                    log.error("SEFTON tidy [%s] failed: %s", agent['id'], e)
        except Exception as e:
            log.error("SEFTON tidy failed: %s", e, exc_info=True)

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

        # Minimum interval validation for auto_extend (>= 60 seconds)
        if trigger_type == 'auto_extend':
            interval_seconds = self._parse_interval_seconds(trigger_config)
            if interval_seconds < 60:
                raise ValueError(
                    f"auto_extend interval must be >= 60 seconds, got {interval_seconds}s")

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

    @staticmethod
    def _run_date_has_tz(run_date_str: str) -> bool:
        """Check if an ISO 8601 string contains timezone information."""
        try:
            return datetime.fromisoformat(run_date_str).tzinfo is not None
        except (ValueError, TypeError):
            return False

    def _make_run_date_aware(self, trigger_config: dict):
        """If run_date lacks timezone info, make it timezone-aware and log a warning."""
        run_date = trigger_config.get('run_date', '')
        if not run_date or not isinstance(run_date, str):
            return
        if self._run_date_has_tz(run_date):
            return
        local_tz = datetime.now().astimezone().tzinfo
        log.warning("run_date '%s' has no timezone info — treating as local time (%s)", run_date, local_tz)
        try:
            dt = datetime.fromisoformat(run_date)
            dt = dt.replace(tzinfo=local_tz)
            # Heuristic: if the naive time's hour differs from current local hour
            # by exactly the UTC offset, the caller may have mistakenly converted to UTC.
            local_now = datetime.now().astimezone()
            offset_hours = int(local_now.utcoffset().total_seconds() / 3600)
            naive_hour = dt.hour
            expected_hour = local_now.hour
            if abs(naive_hour - expected_hour) == abs(offset_hours) and offset_hours != 0:
                log.warning(
                    "run_date '%s' naive hour=%d vs local hour=%d (offset=%+d) — "
                    "caller may have accidentally UTC-converted the time",
                    run_date, naive_hour, expected_hour, offset_hours,
                )
            trigger_config['run_date'] = dt
        except (ValueError, TypeError):
            pass  # Malformed — let APScheduler handle the error

    @staticmethod
    def _parse_interval_seconds(trigger_config: dict) -> int:
        """Convert interval config to total seconds (supports seconds, minutes, hours keys)."""
        if 'seconds' in trigger_config:
            return int(trigger_config['seconds'])
        elif 'minutes' in trigger_config:
            return int(trigger_config['minutes']) * 60
        elif 'hours' in trigger_config:
            return int(trigger_config['hours']) * 3600
        return 0

    def _build_trigger(self, trigger_type: str, trigger_config: dict):
        if trigger_type == 'cron':
            return CronTrigger(**trigger_config)
        elif trigger_type == 'interval':
            return IntervalTrigger(**trigger_config)
        elif trigger_type == 'date':
            self._make_run_date_aware(trigger_config)
            return DateTrigger(**trigger_config)
        elif trigger_type == 'auto_extend':
            self._make_run_date_aware(trigger_config)
            # Only pass run_date and timezone to DateTrigger — filter out interval info
            kwargs = {}
            if 'run_date' in trigger_config:
                kwargs['run_date'] = trigger_config['run_date']
            if 'timezone' in trigger_config:
                kwargs['timezone'] = trigger_config['timezone']
            return DateTrigger(**kwargs)
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
                trigger_type = s['trigger_type']
                run_date = s['trigger_config'].get('run_date', '')
                now = datetime.now()

                # Handle past-due schedules
                if trigger_type == 'date':
                    # Skip expired one-shot schedules
                    if run_date:
                        try:
                            run_dt = datetime.fromisoformat(run_date)
                            if run_dt.tzinfo is None:
                                run_dt = run_dt.replace(tzinfo=now.tzinfo)
                            if run_dt < now:
                                db.update_schedule(s['id'], enabled=0)
                                continue
                        except (ValueError, TypeError):
                            pass
                elif trigger_type == 'auto_extend':
                    # Past-due auto_extend: fire immediately (now + 5s grace)
                    if run_date:
                        try:
                            run_dt = datetime.fromisoformat(run_date)
                            if run_dt.tzinfo is None:
                                run_dt = run_dt.replace(tzinfo=now.tzinfo)
                            if run_dt < now:
                                reschedule = now + timedelta(seconds=5)
                                s['trigger_config']['run_date'] = reschedule.isoformat()
                                db.update_schedule(s['id'],
                                                   trigger_config=s['trigger_config'])
                                log.info("auto_extend %s past-due, rescheduled to %s",
                                         s['id'], reschedule.isoformat())
                        except (ValueError, TypeError):
                            pass

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

        trigger_type = schedule['trigger_type']
        action_type = schedule['action_type']
        action_config = schedule['action_config']
        fired_at = datetime.now().isoformat()

        # --- auto_extend pre-flight guards ---
        running_log_id = None
        if trigger_type == 'auto_extend':
            # Concurrent execution guard: check if previous run is still in progress
            recent_logs = db.get_schedule_logs(schedule_id, limit=1)
            if recent_logs and recent_logs[0].get('status') == 'running':
                log.warning("auto_extend %s: previous run still in progress, skipping", schedule_id)
                return

            # Agent disabled mid-run guard: check agent enablement before proceeding
            agent_id = (action_config.get('agent_id')
                        or schedule.get('owner_id'))
            if agent_id:
                agent = db.get_agent(agent_id)
                if agent and not agent.get('enabled'):
                    log.warning("auto_extend %s: agent %s is disabled, stopping chain",
                                schedule_id, agent_id)
                    # Create an error log and stop the chain
                    db.create_schedule_log(
                        log_id=str(uuid.uuid4()),
                        schedule_id=schedule_id,
                        executed_at=fired_at,
                        duration_ms=0,
                        status='error',
                        action_type=action_type,
                        error_message=f"Agent {agent_id} is disabled — chain stopped",
                    )
                    db.cleanup_old_schedule_logs(schedule_id, keep=100)
                    new_count = schedule['run_count'] + 1
                    db.update_schedule(schedule_id, last_run_at=fired_at,
                                       run_count=new_count, enabled=0)
                    self._remove_job(schedule_id)
                    return

            # Create a running-pending log so concurrent guard works
            running_log_id = str(uuid.uuid4())
            db.create_schedule_log(
                log_id=running_log_id,
                schedule_id=schedule_id,
                executed_at=fired_at,
                duration_ms=0,
                status='running',
                action_type=action_type,
            )

        status = 'success'
        error_message = None
        action_summary = None
        action_output = None
        _cancel_after = None  # set to schedule_id to self-delete after this run
        start_ms = time.monotonic()

        try:
            if action_type == 'emit_event':
                event_payload = self._action_emit_event(action_config)
                action_summary = f"Emitted event '{action_config.get('event_name', '?')}'"
                if event_payload:
                    action_output = json.dumps(event_payload, indent=2) if isinstance(event_payload, dict) else str(event_payload)
            elif action_type in ('static_message', 'agent_message'):
                # agent_message is a deprecated alias for static_message
                msg_result = self._action_static_message(action_config)
                action_summary = f"Sent message to agent '{action_config.get('agent_id', '?')}'"
                if msg_result:
                    action_output = msg_result
            elif action_type == 'session_prompt':
                result = self._action_session_prompt(action_config)
                action_summary = f"Sent prompt to agent '{action_config.get('agent_id', '?')}'"
                if result and isinstance(result, dict):
                    action_output, result_is_error = self._format_session_result(result)
                    if result_is_error:
                        status = 'error'
                        error_message = error_message or 'Agent turn reported an error (see output below)'
            elif action_type == 'webhook':
                result = self._action_webhook(action_config)
                method = action_config.get('method', 'POST').upper()
                url = action_config.get('url', '')
                status_code = result['status_code']
                resp_body = result.get('body')
                action_summary = f"{method} {url} -> {status_code}"
                if resp_body:
                    action_output = resp_body
            elif action_type == 'poll_monitor':
                from backend.agent_runtime.monitors import run_monitor_poll
                result = run_monitor_poll(action_config)
                action_summary = (f"monitor '{action_config.get('command', '?')}': "
                                  f"{result.get('state', '?')}")
                if result.get('done'):
                    # Monitor resolved (fired, ended, or expired) and the agent
                    # was notified. Self-delete so the interval stops running.
                    _cancel_after = schedule_id
            elif action_type == 'poll_background_job':
                # Legacy auto-watch rows left in SQLite from before background
                # processes became opt-in. Drain them silently on first tick.
                action_summary = 'legacy background-job poll: removed'
                _cancel_after = schedule_id
            else:
                log.warning("Unknown action_type '%s' for %s",
                            action_type, schedule_id)
                # Clean up running log if we bailed early for auto_extend
                if running_log_id:
                    db.update_schedule_log(running_log_id, status='error',
                                           error_message=f"Unknown action_type: {action_type}")
                return
        except Exception as e:
            log.error("Action failed for %s (%s): %s",
                      schedule_id, schedule['name'], e)
            status = 'error'
            error_message = str(e)
            action_summary = action_summary or f"Failed to execute {action_type}"

        duration_ms = int((time.monotonic() - start_ms) * 1000)

        # Persist / update execution log
        if running_log_id:
            # Update the running-pending log with final results
            db.update_schedule_log(
                running_log_id,
                status=status,
                duration_ms=duration_ms,
                error_message=error_message,
                action_summary=action_summary,
                action_output=action_output,
            )
            db.cleanup_old_schedule_logs(schedule_id, keep=100)
        else:
            db.create_schedule_log(
                log_id=str(uuid.uuid4()),
                schedule_id=schedule_id,
                executed_at=fired_at,
                duration_ms=duration_ms,
                status=status,
                action_type=action_type,
                action_summary=action_summary,
                error_message=error_message,
                action_output=action_output,
            )
            db.cleanup_old_schedule_logs(schedule_id, keep=100)

        # Update run stats
        new_count = schedule['run_count'] + 1
        updates = {'last_run_at': fired_at, 'run_count': new_count}
        do_extend = False

        # Auto-disable if max_runs reached
        if schedule['max_runs'] and new_count >= schedule['max_runs']:
            updates['enabled'] = 0
            self._remove_job(schedule_id)
        elif trigger_type == 'auto_extend' and status == 'success':
            # The chain continues — extend to next run
            do_extend = True
        elif trigger_type == 'auto_extend' and status == 'error':
            # Chain stops on error — disable schedule
            updates['enabled'] = 0
            self._remove_job(schedule_id)
            log.warning("auto_extend %s: error encountered, chain stopped", schedule_id)
        else:
            self._update_next_run(schedule_id)

        if do_extend:
            # Calculate and validate the auto_extend interval
            trigger_config = dict(schedule['trigger_config'])
            interval_seconds = self._parse_interval_seconds(trigger_config)
            if interval_seconds < 60:
                log.warning("auto_extend %s: interval %ds < 60s, clamping to 60s",
                            schedule_id, interval_seconds)
                interval_seconds = 60

            # Calculate next run date from completion time
            completed_at = datetime.fromisoformat(fired_at)
            next_run = completed_at.replace(tzinfo=None)
            next_run = next_run + timedelta(seconds=interval_seconds)
            trigger_config['run_date'] = next_run.isoformat()
            updates['trigger_config'] = trigger_config
            updates['next_run_at'] = next_run.isoformat()

        db.update_schedule(schedule_id, **updates)

        # Re-register job for auto_extend extension
        if do_extend:
            updated_schedule = db.get_schedule(schedule_id)
            if updated_schedule and updated_schedule.get('trigger_config'):
                self._register_job(schedule_id, 'auto_extend',
                                   updated_schedule['trigger_config'])

        # Emit schedule_fired event
        self._emit('schedule_fired', {
            'schedule_id': schedule_id, 'name': schedule['name'],
            'owner_type': schedule['owner_type'],
            'owner_id': schedule['owner_id'],
            'action_type': action_type, 'fired_at': fired_at,
        })

        # Self-cleanup for resolved monitors — runs last so the
        # row updates above don't touch an already-deleted schedule.
        if _cancel_after:
            try:
                self.cancel_schedule(_cancel_after)
            except Exception as e:
                log.warning("monitor %s: self-cancel failed: %s",
                            _cancel_after, e)

    def _action_emit_event(self, config: dict):
        from backend.event_stream import event_stream
        event_name = config.get('event_name', 'schedule_custom')
        payload = config.get('payload', {})
        event_stream.emit(event_name, payload)
        return payload

    def _action_static_message(self, config: dict):
        """Deliver a pre-composed message directly to the user, bypassing the LLM.

        This is the canonical name; the deprecated 'agent_message' maps here.
        The message was already composed at schedule-creation time — we just
        need to deliver it to the user's session (and push via channel).

        Returns the delivered message text, or the handle_message result dict
        when falling through to the LLM path.
        """
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
                    "Resolved static_message routing: agent=%s -> user=%s channel=%s",
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

            # Push via channel (Telegram, etc.) so the user sees it immediately.
            # Only return on successful delivery — if the channel is down or
            # send_message raises, fall through to handle_message as safety net.
            instance = channel_manager._active.get(channel_id)
            if instance and instance.is_running:
                try:
                    instance.send_message(external_user_id, message)
                    log.info(
                        "Delivered static_message directly: agent=%s user=%s "
                        "session=%s", agent_id, external_user_id, session_id,
                    )
                    return message  # Success — delivered, return the message as output
                except Exception as e:
                    log.error(
                        "Failed to send static_message via channel %s: %s; "
                        "falling through to handle_message",
                        channel_id, e,
                    )
            else:
                log.warning(
                    "Channel %s not available/running for static_message; "
                    "falling through to handle_message",
                    channel_id,
                )

        # Fallback: no real user/channel resolved — use the old LLM path.
        # The agent will process the message in a __scheduler__ session, but
        # the response may not reach the user if no channel is associated.
        log.warning(
            "static_message falling back to handle_message (no real user "
            "resolved): agent=%s external_user_id=%s channel_id=%s",
            agent_id, external_user_id, channel_id or 'none',
        )
        result = agent_runtime.handle_message(
            agent_id=agent_id,
            external_user_id=external_user_id,
            message=message,
            channel_id=channel_id,
        )
        return result.get('response') if isinstance(result, dict) else str(result)

    def _action_session_prompt(self, config: dict):
        """Send a prompt that triggers full LLM processing via handle_message().

        Unlike static_message which delivers a pre-composed message directly,
        this routes the prompt through the agent's real user session so the LLM
        processes it with full tool access.  Useful for scheduled tasks that
        need to run code, query data, or make decisions at execution time.
        """
        from backend.agent_runtime import agent_runtime
        from models.db import db as main_db

        agent_id = config['agent_id']
        message = config['message']
        channel_id = config.get('channel_id')
        external_user_id = config.get('external_user_id', '__scheduler__')

        # Resolve the real human user session — same logic as static_message
        if external_user_id == '__scheduler__':
            human_session = main_db.get_latest_human_session(agent_id)
            if human_session:
                external_user_id = human_session['external_user_id']
                channel_id = channel_id or human_session.get('channel_id')
                log.info(
                    "Resolved session_prompt routing: agent=%s -> user=%s channel=%s",
                    agent_id, external_user_id, channel_id or 'none',
                )

        log.info(
            "Dispatching session_prompt to handle_message: agent=%s user=%s",
            agent_id, external_user_id,
        )
        result = agent_runtime.handle_message(
            agent_id=agent_id,
            external_user_id=external_user_id,
            message=message,
            channel_id=channel_id,
            # Run synchronously so we capture the full turn result (response +
            # tool outputs) for the schedule log. Without this, an agent with
            # message_buffer_seconds set would buffer the prompt and return
            # immediately with response=None — nothing to troubleshoot with.
            skip_buffer=True,
        )
        return result

    @staticmethod
    def _format_session_result(result: dict) -> tuple:
        """Build a troubleshooting-friendly action_output from a handle_message
        result, plus an is_error flag.

        Captures the agent's text response and a compact trace of every tool
        call (name, args preview, result/error preview) so a failed scheduled
        run can be diagnosed from the schedule log alone.

        Returns (output_text_or_None, is_error).
        """
        ARG_CAP = 400      # max chars per tool args preview
        RESULT_CAP = 400   # max chars per tool result preview
        TOTAL_CAP = 8000   # max chars for the whole output block

        def _preview(value, cap):
            try:
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            except Exception:
                text = str(value)
            text = text.replace('\n', ' ').strip()
            if len(text) > cap:
                text = text[:cap] + f"… (+{len(text) - cap} chars)"
            return text

        is_error = bool(result.get('error'))
        parts = []

        # Non-actionable dispatch states — explain why nothing ran synchronously.
        if result.get('response') is None:
            for flag, note in (
                ('buffered', "Prompt was buffered by the agent (message_buffer_seconds) — LLM result not captured synchronously."),
                ('injected', "Prompt was injected into an already-active session loop — processed asynchronously, result not captured here."),
                ('async', "Prompt was queued asynchronously (fire-and-forget) — result not captured here."),
            ):
                if result.get(flag):
                    parts.append(f"[{note}]")

        response_text = result.get('response')
        if response_text:
            parts.append(str(response_text).strip())
        elif not parts:
            parts.append("(no text response)")

        tool_trace = result.get('tool_trace') or []
        if tool_trace:
            parts.append(f"\n── Tools executed ({len(tool_trace)}) ──")
            for i, entry in enumerate(tool_trace, 1):
                name = entry.get('tool', '?')
                args_preview = _preview(entry.get('args'), ARG_CAP)
                res = entry.get('result')
                res_has_error = isinstance(res, dict) and (res.get('error') or res.get('is_error'))
                if res_has_error:
                    is_error = True
                    res_preview = "ERROR: " + _preview(res.get('error') or res, RESULT_CAP)
                else:
                    res_preview = _preview(res, RESULT_CAP)
                parts.append(f"{i}. {name}({args_preview}) → {res_preview}")

        output = "\n".join(parts).strip()
        if not output:
            return None, is_error
        if len(output) > TOTAL_CAP:
            output = output[:TOTAL_CAP] + f"\n… (output truncated, +{len(output) - TOTAL_CAP} chars)"
        return output, is_error

    def _action_webhook(self, config: dict) -> dict:
        method = config.get('method', 'POST').upper()
        url = config['url']
        headers = config.get('headers', {})
        body = config.get('body')
        timeout = config.get('timeout', 30)
        resp = http_lib.request(method, url, headers=headers, json=body,
                                timeout=timeout)
        log.info("Webhook %s %s -> %d", method, url, resp.status_code)
        resp_body = None
        try:
            resp_body = resp.text
        except Exception:
            pass
        return {'status_code': resp.status_code, 'body': resp_body}

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
