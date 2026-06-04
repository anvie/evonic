"""Health check endpoint — used by the update supervisor for post-deploy validation."""
import time
import os
import shutil

from flask import Blueprint, jsonify

health_bp = Blueprint('health', __name__)

_start_time = time.time()


def _get_version() -> str:
    version_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'VERSION')
    try:
        with open(version_file) as f:
            return f.read().strip()
    except FileNotFoundError:
        return 'dev'


def _check_db() -> bool:
    try:
        from models.db import db
        db.get_agents()
        return True
    except Exception:
        return False


def _check_disk() -> dict:
    try:
        import config
        usage = shutil.disk_usage(config.BASE_DIR)
        return {
            'total_gb': round(usage.total / (1024**3), 1),
            'free_gb': round(usage.free / (1024**3), 1),
            'free_pct': round(usage.free / usage.total * 100, 1),
        }
    except Exception:
        return {}


def _check_docker() -> str:
    try:
        import subprocess
        result = subprocess.run(
            ['docker', 'info', '--format', '{{.ServerVersion}}'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return 'unavailable'
    except Exception:
        return 'unknown'


@health_bp.route('/api/health')
def health():
    db_ok = _check_db()
    return jsonify({
        'status': 'ok' if db_ok else 'degraded',
        'uptime': round(time.time() - _start_time, 1),
        'version': _get_version(),
        'checks': {
            'database': 'ok' if db_ok else 'error',
            'disk': _check_disk(),
            'docker': _check_docker(),
        },
    })
