"""HTTP-surface tests for the consolidated Safety page (task #836).

Pins the navigation-consolidation contract:

* ``/system/safety`` renders the page shell with accessible tab semantics
  (General / HMADS / DMSS) and embeds the HMADS rules UI plus the DMSS
  dashboard,
* the legacy ``/system/decim-safety`` dashboard route still resolves via a
  redirect to the DMSS tab of the Safety page,
* the settings console links to Safety and no longer carries its own HMADS
  section (the old ``#hmads`` hash is migrated client-side).
"""

from app import app


def _client():
    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


# ---------------------------------------------------------------------------
# Safety page shell
# ---------------------------------------------------------------------------

def test_safety_page_renders_with_accessible_tabs():
    client = _client()
    resp = client.get("/system/safety")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # ARIA tab semantics
    assert 'role="tablist"' in html
    assert html.count('role="tab"') >= 3
    assert html.count('role="tabpanel"') >= 3

    # All three tabs and their panels are wired
    for tab in ("general", "hmads", "dmss"):
        assert f'data-tab="{tab}"' in html
        assert f'id="safety-panel-{tab}"' in html
        assert f'aria-controls="safety-panel-{tab}"' in html


def test_safety_page_embeds_hmads_and_dmss_surfaces():
    client = _client()
    html = client.get("/system/safety").get_data(as_text=True)

    # HMADS rules CRUD (from partials/hmads.html)
    assert "hmads-rules-body" in html
    assert "hmads-modal" in html

    # DMSS dashboard (migrated from the legacy Decim Safety page)
    assert "tbl-events-body" in html
    assert "kpi-status-card" in html
    assert "kpi-fallback" in html

    # Static assets are wired
    assert "css/safety.css" in html
    assert "js/safety.js" in html


def test_safety_general_tab_explains_policy_choice_and_fallback():
    client = _client()
    html = client.get("/system/safety").get_data(as_text=True)

    # The General tab must explain both policy options…
    assert "HMADS only" in html
    assert "DMSS enabled" in html
    # …and the deterministic HMADS fallback guarantee.
    assert "fallback" in html.lower()
    assert "request_timeout_ms" in html
    assert "minimum_confidence" in html


def test_safety_general_tab_offers_dmss_mode_selector():
    """The General tab exposes a shadow/enforce mode selector for DMSS."""
    client = _client()
    html = client.get("/system/safety").get_data(as_text=True)

    # The mode field and its accessible radiogroup…
    assert 'id="sg-mode-field"' in html
    assert 'id="sg-mode-group"' in html
    assert 'id="sg-mode-label"' in html
    assert "DMSS mode" in html
    # …with exactly the two DMSS modes (shadow / enforce).
    assert 'name="sg-mode" value="shadow" id="sg-radio-shadow"' in html
    assert 'name="sg-mode" value="enforce" id="sg-radio-enforce"' in html
    # …plus a dedicated error slot and a hint explaining both modes.
    assert 'id="sg-err-mode"' in html
    assert "Shadow" in html
    assert "Enforce" in html


# ---------------------------------------------------------------------------
# Legacy route preservation
# ---------------------------------------------------------------------------

def test_legacy_decim_safety_route_redirects_to_safety_dmss_tab():
    client = _client()
    resp = client.get("/system/decim-safety")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/system/safety#dmss"


def test_settings_console_links_to_safety_and_drops_hmads_section():
    client = _client()
    resp = client.get("/system")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # The nav rail now points at the consolidated Safety page…
    assert 'href="/system/safety"' in html
    # …and the old HMADS section is gone from the settings console.
    assert 'id="section-hmads"' not in html
    assert 'data-section="hmads"' not in html
    assert "partials/hmads.html" not in html


# ---------------------------------------------------------------------------
# Diagnostic tester sample payloads (task #843)
# ---------------------------------------------------------------------------

def test_dmss_tester_offers_ready_to_submit_samples():
    """The tester renders one-click sample payload chips (keyboard-reachable buttons)."""
    client = _client()
    html = client.get("/system/safety").get_data(as_text=True)

    assert 'id="dmss-tester-samples"' in html
    for sample in ("bash-safe", "bash-risky", "py-safe", "py-risky"):
        assert f'data-sample="{sample}"' in html
    assert html.count('class="sf-tester-sample"') >= 4
    # Bumped asset versions so the new wiring/CSS is not served stale.
    assert "js/safety.js?v=15" in html
    assert "css/safety.css?v=16" in html


def test_dmss_provider_endpoint_has_a_connectivity_test_button():
    """The provider endpoint row exposes a Test action plus an inline verdict slot."""
    client = _client()
    html = client.get("/system/safety").get_data(as_text=True)

    assert 'id="dc-endpoint-test"' in html
    assert 'id="dc-endpoint-test-result"' in html
    # The verdict is announced to assistive technology.
    assert 'role="status" aria-live="polite"' in html
    # The Test control sits directly after the endpoint input in the same row.
    assert html.index('id="dc-endpoint"') < html.index('id="dc-endpoint-test"')
    assert html.index('id="dc-endpoint-test"') - html.index('id="dc-endpoint"') < 900


def test_dmss_endpoint_is_plaintext_and_primary_buttons_are_dark_mode_readable():
    """The endpoint remains visually readable and Save buttons retain dark-mode contrast."""
    client = _client()
    html = client.get("/system/safety").get_data(as_text=True)

    assert 'id="dc-endpoint" class="sf-input" type="text"' in html
    assert 'id="dc-endpoint" class="sf-input" type="password"' not in html

    with open(f"{app.root_path}/static/css/safety.css", encoding="utf-8") as safety_css_file:
        safety_css = safety_css_file.read()
    assert "html.dark .sf-btn-primary {\n    background: #4338ca;" in safety_css
    assert "    color: #fff;\n}" in safety_css


def test_dmss_tester_samples_pass_payload_guardrails():
    """Every advertised sample is ready to submit as-is (mirrors TESTER_SAMPLES in static/js/safety.js)."""
    from backend.tools.lib.decim_safety import build_policy_packet

    samples = [
        ("bash", 'echo "hello world"'),
        ("bash", "sudo rm -rf ./build"),
        ("python", 'print("hello world")'),
        ("python", 'import subprocess\nsubprocess.run("rm -rf ./dist", shell=True)'),
    ]
    for tool_type, payload in samples:
        # No sensitive_payload / payload_too_large rejection for the default 12,000-char cap.
        packet = build_policy_packet(payload, tool_type, "sandboxed_docker", 12000)
        assert packet["requested_code"] == payload
        assert packet["tool_type"] == tool_type


# ---------------------------------------------------------------------------
# Activity detail modal (DMSS event drill-down)
# ---------------------------------------------------------------------------

def test_activity_modal_exposes_command_detection_and_decision_sections():
    """The modal renders every field an operator needs to audit one decision."""
    client = _client()
    html = client.get("/system/safety").get_data(as_text=True)

    # Accessible dialog shell defined in CSS (never a bare div over the overlay).
    assert 'id="dmss-activity-modal"' in html
    assert 'class="modal dm-modal"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html

    for section in ("dmss-sec-summary", "dmss-sec-command", "dmss-sec-hmads", "dmss-sec-dmss"):
        assert f'id="{section}"' in html

    for field in (
        "dmss-agent", "dmss-tool-type", "dmss-occurred-at", "dmss-final-level", "dmss-decided-by",
        "dmss-command-text", "dmss-command-copy", "dmss-command-empty",
        "dmss-rule-verdict", "dmss-rule-categories", "dmss-rule-score", "dmss-rule-time",
        "dmss-model-decision", "dmss-model-confidence", "dmss-model-latency", "dmss-model-id",
        "dmss-event-id", "dmss-mode", "dmss-policy-version", "dmss-provider",
    ):
        assert f'id="{field}"' in html, field

    # Comparison noise the operator does not need is gone.
    for removed in ("dmss-agreement", "dmss-correlation-id", "dmss-expires-at"):
        assert f'id="{removed}"' not in html, removed


def test_telemetry_table_answers_who_what_and_which_engine():
    html = _client().get("/system/safety").get_data(as_text=True)
    head = html[html.index('id="tbl-events"'):html.index('id="tbl-events-body"')]
    for column in ("When", "Agent", "Command", "HMADS", "DMSS", "Verdict"):
        assert f"<th>{column}</th>" in head, column
    for removed in ("Fingerprint", "Agreement", "Disposition", "Fallback"):
        assert removed not in head, removed
    for control in ("f-agent", "f-decided", "f-verdict"):
        assert f'id="{control}"' in html, control
    assert 'id="tbl-agreement"' not in html


def test_activity_modal_styles_exist_and_use_theme_tokens():
    """.dm-modal* must be defined, and must not hardcode light-only colors."""
    from pathlib import Path

    css = Path(__file__).resolve().parents[1] / "static" / "css" / "safety.css"
    safety_css = css.read_text()
    assert ".dm-modal .dm-modal-panel {" in safety_css
    assert ".dm-detail-grid {" in safety_css
    assert ".dm-command-text {" in safety_css
    assert "background: var(--bg-card);" in safety_css
    # The legacy broken selectors must be gone from the partial.
    client = _client()
    html = client.get("/system/safety").get_data(as_text=True)
    assert ".detail-container {" not in html
    assert "modal-detail-container" not in html


def test_dmss_enforce_behaviour_controls_are_exposed():
    """The recording policy and the HMADS floor must be operable from the UI."""
    html = _client().get("/system/safety").get_data(as_text=True)

    # Fieldset that hosts the two enforce-mode switches.
    assert "Enforce behaviour" in html
    assert 'id="dc-record"' in html
    assert 'id="dc-floor"' in html
    assert 'role="switch"' in html
    # Each switch keeps its own auto-save status slot.
    for slot in ("dc-status-record", "dc-status-floor"):
        assert f'id="{slot}"' in html, slot

    # The recording indicator that the redesign dropped is back on the
    # Provider KPI card.
    assert 'id="kpi-recording"' in html
