from backend.dashboard_attention import STALE_SCHEDULE_SECONDS, build_attention

NOW = 1_800_000_000.0


def _titles(items):
    return [i["title"] for i in items]


def test_missing_workspace_is_flagged_for_enabled_agents_only():
    agents = [
        {"id": "a", "name": "Alpha", "workspace": "/nope", "enabled": 1},
        {"id": "b", "name": "Beta", "workspace": "/nope", "enabled": 0},          # disabled: not worth a nag
        {"id": "c", "name": "Gamma", "workspace": "/ok", "enabled": 1},
        {"id": "d", "name": "Delta", "workspace": "", "enabled": 1},              # empty = default workspace
    ]
    items = build_attention(agents, [], isdir=lambda p: p == "/ok")
    assert _titles(items) == ["Alpha: workspace not found"]
    assert items[0]["href"] == "/agents/a" and items[0]["level"] == "warn"


def test_remote_workplaces_are_not_checked_against_the_local_disk():
    agents = [{"id": "r", "name": "Remote", "workspace": "/srv/x", "workplace_id": "w1", "enabled": 1}]
    assert build_attention(agents, [], workplace_type=lambda _id: "remote", isdir=lambda p: False) == []
    assert build_attention(agents, [], workplace_type=lambda _id: "local", isdir=lambda p: False)


def test_overdue_schedules_use_a_grace_period_and_ignore_paused_ones():
    late = NOW - STALE_SCHEDULE_SECONDS - 60
    schedules = [
        {"name": "stuck", "enabled": 1, "next_run_at": late},
        {"name": "fine", "enabled": 1, "next_run_at": NOW - 60},                  # inside the grace period
        {"name": "future", "enabled": 1, "next_run_at": NOW + 3600},
        {"name": "paused", "enabled": 0, "next_run_at": late},
        {"name": "no-time", "enabled": 1, "next_run_at": None},
    ]
    items = build_attention([], schedules, now=NOW)
    assert _titles(items) == ["Schedule “stuck” is overdue"]
    assert items[0]["href"] == "/scheduler"


def test_iso_timestamps_are_understood():
    iso = "2027-01-15T08:00:00+00:00"
    from datetime import datetime
    now = datetime.fromisoformat(iso).timestamp() + 3 * 3600
    items = build_attention([], [{"name": "x", "enabled": 1, "next_run_at": iso}], now=now)
    assert items and "3 h" in items[0]["detail"]


def test_very_low_latest_evaluation_is_a_danger_and_sorted_first():
    agents = [{"id": "a", "name": "Alpha", "workspace": "/nope", "enabled": 1}]
    run = {"run_id": 6, "model_name": "m", "overall_score": 0.05, "total_points": 3.0, "max_points": 60.0}
    items = build_attention(agents, [], run, isdir=lambda p: False)
    assert [i["level"] for i in items] == ["danger", "warn"]
    assert items[0]["title"] == "Latest evaluation scored only 2" and items[0]["href"] == "/history/6"
    # a run from before points existed still raises the alert, without a number
    legacy = build_attention([], [], {"run_id": 7, "overall_score": 0.05})
    assert legacy[0]["title"] == "Latest evaluation scored low"
    assert build_attention([], [], {"run_id": 8, "total_points": 30.0, "max_points": 60.0}) == []
    assert build_attention([], [], {"run_id": 1, "overall_score": 0.9}) == []
    assert build_attention([], [], {"run_id": 1, "overall_score": None}) == []


def test_output_is_capped():
    agents = [{"id": str(i), "name": f"A{i}", "workspace": "/nope", "enabled": 1} for i in range(20)]
    assert len(build_attention(agents, [], isdir=lambda p: False)) == 6
