"""
AgentAPI Plugin — Flask Route Handlers

Consumer endpoints (/plugin/ prefix → bypass global session auth — plugin handles its own auth):
  POST /plugin/agentapi/v1/chat/completions   — OpenAI-compatible chat completion
  GET  /plugin/agentapi/v1/models             — List available models (filtered by token scope)

Admin endpoints (/api/ prefix → session auth required):
  GET    /api/agentapi/admin              — Admin dashboard page
  GET    /api/agentapi/admin/tokens       — List all tokens
  POST   /api/agentapi/admin/tokens       — Create a new token
  PUT    /api/agentapi/admin/tokens/<id>  — Update a token
  DELETE /api/agentapi/admin/tokens/<id>  — Delete a token
  GET    /api/agentapi/admin/tokens/<id>/stats — Token usage stats
"""

import hashlib
import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone

_logger = logging.getLogger(__name__)

from flask import (
    Blueprint, Response, jsonify, render_template, request,
    stream_with_context,
)

from plugins.agentapi.db import TokenDB, hash_token

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
token_db = TokenDB()

# In-memory cache: maps token_id → plaintext (for the /reveal endpoint)
_plaintext_cache: dict[int, str] = {}
_PLAINTEXT_CACHE_TTL = 3600  # seconds

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _parse_allowed_models(raw) -> list:
    """Parse allowed_models from DB storage (JSON string or '*')."""
    if raw == '*' or raw is None:
        return ['*']
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return []

def _get_model_agent_map():
    """Load MODEL_AGENT_MAP from plugin config, parse as JSON."""
    from backend.plugin_manager import plugin_manager
    cfg = plugin_manager.get_plugin_config('agentapi')
    raw = cfg.get('MODEL_AGENT_MAP', '{}')
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {}

def _validate_bearer_token():
    """Extract and validate Bearer token from Authorization header.

    Returns (token_row, error_tuple).
    error_tuple is (body_dict, status_code) or None if valid.
    """
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return None, ({'error': 'Missing or invalid Authorization header'}, 401)

    token_str = auth_header[7:]  # strip 'Bearer '
    token_h = hash_token(token_str)
    token_row = token_db.get_token_by_hash(token_h)

    if not token_row:
        return None, ({'error': 'Invalid token'}, 401)

    if token_row.get('status') == 'suspended':
        return None, ({'error': 'Token suspended'}, 403)

    if token_db.is_token_expired(token_row):
        return None, ({'error': 'Token expired'}, 403)

    # Check/reset quota
    token_db.reset_quota_if_needed(token_row)
    quota_limit = token_row.get('quota_limit')
    quota_used = token_row.get('quota_used', 0)
    if quota_limit is not None and quota_used >= quota_limit:
        return None, ({
            'error': 'Quota exceeded',
            'quota_limit': quota_limit,
            'quota_used': quota_used,
        }, 429)

    return token_row, None

def _build_user_message(messages: list) -> str:
    """Extract the last user message from an OpenAI-style messages array.

    System messages are merged with the user message by prepending them
    as context, since this endpoint routes to an agent that already has
    its own internal system prompt.
    """
    if not messages:
        return ''

    # Collect system messages
    system_parts = []
    for msg in messages:
        if msg.get('role') == 'system' and msg.get('content'):
            content = msg['content']
            if isinstance(content, list):
                text_parts = [p.get('text', '') for p in content if p.get('type') == 'text']
                content = '\n'.join(text_parts)
            if content:
                system_parts.append(content)

    system_prefix = ''
    if system_parts:
        system_prefix = f"[System Instructions]\n{''.join(system_parts)}\n\n"

    # Prefer the last user message
    for msg in reversed(messages):
        if msg.get('role') == 'user' and msg.get('content'):
            user_content = msg['content']
            if system_prefix:
                return f"{system_prefix}[User Message]\n{user_content}"
            return user_content

    # Fallback: concatenate all non-system content
    parts = []
    for msg in messages:
        if msg.get('role') == 'system':
            continue
        content = msg.get('content', '')
        if isinstance(content, list):
            text_parts = [p.get('text', '') for p in content if p.get('type') == 'text']
            content = '\n'.join(text_parts)
        if content:
            role = msg.get('role', 'user')
            parts.append(f"[{role}]: {content}")

    combined = '\n'.join(parts)
    return f"{system_prefix}{combined}" if system_prefix else combined

def _generate_external_user_id(token_row: dict, agent_id: str, request) -> str:
    """Generate an external_user_id for the API consumer.

    Uses X-Session-Id header if present (opt-in stateful);
    otherwise creates a deterministic ID from token_hash + agent_id.
    The caller is responsible for clearing the session when stateless
    behavior is desired — this function only produces the ID.
    """
    session_header = request.headers.get('X-Session-Id', '').strip()
    if session_header:
        return f"api:{session_header}"
    # Deterministic: same token + agent = same session
    token_part = token_row.get('token_hash', '')[:16]
    return f"api:{token_part}:{agent_id}"

def _format_openai_response(content: str, model: str,
                            usage: dict = None) -> dict:
    """Build an OpenAI-compatible chat completion response."""
    resp = {
        'id': f"chatcmpl-{uuid.uuid4().hex[:12]}",
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': model,
        'choices': [{
            'index': 0,
            'message': {
                'role': 'assistant',
                'content': content,
            },
            'finish_reason': 'stop',
        }],
    }
    if usage:
        resp['usage'] = usage
    return resp


# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

def _cors_enabled():
    """Check if DISABLE_CORS is enabled in plugin config."""
    from backend.plugin_manager import plugin_manager
    cfg = plugin_manager.get_plugin_config('agentapi')
    return bool(cfg.get('DISABLE_CORS', False))

def _add_cors_headers(response):
    """Inject CORS headers into response when DISABLE_CORS is enabled."""
    if not _cors_enabled():
        return response
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Session-Id'
    response.headers['Access-Control-Max-Age'] = '86400'
    return response


def create_blueprint():
    bp = Blueprint('agentapi', __name__,
                   template_folder=os.path.join(PLUGIN_DIR, 'templates'))

    # CORS after_request hook — inject headers on every response
    bp.after_request(_add_cors_headers)

    # Handle CORS preflight (OPTIONS) for all consumer routes
    @bp.route('/plugin/agentapi/v1/chat/completions', methods=['OPTIONS'])
    @bp.route('/plugin/agentapi/v1/models', methods=['OPTIONS'])
    @bp.route('/api/agentapi/admin', methods=['OPTIONS'])
    @bp.route('/api/agentapi/admin/tokens', methods=['OPTIONS'])
    @bp.route('/api/agentapi/admin/tokens/<int:token_id>', methods=['OPTIONS'])
    @bp.route('/api/agentapi/admin/tokens/<int:token_id>/stats', methods=['OPTIONS'])
    @bp.route('/api/agentapi/admin/tokens/<int:token_id>/reveal', methods=['OPTIONS'])
    @bp.route('/api/agentapi/admin/tokens/<int:token_id>/reset', methods=['OPTIONS'])
    @bp.route('/api/agentapi/admin/model-agent-map', methods=['OPTIONS'])
    def cors_preflight(**kwargs):
        return '', 204

    # =======================================================================
    # Consumer endpoints — /plugin/ prefix bypasses global enforce_auth
    # Plugin handles its own authentication via Bearer token
    # =======================================================================

    @bp.route('/plugin/agentapi/v1/chat/completions', methods=['POST'])
    def chat_completions():
        """OpenAI-compatible chat completion endpoint.

        Auth: Bearer token — validated inline.
        Routes to the agent specified by the `model` field in the request body,
        resolved via MODEL_AGENT_MAP.
        """
        # --- Auth ---
        token_row, error = _validate_bearer_token()
        if error:
            body, status = error
            return jsonify(body), status

        # --- Parse request ---
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'error': 'Invalid JSON body'}), 400

        model = data.get('model', '').strip()
        if not model:
            return jsonify({'error': 'model field is required'}), 400

        messages = data.get('messages', [])
        stream = data.get('stream', False)

        # --- Resolve model → agent ---
        model_agent_map = _get_model_agent_map()
        agent_id = model_agent_map.get(model)
        if not agent_id:
            return jsonify({'error': f'Unknown model: {model}'}), 400

        # --- Check model scope ---
        if not TokenDB.token_can_access_model(token_row, model):
            return jsonify({'error': f'Token not authorized for model: {model}'}), 403

        # --- Verify agent exists ---
        from models.db import db
        agent = db.get_agent(agent_id)
        if not agent:
            return jsonify({'error': f'Agent not found: {agent_id}'}), 404
        if not agent.get('is_super') and not agent.get('enabled', True):
            return jsonify({'error': f'Agent is disabled: {agent_id}'}), 503

        # --- Build user message ---
        user_message = _build_user_message(messages)
        if not user_message:
            return jsonify({'error': 'No user message found in messages array'}), 400

        # --- Build external_user_id ---
        external_user_id = _generate_external_user_id(token_row, agent_id, request)

        # Sessions are stateful by default — Evonic manages history &
        # auto-summarization. Use X-Session-Id to isolate conversations.
        # X-Clear-Session header can force a fresh session on demand.

        # --- Call agent ---
        from backend.agent_runtime import agent_runtime

        if stream:
            return _stream_response(token_row, agent_id, external_user_id,
                                    user_message, model, model_agent_map)

        try:
            result = agent_runtime.handle_message(
                agent_id=agent_id,
                external_user_id=external_user_id,
                message=user_message,
                channel_id=None,
                skip_buffer=True,
            )
        except Exception as e:
            return jsonify({'error': f'Agent processing failed: {str(e)}'}), 500

        response_text = result.get('response', '')
        if result.get('error'):
            return jsonify({'error': result['error']}), 500

        # handle_message returns response=None for buffered/async paths
        # (message_buffer_seconds > 0). Poll the session's chatlog for the
        # assistant reply instead of returning an empty completion.
        if not response_text and not result.get('async'):
            try:
                from models.db import db
                session_id = db.get_or_create_session(agent_id, external_user_id, None)
                # Anchor: this message was saved before the buffered return,
                # so the reply is any assistant message with id > this user msg.
                user_msgs = [m for m in db.get_session_messages(session_id) if m.get('role') == 'user']
                anchor_id = user_msgs[-1]['id'] if user_msgs else 0
                import time as _time
                deadline = _time.time() + 120
                while _time.time() < deadline:
                    for m in db.get_messages_after(session_id, anchor_id):
                        if m.get('role') == 'assistant' and m.get('content'):
                            response_text = m['content']
                            break
                    if response_text:
                        break
                    _time.sleep(0.3)
            except Exception as e:
                _logger.warning("agentapi poll reply failed for %s: %s", agent_id, e)

        if not response_text:
            return jsonify({'error': 'Agent returned no response'}), 504

        # --- Increment quota + log ---
        token_db.increment_quota(token_row)
        token_db.log_usage(
            token_id=token_row['id'],
            agent_id=agent_id,
            model=model,
            session_id=external_user_id,
            prompt_tokens=0,   # TODO: extract from agent response if available
            completion_tokens=0,
            duration_ms=0,
        )

        # --- Format OpenAI response ---
        return jsonify(_format_openai_response(response_text, model))

    def _stream_response(token_row, agent_id, external_user_id,
                         user_message, model, model_agent_map):
        """Handle streaming SSE response.

        Pre-computes the session_id so event stream handlers can filter
        by session.  The agent is run in a background thread while the
        main thread yields SSE chunks.
        """
        from backend.event_stream import event_stream
        from backend.agent_runtime import agent_runtime
        from models.db import db as _db

        # Pre-compute session_id the same way handle_message does so we can
        # filter events by session.
        session_id = _db.get_or_create_session(agent_id, external_user_id, None)

        # Sessions are stateful by default — Evonic manages history &
        # auto-summarization. Use X-Session-Id to isolate conversations.

        chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        q = queue.Queue(maxsize=500)
        _SENTINEL = object()

        def on_chunk(data):
            if data.get('session_id') != session_id:
                return
            content = data.get('content', '')
            if content:
                q.put_nowait(('chunk', content))

        def on_turn_complete(data):
            if data.get('session_id') != session_id:
                return
            q.put_nowait((_SENTINEL, None))

        # Tool-call notifications: coalesce tool_executed events within a short
        # window into one SSE event (mirrors the Telegram channel's coalescing).
        # A 100-tool loop collapses to a handful of lines, not one per call.
        _tool_pending: dict = {}  # session_id -> {last, counts: {name: n}, any_error}
        _TOOL_WINDOW = 5.0

        def _flush_tool_notify(sid: str):
            entry = _tool_pending.pop(sid, None)
            if not entry:
                return
            parts = [f"{name} ({n}x)" for name, n in sorted(entry['counts'].items())]
            q.put_nowait(('tool', {
                'tool_name': ', '.join(parts),
                'has_error': entry['any_error'],
            }))

        def on_tool_executed(data):
            if data.get('session_id') != session_id:
                return
            now = time.time()
            entry = _tool_pending.get(session_id)
            if entry and now - entry['last'] < _TOOL_WINDOW:
                name = data.get('tool_name', '')
                entry['counts'][name] = entry['counts'].get(name, 0) + 1
                entry['any_error'] = entry['any_error'] or data.get('has_error', False)
                entry['last'] = now
                return
            if entry:
                _flush_tool_notify(session_id)
            _tool_pending[session_id] = {
                'last': now,
                'counts': {data.get('tool_name', ''): 1},
                'any_error': data.get('has_error', False),
            }
            threading.Timer(_TOOL_WINDOW, _flush_tool_notify, args=[session_id]).start()

        # Thread to run the agent and forward chunks via event_stream
        events_registered = []

        def run_agent():
            event_stream.on('llm_response_chunk', on_chunk)
            event_stream.on('turn_complete', on_turn_complete)
            event_stream.on('tool_executed', on_tool_executed)
            events_registered.extend(['llm_response_chunk', 'turn_complete', 'tool_executed'])

            result = None
            try:
                result = agent_runtime.handle_message(
                    agent_id=agent_id,
                    external_user_id=external_user_id,
                    message=user_message,
                    channel_id=None,
                    skip_buffer=True,
                )
            except Exception:
                result = None
            finally:
                # Only end the stream early when handle_message returned a real
                # reply (non-buffered path). Buffered/async agents return
                # response=None immediately — the real reply arrives later via
                # event_stream (llm_response_chunk/turn_complete), so we must
                # stay subscribed. If we put the sentinel here, generate()
                # exits, the finally below unsubscribes, and the reply events
                # are lost (SSE shows only stop + [DONE]).
                if not result or result.get('response'):
                    q.put_nowait((_SENTINEL, None))

        agent_thread = threading.Thread(target=run_agent, daemon=True)
        agent_thread.start()

        def generate():
            content_sent = False
            try:
                while True:
                    try:
                        item = q.get(timeout=120)
                    except queue.Empty:
                        # Timeout — agent took too long, send a keepalive?
                        break

                    if item[0] is _SENTINEL:
                        # Turn complete: flush any pending coalesced tool events
                        # (the 5s window may not have elapsed yet). Yield them
                        # directly — the queue loop is about to exit, so a
                        # put_nowait'd item would never be consumed.
                        entry = _tool_pending.pop(session_id, None)
                        if entry:
                            parts = [f"{name} ({n}x)" for name, n in sorted(entry['counts'].items())]
                            tool_event = {
                                'type': 'tool',
                                'tool_name': ', '.join(parts),
                                'has_error': entry['any_error'],
                            }
                            yield f"data: {json.dumps(tool_event)}\n\n"
                        break

                    event_type, payload = item
                    if event_type == 'chunk':
                        chunk = {
                            'id': chat_id,
                            'object': 'chat.completion.chunk',
                            'created': created,
                            'model': model,
                            'choices': [{
                                'index': 0,
                                'delta': {'content': payload},
                                'finish_reason': None,
                            }],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                        content_sent = True
                    elif event_type == 'tool':
                        # Tool-call notification: a compact line like
                        #   {"type":"tool","tool_name":"bash (2x), python (1x)","has_error":false}
                        # Streamed inline so the client can show it in the chat
                        # while the agent keeps working.
                        tool_event = {
                            'type': 'tool',
                            'tool_name': payload.get('tool_name', ''),
                            'has_error': payload.get('has_error', False),
                        }
                        yield f"data: {json.dumps(tool_event)}\n\n"

                # Send final [DONE] chunk
                final_chunk = {
                    'id': chat_id,
                    'object': 'chat.completion.chunk',
                    'created': created,
                    'model': model,
                    'choices': [{
                        'index': 0,
                        'delta': {},
                        'finish_reason': 'stop',
                    }],
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"

                # Increment quota after streaming completes
                token_db.increment_quota(token_row)
                token_db.log_usage(
                    token_id=token_row['id'],
                    agent_id=agent_id,
                    model=model,
                    session_id=external_user_id,
                )
            finally:
                # Clean up event subscriptions
                for ev_name in events_registered:
                    try:
                        if ev_name == 'llm_response_chunk':
                            event_stream.off(ev_name, on_chunk)
                        elif ev_name == 'turn_complete':
                            event_stream.off(ev_name, on_turn_complete)
                        elif ev_name == 'tool_executed':
                            event_stream.off(ev_name, on_tool_executed)
                    except Exception:
                        pass

        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
            },
        )

    @bp.route('/plugin/agentapi/v1/models', methods=['GET'])
    def list_models():
        """Return the list of available models filtered by token scope.

        Lightweight auth: token validated but quota NOT consumed.
        """
        token_row, error = _validate_bearer_token()
        if error:
            body, status = error
            return jsonify(body), status

        model_agent_map = _get_model_agent_map()
        visible = TokenDB.token_visible_models(token_row, model_agent_map)

        models = [{
            'id': name,
            'object': 'model',
            'created': 0,
            'owned_by': 'evonic',
        } for name in sorted(visible)]

        return jsonify({
            'object': 'list',
            'data': models,
        })

    # =======================================================================
    # Admin endpoints — /api/ prefix → session auth via global enforce_auth
    # =======================================================================

    @bp.route('/api/agentapi/admin')
    def admin_dashboard():
        """Admin dashboard page for token management."""
        return render_template('admin.html',
                               model_agent_map=_get_model_agent_map())

    @bp.route('/api/agentapi/admin/tokens', methods=['GET'])
    def admin_list_tokens():
        """List all tokens. Query params: ?status=active|suspended"""
        status = request.args.get('status', '').strip() or None
        tokens = token_db.list_tokens(status=status)
        # Mask: never expose token_hash or full token
        result = []
        for t in tokens:
            result.append({
                'id': t['id'],
                'name': t['name'],
                'token_prefix': t['token_prefix'],
                'quota_limit': t.get('quota_limit'),
                'quota_used': t.get('quota_used', 0),
                'status': t.get('status', 'active'),
                'expires_at': t.get('expires_at'),
                'allowed_models': _parse_allowed_models(t.get('allowed_models')),
                'last_used_at': t.get('last_used_at'),
                'created_at': t.get('created_at'),
            })
        return jsonify({'tokens': result})

    @bp.route('/api/agentapi/admin/tokens', methods=['POST'])
    def admin_create_token():
        """Create a new bearer token.

        Body: {"name": "...", "quota_limit": 1000, "expires_at": "...",
               "allowed_models": ["gpt-4-assistant"]}
        quota_limit, expires_at, allowed_models are optional.
        allowed_models defaults to "*" (all models).
        Returns the plaintext token ONCE.
        """
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'error': 'Invalid JSON body'}), 400

        name = data.get('name', '').strip()
        if not name:
            return jsonify({'error': 'name is required'}), 400

        # Validate allowed_models against MODEL_AGENT_MAP
        model_agent_map = _get_model_agent_map()
        allowed_models = data.get('allowed_models') or '*'
        if isinstance(allowed_models, list):
            unknown = [m for m in allowed_models if m not in model_agent_map]
            if unknown:
                return jsonify({
                    'error': f'Unknown model(s): {", ".join(unknown)}',
                }), 400

        # Validate expires_at if provided
        expires_at = data.get('expires_at')
        if expires_at:
            try:
                datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
            except (ValueError, TypeError):
                return jsonify({'error': 'Invalid expires_at format. Use ISO 8601.'}), 400

        row = token_db.create_token({
            'name': name,
            'quota_limit': data.get('quota_limit'),
            'expires_at': expires_at,
            'allowed_models': allowed_models,
        })

        if not row:
            return jsonify({'error': 'Failed to create token'}), 500

        plaintext_token = row.pop('token', None)
        row['allowed_models'] = _parse_allowed_models(row.get('allowed_models'))

        # Cache plaintext temporarily for the /reveal endpoint
        if plaintext_token and row.get('id'):
            _plaintext_cache[row['id']] = plaintext_token

        return jsonify({
            'token': row,
            'plaintext': plaintext_token,
        }), 201

    @bp.route('/api/agentapi/admin/tokens/<int:token_id>', methods=['PUT'])
    def admin_update_token(token_id):
        """Update a token's mutable fields. All fields optional."""
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'error': 'Invalid JSON body'}), 400

        # Validate / normalise allowed_models if provided
        if 'allowed_models' in data:
            am = data['allowed_models']
            if am is None or am == '':
                data['allowed_models'] = '*'
            elif isinstance(am, list):
                model_agent_map = _get_model_agent_map()
                unknown = [m for m in am if m not in model_agent_map]
                if unknown:
                    return jsonify({
                        'error': f'Unknown model(s): {", ".join(unknown)}',
                    }), 400

        # Validate expires_at if provided
        if 'expires_at' in data and data['expires_at'] is not None:
            try:
                datetime.fromisoformat(
                    data['expires_at'].replace('Z', '+00:00'))
            except (ValueError, TypeError):
                return jsonify({
                    'error': 'Invalid expires_at format. Use ISO 8601.',
                }), 400

        ok = token_db.update_token(token_id, data)
        if not ok:
            return jsonify({'error': 'Token not found or no fields to update'}), 404

        row = token_db._get_by_id(token_id)
        if row:
            row['allowed_models'] = _parse_allowed_models(row.get('allowed_models'))

        return jsonify({'token': row})

    @bp.route('/api/agentapi/admin/tokens/<int:token_id>', methods=['DELETE'])
    def admin_delete_token(token_id):
        """Delete a token permanently."""
        ok = token_db.delete_token(token_id)
        if not ok:
            return jsonify({'error': 'Token not found'}), 404
        return '', 204

    @bp.route('/api/agentapi/admin/tokens/<int:token_id>/stats',
              methods=['GET'])
    def admin_token_stats(token_id):
        """Return detailed usage stats for a token."""
        stats = token_db.get_token_stats(token_id)
        if not stats:
            return jsonify({'error': 'Token not found'}), 404
        stats['allowed_models'] = _parse_allowed_models(
            stats.get('allowed_models'))
        return jsonify({'stats': stats})

    @bp.route('/api/agentapi/admin/tokens/<int:token_id>/reveal',
              methods=['GET'])
    def admin_reveal_token(token_id):
        """Return the plaintext token if cached (creation-time only)."""
        plaintext = _plaintext_cache.get(token_id)
        if not plaintext:
            return jsonify({'error': 'Plaintext token no longer available'}), 404
        return jsonify({'plaintext': plaintext})

    @bp.route('/api/agentapi/admin/tokens/<int:token_id>/reset',
              methods=['POST'])
    def admin_reset_token(token_id):
        """Regenerate the secret key for a token. Returns the new plaintext
        once; the old key is immediately invalidated."""
        plaintext = token_db.reset_token(token_id)
        if not plaintext:
            return jsonify({'error': 'Token not found'}), 404
        # Cache for /reveal endpoint
        _plaintext_cache[token_id] = plaintext
        return jsonify({'plaintext': plaintext})

    # =======================================================================
    # Model-agent mapping helper (for admin UI)
    # =======================================================================

    @bp.route('/api/agentapi/admin/model-agent-map', methods=['GET'])
    def admin_model_agent_map():
        """Return the current MODEL_AGENT_MAP for the admin UI."""
        return jsonify({'map': _get_model_agent_map()})

    return bp
