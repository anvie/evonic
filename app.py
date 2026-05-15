import os

from flask import jsonify, redirect, request, session, url_for
import re
import logging

from dotenv import load_dotenv
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
import config
from backend.version import get_version

from flask import Flask
from flask_sock import Sock

logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__)
sock = Sock(app)
app.secret_key = config.SECRET_KEY

# Global upload size limit (defense-in-depth for all endpoints)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB

# Trust proxy headers from Cloudflare / nginx / any reverse proxy
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

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
if not _reloader_active or _is_reloader_child:
    from backend.channels.registry import channel_manager
    channel_manager.start_all_enabled()

    # Register Cloud Workplace connector WebSocket endpoint (served on main port via flask-sock)
    from backend.workplaces.connector_relay import connector_relay

    @sock.route('/ws/connector')
    def connector_ws(ws):
        connector_relay.handle_ws(ws, request)

    # Start global scheduler (loads persisted jobs from DB)
    from backend.scheduler import scheduler as global_scheduler
    global_scheduler.start()

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
        _unreplied_types = frozenset({'user', 'final', 'intermediate', 'error'})

        try:
            _all_agents = db.get_agents()
            _enabled = [a for a in _all_agents if a.get('enabled')]
        except Exception:
            _log.warning("Unreplied-chat check: could not list agents, skipping.")
            return

        _unreplied_count = 0
        _total_sessions = 0

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

        if _unreplied_count:
            _log.warning(
                "Unreplied-chat scan complete: %d/%d human session(s) have no agent reply.",
                _unreplied_count, _total_sessions,
            )
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
    if request.path == '/ws/connector':
        return None  # Evonet connector authenticates via Bearer token, not session
    if request.path.startswith('/static/'):
        return None
    if request.path.startswith('/webhook'):
        return None  # Plugin webhook endpoints handle their own auth
    if request.path in ('/login', '/logout'):
        return None

    # --- Setup flow: when no super agent exists, allow setup endpoints ---
    if not db.has_super_agent():
        if request.path == '/setup':
            return None
        if request.path in ('/api/setup', '/api/setup/test-connection', '/api/setup/docker-status'):
            return None
        # All other requests redirect to setup
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
        use_reloader=False  # Disable reloader to prevent killing evaluation thread
    )
