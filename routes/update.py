"""Routes for the system update UI and API."""

from flask import Blueprint, jsonify, render_template, request, redirect

from backend import update_manager

update_bp = Blueprint('update', __name__)


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------

@update_bp.route('/system/update')
def update_page():
    return render_template('update.html')


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@update_bp.route('/api/system/update/check', methods=['GET'])
def api_update_check():
    force = request.args.get('force', '').lower() in ('1', 'true')
    result = update_manager.check_for_update(force=force)
    return jsonify(result)


@update_bp.route('/api/system/update/status', methods=['GET'])
def api_update_status():
    return jsonify(update_manager.get_status())


@update_bp.route('/api/system/update/start', methods=['POST'])
def api_update_start():
    data = request.get_json(silent=True) or {}
    tag = data.get('tag')
    result = update_manager.start_update(tag=tag)
    if 'error' in result:
        return jsonify(result), 409
    return jsonify(result)


@update_bp.route('/api/system/update/rollback', methods=['POST'])
def api_update_rollback():
    result = update_manager.trigger_rollback()
    if 'error' in result:
        return jsonify(result), 409
    return jsonify(result)


@update_bp.route('/api/system/update/restart', methods=['POST'])
def api_update_restart():
    result = update_manager.trigger_restart()
    return jsonify(result)


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------

@update_bp.route('/api/system/update/stream', methods=['GET'])
def api_update_stream():
    """SSE endpoint for real-time update progress.
    DEPRECATED: Use unified GET /api/realtime/stream?channels=update instead."""
    return redirect(
        '/api/realtime/stream?channels=update&legacy=update&snapshot=1',
        code=307,
    )
