import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _load_package():
    root = Path(__file__).parents[1]
    name = "workflow_guard_testpkg"
    spec = importlib.util.spec_from_file_location(name, root / "__init__.py", submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return name


PKG = _load_package()
Repository = __import__(f"{PKG}.repository", fromlist=["Repository"]).Repository
classify = __import__(f"{PKG}.classifier", fromlist=["classify"]).classify
WorkflowGuard = __import__(f"{PKG}.engine", fromlist=["WorkflowGuard"]).WorkflowGuard


def policy(threshold=25):
    return {
        "policy_id": "p", "agent_id": "a", "threshold": threshold,
        "countable_reason_codes": ["INVALID_IMAGE", "IDENTITY_NO_MATCH", "STAGED_DATA_INVALID"],
        "success_statuses": ["submitted"], "escalation_destination": "staff",
    }


def test_classifier_counts_only_allowlisted_user_failures():
    p = policy()
    assert classify(p, "validate_photo", {"accepted": False, "reason_code": "INVALID_IMAGE"})["action"] == "count"
    assert classify(p, "person_data_match", {"status": "no_match", "matched": False})["action"] == "count"
    assert classify(p, "register", {"status": "unknown", "error": "timeout"})["action"] == "ignore"
    assert classify(p, "cek_status", {"status": "error"})["action"] == "ignore"


def test_threshold_is_atomic_deduplicated_and_creates_one_report(tmp_path):
    repo, p = Repository(str(tmp_path / "policy.db")), policy(25)
    for i in range(24):
        result = repo.record_failure(p, "subject", "***1234", f"attempt-{i}", "photo", "INVALID_IMAGE")
        assert result["count"] == i + 1 and not result["locked"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _i: repo.record_failure(p, "subject", "***1234", "attempt-24", "photo", "INVALID_IMAGE"), range(8)))
    subject = repo.subject(p, "subject", "***1234", create=False)
    assert subject["status"] == "locked" and subject["failure_count"] == 25
    assert sum(not r["duplicate"] for r in results) == 1
    assert len(repo.list_outbox()) == 1
    payload = json.loads(next(iter(repo.pending_outbox()))["payload_json"])
    assert payload["failure_count"] == 25
    assert "\"subject\"" not in json.dumps(payload)


def test_reopen_starts_new_epoch_and_audits(tmp_path):
    repo, p = Repository(str(tmp_path / "policy.db")), policy(1)
    result = repo.record_failure(p, "subject", "***1234", "one", "photo", "INVALID_IMAGE")
    reopened = repo.reopen(result["subject_id"], "staff-id", "manual verification complete")
    subject = repo.subject(p, "subject", "***1234", create=False)
    assert reopened["epoch"] == 2
    assert subject["status"] == "open" and subject["failure_count"] == 0


def test_shadow_mode_records_without_lock(tmp_path):
    repo, p = Repository(str(tmp_path / "policy.db")), policy(1)
    result = repo.record_failure(p, "subject", "***1234", "one", "photo", "INVALID_IMAGE", shadow=True)
    assert result["count"] == 1 and not result["locked"]
    assert repo.subject(p, "subject", "***1234", create=False)["status"] == "open"
    assert repo.list_outbox() == []


def test_image_attachment_requires_configured_validator(tmp_path):
    guard = WorkflowGuard.__new__(WorkflowGuard)
    guard.policies = {
        "a": {
            **policy(),
            "image_attachment_required_tool": "validate_photo",
        }
    }
    guard.repo = Repository(str(tmp_path / "policy.db"))
    guard.config = {"ENFORCEMENT_ENABLED": True}
    guard.log = lambda *_args, **_kwargs: None
    guard.secret = b"x" * 32

    decision = guard.turn_gate({
        "agent_id": "a",
        "external_user_id": "6281234",
        "channel_id": "wa",
        "attachment_ids": ["318"],
        "attachment_mime_types": ["image/jpeg"],
    })

    assert decision["required_tool"] == "validate_photo"
    assert decision["suppress_intermediate"] is True
    assert "required_tool" not in guard.turn_gate({
        "agent_id": "a", "external_user_id": "6281234", "channel_id": "wa",
        "attachment_ids": ["319"], "attachment_mime_types": ["application/pdf"],
    })
