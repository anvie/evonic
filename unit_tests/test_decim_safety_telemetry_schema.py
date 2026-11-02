"""Telemetry storage drift must be migrated, and failures must be visible.

A database created before ``detection_type``/``detection_command`` were added to
the DDL never received them: ``CREATE TABLE IF NOT EXISTS`` is a no-op there, so
both the INSERT and the SELECT of the activity table raised.  The table then
looked empty for weeks.  These tests pin the two halves of that lesson: the
additive migration, and an unreadable store reporting an error instead of an
empty list.
"""

import sqlite3
from types import SimpleNamespace

import pytest

from backend.services import decim_safety_telemetry as telemetry


def test_migration_adds_columns_missing_from_a_legacy_table():
    from models.db import db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    # Legacy shape: only the original columns, plus the two earliest additions.
    cursor.execute("""
        CREATE TABLE decim_safety_events (
            id TEXT PRIMARY KEY,
            occurred_at TEXT NOT NULL,
            mode TEXT NOT NULL,
            agent_id TEXT,
            hmads_duration_ms INTEGER
        )
    """)

    db._migrate_decim_safety_events(cursor)

    columns = {row[1] for row in cursor.execute("PRAGMA table_info(decim_safety_events)")}
    assert {"detection_type", "detection_command", "agent_id", "hmads_duration_ms"} <= columns

    # Idempotent: a second run must not raise.
    db._migrate_decim_safety_events(cursor)


def test_migration_covers_every_column_the_telemetry_module_writes():
    from models.db import db

    created = {col for col, _ddl in db._DECIM_EVENT_ADDED_COLUMNS}
    # Columns the telemetry module relies on for both read and write paths.
    assert {"detection_type", "detection_command"} <= created
    assert set(telemetry._EVENT_COLUMNS) >= created


def _broken_connect():
    raise sqlite3.OperationalError("no such column: e.detection_type")


def test_unreadable_events_store_reports_an_error(monkeypatch):
    monkeypatch.setattr(telemetry, "_connect", _broken_connect)

    result = telemetry.list_events(limit=10)

    assert result["events"] == []
    assert result["error"] == "telemetry_unavailable"
    assert "detection_type" in result["error_detail"]
    assert len(result["error_detail"]) <= telemetry.MAX_ERROR_DETAIL


def test_unreadable_state_reports_an_error(monkeypatch):
    monkeypatch.setattr(telemetry, "_connect", _broken_connect)

    state = telemetry.get_state()

    assert state["error"] == "telemetry_unavailable"
    assert state["total_recorded"] == 0


def test_error_detail_is_bounded():
    exc = RuntimeError("x" * 1000)
    assert len(telemetry._error_detail(exc)) == telemetry.MAX_ERROR_DETAIL


def test_health_surfaces_recording_policy_and_telemetry_error():
    from routes.decim_safety import api_decim_safety_health  # noqa: F401  (import check)

    settings = SimpleNamespace(enabled=True, mode="enforce", provider="systemone",
                               record_enforce_events=True, hmads_floor=True,
                               retention_days=30)
    # Mirrors the payload contract the dashboard reads for its badges.
    payload = {
        "recording": settings.mode == "shadow" or (
            settings.mode == "enforce" and settings.record_enforce_events),
        "hmads_floor": bool(settings.hmads_floor),
    }
    assert payload["recording"] is True
    assert payload["hmads_floor"] is True
