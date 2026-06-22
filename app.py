import fcntl
import os
import secrets
import sys

from flask import jsonify, redirect, request, session, url_for
import re
import logging

from backend.dotenv_loader import load_dotenv
load_dotenv()

from backend.logging_config import configure as configure_logging
configure_logging()

# Set quiet modules from .env (in case configure() ran before load_dotenv()
# finished and missed them)
for _name in (os.environ.get("EVONIC_LOG_QUIET", "").split(",") + ["httpx", "telegram", "httpcore"]):
    _name = _name.strip()
    if _name:
        logging.getLogger(_name).setLevel(logging.ERROR)
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Single-instance guard: acquire exclusive flock on PID file.
# Prevents a second Evonic server process from starting when invoked
# directly via ``python app.py`` (bypassing the CLI guard).
#
# Skipped when EVONIC_TESTING=1 (set by unit_tests/conftest.py) because
# the guard relies on a process-level flock that is not compatible with
# pytest's module import/reload patterns.
# ---------------------------------------------------------------------------
if not os.environ.get('EVONIC_TESTING'):
    _APP_ROOT = os.path.dirname(os.path.abspath(__file__))
    _PID_FILE = os.path.join(_APP_ROOT, "shared", "run", "evonic.pid")
    _PID_DIR = os.path.dirname(_PID_FILE)

    os.makedirs(_PID_DIR, exist_ok=True)
    # If our launcher (the evonic CLI in foreground mode) already holds the
    # single-instance flock on this PID file, skip re-acquiring it: this code
    # runs in the SAME process as the CLI, so a second flock on a different fd
    # would self-conflict and abort startup. The launcher's lock already guards us.
    if os.environ.get("EVONIC_PID_LOCK_HELD") == "1":
        _lock_fd = None
    else:
        try:
            _lock_fd = os.open(_PID_FILE, os.O_CREAT | os.O_RDWR)
        except OSError as e:
            _log.critical("Could not open PID file %s: %s", _PID_FILE, e)
            sys.exit(1)

        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            # Another instance holds the lock — another app.py or CLI foreground
            # process is already running. Read the current PID for a friendly message.
            os.close(_lock_fd)
            try:
                with open(_PID_FILE) as _f:
                    _existing_pid = _f.read().strip()
            except Exception:
                _existing_pid = None
            msg = "Another Evonic server instance is already running"
            if _existing_pid:
                msg += f" (PID: {_existing_pid})"
            _log.critical(msg)
            print(f"\nError: {msg}")
            print("Use 'evonic stop' to stop the running server, then try again.")
            sys.exit(1)

    # Write our PID so ``evonic status`` / ``evonic stop`` can find us.
    # The flock fd stays open for the process lifetime — the OS releases the
    # lock automatically when this process exits.
    with open(_PID_FILE, "w") as _f:
        _f.write(str(os.getpid()))
# ---------------------------------------------------------------------------

from models.db import db
from routes.agents import agents_bp
from routes.auth import auth_bp
from routes.dashboard import dashboard_bp
from routes.evaluation import evaluation_bp
from routes.history import history_bp
from routes.sessions import sessions_bp
from routes.settings import settings_bp
from routes.skills import skills_bp
from routes.plugins import plugins_bp
from routes.scheduler import scheduler_bp
from routes.models import models_bp
from routes.health import health_bp
from routes.workplaces import workplaces_bp
from routes.logs import logs_bp
from routes.safety_rules import safety_rules_bp
from routes.update import update_bp
from routes.rtk import rtk_bp
from routes.realtime import realtime_bp
import config
from backend.version import get_version

from flask import Flask
from flask_sock import Sock

logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__)

# Add plugin template directories to Jinja loader
from jinja2 import ChoiceLoader, FileSystemLoader
from backend.plugin_lifecycle import PLUGINS_DIR
from pathlib import Path
_plugin_template_dirs = []
if Path(PLUGINS_DIR).exists():
    for plugin_dir in Path(PLUGINS_DIR).iterdir():
        tpl_dir = plugin_dir / 'templates'
        if tpl_dir.exists():
            _plugin_template_dirs.append(str(tpl_dir))
app.jinja_loader = ChoiceLoader([
    app.jinja_loader,
    FileSystemLoader(_plugin_template_dirs)
])
sock = Sock(app)
app.secret_key = config.SECRET_KEY

# Auto-reload templates on every request so plugin template changes are
# visible immediately without a server restart (especially useful during
# plugin development where templates are in non-standard directories).
app.config['TEMPLATES_AUTO_RELOAD'] = True

# Make session permanent so it survives mobile browser backgrounding / restarts
from datetime import timedelta
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = config.SESSION_COOKIE_SECURE

# Global upload size limit (defense-in-depth for all endpoints)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB

# Trust proxy headers from Cloudflare / nginx / any reverse proxy
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Response compression. COMPRESS_STREAMS must stay False so SSE endpoints
# (/api/realtime/stream, chat streams) are never buffered by compression.
from flask_compress import Compress
app.config['COMPRESS_STREAMS'] = False
Compress(app)

# ---------------------------------------------------------------------------
# API Rate Limiting (FINDING-004) — tiered, SQLite-backed
# ---------------------------------------------------------------------------
from models.api_rate_limit import (
    classify_request, check_rate_limit, TIERS,
    sse_register as _sse_register,
    sse_unregister as _sse_unregister,
    SSE_MAX_CONCURRENT as _SSE_MAX,
)
from flask import g as _g

# Skip rate limiting for these paths (login already has its own rate limiter)
_RATELIMIT_SKIP_PREFIXES = ('/login', '/logout')

@app.before_request
def _api_rate_limit_before():
    """Check rate limit before processing the request."""
    # Skip login/logout — already rate-limited in auth.py
    path = request.path
    if path.startswith(_RATELIMIT_SKIP_PREFIXES):
        return None

    # Classify the request into a tier
    tier = classify_request(path, request.method)
    if tier is None:
        return None  # no rate limit for this path

    # Build identifier: user ID if authenticated, else IP
    if session.get('authenticated'):
        identifier = f"user:{session.get('_user_id', 'admin')}"
    else:
        identifier = f"ip:{request.remote_addr or '0.0.0.0'}"

    # Check rate limit
    allowed, remaining, limit, retry_after = check_rate_limit(identifier, tier)

    # Store rate limit info on g for after_request
    _g._rate_limit_info = {
        'tier': tier,
        'limit': limit,
        'remaining': remaining,
        'retry_after': retry_after,
    }

    if not allowed:
        from flask import jsonify, make_response
        resp = make_response(jsonify({
            'error': 'rate_limit_exceeded',
            'message': f'Rate limit exceeded for {tier} tier. Try again in {retry_after}s.',
            'retry_after': retry_after,
        }), 429)
        resp.headers['Retry-After'] = str(retry_after)
        resp.headers['X-RateLimit-Limit'] = str(limit)
        resp.headers['X-RateLimit-Remaining'] = '0'
        resp.headers['X-RateLimit-Reset'] = str(int(__import__('time').time() + retry_after))
        return resp

    return None

@app.after_request
def _api_rate_limit_after(response):
    """Add rate limit headers to every API response."""
    info = getattr(_g, '_rate_limit_info', None)
    if info is None:
        return response

    limit = info['limit']
    if limit > 0:
        response.headers['X-RateLimit-Limit'] = str(limit)
        response.headers['X-RateLimit-Remaining'] = str(info['remaining'])
        # Reset time = now + retry_after; use time.time() not monotonic
        import time as _time
        response.headers['X-RateLimit-Reset'] = str(int(_time.time() + info['retry_after']))
    return response

app.register_blueprint(auth_bp)
app.register_blueprint(agents_bp)
app.register_blueprint(skills_bp)
app.register_blueprint(plugins_bp)
app.register_blueprint(scheduler_bp)
app.register_blueprint(evaluation_bp)
app.register_blueprint(history_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(sessions_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(models_bp)
app.register_blueprint(health_bp)
app.register_blueprint(workplaces_bp)
app.register_blueprint(logs_bp)
app.register_blueprint(safety_rules_bp)
app.register_blueprint(update_bp)
app.register_blueprint(rtk_bp)
app.register_blueprint(realtime_bp)


# ---- Backward-compatible redirect: /settings/* → /system/* ----
@app.route('/settings')
@app.route('/settings/<path:subpath>')
def redirect_settings_to_system(subpath=None):
    target = '/system' + ('/' + subpath if subpath else '')
    return redirect(target, code=301)


# Register plugin blueprints (routes from plugins)
from backend.plugin_manager import plugin_manager
for plugin_id, bp in plugin_manager.get_blueprints().items():
    app.register_blueprint(bp)

# Register injection guard for tool-level prompt injection detection
from backend.tools.injection_guard import injection_tool_guard
from backend.plugin_hooks import register_tool_guard
register_tool_guard(injection_tool_guard)

# Auto-download PROMPTPurify L5e ONNX model in background if missing
from backend.promptpurify.downloader import ensure_l5e_model
ensure_l5e_model()

# Display plugin and skill loading summary on startup
loaded_plugins = [p['id'] for p in plugin_manager.list_plugins() if plugin_manager._is_plugin_enabled(p['id'])]
loaded_skills = []
try:
    from backend.skills_manager import skills_manager
    for skill in skills_manager.list_skills():
        if skills_manager.is_skill_enabled(skill["id"]):
            loaded_skills.append(skill)
except Exception:
    pass

# Print Evonic version on startup
print(f"\n  🚀 Evonic v{get_version()}")
print("---------------------------------------------")
status = "enabled" if config.SESSION_ARCHIVE else "disabled"
print("  Session archive: %s" % status)

if loaded_plugins or loaded_skills:
    if loaded_plugins:
        print("\n")
        print("  ⚙️  %d Plugins Loaded:" % len(loaded_plugins))
        print("---------------------------------------------")
        for plugin_id in sorted(loaded_plugins):
            manifest = plugin_manager._read_manifest(plugin_id)
            name = manifest.get("name", plugin_id) if manifest else plugin_id
            version = manifest.get("version", "")
            status = "+ " if plugin_manager._is_plugin_enabled(plugin_id) else "[FAIL]"
            print("    %s %s (%s) %s" % (status, name, plugin_id, version))
    if loaded_skills:
        print("\n  📜 %d Skills Loaded:" % len(loaded_skills))
        print("---------------------------------------------")
        for skill in loaded_skills:
            tools_count = skill.get("tool_count", 0)
            print("    + %s (%s) — %d tool(s)" % (skill['name'], skill['id'], tools_count))

# Display agent count on startup
try:
    all_agents = db.get_agents()
    ready = [a for a in all_agents if a.get("enabled")]
    if ready:
        print("---------------------------------------------")
        print("\n  🤖 %d Agent%s ready for service\n" % (len(ready), 's' if len(ready) != 1 else ''))
except Exception:
    pass

# Initialize super agent notification subscriptions
from backend.super_agent_notifier import init_super_agent_notifier
init_super_agent_notifier()

# Start all enabled channels (Telegram bots, etc.) on boot.
# Guard against Werkzeug reloader's double-import: when debug+reloader is
# active, the module is imported in both the parent (reloader watcher) and the
# child (actual server). WERKZEUG_RUN_MAIN='true' is only set in the child.
import os as _os
_reloader_active = _os.environ.get('WERKZEUG_RUN_MAIN') is not None
_is_reloader_child = _os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
# Smoke-test import (used by the updater to validate a new tree before
# restarting): import all modules but skip channel/scheduler startup.
_smoke_test = _os.environ.get('EVONIC_SMOKE_TEST') == '1'
if (not _reloader_active or _is_reloader_child) and not _smoke_test:
    # Run SYSTEM.md migration eagerly (not lazily on first GET /api/agents).
    # Agents that predate the on-disk SYSTEM.md feature need their file written
    # before they start processing messages — otherwise read_file("/_self/SYSTEM.md")
    # returns "File not found".
    from routes.agents import _migrate_system_prompts
    _migrate_system_prompts()

    from backend.channels.registry import channel_manager
    channel_manager.start_all_enabled()

    # Register Tunnel Workplace connector WebSocket endpoint (served on main port via flask-sock)
    from backend.workplaces.connector_relay import connector_relay

    @sock.route('/ws/connector')
    def connector_ws(ws):
        connector_relay.handle_ws(ws, request)

    # Start global scheduler (loads persisted jobs from DB)
    from backend.scheduler import scheduler as global_scheduler
    global_scheduler.start()

    # Start periodic cleanup of expired rate-limit entries (login + API)
    from models.rate_limit import start_periodic_cleanup
    start_periodic_cleanup()
    from models.api_rate_limit import (
        start_periodic_cleanup as start_api_rate_cleanup,
        reset_sse_connections as _reset_sse_connections,
    )
    # Clear stale SSE connection counts left over from a previous (non-graceful)
    # shutdown — at boot there are zero live connections, so any persisted count
    # is stale and would otherwise wrongly reject new streams with 429.
    _reset_sse_connections()
    start_api_rate_cleanup()

    # If this boot was triggered by /restart, send "Evonic ready!" (no LLM)
    _restart_ready_flag = db.get_setting('restart_ready_needed')
    if _restart_ready_flag:
        import threading as _threading
        import json as _json

        def _send_restart_ready():
            import time as _time
            _time.sleep(5.0)  # Wait for channels + agent_runtime to fully initialize
            try:
                _data = _json.loads(_restart_ready_flag)
                _channel_id = _data.get('channel_id')
                _user_id = _data.get('external_user_id')
                _session_id = _data.get('session_id')
                _agent_id = _data.get('agent_id')
                _log.info("Sending 'Evonic ready!' (channel=%s, user=%s, session=%s)",
                           _channel_id, _user_id, _session_id)

                if _channel_id is not None:
                    # Messaging channel (Telegram, WhatsApp, etc.)
                    from backend.channels.registry import channel_manager
                    _channel = channel_manager.get_channel_instance(_channel_id)
                    if _channel:
                        _channel.send_message(_user_id, "Evonic ready!")
                        _log.info("'Evonic ready!' sent via channel %s", _channel_id)
                    else:
                        _log.warning("Channel %s not found, cannot send restart ready message",
                                     _channel_id)
                elif _session_id and _agent_id:
                    # Web chat — inject directly as system message (no LLM)
                    # Write to SQLite DB so it appears in chat history
                    db.add_chat_message(_session_id, 'system', 'Evonic ready!',
                                        agent_id=_agent_id, metadata={'restart_ready': True})
                    # Write to JSONL chatlog so it appears when polling
                    from models.chatlog import chatlog_manager
                    _cl = chatlog_manager.get(_agent_id, _session_id)
                    _cl.append({'type': 'system', 'session_id': _session_id,
                                'content': 'Evonic ready!',
                                'metadata': {'restart_ready': True}})
                    # Emit SSE event for any reconnected clients
                    from backend.event_stream import event_stream
                    event_stream.emit('message_received', {
                        'agent_id': _agent_id,
                        'session_id': _session_id,
                        'external_user_id': _user_id,
                        'channel_id': None,
                        'message': 'Evonic ready!',
                    })
                    _log.info("'Evonic ready!' sent via web chat (session=%s)", _session_id)
                else:
                    _log.warning("No channel_id or session_id available, cannot send restart ready message")

                db.set_setting('restart_ready_needed', '')
                _log.info("Restart ready flag cleared")

            except Exception as _e:
                _log.error("Failed to send restart ready message: %s", _e, exc_info=True)

        _threading.Thread(target=_send_restart_ready, daemon=True).start()

    # If this boot was triggered by restart tool, send LLM greeting with context
    _restart_greeting_flag = db.get_setting('restart_greeting_needed')
    if _restart_greeting_flag:
        import threading as _threading
        import json as _json

        def _send_restart_greeting():
            import time as _time
            _time.sleep(5.0)  # Wait for channels + agent_runtime to fully initialize
            try:
                _data = _json.loads(_restart_greeting_flag)
                _channel_id = _data.get('channel_id')
                _user_id = _data.get('external_user_id')
                _context = _data.get('context', '')
                _log.info("Sending restart greeting (channel=%s, user=%s, context_len=%d)",
                           _channel_id, _user_id, len(_context))

                _super_agent = db.get_super_agent()
                if not _super_agent:
                    _log.warning("No super agent found, skipping greeting")
                    return

                _trigger_msg = '[SYSTEM] Restart greeting needed\n'
                if _context and _context.strip():
                    _trigger_msg += f'\n<restart_context>\n{_context}\n</restart_context>\n'

                from backend.agent_runtime import agent_runtime
                agent_runtime.handle_message(
                    agent_id=_super_agent['id'],
                    external_user_id=_user_id,
                    message=_trigger_msg,
                    channel_id=_channel_id,
                )

                db.set_setting('restart_greeting_needed', '')
                _log.info("Restart greeting sent, flag cleared")

            except Exception as _e:
                _log.error("Failed to send restart greeting: %s", _e, exc_info=True)

        _threading.Thread(target=_send_restart_greeting, daemon=True).start()

    # ----------------------------------------------------------------
    # Startup check: scan all active sessions for unreplied user messages
    # ----------------------------------------------------------------
    def _check_unreplied_chats():
        """Scan human-facing chat sessions on startup. Log any session where the
        last message is from a user and no agent has replied — these users may
        have been left hanging after a restart or deployment.

        Only scans sessions between a human and an agent (web UI or channels).
        Skips agent-to-agent, scheduler, and system notification sessions.
        """
        import time as _time
        _time.sleep(3.0)  # brief delay for DB + agent_runtime to be ready

        from models.chatlog import ChatLog
        from models.chat import is_human_facing_external_user_id
        _unreplied_types = frozenset({'user', 'final', 'intermediate', 'error', 'system'})

        try:
            _all_agents = db.get_agents()
            _enabled = [a for a in _all_agents if a.get('enabled')]
        except Exception:
            _log.warning("Unreplied-chat check: could not list agents, skipping.")
            return

        _unreplied_count = 0
        _total_sessions = 0
        _pending = []  # (agent_dict, session_id, external_user_id, channel_id)

        for _agent in _enabled:
            if _agent.get('is_subagent'):
                continue
            _agent_id = _agent['id']
            _agent_name = _agent.get('name', _agent_id)
            try:
                _sessions = db._chat_db(_agent_id).get_sessions_with_preview()
            except Exception:
                continue

            for _sess in _sessions:
                if not is_human_facing_external_user_id(_sess.get('external_user_id', '')):
                    continue
                _total_sessions += 1
                _session_id = _sess['id']
                try:
                    with ChatLog(_agent_id, _session_id) as _clog:
                        _last = _clog.get_last_entry(types=_unreplied_types)
                except Exception:
                    continue

                if _last is None:
                    continue  # empty session, skip
                if _last.get('type') != 'user':
                    continue  # last message is agent/system — already replied

                # Slash commands (e.g. /autopilot on, /clear) are system/control
                # instructions that don't require an agent reply — skip them.
                _content = (_last.get('content') or '').strip()
                if _content.startswith('/'):
                    continue

                _unreplied_count += 1
                _ts = _last.get('ts', 0)
                _ts_str = _time.strftime(
                    '%Y-%m-%d %H:%M:%S', _time.localtime(_ts / 1000)
                ) if _ts else 'unknown'
                _preview = (_last.get('content') or '')[:120]

                _log.warning(
                    "Unreplied chat — agent=%s session=%s user_msg_at=%s preview=%r",
                    _agent_name, _session_id, _ts_str, _preview
                )
                _pending.append((_agent, _session_id, _sess.get('external_user_id', ''),
                                 _sess.get('channel_id')))

        if _unreplied_count:
            _log.warning(
                "Unreplied-chat scan complete: %d/%d human session(s) have no agent reply.",
                _unreplied_count, _total_sessions,
            )
            # Re-enqueue unreplied sessions so agents follow up
            from backend.agent_runtime import agent_runtime
            for _agent, _sid, _ext_uid, _ch_id in _pending:
                try:
                    agent_runtime.resume_session(_agent, _sid, _ext_uid, _ch_id)
                    _log.info("Resumed unreplied session %s for agent %s",
                              _sid, _agent.get('name', _agent['id']))
                except Exception as _e:
                    _log.error("Failed to resume session %s: %s", _sid, _e)
        else:
            _log.info(
                "Unreplied-chat scan complete: all %d human session(s) have replies.",
                _total_sessions,
            )

    import threading as _threading
    _threading.Thread(target=_check_unreplied_chats, daemon=True).start()


@app.context_processor
def inject_config():
    return {'config': config}


@app.context_processor
def inject_plugin_nav():
    return {'plugin_nav_items': plugin_manager.get_nav_items()}


@app.context_processor
def inject_version():
    return {'evonic_version': get_version()}


@app.teardown_request
def _close_db_connection(exc):
    """Close thread-local DB connection after each request.

    Flask's dev server creates a new thread per request. Without this,
    SQLite connections accumulate until GC runs → "Too many open files".
    """
    db.close()

def _csrf_exempt(path):
    """Return True if the path should skip CSRF validation."""
    if path in ('/login', '/logout', '/setup',
                '/api/health', '/api/connector/pair',
                '/api/setup', '/api/setup/test-connection', '/api/setup/docker-status'):
        return True
    if path.startswith(('/static/', '/webhook', '/plugin/', '/ws/',
                        '/api/channels/whatsapp-bridge/')):
        return True
    if path.endswith('/download-binary') and path.startswith('/api/workplaces/'):
        return True
    return False


@app.before_request
def csrf_protect():
    """Double-submit cookie CSRF protection for all state-changing requests."""
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    if _csrf_exempt(request.path):
        return None
    if app.testing or os.environ.get('PYTEST_CURRENT_TEST'):
        return None

    token_header = request.headers.get('X-CSRF-Token', '')
    token_form = request.form.get('csrf_token', '')
    submitted = token_header or token_form
    cookie_token = request.cookies.get('csrf_token', '')

    if not submitted or not cookie_token:
        return jsonify({'error': 'CSRF token missing'}), 403
    if not secrets.compare_digest(submitted, cookie_token):
        return jsonify({'error': 'CSRF token mismatch'}), 403
    return None


@app.after_request
def set_static_cache_headers(response):
    """Long-lived caching for /static/ assets.

    Safe because every static reference carries a `?v=N` cache-buster —
    bump the version when an asset changes. Scoped to /static/ only so
    dynamic responses (e.g. agent avatars) are never cached.
    """
    if request.path.startswith('/static/') and not config.DEBUG:
        response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
    return response


@app.after_request
def set_csrf_cookie(response):
    """Ensure csrf_token cookie exists on every response (for login page, first visit, etc.)."""
    if 'csrf_token' not in request.cookies:
        token = secrets.token_hex(32)
        response.set_cookie(
            'csrf_token', token,
            httponly=False,
            samesite='Lax',
            secure=not config.FORCE_INSECURE_COOKIES,
            path='/',
            max_age=604800,
        )
    return response


# Set-once cache for the super-agent existence check (queried on every request).
# Safe because a super agent can never be deleted or disabled once created.
_super_agent_exists = False


@app.before_request
def enforce_auth():
    """Enforce authentication on all API endpoints and page routes.

    Always-accessible: health, connector pairing, WebSocket, static files,
    auth routes (login/logout), and setup flow when no super agent exists.
    """
    # Always-accessible endpoints (no auth required)
    if request.path == '/api/health':
        return None
    if request.path.startswith('/api/channels/whatsapp-bridge/'):
        return None  # Baileys sidecar calls this from localhost
    if request.path == '/api/connector/pair':
        return None  # Evonet pairing is unauthenticated (uses pairing code)
    if request.path.endswith('/download-binary') and request.path.startswith('/api/workplaces/'):
        return None  # Evonet binary download is unauthenticated (uses embedded connector_token)
    if request.path == '/ws/connector':
        return None  # Evonet connector authenticates via Bearer token, not session
    if request.path.startswith('/static/'):
        return None
    if request.path.startswith('/webhook'):
        return None  # Plugin webhook endpoints handle their own auth
    if request.path.startswith('/plugin/'):
        return None  # Plugin routes handle their own auth internally
    if request.path in ('/login', '/logout'):
        return None

    # Setup endpoints handle their own auth/state validation — always allow them
    if request.path == '/setup':
        return None
    if request.path in ('/api/setup', '/api/setup/test-connection', '/api/setup/docker-status'):
        return None

    # --- Setup flow: when no super agent exists, redirect everything else ---
    global _super_agent_exists
    if not _super_agent_exists:
        _super_agent_exists = db.has_super_agent()
    if not _super_agent_exists:
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Super agent setup required', 'setup_required': True}), 503
        return redirect('/setup')

    # --- Normal auth enforcement (super agent exists) ---
    if session.get('authenticated'):
        return None
    # Allow public history access if enabled (read-only routes only)
    if db.get_setting('public_history', '0') == '1':
        if request.method == 'GET' and (
            request.path == '/history'
            or re.match(r'^/history/\d+$', request.path)
            or request.path.startswith('/api/v1/history/')
            or re.match(r'^/api/run/\d+/(matrix|tests/)', request.path)
        ):
            return None
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Authentication required'}), 401
    return redirect(url_for('auth.login_page', next=request.path))


if __name__ == '__main__':
#    from models.db import db as _db
#    _dm = _db.get_default_model()
#    if _dm:
#        _log.info("LLM Base URL : %s", _dm.get('base_url'))
#        _log.info("LLM Model    : %s", _dm.get('model_name'))
#        _key = _dm.get('api_key', '')
#        masked_key = (_key[:8] + '...' + _key[-4:]) if len(_key) > 12 else ('***' if _key else '(not set)')
#        _log.info("LLM API Key  : %s", masked_key)
#    else:
#        _log.info("LLM Model    : No default model configured in database")
    app.run(
        host=config.HOST,
        port=config.PORT,
        debug=config.DEBUG,
        use_reloader=False,  # Disable reloader to prevent killing evaluation thread
        threaded=True  # Serve requests concurrently; otherwise long-lived SSE
                       # streams block all other requests (e.g. image/avatar serving)
    )
