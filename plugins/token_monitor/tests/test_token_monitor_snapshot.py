"""Focused storage and API contracts for the Token Monitor snapshot."""

from datetime import datetime, timedelta, timezone

from flask import Flask

from plugins.token_monitor.db import UsageDB
from plugins.token_monitor.routes import create_blueprint


def _record(db, *, provider="", model="shared-model", agent_id="agent", total=100,
            prompt=80, completion=20, cached=0, reasoning=0):
    db.record(
        source="agent_turn", agent_id=agent_id, agent_name=agent_id,
        session_id="session", provider=provider, model=model,
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
        cached_tokens=cached, reasoning_tokens=reasoning,
        usage_details_available=bool(cached or reasoning),
    )


def test_existing_database_is_migrated_without_losing_historical_rows(tmp_path):
    path = tmp_path / "usage.db"
    import sqlite3
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, agent_id TEXT,
            agent_name TEXT, session_id TEXT, model TEXT, prompt_tokens INTEGER,
            completion_tokens INTEGER, total_tokens INTEGER, estimated INTEGER,
            duration_ms INTEGER, created_at TEXT)""")
        conn.execute("""INSERT INTO token_usage VALUES
            (1, 'legacy', 'agent', 'agent', 'session', 'model', 5, 3, 8, 0, 1, ?)""",
                     (datetime.now(timezone.utc).isoformat(),))
    db = UsageDB(str(path))
    assert db.overall_totals()["total_tokens"] == 8
    with db._connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(token_usage)")}
    assert {"provider", "cached_tokens", "reasoning_tokens", "usage_details_available"} <= columns


def test_snapshot_separates_provider_models_and_preserves_raw_totals(tmp_path):
    db = UsageDB(str(tmp_path / "usage.db"))
    _record(db, provider="openai", cached=30, reasoning=10)
    _record(db, provider="anthropic", cached=5, reasoning=0)
    snapshot = db.snapshot(bucket="day")
    assert snapshot["totals"]["total_tokens"] == 200
    assert snapshot["totals"]["prompt_tokens"] == 160
    assert snapshot["totals"]["cached_tokens"] == 35
    assert snapshot["totals"]["reasoning_tokens"] == 10
    assert {row["key"] for row in snapshot["models"]} == {
        "openai/shared-model", "anthropic/shared-model"}


def test_snapshot_bounds_agents_and_aggregates_other(tmp_path):
    db = UsageDB(str(tmp_path / "usage.db"))
    for index in range(35):
        _record(db, agent_id=f"agent-{index}", total=index + 1)
    snapshot = db.snapshot(agent_limit=30)
    assert snapshot["agent_total"] == 35
    assert len(snapshot["agents"]) == 31
    assert snapshot["agents"][-1]["agent_id"] == "Other"
    assert snapshot["agents"][-1]["total_tokens"] == sum(range(1, 6))


def test_snapshot_route_forces_daily_series_for_30_days(monkeypatch, tmp_path):
    db = UsageDB(str(tmp_path / "usage.db"))
    _record(db, provider="openai", cached=20)
    monkeypatch.setattr("plugins.token_monitor.routes.usage_db", db)
    app = Flask(__name__)
    app.register_blueprint(create_blueprint())
    response = app.test_client().get("/api/token-monitor/snapshot?range=30d&bucket=hour")
    assert response.status_code == 200
    body = response.get_json()
    assert body["bucket"] == "day"
    assert body["totals"]["billable_input_tokens"] == 60
