"""
List emails with optional filters (mailbox, unread_only, search, limit).
"""

from backend.tools._client import get_client


def execute(agent: dict, args: dict) -> dict:
    try:
        client = get_client(agent, args)
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    params = {}
    if args.get("mailbox"):
        params["mailbox"] = args["mailbox"]
    if args.get("unread_only"):
        params["unread"] = "true"
    if args.get("search"):
        params["search"] = args["search"]

    limit = args.get("limit", 20)
    if not isinstance(limit, int) or limit < 1:
        limit = 1
    if limit > 100:
        limit = 100
    params["limit"] = str(limit)

    # Build query string
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    path = f"/api/v1/emails?{qs}" if qs else "/api/v1/emails"

    result = client._request("GET", path)
    return result
