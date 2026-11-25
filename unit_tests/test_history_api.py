"""
Unit tests for V1 History API endpoints.
"""

import pytest
import json
import sys
import os
import uuid

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.db import db


def _auth(client):
    """Authenticate the test client session."""
    with client.session_transaction() as sess:
        sess['authenticated'] = True
    return client


class TestHistoryAPILastId:
    """Test /api/v1/history/last/id endpoint"""
    
    def test_returns_last_run_id(self):
        """Test that endpoint returns the most recent run ID"""
        from app import app
        
        # Create a test run
        run_id = db.create_evaluation_run("test-model-history")
        
        with app.test_client() as client:
            _auth(client)
            response = client.get('/api/v1/history/last/id')
            assert response.status_code == 200

            data = response.get_json()
            assert "run_id" in data
            assert "model_name" in data
            assert "started_at" in data
            assert "status" in data

    def test_returns_404_when_no_runs(self):
        """Test 404 when no runs exist (edge case)"""
        # This test assumes there might be runs in the DB
        # In a clean DB, it would return 404
        from app import app

        with app.test_client() as client:
            _auth(client)
            response = client.get('/api/v1/history/last/id')
            # Either 200 (runs exist) or 404 (no runs)
            assert response.status_code in [200, 404]


class TestHistoryAPIRunDomainLevel:
    """Test /api/v1/history/<run_id>/<domain>/<level> endpoint"""
    
    def test_returns_test_results(self):
        """Test endpoint returns test results with full details"""
        from app import app
        
        # Create a test run and result
        run_id = db.create_evaluation_run("test-model")
        
        # Save test result with full details
        details = {
            "evaluator": "tool_call",
            "called_tools": ["get_current_date"],
            "thinking": "Let me check the date...",
            "tools_available": [
                {"name": "get_current_date", "description": "Get current date"}
            ],
            "conversation_log": [
                {
                    "turn": 1,
                    "thinking": "I need to get the date",
                    "tool_calls": [{"name": "get_current_date", "arguments": {}}],
                    "tool_results": [{"result": {"date": "2026-04-04"}}]
                }
            ]
        }
        
        db.save_individual_test_result(
            run_id=run_id,
            test_id="test_history_1",
            domain="tool_calling",
            level=1,
            prompt="What is today's date?",
            response="Today is April 4th, 2026",
            expected='{"tools": ["get_current_date"]}',
            score=1.0,
            status="passed",
            details=json.dumps(details),
            duration_ms=500,
            model_name="test-model"
        )
        
        with app.test_client() as client:
            _auth(client)
            response = client.get(f'/api/v1/history/{run_id}/tool_calling/1')
            assert response.status_code == 200
            
            data = response.get_json()
            assert data["run_id"] == run_id
            assert data["domain"] == "tool_calling"
            assert data["level"] == 1
            assert len(data["tests"]) == 1
            
            test = data["tests"][0]
            assert test["test_id"] == "test_history_1"
            assert test["score"] == 1.0
            assert test["status"] == "passed"
            
            # Check details are parsed
            assert isinstance(test["details"], dict)
            assert "conversation_log" in test["details"]
            assert "tools_available" in test["details"]
            assert len(test["details"]["conversation_log"]) == 1
    
    def test_returns_404_for_invalid_run(self):
        """Test 404 for non-existent run ID"""
        from app import app

        with app.test_client() as client:
            _auth(client)
            response = client.get('/api/v1/history/99999999/math/1')
            assert response.status_code == 404

            data = response.get_json()
            assert "error" in data
    
    def test_returns_empty_tests_for_no_results(self):
        """Test empty tests array when no results for domain/level"""
        from app import app
        
        # Create a run but don't add results for specific domain
        run_id = db.create_evaluation_run("test-model")
        
        with app.test_client() as client:
            _auth(client)
            response = client.get(f'/api/v1/history/{run_id}/nonexistent_domain/1')
            assert response.status_code == 200
            
            data = response.get_json()
            assert data["tests"] == []


class TestHistoryAPILastDomainLevel:
    """Test /api/v1/history/last/<domain>/<level> endpoint"""
    
    def test_returns_last_run_results(self):
        """Test endpoint returns results from most recent run"""
        from app import app
        
        # Create test run and result
        run_id = db.create_evaluation_run("test-model-last")
        
        db.save_individual_test_result(
            run_id=run_id,
            test_id="test_last_1",
            domain="math",
            level=2,
            prompt="What is 5 + 3?",
            response="8",
            expected='{"answer": 8}',
            score=1.0,
            status="passed",
            details='{"evaluator": "two_pass"}',
            duration_ms=100,
            model_name="test-model-last"
        )
        
        with app.test_client() as client:
            _auth(client)
            response = client.get('/api/v1/history/last/math/2')
            assert response.status_code == 200
            
            data = response.get_json()
            assert "run_id" in data
            assert data["domain"] == "math"
            assert data["level"] == 2
            assert "tests" in data
    
    def test_returns_empty_for_no_results(self):
        """Test returns empty tests when no results for domain/level"""
        from app import app
        
        # Ensure there's at least one run
        db.create_evaluation_run("test-model")
        
        with app.test_client() as client:
            _auth(client)
            response = client.get('/api/v1/history/last/nonexistent/5')
            # Should return 200 with empty tests (not 404)
            assert response.status_code == 200
            
            data = response.get_json()
            assert data["tests"] == []


class TestDatabaseLastRun:
    """Test database methods for last run"""
    
    def test_get_last_run(self):
        """Test get_last_run returns most recent run"""
        # Create multiple runs
        run1 = db.create_evaluation_run("model-1")
        run2 = db.create_evaluation_run("model-2")
        
        last_run = db.get_last_run()
        assert last_run is not None
        assert last_run["run_id"] == run2
    
    def test_get_last_run_id(self):
        """Test get_last_run_id returns ID string"""
        run_id = db.create_evaluation_run("model-test")
        
        last_id = db.get_last_run_id()
        assert last_id is not None
        assert isinstance(last_id, int)
        assert last_id == run_id


class TestHistoryAPIModelNameEdit:
    """Test PATCH /api/run/<run_id>/model-name endpoint"""

    def test_updates_model_name(self):
        """Renaming a run persists the new model name and cascades to results"""
        from app import app

        run_id = db.create_evaluation_run("old-model")
        db.save_individual_test_result(
            run_id=run_id,
            test_id="test_rename_1",
            domain="math",
            level=1,
            prompt="2+2?",
            response="4",
            expected='{"answer": 4}',
            score=1.0,
            status="passed",
            details="{}",
            duration_ms=10,
            model_name="old-model"
        )

        with app.test_client() as client:
            _auth(client)
            response = client.patch(
                f"/api/run/{run_id}/model-name",
                json={"model_name": "new-model"}
            )
            assert response.status_code == 200
            data = response.get_json()
            assert data["success"] is True
            assert data["model_name"] == "new-model"

        # Persisted on the run...
        run_info = db.get_evaluation_run(run_id)
        assert run_info["model_name"] == "new-model"

        # ...and cascaded to the stored individual results
        results = db.get_individual_test_results(run_id, "math", 1)
        assert results and results[0]["model_name"] == "new-model"

    def test_trims_whitespace(self):
        """Surrounding whitespace is stripped before saving"""
        from app import app

        run_id = db.create_evaluation_run("model-x")
        with app.test_client() as client:
            _auth(client)
            response = client.patch(
                f"/api/run/{run_id}/model-name",
                json={"model_name": "  spaced-model  "}
            )
            assert response.status_code == 200
            assert response.get_json()["model_name"] == "spaced-model"

        assert db.get_evaluation_run(run_id)["model_name"] == "spaced-model"

    def test_rejects_empty_model_name(self):
        """An empty or whitespace-only name is rejected with 400"""
        from app import app

        run_id = db.create_evaluation_run("model-keep")
        with app.test_client() as client:
            _auth(client)
            for payload in ({"model_name": ""}, {"model_name": "   "}):
                response = client.patch(
                    f"/api/run/{run_id}/model-name",
                    json=payload
                )
                assert response.status_code == 400
                assert "error" in response.get_json()

        # Unchanged
        assert db.get_evaluation_run(run_id)["model_name"] == "model-keep"

    def test_rejects_missing_field(self):
        """A payload without model_name is rejected with 400"""
        from app import app

        run_id = db.create_evaluation_run("model-keep-2")
        with app.test_client() as client:
            _auth(client)
            response = client.patch(f"/api/run/{run_id}/model-name", json={})
            assert response.status_code == 400

    def test_rejects_too_long_model_name(self):
        """Model names longer than 200 characters are rejected"""
        from app import app

        run_id = db.create_evaluation_run("model-len")
        with app.test_client() as client:
            _auth(client)
            response = client.patch(
                f"/api/run/{run_id}/model-name",
                json={"model_name": "x" * 201}
            )
            assert response.status_code == 400

    def test_returns_404_for_unknown_run(self):
        """Renaming a non-existent run returns 404"""
        from app import app

        with app.test_client() as client:
            _auth(client)
            response = client.patch(
                "/api/run/99999999/model-name",
                json={"model_name": "whatever"}
            )
            assert response.status_code == 404


class TestHistoryDetailModelEditUI:
    """Test the model-name edit control on the /history/<id> detail page"""

    def test_renders_edit_control_when_authenticated(self):
        """Authenticated users see an editable model name control"""
        from app import app

        run_id = db.create_evaluation_run("ui-model")
        with app.test_client() as client:
            _auth(client)
            response = client.get(f"/history/{run_id}")
            assert response.status_code == 200
            html = response.get_data(as_text=True)
            assert 'id="run-model-edit-btn"' in html
            assert 'id="run-model-input"' in html
            assert 'id="run-model-save-btn"' in html
            assert f"/api/run/{run_id}/model-name" in html
            assert "ui-model" in html

    def test_hides_edit_control_when_not_authenticated(self):
        """Unauthenticated visitors only see the read-only model value"""
        from app import app

        db.set_setting("public_history", "1")
        run_id = db.create_evaluation_run("ui-model-readonly")
        with app.test_client() as client:
            response = client.get(f"/history/{run_id}")
            assert response.status_code == 200
            html = response.get_data(as_text=True)
            assert 'id="run-model-edit-btn"' not in html
            assert 'id="run-model-input"' not in html
            assert "ui-model-readonly" in html


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestRunLogDownload:
    """Test /api/run/<id>/log endpoint (zip of logs/eval/<run-id>)"""

    @pytest.fixture
    def log_dir(self, monkeypatch, tmp_path):
        import config
        monkeypatch.setattr(config, 'BASE_DIR', str(tmp_path))
        return tmp_path / 'logs' / 'eval'

    def test_downloads_run_log_directory_as_zip(self, log_dir):
        import io
        import zipfile
        from app import app

        run_id = db.create_evaluation_run("test-model-log-download")
        run_dir = log_dir / str(run_id)
        run_dir.mkdir(parents=True)
        (run_dir / '_run_meta.json').write_text('{"run_id": %d}' % run_id)
        (run_dir / 'math_L1_001.json').write_text('{"prompt": "1+1"}')

        with app.test_client() as client:
            _auth(client)
            response = client.get(f'/api/run/{run_id}/log')
            assert response.status_code == 200
            assert f'evaluation-run-{run_id}-logs.zip' in response.headers['Content-Disposition']

            names = zipfile.ZipFile(io.BytesIO(response.data)).namelist()
            assert sorted(names) == [f'{run_id}/_run_meta.json', f'{run_id}/math_L1_001.json']

    def test_run_without_log_directory_is_404(self, log_dir):
        from app import app

        run_id = db.create_evaluation_run("test-model-log-missing")
        with app.test_client() as client:
            _auth(client)
            assert client.get(f'/api/run/{run_id}/log').status_code == 404

    def test_requires_authentication(self, log_dir):
        from app import app

        run_id = db.create_evaluation_run("test-model-log-auth")
        (log_dir / str(run_id)).mkdir(parents=True)
        with app.test_client() as client:
            assert client.get(f'/api/run/{run_id}/log').status_code in (401, 302)

    def test_detail_page_shows_button_only_when_log_files_exist(self, log_dir):
        from app import app

        run_id = db.create_evaluation_run("test-model-log-button")
        link = f'/api/run/{run_id}/log'.encode()
        with app.test_client() as client:
            _auth(client)
            assert link not in client.get(f'/history/{run_id}').data
            (log_dir / str(run_id)).mkdir(parents=True)
            assert link in client.get(f'/history/{run_id}').data
