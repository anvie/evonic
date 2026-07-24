"""
Fetch a single email by its ID. Returns full content including headers, body,
and attachments metadata.
"""

from backend.tools._client import get_client


def execute(agent: dict, args: dict) -> dict:
    email_id = args.get("email_id", "").strip()
    if not email_id:
        return {"status": "error", "message": "email_id is required."}

    try:
        client = get_client(agent, args)
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    result = client._request("GET", f"/api/v1/emails/{email_id}")
    return result
