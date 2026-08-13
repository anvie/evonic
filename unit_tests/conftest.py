"""
Pytest configuration and fixtures for unit tests.
Uses a separate test database to avoid polluting production data.
"""

import pytest
import os
import sys
import tempfile
import shutil
import threading as _threading

# Signal to app.py that we are running under test — skip the single-instance
# flock guard.  Must be set at module level before app.py is ever imported.
os.environ['EVONIC_TESTING'] = '1'

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

@pytest.fixture(autouse=True)
def use_test_database(monkeypatch, tmp_path):
    """
    Automatically use a temporary test database for all tests.
    This prevents unit tests from polluting the production database.
    """
    # Create a temporary database file
    test_db_path = str(tmp_path / "test_evonic.db")
    
    # Patch the database path before importing db
    from models import db as db_module
    
    # Store original path
    original_path = db_module.db.db_path

    # Close cached handles before redirecting every SQLite-backed global used
    # by Flask hooks and durable realtime state.
    db_module.db.close()
    import models.api_rate_limit as api_rate_limit
    import models.rate_limit as login_rate_limit
    from backend.realtime_store import realtime_store
    api_rate_limit.close()
    login_rate_limit.close()
    realtime_store.close()

    # Set test database path and clear cached connection so _connect() uses the new path
    db_module.db.db_path = test_db_path
    db_module.db._tls = _threading.local()
    monkeypatch.setattr(api_rate_limit, '_DB_PATH', str(tmp_path / 'api_rate_limit.db'))
    monkeypatch.setattr(login_rate_limit, '_RATE_LIMIT_DB', str(tmp_path / 'rate_limit.db'))

    # Reinitialize tables in test database
    db_module.db._init_tables()
    
    yield

    # Restore original path only after every temporary store has been closed.
    db_module.db.close()
    api_rate_limit.close()
    login_rate_limit.close()
    realtime_store.close()
    db_module.db._tls = _threading.local()
    db_module.db.db_path = original_path


@pytest.fixture(autouse=True)
def isolate_agent_dirs(monkeypatch, tmp_path):
    """Redirect agent file I/O to tmp_path so tests don't pollute agents/."""
    agents_tmp = str(tmp_path / 'agents')
    sub_tmp = str(tmp_path / 'evonic-sub-agents')
    monkeypatch.setattr('models.chat.AGENTS_DIR', agents_tmp)
    monkeypatch.setattr('models.chatlog._AGENTS_DIR', agents_tmp)
    monkeypatch.setattr('models.chat.SUB_AGENTS_TMP_DIR', sub_tmp)


@pytest.fixture(autouse=True)
def ensure_super_agent(use_test_database):
    """Create a super agent in the test DB so Flask API routes pass the setup check."""
    from models.db import db
    if not db.has_super_agent():
        db.create_agent({
            'id': 'test_super_agent',
            'name': 'Test Super Agent',
            'system_prompt': '',
            'is_super': True,
        })


@pytest.fixture(autouse=True)
def enable_testing_mode(use_test_database):
    """Set TESTING=True so the Werkzeug test client bypasses CSRF protection.

    flask.Flask.test_client() does NOT automatically set TESTING=True,
    so the CSRF before_request hook would block all POST/PUT/DELETE
    requests from unit tests.  This fixture fixes that.
    """
    from app import app
    app.config['TESTING'] = True
