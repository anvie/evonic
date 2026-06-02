"""
Unit tests for domain-specific evaluators
"""

import pytest
from evaluator.strategies.base import BaseEvaluator, EvaluationResult
from evaluator.strategies.two_pass import TwoPassEvaluator
from evaluator.strategies.keyword import KeywordEvaluator
from evaluator.strategies.sql_executor import SQLExecutorEvaluator
from evaluator.strategies.tool_call import ToolCallEvaluator
from evaluator.domain_evaluators import get_evaluator, get_evaluator_info, list_evaluators


class TestKeywordEvaluator:
    """Test keyword-based evaluation for conversation domain"""
    
    def setup_method(self):
        self.evaluator = KeywordEvaluator("conversation")
    
    def test_evaluator_properties(self):
        """Test evaluator metadata"""
        assert self.evaluator.name == "keyword_conversation"
        assert self.evaluator.uses_pass2 is False
    
    def test_evaluate_level_1_good_response(self):
        """Test evaluation of good introduction response"""
        response = "Halo! Saya adalah AI assistant bernama Qwen. Saya dikembangkan oleh Alibaba Cloud untuk membantu Anda."
        expected = {"keywords": ["ai", "assistant", "membantu"]}
        
        result = self.evaluator.evaluate(response, expected, 1)
        
        assert result.score > 0.5
        assert result.status in ["passed", "partial"]
        assert result.pass2_used is False
        assert "relevance" in result.details
        assert "correctness" in result.details
        assert "fluency" in result.details
    
    def test_evaluate_level_2_jakarta_response(self):
        """Test evaluation of Jakarta capital response"""
        response = "Ibu kota Indonesia adalah Jakarta. Jakarta adalah pusat pemerintahan dan ekonomi Indonesia."
        expected = {"keywords": ["jakarta", "ibu kota", "indonesia"]}
        
        result = self.evaluator.evaluate(response, expected, 2)
        
        assert result.score > 0.6
        assert "jakarta" in result.details.get("keywords_found", [])
    
    def test_evaluate_poor_response(self):
        """Test evaluation of poor response"""
        response = "Yes no maybe"
        expected = {"keywords": ["ai", "assistant"]}
        
        result = self.evaluator.evaluate(response, expected, 1)
        
        assert result.score < 0.5
        assert result.status == "failed"
    
    def test_relevance_scoring(self):
        """Test relevance keyword matching"""
        response = "Saya adalah AI assistant yang bisa membantu Anda dengan berbagai pertanyaan."
        keywords = ["ai", "assistant", "membantu"]
        
        relevance = self.evaluator._score_relevance(response, keywords)
        
        assert relevance > 0.7
    
    def test_fluency_scoring(self):
        """Test Indonesian fluency scoring"""
        response = "Ini adalah contoh kalimat dalam bahasa Indonesia. Saya menggunakan kata-kata seperti dan, yang, dengan untuk menunjukkan kelancaran bahasa."
        
        fluency = self.evaluator._score_fluency(response)
        
        assert fluency > 0.5


class TestTwoPassEvaluator:
    """Test two-pass evaluation for math/reasoning domains"""
    
    def test_evaluator_properties(self):
        """Test evaluator metadata"""
        evaluator = TwoPassEvaluator("math")
        assert evaluator.name == "two_pass_math"
        assert evaluator.uses_pass2 is True
    
    def test_evaluator_uses_extractor(self):
        """Test that evaluator uses answer extractor"""
        evaluator = TwoPassEvaluator("math")
        assert evaluator.extractor is not None


class TestSQLEvaluator:
    """Test SQL evaluation"""
    
    def test_evaluator_properties(self):
        """Test evaluator metadata"""
        evaluator = SQLExecutorEvaluator("sql")
        assert evaluator.name == "sql_executor"
        assert evaluator.uses_pass2 is True
    
    def test_scoring_logic(self):
        """Test SQL result scoring"""
        evaluator = SQLExecutorEvaluator("sql")
        
        # Test scoring with good results
        score_result = evaluator._score_results(
            "SELECT name FROM customers",
            [{"name": "John"}, {"name": "Jane"}],
            ["name"],
            {"required_columns": ["name"], "min_rows": 1},
            1
        )
        
        assert score_result["score"] > 0.5


class TestToolCallEvaluator:
    """Test tool call evaluation"""
    
    def test_evaluator_properties(self):
        """Test evaluator metadata"""
        evaluator = ToolCallEvaluator("tool_calling")
        assert evaluator.name == "tool_call"
        assert evaluator.uses_pass2 is True
    
    def test_validate_tool_names(self):
        """Test tool name validation"""
        evaluator = ToolCallEvaluator("tool_calling")
        
        result = evaluator._validate_tool_names(
            ["get_weather", "get_time"],
            {"tools": ["get_weather"]},
            1,
            {"success": True, "expected_format": "tools"}
        )
        
        assert result.score >= 0.8
        assert "get_weather" in result.details["called_tools"]


class TestDomainEvaluatorRegistry:
    """Test domain evaluator registry"""
    
    def test_get_evaluator_math(self):
        """Test getting evaluator for math domain"""
        evaluator = get_evaluator("math")
        assert isinstance(evaluator, TwoPassEvaluator)
        assert evaluator.uses_pass2 is True
    
    def test_get_evaluator_conversation(self):
        """Test getting evaluator for conversation domain"""
        evaluator = get_evaluator("conversation")
        assert isinstance(evaluator, KeywordEvaluator)
        assert evaluator.uses_pass2 is False
    
    def test_get_evaluator_sql(self):
        """Test getting evaluator for SQL domain"""
        evaluator = get_evaluator("sql")
        assert isinstance(evaluator, SQLExecutorEvaluator)
    
    def test_get_evaluator_tool_calling(self):
        """Test getting evaluator for tool_calling domain"""
        evaluator = get_evaluator("tool_calling")
        assert isinstance(evaluator, ToolCallEvaluator)
    
    def test_get_evaluator_info(self):
        """Test getting evaluator info"""
        info = get_evaluator_info("conversation")
        assert "name" in info
        assert "uses_pass2" in info
        assert info["uses_pass2"] is False
    
    def test_list_evaluators(self):
        """Test listing all evaluators"""
        evaluators = list_evaluators()
        assert "math" in evaluators
        assert "conversation" in evaluators
        assert "sql" in evaluators
        assert "reasoning" in evaluators
        assert "tool_calling" in evaluators


class TestEvaluationResult:
    """Test evaluation result dataclass"""
    
    def test_result_creation(self):
        """Test creating evaluation result"""
        result = EvaluationResult(
            score=0.85,
            status="passed",
            details={"relevance": 0.9},
            extracted_answer="42",
            pass2_used=True
        )
        
        assert result.score == 0.85
        assert result.status == "passed"
        assert result.details["relevance"] == 0.9
        assert result.extracted_answer == "42"
        assert result.pass2_used is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
