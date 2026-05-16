import datetime
import json
import os
import secrets
import string
import uuid

import queue
import threading

from flask import Blueprint, Response, jsonify, render_template, request, stream_with_context

from models.db import db
from backend import connector_pair_rate_limit

workplaces_bp = Blueprint('workplaces', __name__)


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@workplaces_bp.route('/workplaces')
def workplaces_list():
    return render_template('workplaces.html')


@workplaces_bp.route('/workplaces/<workplace_id>')
def workplace_detail(workplace_id):
    workplace = db.get_workplace(workplace_id)
    if not workplace:
        return 'Workplace not found', 404
    return render_template('workplace_detail.html', workplace=workplace)


# ---------------------------------------------------------------------------
# API — Workplace CRUD
# ---------------------------------------------------------------------------

@workplaces_bp.route('/api/workplaces', methods=['GET'])
def api_list_workplaces():
    workplaces = db.get_workplaces()
    for w in workplaces:
        w['agents'] = db.get_workplace_agents(w['id'])
        w['agent_count'] = len(w['agents'])
        if w.get('type') == 'cloud':
            connector = db.get_connector_by_workplace(w['id'])
            w['connector'] = connector
    return jsonify(workplaces)


@workplaces_bp.route('/api/workplaces', methods=['POST'])
def api_create_workplace():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    workplace_type = data.get('type', '').strip()

    if not name:
        return jsonify({'error': 'name is required'}), 400
    if workplace_type not in ('local', 'remote', 'cloud'):
        return jsonify({'error': 'type must be local, remote, or cloud'}), 400

    config = data.get('config', {})
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except (json.JSONDecodeError, ValueError):
            return jsonify({'error': 'config must be valid JSON'}), 400

    workplace_id = db.create_workplace({'name': name, 'type': workplace_type, 'config': config})
    workplace = db.get_workplace(workplace_id)
    return jsonify(workplace), 201


@workplaces_bp.route('/api/workplaces/<workplace_id>', methods=['GET'])
def api_get_workplace(workplace_id):
    workplace = db.get_workplace(workplace_id)
    if not workplace:
        return jsonify({'error': 'Not found'}), 404
    workplace['agents'] = db.get_workplace_agents(workplace_id)
    workplace['agent_count'] = len(workplace['agents'])
    if workplace.get('type') == 'cloud':
        workplace['connector'] = db.get_connector_by_workplace(workplace_id)
    return jsonify(workplace)


@workplaces_bp.route('/api/workplaces/<workplace_id>', methods=['PUT'])
def api_update_workplace(workplace_id):
    workplace = db.get_workplace(workplace_id)
    if not workplace:
        return jsonify({'error': 'Not found'}), 404

    data = request.get_json() or {}
    updates = {}
    if 'name' in data:
        updates['name'] = (data['name'] or '').strip()
    if 'config' in data:
        cfg = data['config']
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except (json.JSONDecodeError, ValueError):
                return jsonify({'error': 'config must be valid JSON'}), 400
        updates['config'] = json.dumps(cfg)

    if updates:
        db.update_workplace(workplace_id, updates)
    return jsonify(db.get_workplace(workplace_id))


@workplaces_bp.route('/api/workplaces/<workplace_id>', methods=['DELETE'])
def api_delete_workplace(workplace_id):
    workplace = db.get_workplace(workplace_id)
    if not workplace:
        return jsonify({'error': 'Not found'}), 404

    agents = db.get_workplace_agents(workplace_id)
    if agents:
        names = ', '.join(a['name'] for a in agents[:3])
        extra = f' and {len(agents) - 3} more' if len(agents) > 3 else ''
        return jsonify({
            'error': f'Workplace is still assigned to agent(s): {names}{extra}. '
                     'Remove the workplace assignment from all agents first.'
        }), 409

    # Disconnect backend if running
    try:
        from backend.workplaces.manager import workplace_manager
        workplace_manager.disconnect(workplace_id)
    except Exception:
        pass

    db.delete_workplace(workplace_id)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# API — Workplace connection control
# ---------------------------------------------------------------------------

@workplaces_bp.route('/api/workplaces/<workplace_id>/status', methods=['GET'])
def api_workplace_status(workplace_id):
    workplace = db.get_workplace(workplace_id)
    if not workplace:
        return jsonify({'error': 'Not found'}), 404
    try:
        from backend.workplaces.manager import workplace_manager
        status = workplace_manager.get_status(workplace_id)
    except Exception as e:
        status = {'status': workplace.get('status', 'disconnected'), 'error': str(e)}
    return jsonify(status)


@workplaces_bp.route('/api/workplaces/<workplace_id>/events', methods=['GET'])
def api_workplace_events(workplace_id):
    """SSE stream for real-time workplace status changes (connector connect/disconnect, status)."""
    workplace = db.get_workplace(workplace_id)
    if not workplace:
        return jsonify({'error': 'Not found'}), 404

    q = queue.Queue(maxsize=20)

    _WATCHED = ('connector_connected', 'connector_disconnected', 'connector_paired', 'workplace_status_changed')

    def handler(data):
        if data.get('workplace_id') == workplace_id:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

    from backend.event_stream import event_stream
    for ev in _WATCHED:
        event_stream.on(ev, handler)

    @stream_with_context
    def generate():
        try:
            while True:
                try:
                    data = q.get(timeout=30)
                    yield f"event: {data['_event']}\ndata: {json.dumps(data)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            for ev in _WATCHED:
                event_stream.off(ev, handler)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


@workplaces_bp.route('/api/workplaces/<workplace_id>/connect', methods=['POST'])
def api_workplace_connect(workplace_id):
    workplace = db.get_workplace(workplace_id)
    if not workplace:
        return jsonify({'error': 'Not found'}), 404
    from backend.workplaces.manager import workplace_manager
    result = workplace_manager.connect(workplace_id)
    return jsonify(result)


@workplaces_bp.route('/api/workplaces/<workplace_id>/disconnect', methods=['POST'])
def api_workplace_disconnect(workplace_id):
    workplace = db.get_workplace(workplace_id)
    if not workplace:
        return jsonify({'error': 'Not found'}), 404
    from backend.workplaces.manager import workplace_manager
    result = workplace_manager.disconnect(workplace_id)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_pairing_code(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    # Avoid ambiguous chars
    alphabet = alphabet.replace('0', '').replace('O', '').replace('I', '').replace('1', '')
    return ''.join(secrets.choice(alphabet) for _ in range(length))


@workplaces_bp.route('/api/workplaces/<workplace_id>/pairing-code', methods=['POST'])
def api_generate_pairing_code(workplace_id):
    workplace = db.get_workplace(workplace_id)
    if not workplace:
        return jsonify({'error': 'Not found'}), 404
    if workplace.get('type') != 'cloud':
        return jsonify({'error': 'Pairing codes are only for cloud workplaces'}), 400

    import config as cfg
    ttl = getattr(cfg, 'CONNECTOR_PAIRING_CODE_TTL', 300)
    expires_at = (
        datetime.datetime.utcnow() + datetime.timedelta(seconds=ttl)
    ).strftime('%Y-%m-%dT%H:%M:%SZ')

    code = _generate_pairing_code()
    # Ensure uniqueness (retry up to 5 times)
    for _ in range(5):
        if not db.get_connector_by_pairing_code(code):
            break
        code = _generate_pairing_code()

    db.set_pairing_code(workplace_id, code, expires_at)

    import config as _cfg
    ws_port = getattr(_cfg, 'CONNECTOR_WS_PORT', 8081)
    return jsonify({
        'code': code,
        'expires_at': expires_at,
        'ttl_seconds': ttl,
        'ws_port': ws_port,
    })


@workplaces_bp.route('/api/workplaces/<workplace_id>/connector', methods=['DELETE'])
def api_unpair_connector(workplace_id):
    workplace = db.get_workplace(workplace_id)
    if not workplace:
        return jsonify({'error': 'Not found'}), 404
    if workplace.get('type') != 'cloud':
        return jsonify({'error': 'Only cloud workplaces have a connector'}), 400

    connector = db.get_connector_by_workplace(workplace_id)
    if not connector:
        return jsonify({'ok': True})  # already unpaired

    # Disconnect live WebSocket if connected
    try:
        from backend.workplaces.manager import workplace_manager
        workplace_manager.disconnect(workplace_id)
    except Exception:
        pass

    db.delete_connector(connector['id'])
    db.update_workplace_status(workplace_id, 'disconnected')
    return jsonify({'ok': True})


@workplaces_bp.route('/api/workplaces/<workplace_id>/pairing-code', methods=['DELETE'])
def api_cancel_pairing_code(workplace_id):
    workplace = db.get_workplace(workplace_id)
    if not workplace:
        return jsonify({'error': 'Not found'}), 404
    db.clear_pairing_code(workplace_id)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# API — Evonet connector pairing (called by the Evonet Go binary, no auth)
# ---------------------------------------------------------------------------

@workplaces_bp.route('/api/connector/pair', methods=['POST'])
def api_connector_pair():
    ip = request.remote_addr or '0.0.0.0'
    if connector_pair_rate_limit.is_rate_limited(ip):
        return jsonify({'error': 'Too many pairing attempts. Please try again later.'}), 429

    connector_pair_rate_limit.record_attempt(ip)

    data = request.get_json() or {}
    pairing_code = (data.get('pairing_code') or '').strip().upper()
    device_name = (data.get('device_name') or 'unknown').strip()
    platform = (data.get('platform') or 'unknown').strip()
    version = (data.get('version') or '').strip()

    if not pairing_code:
        return jsonify({'error': 'pairing_code is required'}), 400

    connector_token = secrets.token_urlsafe(32)
    connector = db.finalize_pairing(
        pairing_code=pairing_code,
        connector_token=connector_token,
        device_name=device_name,
        platform=platform,
        version=version,
    )
    if not connector:
        return jsonify({'error': 'Invalid or expired pairing code'}), 400

    workplace = db.get_workplace(connector['workplace_id'])
    import config as _cfg
    ws_port = getattr(_cfg, 'CONNECTOR_WS_PORT', 8081)

    # Emit SSE so the workplace detail page updates immediately without a refresh
    try:
        from backend.event_stream import event_stream
        event_stream.emit('connector_paired', {
            'workplace_id': connector['workplace_id'],
            'device_name': device_name,
            'platform': platform,
        })
    except Exception:
        pass

    # The actual token used may be the preserved master token, not the newly generated one
    actual_token = connector['connector_token']
    return jsonify({
        'ok': True,
        'connector_token': actual_token,
        'workplace_id': connector['workplace_id'],
        'workplace_name': workplace['name'] if workplace else '',
        'ws_port': ws_port,
    })


# ---------------------------------------------------------------------------
# API — Build Evonet binary with embedded config
# ---------------------------------------------------------------------------

# platform → pre-built binary filename (relative to evonet/dist/)
_PLATFORM_BINARIES = {
    'linux-amd64':   'evonet-linux-amd64',
    'darwin-arm64':  'evonet-darwin-arm64',
    'darwin-amd64':  'evonet-darwin-amd64',
    'windows-amd64': 'evonet-windows-amd64.exe',
}

_EVONET_MARKER = b'\x00\x00EVONET_CFG\x00\x00'


@workplaces_bp.route('/api/workplaces/<workplace_id>/binary-platforms', methods=['GET'])
def api_binary_platforms(workplace_id):
    """Return which platforms have a pre-built binary available."""
    import config as _cfg
    dist_dir = os.path.join(_cfg.BASE_DIR, 'evonet', 'dist')
    result = {}
    for platform, filename in _PLATFORM_BINARIES.items():
        result[platform] = os.path.isfile(os.path.join(dist_dir, filename))
    resp = jsonify(result)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@workplaces_bp.route('/api/workplaces/<workplace_id>/download-binary', methods=['POST'])
def api_download_binary(workplace_id):
    import config as _cfg

    workplace = db.get_workplace(workplace_id)
    if not workplace:
        return jsonify({'error': 'Not found'}), 404
    if workplace.get('type') != 'cloud':
        return jsonify({'error': 'Binary download is only for cloud workplaces'}), 400

    data = request.get_json() or {}
    platform = data.get('platform', 'linux-amd64')
    if platform not in _PLATFORM_BINARIES:
        return jsonify({'error': f'Unknown platform. Choose from: {", ".join(_PLATFORM_BINARIES)}'}), 400

    binary_filename = _PLATFORM_BINARIES[platform]
    dist_dir = os.path.join(_cfg.BASE_DIR, 'evonet', 'dist')
    binary_path = os.path.join(dist_dir, binary_filename)

    if not os.path.isfile(binary_path):
        return jsonify({
            'error': f'Pre-built binary not found for {platform}. '
                     f'Run "make build-all" in the evonet/ directory first.'
        }), 404

    # Ensure connector token exists — create one if not yet paired
    connector = db.get_connector_by_workplace(workplace_id)
    if connector and connector.get('connector_token'):
        token = connector['connector_token']
    else:
        token = 'ect_' + secrets.token_hex(32)
        if connector:
            db.update_connector(connector['id'], {'connector_token': token})
        else:
            db.create_connector({
                'id': uuid.uuid4().hex,
                'workplace_id': workplace_id,
                'connector_token': token,
            })

    workplace_cfg = {}
    raw_cfg = workplace.get('config')
    if raw_cfg:
        try:
            workplace_cfg = json.loads(raw_cfg) if isinstance(raw_cfg, str) else raw_cfg
        except (json.JSONDecodeError, TypeError):
            workplace_cfg = {}

    ws_port = getattr(_cfg, 'CONNECTOR_WS_PORT', 8081)
    # Detect real protocol robustly: ProxyFix already corrects request.scheme, but also
    # check Cloudflare-specific and common proxy headers as additional fallbacks.
    proto = request.scheme  # corrected by ProxyFix when behind a proxy
    if proto not in ('http', 'https'):
        proto = 'https'
    # Explicit header overrides (in priority order)
    for hdr in ('X-Forwarded-Proto', 'X-Real-Proto', 'CF-Visitor'):
        val = request.headers.get(hdr, '')
        if not val:
            continue
        # CF-Visitor is JSON: {"scheme":"https"}
        if hdr == 'CF-Visitor':
            try:
                import json as _j
                cf = _j.loads(val)
                val = cf.get('scheme', '')
            except Exception:
                val = ''
        # X-Forwarded-Proto may be comma-separated when there are multiple proxies
        val = val.split(',')[0].strip().lower()
        if val in ('http', 'https'):
            proto = val
            break
    server_url = f"{proto}://{request.host}"
    embedded_cfg = {
        'server_url': server_url,
        'connector_token': token,
        'workplace_id': workplace_id,
        'workplace_name': workplace['name'],
        'work_dir': workplace_cfg.get('workspace_path', ''),
        'ws_port': ws_port,
    }

    with open(binary_path, 'rb') as f:
        binary_data = f.read()

    binary_data += _EVONET_MARKER + json.dumps(embedded_cfg).encode()

    ext = '.exe' if platform == 'windows-amd64' else ''
    download_name = f'evonet-{platform}{ext}'
    return Response(
        binary_data,
        mimetype='application/octet-stream',
        headers={
            'Content-Disposition': f'attachment; filename="{download_name}"',
            'Content-Length': str(len(binary_data)),
            'Cache-Control': 'no-store, no-cache, must-revalidate',
            'Pragma': 'no-cache',
        }
    )
