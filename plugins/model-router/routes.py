"""
Model Router Plugin - Flask Route Handlers

Consumer endpoints (/plugin/ prefix - no global session auth, plugin handles its own auth):
  POST /plugin/model-router/v1/chat/completions   - OpenAI-compatible chat completion
  GET  /plugin/model-router/v1/models             - List available models (filtered by token scope)

Admin endpoints (/api/ prefix - session auth required):
  GET    /api/model-router/admin              - Admin dashboard page
  GET    /api/model-router/admin/tokens       - List all tokens
  POST   /api/model-router/admin/tokens       - Create a new token
  PUT    /api/model-router/admin/tokens/<id>  - Update a token
  DELETE /api/model-router/admin/tokens/<id>  - Delete a token
  GET    /api/model-router/admin/tokens/<id>/stats - Token usage stats
  GET    /api/model-router/admin/tokens/<id>/logs  - Usage logs
"""

import hashlib
import json
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import (
    Blueprint, Response, jsonify, render_template, request,
    stream_with_context,
)

from plugins.model_router.db import TokenDB, hash_token

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
token_db = TokenDB()

# In-memory cache: maps token_id -> plaintext (for the /reveal endpoint)
_plaintext_cache = {}
_PLAINTEXT_CACHE_TTL = 3600  # seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _parse_allowed_models(raw):
    """Parse allowed_models from DB storage (JSON string or '*')."""
    if raw == '*' or raw is None:
        return ['*']
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return []


def _get_router_model_list():
    """Load ROUTER_MODEL_LIST from plugin config, parse as comma-separated."""
    from backend.plugin_manager import plugin_manager
    cfg = plugin_manager.get_plugin_config('model_router')
    raw = cfg.get('ROUTER_MODEL_LIST', '')
    if not raw or not raw.strip():
        return []  # empty means allow all enabled models
    return [m.strip() for m in raw.split(',') if m.strip()]


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


def _format_openai_response(content, model, usage=None):
    """Build an OpenAI-compatible chat completion response."""
    resp = {
        'id': f'chatcmpl-{uuid.uuid4().hex[:12]}',
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


def _extract_usage_from_response(resp_data):
    """Extract token usage from an OpenAI-compatible API response."""
    usage = resp_data.get('usage', {})
    if usage:
        return {
            'prompt_tokens': usage.get('prompt_tokens', 0),
            'completion_tokens': usage.get('completion_tokens', 0),
            'total_tokens': usage.get('total_tokens', 0),
        }
    return {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}


def _lookup_model(model_name):
    """Lookup model config from llm_models table by model name or id.

    Returns (model_config_dict, error_tuple) tuple.
    """
    from models.db import db as models_db

    # Try by model_name field first, then by id
    model = models_db.get_model_by_model_name(model_name)
    if not model:
        model = models_db.get_model_by_id(model_name)

    if not model:
        return None, ({'error': f'Unknown model: {model_name}'}, 400)

    if not model.get('enabled', 1):
        return None, ({'error': f'Model is disabled: {model_name}'}, 503)

    # Check router whitelist if set
    whitelist = _get_router_model_list()
    if whitelist:
        if model['id'] not in whitelist and model.get('model_name') not in whitelist:
            return None, ({'error': f'Model not in router list: {model_name}'}, 403)

    return model, None


def _build_request_body(model_config, messages, **kwargs):
    """Build the request body for the upstream LLM API."""
    body = {
        'model': model_config['model_name'],
        'messages': messages,
    }
    if 'temperature' in kwargs and kwargs['temperature'] is not None:
        body['temperature'] = kwargs['temperature']
    if 'max_tokens' in kwargs and kwargs['max_tokens'] is not None:
        body['max_tokens'] = kwargs['max_tokens']
    if 'stream' in kwargs:
        body['stream'] = kwargs['stream']
    return body


# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

def create_blueprint():

    # Initialize plugin config from installed models (if not already set)
    try:
        from .db import init_plugin_config
        init_plugin_config()
    except Exception:
        pass  # Non-critical, user can configure manually

    bp = Blueprint('model_router', __name__,
                    template_folder=os.path.join(PLUGIN_DIR, 'templates'))

    # =======================================================================
    # Consumer endpoints - /plugin/ prefix skips global session auth
    # Plugin handles its own authentication via Bearer token
    # =======================================================================

    @bp.route('/plugin/model-router/v1/chat/completions', methods=['POST'])
    def chat_completions():
        """OpenAI-compatible chat completion endpoint.

        Auth: Bearer token - validated inline.
        Routes directly to the LLM model specified by the model field.
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

        alias = data.get('model', '').strip()
        if not alias:
            return jsonify({'error': 'model field is required'}), 400

        # --- Resolve alias to actual model_name ---
        model_map = _get_model_model_map()
        actual_model_name = model_map.get(alias)
        if not actual_model_name:
            return jsonify({'error': f'Unknown model alias: {alias}'}), 400

        messages = data.get('messages', [])
        stream = data.get('stream', False)
        temperature = data.get('temperature')
        max_tokens = data.get('max_tokens')

        # --- Lookup model config ---
        model_config, error = _lookup_model(actual_model_name)
        if error:
            return jsonify({'error': f'Model not found in DB: {actual_model_name}'}), 400

        # --- Check model scope (against alias) ---
        if not TokenDB.token_can_access_model(token_row, alias):
            return jsonify({
                'error': f'Token not authorized for model: {alias}'
            }), 403

        # --- Validate messages ---
        if not messages:
            return jsonify({'error': 'messages array is required'}), 400

        # Check if there's any user/assistant content
        has_content = any(
            msg.get('content') for msg in messages
            if msg.get('role') in ('user', 'assistant', 'system')
        )
        if not has_content:
            return jsonify({'error': 'No content found in messages'}), 400

        # --- Call LLM ---
        start_time = time.time()

        if stream:
            return _stream_response(
                token_row, model_config, messages,
                alias, temperature, max_tokens
            )

        # Sync call via requests
        import requests
        url = f"{model_config['base_url']}/chat/completions"
        headers = {
            'Content-Type': 'application/json',
        }
        if model_config.get('api_key'):
            headers['Authorization'] = f"Bearer {model_config['api_key']}"

        body = _build_request_body(
            model_config, messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
        timeout = model_config.get('timeout', 60)

        try:
            resp = requests.post(url, json=body, headers=headers, timeout=timeout)
            resp.raise_for_status()
            resp_data = resp.json()
            duration_ms = int((time.time() - start_time) * 1000)
            usage = _extract_usage_from_response(resp_data)

            content = ''
            for choice in resp_data.get('choices', []):
                msg = choice.get('message', {})
                content = msg.get('content', '') or ''
                break

            # Increment quota + log
            token_db.increment_quota(token_row)
            token_db.log_usage(
                token_id=token_row['id'],
                model=alias,
                session_id=None,
                prompt_tokens=usage['prompt_tokens'],
                completion_tokens=usage['completion_tokens'],
                duration_ms=duration_ms,
            )

            return jsonify(_format_openai_response(content, alias, usage))

        except requests.exceptions.Timeout:
            return jsonify({'error': 'LLM API request timed out'}), 504
        except requests.exceptions.ConnectionError:
            return jsonify({'error': 'Cannot connect to LLM API'}), 502
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else 502
            detail = ''
            try:
                detail = e.response.json().get('error', {}).get('message', '')
            except Exception:
                detail = str(e)
            return jsonify({
                'error': f'LLM API error ({status_code}): {detail}'
            }), status_code
        except Exception as e:
            return jsonify({'error': f'LLM request failed: {str(e)}'}), 500

    def _stream_response(token_row, model_config, messages,
                         model_name, temperature, max_tokens):
        """Handle streaming SSE response via requests stream=True."""
        import requests

        url = f"{model_config['base_url']}/chat/completions"
        headers = {
            'Content-Type': 'application/json',
        }
        if model_config.get('api_key'):
            headers['Authorization'] = f"Bearer {model_config['api_key']}"

        body = _build_request_body(
            model_config, messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        timeout = model_config.get('timeout', 60)

        chat_id = f'chatcmpl-{uuid.uuid4().hex[:12]}'
        created = int(time.time())

        def generate():
            try:
                resp = requests.post(
                    url, json=body, headers=headers,
                    timeout=timeout, stream=True
                )
                resp.raise_for_status()

                content_sent = False
                total_prompt_tokens = 0
                total_completion_tokens = 0

                stream_start_time = time.time()
                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if line.startswith('data: '):
                        data_str = line[6:]
                        if data_str == '[DONE]':
                            break

                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        # Extract usage from the last chunk if present
                        if 'usage' in chunk:
                            u = chunk['usage']
                            total_prompt_tokens = u.get('prompt_tokens', 0)
                            total_completion_tokens = u.get('completion_tokens', 0)

                        for choice in chunk.get('choices', []):
                            delta = choice.get('delta', {})
                            delta_content = delta.get('content')
                            if delta_content:
                                chunk_obj = {
                                    'id': chat_id,
                                    'object': 'chat.completion.chunk',
                                    'created': created,
                                    'model': model_name,
                                    'choices': [{
                                        'index': choice.get('index', 0),
                                        'delta': {'content': delta_content},
                                        'finish_reason': None,
                                    }],
                                }
                                yield f"data: {json.dumps(chunk_obj)}\n\n"
                                content_sent = True

                # Send final [DONE] chunk
                final_chunk = {
                    'id': chat_id,
                    'object': 'chat.completion.chunk',
                    'created': created,
                    'model': model_name,
                    'choices': [{
                        'index': 0,
                        'delta': {},
                        'finish_reason': 'stop',
                    }],
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"

                # Increment quota after streaming completes
                duration_ms = int((time.time() - stream_start_time) * 1000)
                token_db.increment_quota(token_row)
                token_db.log_usage(
                    token_id=token_row['id'],
                    model=alias,
                    session_id=None,
                    prompt_tokens=total_prompt_tokens,
                    completion_tokens=total_completion_tokens,
                    duration_ms=0,
                )

            except Exception as e:
                error_chunk = {
                    'id': chat_id,
                    'object': 'chat.completion.chunk',
                    'created': created,
                    'model': model_name,
                    'choices': [{
                        'index': 0,
                        'delta': {'content': f'[Stream error: {str(e)}]'},
                        'finish_reason': 'stop',
                    }],
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"
                yield "data: [DONE]\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
            },
        )

    @bp.route('/plugin/model-router/v1/models', methods=['GET'])
    def list_models():
        """Return the list of available model aliases filtered by token scope.

        Lightweight auth: token validated but quota NOT consumed.
        """
        token_row, error = _validate_bearer_token()
        if error:
            body, status = error
            return jsonify(body), status

        model_map = _get_model_model_map()
        whitelist = _get_router_model_list()

        # Filter aliases by whitelist
        visible_aliases = []
        for alias in model_map:
            if whitelist and alias not in whitelist:
                continue
            visible_aliases.append(alias)

        models = [{
            'id': alias,
            'object': 'model',
            'created': 0,
            'owned_by': 'evonic',
        } for alias in visible_aliases]

        return jsonify({
            'object': 'list',
            'data': models,
        })

    # =======================================================================
    # Admin endpoints - /api/ prefix - session auth via global enforce_auth
    # =======================================================================

    @bp.route('/api/model-router/admin')
    def admin_dashboard():
        """Admin dashboard page for token management."""
        return render_template('admin.html')

    @bp.route('/api/model-router/admin/tokens', methods=['GET'])
    def admin_list_tokens():
        """List all tokens. Query params: ?status=active|suspended"""
        status = request.args.get('status', '').strip() or None
        tokens = token_db.list_tokens(status=status)
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

    @bp.route('/api/model-router/admin/tokens', methods=['POST'])
    def admin_create_token():
        """Create a new bearer token.

        Body: {"name": "...", "quota_limit": 1000, "expires_at": "...",
               "allowed_models": ["gpt-4"]}
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

        # Validate allowed_models if provided as list
        allowed_models = data.get('allowed_models', '*')
        if isinstance(allowed_models, list):
            # Don't validate against specific models - any model name is fine
            for m in allowed_models:
                if not isinstance(m, str) or not m.strip():
                    return jsonify({'error': 'allowed_models must be strings'}), 400

        # Validate expires_at if provided
        expires_at = data.get('expires_at')
        if expires_at:
            try:
                datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
            except (ValueError, TypeError):
                return jsonify({
                    'error': 'Invalid expires_at format. Use ISO 8601.'
                }), 400

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

    @bp.route('/api/model-router/admin/tokens/<int:token_id>', methods=['PUT'])
    def admin_update_token(token_id):
        """Update a token's mutable fields. All fields optional."""
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'error': 'Invalid JSON body'}), 400

        # Validate allowed_models if provided
        if 'allowed_models' in data:
            am = data['allowed_models']
            if isinstance(am, list):
                for m in am:
                    if not isinstance(m, str) or not m.strip():
                        return jsonify({
                            'error': 'allowed_models must be strings'
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
            return jsonify({
                'error': 'Token not found or no fields to update'
            }), 404

        row = token_db._get_by_id(token_id)
        if row:
            row['allowed_models'] = _parse_allowed_models(
                row.get('allowed_models'))

        return jsonify({'token': row})

    @bp.route('/api/model-router/admin/tokens/<int:token_id>',
              methods=['DELETE'])
    def admin_delete_token(token_id):
        """Delete a token permanently."""
        ok = token_db.delete_token(token_id)
        if not ok:
            return jsonify({'error': 'Token not found'}), 404
        return '', 204

    @bp.route('/api/model-router/admin/tokens/<int:token_id>/stats',
              methods=['GET'])
    def admin_token_stats(token_id):
        """Return detailed usage stats for a token."""
        stats = token_db.get_token_stats(token_id)
        if not stats:
            return jsonify({'error': 'Token not found'}), 404
        stats['allowed_models'] = _parse_allowed_models(
            stats.get('allowed_models'))
        return jsonify({'stats': stats})

    @bp.route('/api/model-router/admin/tokens/<int:token_id>/logs',
              methods=['GET'])
    def admin_token_logs(token_id):
        """Return usage logs for a token. Query params: ?limit=50&offset=0"""
        token = token_db._get_by_id(token_id)
        if not token:
            return jsonify({'error': 'Token not found'}), 404

        limit = request.args.get('limit', 50, type=int)
        offset = request.args.get('offset', 0, type=int)
        logs = token_db.get_usage_logs(token_id, limit=limit, offset=offset)

        return jsonify({
            'logs': logs,
            'total': token_db.get_usage_count(token_id),
            'limit': limit,
            'offset': offset,
        })

    @bp.route('/api/model-router/admin/tokens/<int:token_id>/reveal',
              methods=['GET'])
    def admin_reveal_token(token_id):
        """Return the plaintext token if cached (creation-time only)."""
        plaintext = _plaintext_cache.get(token_id)
        if not plaintext:
            return jsonify({
                'error': 'Plaintext token no longer available'
            }), 404
        return jsonify({'plaintext': plaintext})

    @bp.route('/api/model-router/admin/tokens/<int:token_id>/reset',
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

    @bp.route('/api/model-router/admin/models', methods=['GET'])
    def admin_models():
        """Return all enabled LLM models from the DB for the admin UI."""
        from models.db import db as _db
        models = _db.get_llm_models()
        # Sanitize: remove api_key
        for m in models:
            m.pop('api_key', None)
        return jsonify({'models': models})

    @bp.route('/api/model-router/admin/models', methods=['GET'])
    def admin_models():
        """Return all enabled LLM models from the DB for the admin UI."""
        from models.db import db as _db
        models = _db.get_llm_models()
        # Sanitize: remove api_key
        for m in models:
            m.pop('api_key', None)
        return jsonify({'models': models})

    @bp.route('/api/model-router/admin/model-model-map', methods=['GET'])
    def admin_model_model_map():
        """Return the current MODEL_MODEL_MAP for the admin UI."""
        return jsonify({'map': _get_model_model_map()})

    @bp.route('/api/model-router/admin/config', methods=['GET', 'PUT'])
    def admin_config():
        """Get or update plugin config."""
        if request.method == 'GET':
            whitelist = _get_router_model_list()
            return jsonify({
                'whitelist': whitelist,
                'whitelist_enabled': bool(whitelist),
            })
        
        # PUT: Update config
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'error': 'Invalid JSON body'}), 400
        
        # Validate MODEL_MODEL_MAP if provided
        if 'MODEL_MODEL_MAP' in data:
            try:
                model_map = json.loads(data['MODEL_MODEL_MAP'])
                if not isinstance(model_map, dict):
                    return jsonify({'error': 'MODEL_MODEL_MAP must be a JSON object'}), 400
                for k, v in model_map.items():
                    if not isinstance(k, str) or not isinstance(v, str):
                        return jsonify({'error': 'All keys and values in MODEL_MODEL_MAP must be strings'}), 400
                # Save as string
                data['MODEL_MODEL_MAP'] = json.dumps(model_map)
            except (json.JSONDecodeError, TypeError):
                return jsonify({'error': 'Invalid MODEL_MODEL_MAP format'}), 400
        
        # Save config
        try:
            # Import PluginManager to save config
            from backend.plugin_lifecycle import plugin_manager
            plugin_manager.save_plugin_config('model_router', data)
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'error': f'Failed to save config: {str(e)}'}), 500

    return bp
