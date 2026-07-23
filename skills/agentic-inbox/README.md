# Cloudflare Agentic Inbox — Evonic Skill

A lazy-loaded skill that gives Evonic agents email management capabilities via the [Cloudflare Agentic Inbox](https://github.com/cloudflare/agentic-inbox) REST API.

## Architecture

This is a **lazy skill** — agents must call `use_skill("agentic_inbox")` to load the tools and instructions into their context. Once loaded, 6 tools become available:

```
skills/agentic-inbox/
├── skill.json                  # Manifest (lazy_tools: true, variables)
├── SYSTEM.md                   # Agent instructions (injected on load)
├── tools.json                  # 6 OpenAI-format tool definitions
├── README.md                   # This file
└── backend/tools/
    ├── __init__.py
    ├── _client.py              # Shared HTTP helper (auth, error handling)
    ├── list_mailboxes.py       # List all configured mailboxes
    ├── list_emails.py          # List emails with filters
    ├── get_email.py            # Fetch a single email by ID
    ├── send_email.py           # Send a new email
    ├── reply_email.py          # Reply to an existing email
    └── delete_email.py         # Move an email to trash
```

## Tools

| Tool | Description |
|---|---|
| `list_mailboxes` | List all configured mailboxes |
| `list_emails` | List emails with filters (mailbox, unread, search, limit) |
| `get_email` | Fetch a single email by ID (full content) |
| `send_email` | Send a new email (to, from, subject, body, cc, bcc) |
| `reply_email` | Reply to an existing email (Reply / Reply-All) |
| `delete_email` | Move an email to trash (soft delete) |

## Configuration

Two variables must be set in the skill settings UI:

| Variable | Type | Description |
|---|---|---|
| `WORKER_URL` | string | Base URL of the deployed Agentic Inbox Worker (e.g. `https://inbox.evonic.dev`) |
| `ACCESS_TOKEN` | secret | Cloudflare Access JWT (from `CF_Authorization` cookie after browser login). Sent as `cf-access-jwt-assertion` header. |

## Authentication

The Agentic Inbox Worker uses **Cloudflare Access JWT** for authentication. The Worker expects the `cf-access-jwt-assertion` HTTP header. You have two options:

### Option A — Browser JWT (quick, token expires ~24h)

1. Set Worker secrets `POLICY_AUD` and `TEAM_DOMAIN` (from Cloudflare Access setup)
2. Visit your Worker URL in a browser, authenticate via Cloudflare Access
3. Open DevTools → Application → Cookies → copy `CF_Authorization` value
4. Set it as `ACCESS_TOKEN` in the skill config

### Option B — Service Token (recommended for production)

Modify the Worker's `app.ts` auth middleware to also accept `Authorization: Bearer <secret>`. This gives you a permanent token:

```ts
// After the existing JWT check in app.ts, add:
const bearer = c.req.header("Authorization")?.replace("Bearer ", "");
if (bearer && bearer === c.env.API_KEY) {
    return next();
}
```

Then set `API_KEY` as a Worker secret and add `Authorization: Bearer` support to the skill's `_client.py`.

## Agent Usage

```
→ use_skill("agentic_inbox")
→ list_mailboxes()
→ list_emails(unread_only=true, limit=5)
→ get_email(email_id="abc123")
→ reply_email(email_id="abc123", body="Got it, thanks!")
→ send_email(from_mailbox="robin@evonic.dev", to="alice@example.com", subject="Hello", body="...")
→ delete_email(email_id="xyz789")
```

## Prerequisites

- A deployed [Cloudflare Agentic Inbox](https://github.com/cloudflare/agentic-inbox) Worker.
- Cloudflare Access configured with a JWT token for API authentication.
- Cloudflare Email Routing and Email Service enabled on the target domain.
