"""Durable, multiplexed Server-Sent Events gateway."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime

from flask import Blueprint, Response, request, stream_with_context

from backend.realtime_store import realtime_store


log = logging.getLogger(__name__)
realtime_bp = Blueprint('realtime', __name__)

HEARTBEAT_INTERVAL = 15
SQLITE_MAX_ID = 2**63 - 1
_ALLOWED_CHANNELS = {'chat', 'status', 'approvals', 'update', 'workplace'}


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _format_sse_event(event_name: str, data: dict,
                      event_id: int | str | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f'id: {event_id}')
    if event_name:
        lines.append(f'event: {event_name}')
    lines.append('data: ' + json.dumps(
        data, separators=(',', ':'), default=_json_default,
    ))
    return '\n'.join(lines) + '\n\n'


def _parse_cursor(value) -> int | None:
    text = str(value).strip() if value is not None else ''
    if not text:
        return None
    try:
        cursor = int(text)
    except (TypeError, ValueError):
        return None
    return cursor if 0 <= cursor <= SQLITE_MAX_ID else None


def _snapshot_payload(channel: str, payload: dict) -> dict:
    result = dict(payload)
    result.setdefault('timestamp', int(time.time() * 1000))
    result.setdefault('channel', channel)
    result['snapshot'] = True
    return result


def _build_snapshot(channels: set[str], session_id: str | None = None,
                    agent_id: str | None = None,
                    workplace_id: str | None = None,
                    skip_approval_ids: set[str] | None = None
                    ) -> list[tuple[str, str, dict]]:
    events: list[tuple[str, str, dict]] = []
    skip_approval_ids = skip_approval_ids or set()

    if 'status' in channels or session_id:
        try:
            from models.db import db
            busy = realtime_store.busy_agents()
            for agent in db.get_agents():
                if session_id and 'status' not in channels and agent['id'] != agent_id:
                    continue
                entry = busy.get(agent['id'])
                events.append(('status', 'agent_busy_changed', {
                    'agent_id': agent['id'],
                    'busy': bool(entry),
                    'session_id': entry.get('session_id', '') if entry else '',
                    'session_ids': entry.get('session_ids', []) if entry else [],
                    'active_count': entry.get('active_count', 0) if entry else 0,
                    'state': entry.get('state', 'idle') if entry else 'idle',
                }))
        except Exception as exc:
            log.warning('realtime status snapshot failed: %s', exc)

    if 'approvals' in channels or session_id:
        try:
            from models.db import db
            for approval in db.get_pending_tool_approvals() or []:
                approval_session = approval.get('session_id')
                if str(approval.get('id', '')) in skip_approval_ids:
                    continue
                if session_id and 'approvals' not in channels \
                        and approval_session != session_id:
                    continue
                events.append(('approvals', 'approval_required', {
                    'approval_id': approval.get('id', ''),
                    'agent_id': approval.get('agent_id', ''),
                    'source_agent_id': approval.get('source_agent_id', ''),
                    'source_agent_name': approval.get('source_agent_name', ''),
                    'tool': approval.get('tool_name', ''),
                    'args': approval.get('tool_args', {}),
                    'approval_info': approval.get('approval_info', {}),
                    'reasons': approval.get('reasons', []),
                    'score': approval.get('score'),
                }))
        except Exception as exc:
            log.warning('realtime approval snapshot failed: %s', exc)

    if 'update' in channels:
        try:
            from backend import update_manager
            events.append(('update', 'update_status', update_manager.get_status()))
        except Exception as exc:
            log.warning('realtime update snapshot failed: %s', exc)

    if 'workplace' in channels and workplace_id:
        try:
            from backend.workplaces.manager import workplace_manager
            events.append((
                'workplace', 'workplace_status_changed',
                workplace_manager.get_status(workplace_id),
            ))
        except Exception as exc:
            log.warning('realtime workplace snapshot failed: %s', exc)

    return events


@realtime_bp.route('/api/realtime/stream', methods=['GET'])
def api_realtime_stream():
    channels = {
        value.strip() for value in request.args.get('channels', '').split(',')
        if value.strip()
    }
    unknown = channels - _ALLOWED_CHANNELS
    if unknown:
        return Response(
            json.dumps({'error': 'unknown channels', 'channels': sorted(unknown)}),
            status=400, mimetype='application/json',
        )

    chat_enabled = request.args.get('chat') == '1'
    session_id = request.args.get('session_id', '').strip() or None
    agent_id = request.args.get('agent_id', '').strip() or None
    workplace_id = request.args.get('workplace', '').strip() or None
    if chat_enabled:
        if not session_id or not agent_id:
            return Response(
                json.dumps({'error': 'session_id and agent_id required when chat=1'}),
                status=400, mimetype='application/json',
            )
        channels.add('chat')
    if workplace_id:
        channels.add('workplace')
    if not channels:
        return Response(
            json.dumps({'error': 'At least one channel must be requested'}),
            status=400, mimetype='application/json',
        )

    raw_query_cursor = request.args.get('after')
    raw_header_cursor = request.headers.get('Last-Event-ID')
    versioned_cursor = request.args.get('cursor_version') == '2'
    query_cursor = _parse_cursor(raw_query_cursor) if versioned_cursor else None
    header_cursor = _parse_cursor(raw_header_cursor) if versioned_cursor else None
    supplied_cursor = bool(
        str(raw_query_cursor or '').strip() or str(raw_header_cursor or '').strip()
    )
    valid_cursors = [value for value in (query_cursor, header_cursor) if value is not None]
    cursor = max(valid_cursors) if valid_cursors else 0
    reset_cursor = not versioned_cursor or (supplied_cursor and not valid_cursors)
    valid_header_cursor = header_cursor is not None and bool(str(raw_header_cursor or '').strip())
    snapshot_arg = request.args.get('snapshot')
    send_snapshot = (
        snapshot_arg == '1' if snapshot_arg in {'0', '1'}
        else not supplied_cursor
    )
    if reset_cursor:
        send_snapshot = True
    if valid_header_cursor:
        # Native EventSource reconnects reuse the original snapshot=1 URL but
        # provide Last-Event-ID. Durable replay is sufficient on reconnect.
        send_snapshot = False
    legacy = request.args.get('legacy', '').strip()

    def public_event_name(event_name: str) -> str:
        if legacy == 'update':
            return {
                'update_status': 'status',
                'update_done': 'done',
            }.get(event_name, event_name)
        return event_name

    from flask import session as flask_session
    from models.api_rate_limit import sse_register, sse_unregister, SSE_MAX_CONCURRENT
    sse_identity = (
        f"user:{flask_session.get('_user_id', 'admin')}"
        if flask_session.get('authenticated')
        else f"ip:{request.remote_addr or '0.0.0.0'}"
    )
    allowed, _count = sse_register(sse_identity)
    if not allowed:
        return Response(
            json.dumps({
                'error': 'too_many_sse_connections',
                'message': f'Maximum {SSE_MAX_CONCURRENT} concurrent SSE connections allowed.',
                'retry_after': 30,
            }),
            status=429, headers={'Retry-After': '30'},
            mimetype='application/json',
        )

    from models.db import db
    db.close()
    connected_at = time.time()

    @stream_with_context
    def generate():
        nonlocal cursor, send_snapshot
        try:
            yield 'retry: 3000\n\n'

            high_water = realtime_store.high_water()
            if reset_cursor or cursor > high_water:
                cursor = high_water
                send_snapshot = True
            elif not supplied_cursor:
                cursor = high_water

            replayed_approval_ids: set[str] = set()

            # Restore telemetry already represented by the HTTP chat history
            # before sending current-state snapshots such as pending approval.
            if send_snapshot and session_id:
                snapshot_cursor = realtime_store.last_session_clear_id(
                    session_id, cursor,
                )
                while snapshot_cursor < cursor:
                    active_events = realtime_store.events_after(
                        snapshot_cursor, {'chat'}, session_id=session_id,
                        agent_id=agent_id, up_to_id=cursor,
                        active_only=True, limit=500,
                    )
                    if not active_events:
                        break
                    for event in active_events:
                        snapshot_cursor = event['id']
                        data = dict(event['data'])
                        data['snapshot'] = True
                        data['source_event_id'] = event['id']
                        data['event_id'] = 0
                        data['seq'] = 0
                        if event['event'] == 'approval_required':
                            approval_id = str(data.get('approval_id', ''))
                            if approval_id:
                                replayed_approval_ids.add(approval_id)
                        yield _format_sse_event(
                            public_event_name(event['event']), data,
                        )

            # Close the history-to-stream handoff gap before taking state
            # snapshots. Filtered global IDs may be skipped safely.
            if send_snapshot and cursor < high_water:
                while cursor < high_water:
                    events = realtime_store.events_after(
                        cursor, channels, session_id=session_id, agent_id=agent_id,
                        workplace_id=workplace_id, up_to_id=high_water,
                    )
                    if not events:
                        break
                    for event in events:
                        cursor = event['id']
                        if event['event'] == 'approval_required':
                            approval_id = str(event['data'].get('approval_id', ''))
                            if approval_id:
                                replayed_approval_ids.add(approval_id)
                        yield _format_sse_event(
                            public_event_name(event['event']),
                            event['data'], event['id'],
                        )
                cursor = high_water

            if send_snapshot:
                for channel, event_name, payload in _build_snapshot(
                        channels, session_id, agent_id, workplace_id,
                        replayed_approval_ids):
                    yield _format_sse_event(
                        public_event_name(event_name),
                        _snapshot_payload(channel, payload),
                    )

            yield _format_sse_event('ready', {
                'event_id': cursor, 'seq': cursor,
                'timestamp': int(time.time() * 1000),
                'channel': 'system',
            }, cursor)

            while True:
                if time.time() - connected_at >= 24 * 60 * 60:
                    yield _format_sse_event('auth_expired', {
                        'message': 'Connection expired, please reconnect',
                    }, cursor)
                    break

                observed_high_water = realtime_store.high_water()
                events = realtime_store.events_after(
                    cursor, channels, session_id=session_id, agent_id=agent_id,
                    workplace_id=workplace_id, up_to_id=observed_high_water,
                )
                if events:
                    for event in events:
                        cursor = event['id']
                        yield _format_sse_event(
                            public_event_name(event['event']),
                            event['data'], event['id'],
                        )
                    continue

                realtime_store.wait_for_events(observed_high_water, HEARTBEAT_INTERVAL)
                if realtime_store.high_water() <= observed_high_water:
                    yield 'event: heartbeat\ndata: {}\n\n'
        except GeneratorExit:
            pass
        finally:
            realtime_store.close()
            sse_unregister(sse_identity)

    return Response(
        generate(), mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )


@realtime_bp.route('/api/realtime/pause', methods=['POST'])
def api_realtime_pause():
    # Delivery is durable now.  Browser visibility may pause rendering locally;
    # the server no longer needs a second per-connection buffer.
    return Response(json.dumps({'ok': True}), mimetype='application/json')


@realtime_bp.route('/api/realtime/resume', methods=['POST'])
def api_realtime_resume():
    return Response(json.dumps({'ok': True}), mimetype='application/json')
