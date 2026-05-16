"""Scheduler management routes — list, toggle, cancel, run-now, and create schedules."""

from flask import Blueprint, render_template, jsonify, request
from backend.scheduler import scheduler

scheduler_bp = Blueprint('scheduler', __name__)


@scheduler_bp.route('/scheduler')
def scheduler_page():
    return render_template('scheduler.html')


@scheduler_bp.route('/api/schedules')
def api_list_schedules():
    owner_type = request.args.get('owner_type')
    owner_id = request.args.get('owner_id')
    schedules = scheduler.list_schedules(owner_type=owner_type, owner_id=owner_id)
    return jsonify({'schedules': schedules})


@scheduler_bp.route('/api/schedules', methods=['POST'])
def api_create_schedule():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    required = ['name', 'trigger_type', 'trigger_config', 'action_type', 'action_config']
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({'error': f'Missing fields: {", ".join(missing)}'}), 400

    try:
        result = scheduler.create_schedule(
            name=data['name'],
            owner_type=data.get('owner_type', 'user'),
            owner_id=data.get('owner_id', 'admin'),
            trigger_type=data['trigger_type'],
            trigger_config=data['trigger_config'],
            action_type=data['action_type'],
            action_config=data['action_config'],
            max_runs=data.get('max_runs'),
            metadata=data.get('metadata'),
        )
        return jsonify({'schedule': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@scheduler_bp.route('/api/schedules/<schedule_id>', methods=['GET'])
def api_get_schedule(schedule_id):
    s = scheduler.get_schedule(schedule_id)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'schedule': s})


@scheduler_bp.route('/api/schedules/<schedule_id>/cancel', methods=['POST'])
def api_cancel_schedule(schedule_id):
    success = scheduler.cancel_schedule(schedule_id)
    if not success:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'success': True})


@scheduler_bp.route('/api/schedules/<schedule_id>/toggle', methods=['POST'])
def api_toggle_schedule(schedule_id):
    result = scheduler.toggle_schedule(schedule_id)
    if not result:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'schedule': result})


@scheduler_bp.route('/api/schedules/<schedule_id>/run-now', methods=['POST'])
def api_run_now(schedule_id):
    success = scheduler.run_now(schedule_id)
    if not success:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'success': True})


@scheduler_bp.route('/api/schedules/<schedule_id>/logs')
def api_schedule_logs(schedule_id):
    from models.db import db
    s = scheduler.get_schedule(schedule_id)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    try:
        limit = min(int(request.args.get('limit', 50)), 200)
        offset = int(request.args.get('offset', 0))
    except ValueError:
        return jsonify({'error': 'Invalid limit/offset'}), 400
    logs = db.get_schedule_logs(schedule_id, limit=limit, offset=offset)
    return jsonify({'logs': logs, 'schedule_id': schedule_id})


@scheduler_bp.route('/api/schedules/cleanup', methods=['POST'])
def api_cleanup_schedules():
    cleaned = scheduler.cleanup_once_schedules()
    return jsonify({'success': True, 'cleaned': cleaned})


@scheduler_bp.route('/api/schedules/<schedule_id>/logs', methods=['DELETE'])
def api_clear_schedule_logs(schedule_id):
    from models.db import db
    s = scheduler.get_schedule(schedule_id)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    deleted = db.delete_schedule_logs(schedule_id)
    return jsonify({'success': True, 'deleted': deleted})
