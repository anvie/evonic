"""
Send a new email from a configured mailbox. Supports CC and BCC recipients.
"""

from backend.tools._client import get_client


def execute(agent: dict, args: dict) -> dict:
    from_mailbox = args.get("from_mailbox", "").strip()
    to = args.get("to", "").strip()
    subject = args.get("subject", "").strip()
    body = args.get("body", "")

    missing = []
    if not from_mailbox:
        missing.append("from_mailbox")
    if not to:
        missing.append("to")
    if not subject:
        missing.append("subject")
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
        "from": from_mailbox,
        "to": to,
        "subject": subject,
        "body": body,
    }

    cc = args.get("cc", "").strip()
    if cc:
        payload["cc"] = cc

    bcc = args.get("bcc", "").strip()
    if bcc:
        payload["bcc"] = bcc

    result = client._request("POST", "/api/v1/emails", payload)
    return result
