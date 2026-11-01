"""Focused static coverage for the Decision Activity administration UI."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates/partials/settings/decision_models.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "static/js/settings-decision-models.js").read_text(encoding="utf-8")


def test_activity_ui_exposes_all_observability_filters_and_states():
    for value in ("source", "profile_key", "model_id", "result_class", "error_code", "fallback_used", "correlation_id", 'name="from"', 'name="to"'):
        assert value in TEMPLATE
    for value in ("Loading activity", "No matching activity", "Could not load Decision Activity", "Load more", "dark:", "focus:ring-2"):
        assert value in TEMPLATE or value in SCRIPT
    assert "cursor" in SCRIPT and "next_cursor" in SCRIPT


def test_activity_events_are_a_table_not_cards():
    assert '<tbody id="decision-activity-list"' in TEMPLATE and "<thead>" in TEMPLATE.split("decision-activity-list")[0].rsplit("<table", 1)[1]
    row = SCRIPT.split("activityRow(event) {", 1)[1].split("activityDate(value)", 1)[0]
    assert "<tr " in row and "<td " in row and "<button" not in row and "rounded-lg border" not in row


def test_activity_detail_shows_input_and_output_and_reproduces_from_the_recorded_input():
    detail = SCRIPT.split("renderActivityDetail(event) {", 1)[1].split("async rerunActivity", 1)[0]
    assert 'heading("Input")' in detail and 'heading("Output")' in detail
    assert "JSON.stringify(event.input_context, null, 2)" in detail and "Test in Playground" in detail
    rerun = SCRIPT.split("async rerunActivity(id)", 1)[1].split("async loadRecordInput", 1)[0]
    assert "JSON.stringify(event.input_context, null, 2)" in rerun and "outcome_summary" not in rerun   # the answer is never used as input
    assert "it has not run" in rerun and "playgroundStateSave()" in rerun                              # never runs by itself
    assert "event.source ===" not in rerun                                                                 # production events can be reproduced too


def test_recording_inputs_can_be_switched_off_from_the_activity_page():
    assert 'id="decision-activity-record-input"' in TEMPLATE
    assert "/api/admin/decision-activity/settings" in SCRIPT and "setRecordInput" in SCRIPT


def test_decision_profile_save_errors_use_global_toasts():
    save_method = SCRIPT.split("async save(event)", 1)[1].split("showError(message)", 1)[0]

    assert 'toast.show(message, "error")' in save_method
    assert 'toast.show("Could not save profile. Check the values and try again.", "error")' in save_method
    assert "if (!response.success) {\n                const message" in save_method
    assert "this.showError((response.error" not in save_method
    assert "this.showError(\"Could not save profile" not in save_method
