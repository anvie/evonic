"""
Session Recap Extractor — parses native session summaries for actionable items.

Hooks into the `summary_updated` event, which fires whenever the built-in
conversation summarizer creates or updates a session summary. This plugin
parses that summary to extract items that need human attention:

- Payment verification requests
- User needs human agent (AI can't handle)
- Issues requiring human intervention
- Unresolved complaints or escalations

Also extracts user name, phone number, and booking/reservation details
from the summary and recent messages to enrich notifications.

Then notifies admins via webhook and/or channel message.
"""
from typing import Optional

import re


# Track previously sent actionable types per session to avoid duplicate notifications
# Key: session_id, Value: set of action_type strings already dispatched
_sent_actionables: dict = {}

# Keywords that signal actionable items in the summary
ACTIONABLE_PATTERNS = [
    # Payment / transaction
    (r'(?i)(payment|pembayaran|transfer|transaksi|bayar|bukti\s*(?:transfer|bayar))',
     'payment_verification', 'Payment verification may be needed'),
    # Human escalation
    (r'(?i)(human|manusia|operator|cs\b|customer\s*service|staff|admin|escalat)',
     'human_escalation', 'User requested human agent or escalation'),
    # Cannot handle / unable
    (r'(?i)(cannot|can\'t|tidak\s*bisa|tidak\s*mampu|unable|gagal|fail|error)',
     'agent_limitation', 'Agent may be unable to handle request'),
    # Complaint / dissatisfaction
    (r'(?i)(complaint|keluhan|komplain|kecewa|marah|tidak\s*puas|dissatisf)',
     'complaint', 'User complaint or dissatisfaction detected'),
    # Refund / cancellation
    (r'(?i)(refund|pengembalian|batal|cancel|pembatalan)',
     'refund_cancellation', 'Refund or cancellation request'),
]

# Labeled-field patterns for the structured summary format
# Matches "Full Name: Siwa WAWA" or "Phone Number: 084787654321"
_FIELD_RE = re.compile(
    r'(?i)^\s*\*?\s*(?P<key>full\s*name|phone\s*number|nama\s*lengkap|nomor\s*(?:hp|telepon|wa|whatsapp))\s*:\s*(?P<value>.+)$',
    re.MULTILINE,
)

# Fallback phone pattern for when the phone isn't in a labeled field
_PHONE_RE = re.compile(
    r'(?<!\d)(\+?62[\s\-]?\d{2,4}[\s\-]?\d{3,4}[\s\-]?\d{3,4}|0\d{2,3}[\s\-]?\d{3,4}[\s\-]?\d{3,5})(?!\d)'
)

# Fallback name patterns (natural language, Indonesian + English)
_NAME_FALLBACK = [
    re.compile(r'(?i)(?:CP|contact\s*person)\s*:\s*([A-Za-z][A-Za-z\s]{1,40}?)(?=[,.\n]|$)'),
    re.compile(r'(?i)(?:atas\s*nama|nama\s*saya|my\s*name\s*is)\s*([A-Za-z][A-Za-z\s]{1,40}?)(?=[,.\n]|$)'),
]


def on_summary_updated(event, sdk):
    """Analyze the native session summary for actionable items."""
    config = sdk.config
    min_messages = int(config.get('MIN_MESSAGES', 6))
    webhook_url = config.get('WEBHOOK_URL', '').strip()
    notify_agent_id = config.get('NOTIFY_AGENT_ID', '').strip()
    notify_channel_id = config.get('NOTIFY_CHANNEL_ID', '').strip()
    notify_user_id = config.get('NOTIFY_USER_ID', '').strip()

    # Skip if no notification targets configured
    has_webhook = bool(webhook_url)
    has_channel = bool(notify_agent_id and notify_channel_id and notify_user_id)
    if not has_webhook and not has_channel:
        sdk.log("Skipped: no notification targets configured "
                f"(webhook={bool(webhook_url)}, agent_id={bool(notify_agent_id)}, "
                f"channel_id={bool(notify_channel_id)}, user_id={bool(notify_user_id)})",
                level='warn')
        return

    # Skip short sessions
    message_count = event.get('message_count', 0)
    if message_count < min_messages:
        sdk.log(f"Skipped: session too short ({message_count}/{min_messages} messages)")
        return

    summary = event.get('summary', '')
    if not summary:
        sdk.log("Skipped: empty summary", level='warn')
        return

    # Extract actionable items from the summary
    actionables = _extract_actionables(summary)
    if not actionables:
        sdk.log("No actionable items found in summary")
        return

    session_id = event.get('session_id', '')

    # Deduplicate: only keep actionable types not yet sent for this session
    previously_sent = _sent_actionables.get(session_id, set())
    new_actionables = [a for a in actionables if a['type'] not in previously_sent]
    if not new_actionables:
        sdk.log("All actionable items already sent for this session, skipping")
        return
    actionables = new_actionables
    agent_id = event.get('agent_id', '')
    agent_name = event.get('agent_name', '')

    # Fetch session for external_user_id
    session = sdk.get_session(session_id)
    external_user_id = session.get('external_user_id', '') if session else ''

    # Extract user info and booking from summary + tail messages
    tail_messages = event.get('tail_messages', [])
    user_info = _extract_user_info(summary, tail_messages)
    booking_info = _extract_booking_info(summary, tail_messages)

    sdk.log(f"User info extracted: name={user_info['name']!r}, phone={user_info['phone']!r}")
    if booking_info:
        sdk.log(f"Booking info: {booking_info[:80]}")

    # Build payload
    payload = {
        'agent_id': agent_id,
        'agent_name': agent_name,
        'session_id': session_id,
        'external_user_id': external_user_id,
        'user_name': user_info['name'],
        'user_phone': user_info['phone'],
        'booking_info': booking_info,
        'message_count': message_count,
        'summary': summary,
        'actionables': actionables,
    }

    sdk.log(f"Found {len(actionables)} actionable(s): "
            + ', '.join(a['type'] for a in actionables))

    # Send to webhook
    if webhook_url:
        result = sdk.http_request('POST', webhook_url, json=payload, timeout=15)
        if result.get('ok'):
            sdk.log(f"Webhook POST success: {result.get('status_code')}")
        else:
            sdk.log(f"Webhook POST failed: {result.get('error', result.get('status_code', '?'))}",
                    level='error')

    # Send channel notification to admin
    if notify_agent_id and notify_channel_id and notify_user_id:
        message = _format_notification(
            agent_name, session_id, external_user_id,
            user_info, booking_info, actionables
        )
        result = sdk.send_message(notify_agent_id, notify_user_id, notify_channel_id, message)
        if result.get('success'):
            sdk.log(f"Notification sent to user {notify_user_id} via agent {notify_agent_id}")
        else:
            sdk.log(f"Notification send failed: {result}", level='error')

    # Record sent actionable types so we don't re-send them
    if session_id not in _sent_actionables:
        _sent_actionables[session_id] = set()
    _sent_actionables[session_id].update(a['type'] for a in actionables)


def _extract_actionables(summary: str) -> list:
    """Scan summary text for actionable patterns. Returns list of dicts."""
    found = []
    seen_types = set()

    for pattern, action_type, description in ACTIONABLE_PATTERNS:
        if action_type in seen_types:
            continue
        matches = re.findall(pattern, summary)
        if matches:
            seen_types.add(action_type)
            found.append({
                'type': action_type,
                'description': description,
                'matched_terms': list(set(m if isinstance(m, str) else m[0] for m in matches))[:3],
            })

    return found


def _extract_user_info(summary: str, tail_messages: list) -> dict:
    """Extract user name and phone from summary + recent messages.

    Prefers labeled fields in the structured summary format:
        Full Name: Siwa WAWA
        Phone Number: 084787654321
    Falls back to CP: lines and natural-language patterns.
    """
    texts = [summary]
    for msg in tail_messages:
        if msg.get('role') == 'user':
            texts.append(msg.get('content', ''))
    combined = '\n'.join(texts)

    name = None
    phone = None

    # Primary: labeled field scan
    for m in _FIELD_RE.finditer(combined):
        key = m.group('key').lower().replace(' ', '')
        value = m.group('value').strip().rstrip('.,')
        if not value:
            continue
        if 'fullname' in key or 'namalengkap' in key:
            if name is None:
                name = value
        elif any(k in key for k in ('phone', 'telepon', 'hp', 'wa', 'whatsapp', 'nomor')):
            if phone is None:
                phone = re.sub(r'[\s\-]', '', value)

    # Fallback: name from CP: or natural-language patterns
    if name is None:
        for pattern in _NAME_FALLBACK:
            m = pattern.search(combined)
            if m:
                candidate = m.group(1).strip().rstrip('.,')
                if len(candidate) >= 2:
                    name = candidate
                    break

    # Fallback: phone from any numeric pattern
    if phone is None:
        m = _PHONE_RE.search(combined)
        if m:
            phone = re.sub(r'[\s\-]', '', m.group(1))

    return {'name': name, 'phone': phone}


def _extract_booking_info(summary: str, tail_messages: list) -> Optional[str]:
    """Extract booking details from the structured summary Bookings section."""
    # Try to pull out the Bookings: block from the structured summary
    bookings_match = re.search(
        r'(?i)\*?\s*Bookings?\s*:\s*\n((?:[ \t]*\*[^\n]*\n?)+)',
        summary,
    )
    if bookings_match:
        block = bookings_match.group(1).strip()
        # Collect key detail lines: Date, Units, Status, CP, DP
        wanted = re.compile(r'(?i)(Date|Units|Status|CP|DP|Extra)', )
        lines = [ln.strip().lstrip('* ') for ln in block.splitlines()
                 if wanted.search(ln)]
        if lines:
            return ' | '.join(lines[:5])  # cap at 5 fields for readability

    # Fallback: first sentence mentioning booking/reservation keywords
    combined = summary + '\n' + '\n'.join(m.get('content', '') for m in tail_messages)
    m = re.search(
        r'(?i)([^.\n]*(?:book(?:ing)?|reserv(?:asi|ation|e)|pesan(?:an)?)[^.\n]{0,120})',
        combined,
    )
    if m:
        snippet = m.group(1).strip()
        if len(snippet) > 10:
            return snippet[:120] + ('...' if len(snippet) > 120 else '')

    return None


def _format_notification(agent_name: str, session_id: str, external_user_id: str,
                         user_info: dict, booking_info: str | None,
                         actionables: list) -> str:
    """Format a human-readable notification message."""
    lines = [f"[Session Recap Alert] Agent: {agent_name or 'Unknown'}"]
    lines.append(f"Session: {session_id[:8]}...")

    # User identity
    user_name = user_info.get('name')
    user_phone = user_info.get('phone')
    if user_name and user_phone:
        lines.append(f"User: {user_name} | {user_phone} (ID: {external_user_id})")
    elif user_name:
        lines.append(f"User: {user_name} (ID: {external_user_id})")
    elif user_phone:
        lines.append(f"User: {user_phone} (ID: {external_user_id})")
    elif external_user_id:
        lines.append(f"User ID: {external_user_id}")

    # Booking info
    if booking_info:
        lines.append(f"Booking: {booking_info}")

    lines.append("")
    lines.append("Actionable items detected:")
    for item in actionables:
        lines.append(f"  - {item['description']}")
    lines.append("")
    lines.append("Please review this session.")
    return "\n".join(lines)
