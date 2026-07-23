"""
List all configured mailboxes and their capabilities.
"""

from backend.tools._client import get_client


def execute(agent: dict, args: dict) -> dict:
    try:
        client = get_client(agent, args)
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    result = client._request("GET", "/api/v1/mailboxes")
    return result
