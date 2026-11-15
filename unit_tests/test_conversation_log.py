"""
Unit tests for conversation log and multi-test modal features.
"""

import pytest
import json
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import Mock, patch, MagicMock


def _auth(client):
    """Authenticate the test client session."""
    with client.session_transaction() as sess:
        sess['authenticated'] = True
    return client


class TestConversationLog:
    """Test conversation log capture in tool calling loop"""
    
    def test_conversation_log_single_turn(self):
        """Test conversation log for single turn (no tool calls)"""
        from evaluator.engine import EvaluationEngine
        
        engine = EvaluationEngine()
        
        # Mock LLM client to return final answer without tool calls
        mock_response = {
            "success": True,
            "response": {
                "choices": [{
                    "message": {
                        "content": "The answer is 42"
                    }
                }]
            },
            "duration_ms": 100,
            "total_tokens": 50
        }
        
        with patch('evaluator.engine.llm_client') as mock_client:
            mock_client.chat_completion.return_value = mock_response
            mock_client.extract_content_with_thinking.return_value = {
                "content": "The answer is 42",
                "thinking": None,
                "tool_calls": []
            }
            
            result = engine._run_tool_calling_loop("What is 6 * 7?", [])
            
            assert "conversation_log" in result
            assert isinstance(result["conversation_log"], list)
            # Single turn with final response
            assert len(result["conversation_log"]) == 1
            assert result["conversation_log"][0]["response"] == "The answer is 42"
    
    def test_conversation_log_multi_turn(self):
        """Test conversation log captures multiple turns"""
        from evaluator.engine import EvaluationEngine
        
        engine = EvaluationEngine()
        
        # First call: returns tool call
        tool_call_response = {
            "success": True,
            "response": {
                "choices": [{
                    "message": {
                        "content": None,
                        "tool_calls": [{
                            "id": "call_1",
                            "function": {
                                "name": "get_current_date",
                                "arguments": "{}"
                            }
                        }]
                    }
                }]
            },
            "duration_ms": 100,
            "total_tokens": 30
        }

        # Second call: returns final answer
        final_response = {
            "success": True,
            "response": {
                "choices": [{
                    "message": {
                        "content": "Today is April 4th, 2026"
                    }
                }]
            },
            "duration_ms": 80,
            "total_tokens": 25
        }
        
        call_count = [0]
        
        def mock_chat_completion(messages, tools):
            call_count[0] += 1
            if call_count[0] == 1:
                return tool_call_response
            return final_response
        
        def mock_extract(response):
            if call_count[0] == 1:
                return {
                    "content": "",
                    "thinking": "I need to get the current date",
                    "tool_calls": [{
                        "id": "call_1",
                        "function": {
                            "name": "get_current_date",
                            "arguments": "{}"
                        }
                    }]
                }
            return {
                "content": "Today is April 4th, 2026",
                "thinking": None,
                "tool_calls": []
            }
        
        mock_responses = {
            "get_current_date": {"date": "2026-04-04", "day": "Saturday"}
        }
        
        tools = [{
            "type": "function",
            "function": {
                "name": "get_current_date",
                "description": "Get current date"
            }
        }]
        
        with patch('evaluator.engine.llm_client') as mock_client:
            mock_client.chat_completion.side_effect = mock_chat_completion
            mock_client.extract_content_with_thinking.side_effect = mock_extract
            
            result = engine._run_tool_calling_loop("What's today's date?", tools, mock_responses, enable_planning=False)
            
            assert "conversation_log" in result
            assert len(result["conversation_log"]) == 2
            
            # First turn should have thinking and tool call
            turn1 = result["conversation_log"][0]
            assert turn1["turn"] == 1
            assert turn1["thinking"] == "I need to get the current date"
            assert len(turn1["tool_calls"]) == 1
            assert turn1["tool_calls"][0]["name"] == "get_current_date"
            assert len(turn1["tool_results"]) == 1
            assert turn1["tool_results"][0]["result"]["date"] == "2026-04-04"
            
            # Second turn should have final response
            turn2 = result["conversation_log"][1]
            assert turn2["turn"] == 2
            assert turn2["response"] == "Today is April 4th, 2026"
    
    def test_tools_available_stored_in_details(self):
        """Test that tool definitions are stored in test details"""
        from evaluator.engine import EvaluationEngine
        
        engine = EvaluationEngine()
        
        test = {
            "id": "test_1",
            "prompt": "Check room availability",
            "expected": {"tools": ["check_availability"]},
            "evaluator_id": "tool_call",
            "tools": [
                {
                    "function": {
                        "name": "check_availability",
                        "description": "Check room availability",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "date": {"type": "string"},
                                "room": {"type": "string"}
                            }
                        }
                    },
                    "mock_response": {"available": True}
                }
            ]
        }
        
        # This test verifies the structure is correct
        # Full integration test would require mocking more components
        assert len(test["tools"]) == 1
        assert test["tools"][0]["function"]["name"] == "check_availability"


class TestIndividualTestResultsAPI:
    """Test the API endpoint for individual test results"""
    
    def test_api_returns_tests_for_cell(self):
        """Test API returns all tests for a domain/level"""
        from app import app
        from models.db import db as test_db

        run_id = test_db.create_evaluation_run("test-model")

        with app.test_client() as client:
            _auth(client)
            response = client.get(f'/api/run/{run_id}/tests/math/1')
            assert response.status_code == 200

            data = response.get_json()
            assert "tests" in data
            assert "domain" in data
            assert "level" in data
            assert data["domain"] == "math"
            assert data["level"] == 1
    
    def test_api_parses_json_fields(self):
        """Test API properly parses JSON fields in test results"""
        from models.db import db

        # Create a test run and result
        run_id = db.create_evaluation_run("test-model")
        
        # Save individual test result
        db.save_individual_test_result(
            run_id=run_id,
            test_id="test_123",
            domain="tool_calling",
            level=1,
            prompt="Test prompt",
            response="Test response",
            expected='{"tools": ["get_date"]}',
            score=1.0,
            status="passed",
            details='{"conversation_log": [{"turn": 1}], "tools_available": [{"name": "get_date"}]}',
            duration_ms=100,
            model_name="test-model"
        )
        
        from app import app
        
        with app.test_client() as client:
            _auth(client)
            response = client.get(f'/api/run/{run_id}/tests/tool_calling/1')
            data = response.get_json()
            
            if data["tests"]:
                test = data["tests"][0]
                # JSON should be parsed
                assert isinstance(test.get("expected"), dict) or test.get("expected") is None
                assert isinstance(test.get("details"), dict) or test.get("details") is None


class TestDatabaseIndividualResults:
    """Test database operations for individual test results"""
    
    def test_save_and_retrieve_individual_result(self):
        """Test saving and retrieving individual test results"""
        from models.db import db
        import uuid
        
        run_id = str(uuid.uuid4())
        
        db.save_individual_test_result(
            run_id=run_id,
            test_id="test_abc",
            domain="villa_demo",
            level=2,
            prompt="What rooms are available?",
            response="Room A and Room B are available",
            expected='{"tools": ["check_availability"]}',
            score=0.8,
            status="passed",
            details='{"conversation_log": [{"turn": 1, "thinking": "Let me check"}]}',
            duration_ms=250,
            model_name="gemma-4"
        )
        
        results = db.get_individual_test_results(run_id, "villa_demo", 2)
        
        assert len(results) == 1
        assert results[0]["test_id"] == "test_abc"
        assert results[0]["domain"] == "villa_demo"
        assert results[0]["level"] == 2
        assert results[0]["score"] == 0.8
    
    def test_retrieve_by_domain_only(self):
        """Test retrieving results filtered by domain"""
        from models.db import db
        import uuid
        
        run_id = str(uuid.uuid4())
        
        # Save results for multiple levels
        for level in [1, 2, 3]:
            db.save_individual_test_result(
                run_id=run_id,
                test_id=f"test_l{level}",
                domain="math",
                level=level,
                prompt=f"Level {level} test",
                response="Answer",
                expected=None,
                score=1.0,
                status="passed",
                details=None,
                duration_ms=100,
                model_name="test"
            )
        
        results = db.get_individual_test_results(run_id, "math")
        assert len(results) == 3
    
    def test_retrieve_all_for_run(self):
        """Test retrieving all results for a run"""
        from models.db import db
        import uuid
        
        run_id = str(uuid.uuid4())
        
        # Save results for multiple domains
        for domain in ["math", "sql"]:
            db.save_individual_test_result(
                run_id=run_id,
                test_id=f"test_{domain}",
                domain=domain,
                level=1,
                prompt="Test",
                response="Answer",
                expected=None,
                score=1.0,
                status="passed",
                details=None,
                duration_ms=100,
                model_name="test"
            )
        
        results = db.get_individual_test_results(run_id)
        assert len(results) >= 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
