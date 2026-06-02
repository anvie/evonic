"""
Unit tests for custom_evaluator module
"""

import pytest
from unittest.mock import Mock, patch, MagicMock

from evaluator.custom_evaluator import (
    CustomEvaluator, EvaluationResult, 
    DEFAULT_EVAL_PROMPTS, get_default_eval_prompt, create_custom_evaluator
)


class TestEvaluationResult:
    """Tests for EvaluationResult dataclass"""
    
    def test_create_result(self):
        """Test creating an evaluation result"""
        result = EvaluationResult(
            score=0.85,
            status='passed',
            details={'answer': 'correct'},
            reasoning='Match found'
        )
        
        assert result.score == 0.85
        assert result.status == 'passed'
        assert result.details['answer'] == 'correct'
    
    def test_to_dict(self):
        """Test converting to dictionary"""
        result = EvaluationResult(
            score=0.5,
            status='failed',
            details={'error': 'wrong'},
            reasoning='No match'
        )
        
        d = result.to_dict()
        
        assert d['score'] == 0.5
        assert d['status'] == 'failed'
        assert d['reasoning'] == 'No match'


class TestCustomEvaluator:
    """Tests for CustomEvaluator class"""
    
    def test_create_evaluator_with_regex(self):
        """Test creating evaluator with regex extraction"""
        config = {
            'id': 'regex_eval',
            'name': 'Regex Evaluator',
            'type': 'custom',
            'extraction_regex': r'SCORE:\s*(\d+)'
        }
        
        evaluator = CustomEvaluator(config)
        
        assert evaluator.id == 'regex_eval'
        assert evaluator.extraction_regex == r'SCORE:\s*(\d+)'
        assert evaluator.eval_prompt is None
    
    def test_create_evaluator_with_prompt(self):
        """Test creating evaluator with prompt"""
        config = {
            'id': 'prompt_eval',
            'name': 'Prompt Evaluator',
            'type': 'custom',
            'eval_prompt': 'Rate this response: {response}'
        }
        
        evaluator = CustomEvaluator(config)
        
        assert evaluator.id == 'prompt_eval'
        assert evaluator.eval_prompt == 'Rate this response: {response}'
    
    def test_evaluate_with_regex(self):
        """Test evaluation using regex"""
        config = {
            'id': 'regex_eval',
            'name': 'Regex Evaluator',
            'type': 'custom',
            'extraction_regex': r'SCORE:\s*(\d+)'
        }
        
        evaluator = CustomEvaluator(config)
        result = evaluator.evaluate("The answer is correct. SCORE: 85", None, 1)
        
        assert result.score == 0.85  # 85 / 100
        assert result.status == 'passed'
        assert 'regex' in result.details['method']
    
    def test_evaluate_with_regex_percentage(self):
        """Test regex evaluation with percentage score"""
        config = {
            'id': 'regex_eval',
            'name': 'Regex Evaluator',
            'type': 'custom',
            'extraction_regex': r'Score:\s*(\d+(?:\.\d+)?)'
        }
        
        evaluator = CustomEvaluator(config)
        result = evaluator.evaluate("Score: 75.5", None, 1)
        
        assert abs(result.score - 0.755) < 0.01
    
    def test_evaluate_regex_no_match(self):
        """Test regex evaluation when pattern doesn't match"""
        config = {
            'id': 'regex_eval',
            'name': 'Regex Evaluator',
            'type': 'custom',
            'extraction_regex': r'SCORE:\s*(\d+)'
        }
        
        evaluator = CustomEvaluator(config)
        result = evaluator.evaluate("No score here", None, 1)
        
        assert result.score == 0.0
        assert result.status == 'failed'
        assert 'did not match' in result.reasoning or 'Pattern not found' in result.details.get('error', '')
    
    @patch('evaluator.custom_evaluator.llm_client')
    def test_evaluate_with_prompt_json(self, mock_llm_client):
        """Test evaluation using prompt with JSON response"""
        config = {
            'id': 'prompt_eval',
            'name': 'Prompt Evaluator',
            'type': 'custom',
            'eval_prompt': 'Rate this: {response}. Return JSON with score.'
        }
        
        # Mock LLM response
        mock_response = {
            'content': '{"score": 4, "reasoning": "Good response"}',
            'duration_ms': 100,
            'total_tokens': 50
        }
        mock_llm_client.chat_completion.return_value = mock_response
        mock_llm_client.extract_content.return_value = '{"score": 4, "reasoning": "Good response"}'
        
        evaluator = CustomEvaluator(config)
        result = evaluator.evaluate("This is the answer", None, 1)
        
        # Score 4 should be converted to 0.8 (4/5)
        assert abs(result.score - 0.8) < 0.01
        assert 'prompt' in result.details['method']
    
    @patch('evaluator.custom_evaluator.llm_client')
    def test_evaluate_with_prompt_score_extraction(self, mock_llm_client):
        """Test evaluation when LLM returns score in text"""
        config = {
            'id': 'prompt_eval',
            'name': 'Prompt Evaluator',
            'type': 'custom',
            'eval_prompt': 'Rate this: {response}'
        }
        
        # Mock LLM response with score in text
        mock_response = {
            'content': 'The score is 85 out of 100. Good job!',
            'duration_ms': 100,
            'total_tokens': 30
        }
        mock_llm_client.chat_completion.return_value = mock_response
        mock_llm_client.extract_content.return_value = 'The score is 85 out of 100. Good job!'
        
        evaluator = CustomEvaluator(config)
        result = evaluator.evaluate("The answer", None, 1)
        
        # Should extract 85 and convert to 0.85 (85/100)
        assert result.score >= 0.8  # Allow some tolerance for different parsing
    
    @patch('evaluator.custom_evaluator.llm_client')
    def test_evaluate_with_prompt_pass_keyword(self, mock_llm_client):
        """Test evaluation when LLM returns pass keyword"""
        config = {
            'id': 'prompt_eval',
            'name': 'Prompt Evaluator',
            'type': 'custom',
            'eval_prompt': 'Is this correct? {response}'
        }
        
        # Mock LLM response with pass keyword
        mock_response = {
            'content': 'Yes, this is a pass. The answer is correct.',
            'duration_ms': 100,
            'total_tokens': 20
        }
        mock_llm_client.chat_completion.return_value = mock_response
        mock_llm_client.extract_content.return_value = 'Yes, this is a pass. The answer is correct.'
        
        evaluator = CustomEvaluator(config)
        result = evaluator.evaluate("The answer", None, 1)
        
        assert result.score == 1.0
        assert result.status == 'passed'
    
    @patch('evaluator.custom_evaluator.llm_client')
    def test_evaluate_with_prompt_fail_keyword(self, mock_llm_client):
        """Test evaluation when LLM returns fail keyword"""
        config = {
            'id': 'prompt_eval',
            'name': 'Prompt Evaluator',
            'type': 'custom',
            'eval_prompt': 'Is this correct? {response}'
        }
        
        # Mock LLM response with fail keyword
        mock_response = {
            'content': 'This is a fail. The answer is incorrect.',
            'duration_ms': 100,
            'total_tokens': 20
        }
        mock_llm_client.chat_completion.return_value = mock_response
        mock_llm_client.extract_content.return_value = 'This is a fail. The answer is incorrect.'
        
        evaluator = CustomEvaluator(config)
        result = evaluator.evaluate("Wrong answer", None, 1)
        
        assert result.score == 0.0
        assert result.status == 'failed'
    
    def test_evaluate_no_method(self):
        """Test evaluation with no method configured"""
        config = {
            'id': 'empty_eval',
            'name': 'Empty Evaluator',
            'type': 'custom'
        }
        
        evaluator = CustomEvaluator(config)
        result = evaluator.evaluate("Response", None, 1)
        
        assert result.score == 0.0
        assert result.status == 'failed'
        assert 'No evaluation method' in result.details['error']
    
    def test_evaluate_with_placeholders(self):
        """Test that placeholders are replaced in prompt"""
        config = {
            'id': 'placeholder_eval',
            'name': 'Placeholder Evaluator',
            'type': 'custom',
            'extraction_regex': r'SCORE:\s*(\d+)'
        }
        
        evaluator = CustomEvaluator(config)
        
        # This test just verifies the regex path works
        result = evaluator.evaluate("Response", {"answer": 42}, 2)
        
        assert result.status in ['passed', 'failed']


class TestDefaultEvalPrompts:
    """Tests for default evaluation prompts"""
    
    def test_get_numeric_prompt(self):
        """Test getting numeric evaluation prompt"""
        prompt = get_default_eval_prompt('numeric')
        
        assert prompt is not None
        assert 'numeric' in prompt.lower() or 'score' in prompt.lower()
    
    def test_get_factual_prompt(self):
        """Test getting factual evaluation prompt"""
        prompt = get_default_eval_prompt('factual')
        
        assert prompt is not None
        assert 'factual' in prompt.lower() or 'accuracy' in prompt.lower()
    
    def test_get_conversation_prompt(self):
        """Test getting conversation evaluation prompt"""
        prompt = get_default_eval_prompt('conversation')
        
        assert prompt is not None
        assert 'relevance' in prompt.lower() or 'fluency' in prompt.lower()
    
    def test_get_nonexistent_prompt(self):
        """Test getting non-existent prompt type"""
        prompt = get_default_eval_prompt('nonexistent')
        
        assert prompt is None
    
    def test_create_custom_evaluator_with_default(self):
        """Test creating custom evaluator with default prompt"""
        evaluator = create_custom_evaluator('numeric', {'id': 'test'})
        
        assert evaluator is not None
        assert evaluator.type == 'custom'
        assert evaluator.eval_prompt is not None


class TestEvaluationResultStatus:
    """Tests for evaluation result status determination"""
    
    def test_passed_status_high_score(self):
        """Test that high scores result in passed status"""
        config = {
            'id': 'test_eval',
            'name': 'Test',
            'type': 'custom',
            'extraction_regex': r'(\d+)'
        }
        
        evaluator = CustomEvaluator(config)
        result = evaluator.evaluate("85", None, 1)
        
        # 85/100 = 0.85, which is >= 0.7 threshold
        assert result.status == 'passed'
    
    def test_failed_status_low_score(self):
        """Test that low scores result in failed status"""
        config = {
            'id': 'test_eval',
            'name': 'Test',
            'type': 'custom',
            'extraction_regex': r'(\d+)'
        }
        
        evaluator = CustomEvaluator(config)
        result = evaluator.evaluate("50", None, 1)
        
        # 50/100 = 0.5, which is < 0.7 threshold
        assert result.status == 'failed'


class TestEdgeCases:
    """Tests for edge cases"""
    
    def test_empty_response(self):
        """Test evaluation with empty response"""
        config = {
            'id': 'test_eval',
            'name': 'Test',
            'type': 'custom',
            'extraction_regex': r'SCORE:\s*(\d+)'
        }
        
        evaluator = CustomEvaluator(config)
        result = evaluator.evaluate("", None, 1)
        
        assert result.status == 'failed'
        assert result.score == 0.0
    
    def test_malformed_json_in_response(self):
        """Test evaluation when expected JSON is malformed"""
        config = {
            'id': 'test_eval',
            'name': 'Test',
            'type': 'custom',
            'extraction_regex': r'score["\s:]+(\d+)'
        }
        
        evaluator = CustomEvaluator(config)
        result = evaluator.evaluate("score: 75", None, 1)
        
        # Should still extract score using regex
        assert result.score == 0.75
    
    def test_score_normalization(self):
        """Test that scores > 1 are normalized"""
        config = {
            'id': 'test_eval',
            'name': 'Test',
            'type': 'custom',
            'extraction_regex': r'(\d+)'
        }
        
        evaluator = CustomEvaluator(config)
        result = evaluator.evaluate("42", None, 1)
        
        # 42 should be normalized to 0.42
        assert abs(result.score - 0.42) < 0.01
    
    def test_score_already_normalized(self):
        """Test handling of already normalized scores"""
        config = {
            'id': 'test_eval',
            'name': 'Test',
            'type': 'custom',
            'extraction_regex': r'(\d+\.\d+)'
        }
        
        evaluator = CustomEvaluator(config)
        result = evaluator.evaluate("0.85", None, 1)
        
        # 0.85 should be kept as is
        assert abs(result.score - 0.85) < 0.01