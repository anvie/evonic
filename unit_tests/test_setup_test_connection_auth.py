"""/api/setup/test-connection is exempt from the auth hook; it must lock after setup."""
from unittest.mock import patch

from flask import Flask

import routes.dashboard as dashboard


def _client():
    app = Flask(__name__)
    app.secret_key = "t"
    app.register_blueprint(dashboard.dashboard_bp)
    return app.test_client()


def _post(client):
    return client.post("/api/setup/test-connection", json={"base_url": "http://127.0.0.1:1"})


def test_allowed_before_setup():
    with patch.object(dashboard.db, "has_super_agent", return_value=False), \
         patch.object(dashboard, "test_connection", return_value={"success": True}) as tc:
        assert _post(_client()).status_code == 200
        tc.assert_called_once()


def test_rejected_after_setup_when_anonymous():
    with patch.object(dashboard.db, "has_super_agent", return_value=True), \
         patch.object(dashboard, "test_connection") as tc:
        assert _post(_client()).status_code == 401
        tc.assert_not_called()


def test_allowed_after_setup_when_authenticated():
    client = _client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    with patch.object(dashboard.db, "has_super_agent", return_value=True), \
         patch.object(dashboard, "test_connection", return_value={"success": True}):
        assert _post(client).status_code == 200
