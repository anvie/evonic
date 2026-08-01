"""Unit tests for backend/agent_state.py"""

import pytest
from backend.agent_state import AgentState, GUARDED_TOOLS


class TestIsBlocked:
    def test_write_tools_blocked_in_plan_mode(self):
        ms = AgentState(mode="plan")
        for tool in GUARDED_TOOLS:
            assert ms.is_blocked(tool), f"{tool} should be blocked in plan mode"

    def test_write_tools_allowed_in_execute_mode(self):
        ms = AgentState(mode="execute")
        for tool in GUARDED_TOOLS:
            assert not ms.is_blocked(tool), f"{tool} should not be blocked in execute mode"

    def test_non_write_tools_never_blocked(self):
        for mode in ("plan", "execute"):
            ms = AgentState(mode=mode)
            for tool in ("read_file", "bash", "runpy", "calculator", "search_restaurants"):
                assert not ms.is_blocked(tool), f"{tool} should not be blocked in {mode} mode"


class TestSetMode:
    def test_transition_plan_to_execute(self):
        ms = AgentState(mode="plan", plan_file="plan/my-plan.md")
        result = ms.set_mode("execute")
        assert ms.mode == "execute"
        assert "error" not in result
        assert result["mode"] == "execute"

    def test_transition_execute_to_plan(self):
        ms = AgentState(mode="execute")
        result = ms.set_mode("plan")
        assert ms.mode == "plan"
        assert result["mode"] == "plan"

    def test_invalid_mode_returns_error(self):
        ms = AgentState(mode="plan")
        result = ms.set_mode("review")
        assert "error" in result
        assert ms.mode == "plan"  # unchanged

    def test_reason_included_in_result(self):
        ms = AgentState(plan_file="plan/my-plan.md")
        result = ms.set_mode("execute", reason="user approved")
        assert "user approved" in result["result"]

    def test_execute_blocked_without_plan_file(self):
        ms = AgentState(mode="plan")
        result = ms.set_mode("execute")
        assert "error" in result
        assert ms.mode == "plan"  # unchanged

    def test_explicit_user_bypass_allows_execute_without_plan_file(self):
        ms = AgentState(mode="plan")
        result = ms.set_mode("execute", bypass_plan_requirement=True)
        assert "error" not in result
        assert ms.mode == "execute"

    def test_execute_allowed_with_plan_file(self):
        ms = AgentState(mode="plan", plan_file="plan/test.md")
        result = ms.set_mode("execute")
        assert "error" not in result
        assert ms.mode == "execute"


class TestSetPlanFile:
    def test_set_plan_file(self):
        ms = AgentState()
        result = ms.set_plan_file("plan/my-plan.md")
        assert ms.plan_file == "plan/my-plan.md"
        assert "error" not in result
        assert result["plan_file"] == "plan/my-plan.md"

    def test_empty_path_returns_error(self):
        ms = AgentState()
        result = ms.set_plan_file("")
        assert "error" in result
        assert ms.plan_file is None


class TestUpdateTasks:
    def test_set_replaces_task_list(self):
        ms = AgentState()
        result = ms.update_tasks("set", tasks=["Task A", "Task B", "Task C"])
        assert len(ms.tasks) == 3
        assert ms.tasks[0]["text"] == "Task A"
        assert ms.tasks[0]["status"] == "pending"
        assert "error" not in result

    def test_set_discards_completed_and_in_progress_tasks(self):
        ms = AgentState()
        ms.update_tasks("set", tasks=["Completed", "Active", "Pending"])
        ms.update_tasks("done", task_id=1)
        ms.update_tasks("in_progress", task_id=2)

        result = ms.update_tasks("set", tasks=["New A", "New B"])

        assert result["result"] == "Task list replaced with 2 tasks."
        assert result["tasks"] == [
            {"id": 1, "text": "New A", "status": "pending"},
            {"id": 2, "text": "New B", "status": "pending"},
        ]
        assert ms.tasks == result["tasks"]

    def test_set_resets_ids_from_1(self):
        ms = AgentState()
        ms.update_tasks("add", text="First")
        ms.update_tasks("add", text="Second")
        ms.update_tasks("set", tasks=["New A", "New B"])
        assert ms.tasks[0]["id"] == 1
        assert ms.tasks[1]["id"] == 2
        ms.update_tasks("add", text="New C")
        assert ms.tasks[2]["id"] == 3

    def test_add_appends_task(self):
        ms = AgentState()
        ms.update_tasks("add", text="Write tests")
        assert len(ms.tasks) == 1
        assert ms.tasks[0]["text"] == "Write tests"
        assert ms.tasks[0]["status"] == "pending"

    def test_add_auto_increments_id(self):
        ms = AgentState()
        ms.update_tasks("add", text="First")
        ms.update_tasks("add", text="Second")
        assert ms.tasks[0]["id"] == 1
        assert ms.tasks[1]["id"] == 2

    def test_done_marks_task(self):
        ms = AgentState()
        ms.update_tasks("set", tasks=["Step 1", "Step 2"])
        result = ms.update_tasks("done", task_id=1)
        assert ms.tasks[0]["status"] == "done"
        assert "error" not in result

    def test_in_progress_marks_task(self):
        ms = AgentState()
        ms.update_tasks("set", tasks=["Step 1"])
        ms.update_tasks("in_progress", task_id=1)
        assert ms.tasks[0]["status"] == "in_progress"

    def test_in_progress_replaces_the_previous_active_task(self):
        ms = AgentState()
        ms.update_tasks("set", tasks=["Step 1", "Step 2", "Step 3"])
        ms.update_tasks("done", task_id=3)

        ms.update_tasks("in_progress", task_id=1)
        result = ms.update_tasks("in_progress", task_id=2)

        assert result["tasks"] == [
            {"id": 1, "text": "Step 1", "status": "pending"},
            {"id": 2, "text": "Step 2", "status": "in_progress"},
            {"id": 3, "text": "Step 3", "status": "done"},
        ]

    @pytest.mark.parametrize("mode", ["plan", "execute"])
    def test_task_transitions_remain_available_in_both_modes(self, mode):
        ms = AgentState(mode=mode)
        ms.update_tasks("set", tasks=["First"])
        added = ms.update_tasks("add", text="Second")
        ms.update_tasks("in_progress", task_id=added["task_id"])
        ms.update_tasks("done", task_id=added["task_id"])
        ms.update_tasks("remove", task_id=1)

        assert ms.tasks == [{"id": 2, "text": "Second", "status": "done"}]
        assert ms.mode == mode

    def test_remove_deletes_task(self):
        ms = AgentState()
        ms.update_tasks("set", tasks=["Keep", "Delete me"])
        ms.update_tasks("remove", task_id=2)
        assert len(ms.tasks) == 1
        assert ms.tasks[0]["text"] == "Keep"

    def test_done_nonexistent_task_returns_error(self):
        ms = AgentState()
        result = ms.update_tasks("done", task_id=99)
        assert "error" in result

    def test_add_without_text_returns_error(self):
        ms = AgentState()
        result = ms.update_tasks("add")
        assert "error" in result

    def test_set_without_tasks_returns_error(self):
        ms = AgentState()
        result = ms.update_tasks("set")
        assert "error" in result

    def test_unknown_action_returns_error(self):
        ms = AgentState()
        result = ms.update_tasks("explode")
        assert "error" in result

    def test_replace_preserves_id_and_status(self):
        ms = AgentState(tasks=[{
            "id": 7, "text": "Original", "status": "done",
        }])

        result = ms.update_tasks("replace", task_id=7, text="Updated")

        assert "error" not in result
        assert ms.tasks == [{"id": 7, "text": "Updated", "status": "done"}]

    def test_set_preserves_valid_structured_ids_and_statuses(self):
        ms = AgentState()

        ms.update_tasks("set", tasks=[
            {"id": 9, "text": "Done step", "status": "done"},
            {"id": 4, "text": "Pending step", "status": "pending"},
        ])

        assert [(task["id"], task["status"]) for task in ms.tasks] == [(9, "done"), (4, "pending")]


class TestAutomaticTaskLifecycle:
    def test_auto_activate_selects_first_pending_task(self):
        ms = AgentState(mode="execute")
        ms.update_tasks("set", tasks=["First", "Second"])

        result = ms.auto_activate(now=100.0)

        assert result["transitioned"] is True
        assert ms.tasks[0]["status"] == "in_progress"
        assert ms.tasks[0]["in_progress_since"] == 100.0

    def test_auto_activate_does_not_replace_existing_active_task(self):
        ms = AgentState(tasks=[{"id": 1, "text": "First", "status": "in_progress"},
                               {"id": 2, "text": "Second", "status": "pending"}])

        result = ms.auto_activate(now=100.0)

        assert result["transitioned"] is False
        assert result["task_id"] == 1
        assert ms.tasks[1]["status"] == "pending"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"tool_errors": True, "mutated": True},
            {"stopped": True, "mutated": True},
            {"final_text": "Please confirm before I continue.", "mutated": True},
            {"mutated": False},
        ],
    )
    def test_completion_eligibility_rejects_unsafe_completion(self, kwargs):
        ms = AgentState(tasks=[{"id": 1, "text": "Work", "status": "in_progress"}])

        assert ms.completion_eligible(**kwargs)["eligible"] is False

    def test_completion_eligibility_accepts_successful_mutating_turn(self):
        ms = AgentState(tasks=[{"id": 1, "text": "Work", "status": "in_progress"}])

        result = ms.completion_eligible(mutated=True, final_text="Implemented and tested.")

        assert result == {"eligible": True, "task_id": 1, "reason": None}

    def test_completion_eligibility_rejects_multiple_active_tasks(self):
        # Direct assignment bypasses the single-active normalizer on purpose:
        # completion_eligible is a pure query and must never fire when more
        # than one task claims the active slot.
        ms = AgentState()
        ms.tasks = [
            {"id": 1, "text": "One", "status": "in_progress"},
            {"id": 2, "text": "Two", "status": "in_progress"},
        ]

        result = ms.completion_eligible(mutated=True)

        assert result["eligible"] is False
        assert result["task_id"] is None

    def test_reconcile_tasks_reports_stale_tasks_without_mutating_state(self):
        ms = AgentState(tasks=[{"id": 1, "text": "Work", "status": "in_progress",
                                "in_progress_since": 10.0}])

        stale = ms.reconcile_tasks(now=400.0, stale_after=300.0)

        assert stale == [{"id": 1, "text": "Work", "age": 390.0}]
        assert ms.tasks[0]["status"] == "in_progress"


class TestResolveStaleTasks:
    def test_legacy_active_task_without_timestamp_is_demoted(self):
        # Pre-#742 states stored plain strings: no in_progress_since ever existed.
        ms = AgentState(tasks=[{"id": 1, "text": "Old active", "status": "in_progress"}])

        resolved = ms.resolve_stale_tasks(now=1000.0)

        assert resolved == [{
            "id": 1, "text": "Old active", "age": None,
            "action": "demote", "reason": "legacy",
        }]
        assert ms.tasks[0]["status"] == "pending"
        assert "in_progress_since" not in ms.tasks[0]

    def test_recent_managed_active_task_is_kept(self):
        ms = AgentState(tasks=[{
            "id": 1, "text": "Active work", "status": "in_progress",
            "in_progress_since": 500.0,
        }])

        resolved = ms.resolve_stale_tasks(now=1000.0, stale_after=3600.0)

        assert resolved == []
        assert ms.tasks[0]["status"] == "in_progress"
        assert ms.tasks[0]["in_progress_since"] == 500.0

    def test_very_old_managed_active_task_is_demoted(self):
        ms = AgentState(tasks=[{
            "id": 3, "text": "Abandoned", "status": "in_progress",
            "in_progress_since": 100.0,
        }])

        resolved = ms.resolve_stale_tasks(now=1000.0, stale_after=600.0)

        assert resolved == [{
            "id": 3, "text": "Abandoned", "age": 900.0,
            "action": "demote", "reason": "stale",
        }]
        assert ms.tasks[0]["status"] == "pending"
        assert "in_progress_since" not in ms.tasks[0]

    def test_pending_and_done_tasks_are_never_touched(self):
        ms = AgentState(tasks=[
            {"id": 1, "text": "Pending", "status": "pending"},
            {"id": 2, "text": "Done", "status": "done"},
        ])

        resolved = ms.resolve_stale_tasks(now=1000.0, stale_after=0.0)

        assert resolved == []
        assert ms.tasks[0]["status"] == "pending"
        assert ms.tasks[1]["status"] == "done"

    def test_default_threshold_demotes_legacy_active_task(self):
        # Real-world default: a pre-lifecycle active task (no timestamp) is
        # demoted to pending on session wake.
        ms = AgentState(tasks=[{"id": 1, "text": "Legacy active",
                                "status": "in_progress"}])

        resolved = ms.resolve_stale_tasks()

        assert [r["id"] for r in resolved] == [1]
        assert ms.tasks[0]["status"] == "pending"

    def test_default_threshold_keeps_fresh_managed_active_task(self):
        # A managed active task stamped moments ago is younger than the 6h
        # default threshold and must survive reconciliation untouched.
        ms = AgentState(tasks=[{
            "id": 1, "text": "Fresh active", "status": "in_progress",
            "in_progress_since": __import__("time").time(),
        }])

        resolved = ms.resolve_stale_tasks()

        assert resolved == []
        assert ms.tasks[0]["status"] == "in_progress"


class TestLosslessTaskUpdates:
    def test_structured_set_preserves_ids_statuses_and_timestamp(self):
        ms = AgentState()
        result = ms.update_tasks(
            "set",
            tasks=[
                {"id": 4, "text": "Done work", "status": "done"},
                {"id": 9, "text": "Active work", "status": "in_progress", "in_progress_since": 12.0},
            ],
        )

        assert result["tasks"] == [
            {"id": 4, "text": "Done work", "status": "done"},
            {"id": 9, "text": "Active work", "status": "in_progress"},
        ]
        assert ms.tasks[1]["in_progress_since"] == 12.0
        assert ms._next_task_id == 10

    def test_replace_preserves_task_id_and_status(self):
        ms = AgentState(tasks=[{"id": 7, "text": "Old", "status": "done"}])

        result = ms.update_tasks("replace", task_id=7, text="New")

        assert result["tasks"] == [{"id": 7, "text": "New", "status": "done"}]


class TestRender:
    def test_plan_mode_shows_blocked_note(self):
        ms = AgentState(mode="plan")
        rendered = ms.render()
        assert "blocked" in rendered.lower()
        assert "plan" in rendered

    def test_execute_mode_shows_allowed_note(self):
        ms = AgentState(mode="execute")
        rendered = ms.render()
        assert "allowed" in rendered.lower()

    def test_tasks_rendered_with_checkboxes(self):
        ms = AgentState()
        ms.update_tasks("set", tasks=["Read file", "Write fix"])
        ms.update_tasks("done", task_id=1)
        rendered = ms.render()
        assert "[x]" in rendered
        assert "[ ]" in rendered
        assert "Read file" in rendered
        assert "Write fix" in rendered

    def test_in_progress_task_shows_tilde(self):
        ms = AgentState()
        ms.update_tasks("add", text="Working on it")
        ms.update_tasks("in_progress", task_id=1)
        rendered = ms.render()
        assert "[~]" in rendered

    def test_no_tasks_shows_hint(self):
        ms = AgentState()
        rendered = ms.render()
        assert "No tasks" in rendered or "update_tasks" in rendered


class TestSerializeDeserialize:
    def test_roundtrip_empty(self):
        ms = AgentState()
        restored = AgentState.deserialize(ms.serialize())
        assert restored.mode == ms.mode
        assert restored.tasks == ms.tasks
        assert restored.plan_file is None

    def test_roundtrip_with_tasks_and_mode(self):
        ms = AgentState(mode="execute")
        ms.update_tasks("set", tasks=["A", "B", "C"])
        ms.update_tasks("done", task_id=2)
        restored = AgentState.deserialize(ms.serialize())
        assert restored.mode == "execute"
        assert len(restored.tasks) == 3
        assert restored.tasks[1]["status"] == "done"

    def test_roundtrip_preserves_single_active_task_after_serial_updates(self):
        ms = AgentState(mode="execute")
        ms.update_tasks("set", tasks=["A", "B", "C"])
        for task_id in (1, 2, 3):
            ms.update_tasks("in_progress", task_id=task_id)

        restored = AgentState.deserialize(ms.serialize())

        assert [task["status"] for task in restored.tasks] == [
            "pending", "pending", "in_progress",
        ]

    def test_transition_repairs_legacy_state_with_multiple_active_tasks(self):
        legacy = AgentState(tasks=[
            {"id": 1, "text": "A", "status": "in_progress"},
            {"id": 2, "text": "B", "status": "done"},
            {"id": 3, "text": "C", "status": "in_progress"},
        ], next_task_id=4)

        legacy.update_tasks("in_progress", task_id=3)

        assert [task["status"] for task in legacy.tasks] == [
            "pending", "done", "in_progress",
        ]

    def test_next_task_id_preserved(self):
        ms = AgentState()
        ms.update_tasks("add", text="First")
        ms.update_tasks("add", text="Second")
        restored = AgentState.deserialize(ms.serialize())
        restored.update_tasks("add", text="Third")
        assert restored.tasks[2]["id"] == 3  # not 1

    def test_invalid_json_returns_fresh_state(self):
        restored = AgentState.deserialize("not valid json{{{")
        assert restored.mode == "plan"
        assert restored.tasks == []

    def test_plan_file_roundtrip(self):
        ms = AgentState(plan_file="plan/runpy-heuristic.md")
        restored = AgentState.deserialize(ms.serialize())
        assert restored.plan_file == "plan/runpy-heuristic.md"

    def test_plan_file_none_roundtrip(self):
        ms = AgentState()
        restored = AgentState.deserialize(ms.serialize())
        assert restored.plan_file is None

    def test_states_roundtrip(self):
        ms = AgentState()
        ms.set_state('kanban', 'pending', data={'task_id': '42'},
                     allowed_tools=['kanban_update_status', 'state'])
        restored = AgentState.deserialize(ms.serialize())
        slot = restored.get_state('kanban')
        assert slot is not None
        assert slot['state'] == 'pending'
        assert slot['data'] == {'task_id': '42'}
        assert slot['allowed_tools'] == ['kanban_update_status', 'state']

    def test_empty_states_roundtrip(self):
        ms = AgentState()
        restored = AgentState.deserialize(ms.serialize())
        assert restored.states == {}


class TestStates:
    def test_set_and_get_state(self):
        ms = AgentState()
        ms.set_state('kanban', 'pending', data={'task_id': '1'})
        slot = ms.get_state('kanban')
        assert slot is not None
        assert slot['state'] == 'pending'
        assert slot['data'] == {'task_id': '1'}

    def test_get_state_returns_none_for_missing_namespace(self):
        ms = AgentState()
        assert ms.get_state('kanban') is None

    def test_clear_state_removes_slot(self):
        ms = AgentState()
        ms.set_state('kanban', 'active')
        ms.clear_state('kanban')
        assert ms.get_state('kanban') is None

    def test_clear_state_noop_for_missing(self):
        ms = AgentState()
        ms.clear_state('nonexistent')  # should not raise

    def test_multiple_namespaces_independent(self):
        ms = AgentState()
        ms.set_state('kanban', 'active')
        ms.set_state('deploy', 'locked')
        assert ms.get_state('kanban')['state'] == 'active'
        assert ms.get_state('deploy')['state'] == 'locked'
        ms.clear_state('kanban')
        assert ms.get_state('kanban') is None
        assert ms.get_state('deploy') is not None

    def test_overwrite_state_slot(self):
        ms = AgentState()
        ms.set_state('kanban', 'pending')
        ms.set_state('kanban', 'active')
        assert ms.get_state('kanban')['state'] == 'active'

    # ── is_blocked_by_state ───────────────────────────────────────────────────

    def test_blocked_tool_not_in_allowed_tools(self):
        ms = AgentState()
        ms.set_state('kanban', 'pending', allowed_tools=['kanban_update_status', 'state'])
        result = ms.is_blocked_by_state('write_file')
        assert result is not None
        assert isinstance(result, str)
        assert 'write_file' in result

    def test_allowed_tool_passes(self):
        ms = AgentState()
        ms.set_state('kanban', 'pending', allowed_tools=['kanban_update_status', 'state'])
        assert ms.is_blocked_by_state('kanban_update_status') is None

    def test_blocked_tools_list_blocks_listed_tool(self):
        ms = AgentState()
        ms.set_state('kanban', 'active', blocked_tools=['rm_rf', 'drop_db'])
        result = ms.is_blocked_by_state('rm_rf')
        assert result is not None
        assert 'rm_rf' in result

    def test_blocked_tools_list_allows_unlisted_tool(self):
        ms = AgentState()
        ms.set_state('kanban', 'active', blocked_tools=['rm_rf'])
        assert ms.is_blocked_by_state('write_file') is None

    def test_no_states_never_blocks(self):
        ms = AgentState()
        assert ms.is_blocked_by_state('write_file') is None

    def test_error_message_includes_namespace_and_state(self):
        ms = AgentState()
        ms.set_state('kanban', 'pending', allowed_tools=['state'])
        msg = ms.is_blocked_by_state('bash')
        assert 'kanban' in msg
        assert 'pending' in msg

    # ── is_blocked integration ────────────────────────────────────────────────

    def test_is_blocked_returns_true_for_mode_block(self):
        ms = AgentState(mode='plan')
        result = ms.is_blocked('write_file')
        assert result is True

    def test_is_blocked_returns_string_for_state_block(self):
        ms = AgentState(mode='execute')
        ms.set_state('kanban', 'pending', allowed_tools=['state'])
        result = ms.is_blocked('write_file')
        assert isinstance(result, str)
        assert 'write_file' in result

    def test_is_blocked_returns_false_when_nothing_blocks(self):
        ms = AgentState(mode='execute')
        result = ms.is_blocked('write_file')
        assert not result

    def test_mode_block_takes_precedence_over_state(self):
        ms = AgentState(mode='plan')
        ms.set_state('kanban', 'pending', allowed_tools=['write_file'])
        # In plan mode, write_file is blocked by mode (returns True before state check)
        result = ms.is_blocked('write_file')
        assert result is True

    # ── render ────────────────────────────────────────────────────────────────

    def test_render_shows_active_states_when_set(self):
        ms = AgentState(mode='execute')
        ms.set_state('kanban', 'pending', allowed_tools=['kanban_update_status'])
        rendered = ms.render()
        assert 'Active States' in rendered
        assert 'kanban' in rendered
        assert 'pending' in rendered

    def test_render_no_states_section_when_empty(self):
        ms = AgentState(mode='execute')
        rendered = ms.render()
        assert 'Active States' not in rendered

    def test_render_shows_allowed_tools_when_set(self):
        ms = AgentState(mode='execute')
        ms.set_state('kanban', 'pending', allowed_tools=['kanban_update_status', 'state'])
        rendered = ms.render()
        assert 'kanban_update_status' in rendered

    def test_render_shows_blocked_tools_when_no_allowed(self):
        ms = AgentState(mode='execute')
        ms.set_state('kanban', 'active', blocked_tools=['drop_db'])
        rendered = ms.render()
        assert 'drop_db' in rendered
