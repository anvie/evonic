# Cloudflare Agentic Inbox Skill

## Overview

You have access to email management via the Cloudflare Agentic Inbox REST API. You can list mailboxes, read emails, send new emails, reply to existing threads, and delete emails — all through a clean, intent-based tool interface.

## Configuration

Before using any tool, the admin must configure two variables in the skill settings:

- **WORKER_URL** — Base URL of the deployed Cloudflare Agentic Inbox Worker (e.g. `https://evonic-agentic-inbox.anvie-2194.workers.dev`). No trailing slash.
- **ACCESS_TOKEN** — Cloudflare Access JWT token (copy the `CF_Authorization` cookie value after logging in via browser). This is sent as the `cf-access-jwt-assertion` HTTP header.

If either variable is missing, all tools will return an error telling you to ask the admin to configure the skill.

## Available Tools

| Tool | Purpose |
|---|---|
| `list_mailboxes` | List all configured mailboxes and their capabilities |
| `list_emails` | List emails with filters (mailbox, unread, search, limit) |
| `get_email` | Fetch full content of a single email by ID |
| `send_email` | Send a new email from a configured mailbox |
| `reply_email` | Reply to an existing email (supports Reply-All) |
| `delete_email` | Move an email to trash (soft delete) |

## Workflow

When asked to interact with email, follow this general flow:

1. **Identify the mailbox** — Use `list_mailboxes` to discover available addresses if you don't already know them.
2. **Find the email** — Use `list_emails` with appropriate filters (unread, search, mailbox) to locate relevant messages.
3. **Read before acting** — Always `get_email` to see full content before replying or deleting.
4. **Act** — Use `send_email`, `reply_email`, or `delete_email` as appropriate.

## Sending Email Checklist

Before calling `send_email`, verify:

- The `from_mailbox` exists (use `list_mailboxes` to confirm).
- The `to` address is valid and intended.
- The subject line is clear and accurate.
- The body is well-formatted and reviewed for errors.
- CC/BCC recipients are only added when explicitly requested.

## Replying to Email

- Always use `get_email` first to read the full thread context.
- Use `reply_all: true` only when the user explicitly asks for Reply-All, or when it is clear from context that all recipients should be included.
- Keep replies concise and professional.

## Rules

- **Never send without confirmation** — Always present the draft to the user before calling `send_email`, unless autopilot is ON or the user has given blanket approval.
- **Never delete without confirmation** — Always ask before moving an email to trash, unless the user explicitly requested deletion.
- **Quote only what you need** — When replying, don't regurgitate the entire original thread unless relevant.
- **Privacy first** — Do not expose email content to external systems. All API calls go directly to the configured Worker.
