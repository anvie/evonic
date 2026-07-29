from __future__ import annotations


def classify(policy: dict, tool_name: str, result: dict) -> dict:
    if not isinstance(result, dict):
        return {"action": "ignore", "reason_code": "UNSTRUCTURED_RESULT"}
    status = str(result.get("status") or "").lower()
    if status in set(policy.get("success_statuses", [])):
        return {"action": "success", "reason_code": "WORKFLOW_SUCCEEDED"}
    if result.get("accepted") is False:
        code = str(result.get("reason_code") or "PHOTO_REJECTED").upper()
        return _count_if_allowed(policy, code, "photo_validation")
    outcome = result.get("workflow_outcome")
    if isinstance(outcome, dict):
        category = str(outcome.get("category") or "").lower()
        code = str(outcome.get("reason_code") or "UNSPECIFIED").upper()
        if category == "success":
            return {"action": "success", "reason_code": code}
        if category == "user_correctable_failure":
            return _count_if_allowed(policy, code, str(outcome.get("stage") or tool_name))
        return {"action": "ignore", "reason_code": code}
    if tool_name == "person_data_match" and (result.get("matched") is False or status in {"no_match", "ambiguous"}):
        code = "IDENTITY_AMBIGUOUS" if status == "ambiguous" else "IDENTITY_NO_MATCH"
        return _count_if_allowed(policy, code, "identity_match")
    if tool_name == "register" and status == "blocked":
        return _count_if_allowed(policy, "STAGED_DATA_INVALID", "submission")
    if tool_name == "registration_draft" and status == "invalid":
        return _count_if_allowed(policy, "REQUIRED_DATA_INVALID", "draft")
    return {"action": "ignore", "reason_code": "NON_COUNTABLE"}


def _count_if_allowed(policy: dict, code: str, stage: str) -> dict:
    allowed = set(policy.get("countable_reason_codes", []))
    return ({"action": "count", "reason_code": code, "stage": stage}
            if code in allowed else {"action": "ignore", "reason_code": code})
