"""
Reply to an existing email. Supports Reply-All via the reply_all flag.
"""

from backend.tools._client import get_client


def execute(agent: dict, args: dict) -> dict:
    email_id = args.get("email_id", "").strip()
    body = args.get("body", "")

    missing = []
    if not email_id:
        missing.append("email_id")
    if not body:
        missing.append("body")
    if missing:
        return {
            "status": "error",
            "message": f"Missing required parameters: {', '.join(missing)}.",
        }

    try:
        client = get_client(agent, args)
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    payload = {
        "body": body,
    }

    if args.get("reply_all"):
        payload["reply_all"] = True

    result = client._request("POST", f"/api/v1/emails/{email_id}/reply", payload)
    return result
