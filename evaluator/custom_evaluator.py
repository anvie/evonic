"""
Custom Evaluator - Handle custom evaluators for test scoring.

Supports two types of custom evaluators:
1. Prompt-based: Send evaluation prompt to LLM
2. Regex-based: Extract score using regex pattern from response
"""

import re
import json
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass

from evaluator.llm_client import llm_client


@dataclass
class EvaluationResult:
    """Result of evaluating a response"""
    score: float
    status: str
    details: Dict[str, Any]
    reasoning: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'score': self.score,
            'status': self.status,
            'details': self.details,
            'reasoning': self.reasoning
        }


class CustomEvaluator:
    """Handle custom evaluator logic"""
    
    def __init__(self, evaluator_config: Dict[str, Any], llm_client_override=None):
        """
        Initialize custom evaluator with configuration.
        
        Args:
            evaluator_config: Evaluator configuration dictionary containing:
                - id: Evaluator ID
                - name: Display name
                - type: 'custom' or 'predefined'
                - eval_prompt: (optional) Prompt template for LLM evaluation
                - extraction_regex: (optional) Regex to extract score from response
                - config: Additional configuration
        """
        self.config = evaluator_config
        self.id = evaluator_config.get('id', '')
        self.name = evaluator_config.get('name', '')
        self.type = evaluator_config.get('type', 'custom')
        self.eval_prompt = evaluator_config.get('eval_prompt')
        self.extraction_regex = evaluator_config.get('extraction_regex')
        self.evaluator_config = evaluator_config.get('config', {})
        self.llm_client = llm_client_override or llm_client
    
    def evaluate(self, response: str, expected: Any, level: int = 1) -> EvaluationResult:
        """
        Evaluate a response using the configured evaluation method.
        
        Supports three modes:
        1. Regex-only: Extract score/value directly from response
        2. LLM prompt: Use LLM to evaluate and return score
        3. Hybrid: Use LLM to generate evaluation text, then regex to extract score
        
        Args:
            response: The LLM response to evaluate
            expected: Expected result or criteria
            level: Test level (1-5)
            
        Returns:
            EvaluationResult with score and details
        """
        # Determine evaluation mode
        has_regex = bool(self.extraction_regex)
        has_prompt = bool(self.eval_prompt)
        
        if has_regex and has_prompt:
            # Hybrid mode: LLM evaluates, regex extracts score
            return self._evaluate_hybrid(response, expected, level)
        elif has_regex:
            # Regex-only mode
            return self._evaluate_with_regex(response, expected, level)
        elif has_prompt:
            # LLM prompt-only mode
            return self._evaluate_with_prompt(response, expected, level)
        else:
            return EvaluationResult(
                score=0.0,
                status='failed',
                details={'error': 'No evaluation method configured'},
                reasoning='Evaluator missing both eval_prompt and extraction_regex'
            )
    
    def _evaluate_with_regex(self, response: str, expected: Any, level: int) -> EvaluationResult:
        """
        Evaluate using regex pattern.
        
        Supports three modes:
        1. Regex Matcher: Use expected value as the regex pattern (config: use_expected_as_pattern=True)
        2. Score extraction: Regex captures a number (0-100 or 0-1)
        3. Exact match: Regex matches the entire expected answer
        """
        try:
            # Check if we should use expected value as the pattern
            if self.evaluator_config.get('use_expected_as_pattern') and expected:
                # Extract pattern from expected - support both string and dict formats
                if isinstance(expected, dict):
                    pattern = str(expected.get('expected', expected.get('answer', '')))
                else:
                    pattern = str(expected)
                match = re.search(pattern, response, re.IGNORECASE | re.DOTALL)
                
                if match:
                    return EvaluationResult(
                        score=1.0,
                        status='passed',
                        details={
                            'method': 'regex_matcher',
                            'matched_text': match.group(0),
                            'pattern': pattern
                        },
                        reasoning=f'Pattern matched: {match.group(0)}'
                    )
                else:
                    return EvaluationResult(
                        score=0.0,
                        status='failed',
                        details={
                            'method': 'regex_matcher',
                            'error': 'Pattern not found in response',
                            'pattern': pattern
                        },
                        reasoning=f'Regex pattern did not match response'
                    )
            
            # Standard regex evaluation
            pattern = self.extraction_regex
            match = re.search(pattern, response, re.IGNORECASE | re.DOTALL)
            
            if match:
                # Check if we have capture groups
                if match.lastindex and match.lastindex >= 1:
                    # Mode 1: Extract value from capture group
                    extracted = match.group(1)
                    
                    # Try to interpret as score
                    try:
                        score = float(extracted)

                        # exact_number: compare numeric value to expected
                        if self.evaluator_config.get('comparison') == 'exact_number':
                            expected_num = float(str(expected)) if expected is not None else None
                            is_match = expected_num is not None and score == expected_num
                            return EvaluationResult(
                                score=1.0 if is_match else 0.0,
                                status='passed' if is_match else 'failed',
                                details={
                                    'method': 'exact_number',
                                    'extracted_value': extracted,
                                    'expected': expected,
                                    'match': is_match,
                                    'pattern': pattern
                                },
                                reasoning=f'Extracted {extracted} vs expected {expected}: {"MATCH" if is_match else "NO MATCH"}'
                            )

                        # Normalize to 0-1 if > 1
                        if score > 1.0:
                            score = score / 100.0
                        status = 'passed' if score >= 0.7 else 'failed'

                        return EvaluationResult(
                            score=score,
                            status=status,
                            details={
                                'method': 'regex_extract',
                                'extracted_value': extracted,
                                'pattern': pattern
                            },
                            reasoning=f'Extracted value "{extracted}" → score {score}'
                        )
                    except ValueError:
                        # Not a number, treat as string matching
                        # Compare extracted value with expected
                        if expected is not None:
                            expected_str = str(expected)
                            # Check if extracted matches expected
                            is_match = extracted.strip().lower() == expected_str.strip().lower()
                            score = 1.0 if is_match else 0.0
                            
                            return EvaluationResult(
                                score=score,
                                status='passed' if is_match else 'failed',
                                details={
                                    'method': 'regex_match',
                                    'extracted_value': extracted,
                                    'expected': expected_str,
                                    'match': is_match,
                                    'pattern': pattern
                                },
                                reasoning=f'Extracted "{extracted}" vs expected "{expected_str}": {"MATCH" if is_match else "NO MATCH"}'
                            )
                        else:
                            # No expected value, just return extracted
                            return EvaluationResult(
                                score=1.0,
                                status='passed',
                                details={
                                    'method': 'regex_extract',
                                    'extracted_value': extracted,
                                    'pattern': pattern
                                },
                                reasoning=f'Successfully extracted: {extracted}'
                            )
                else:
                    # Mode 2: Full match (no capture groups)
                    # Regex matched something - that's a pass
                    return EvaluationResult(
                        score=1.0,
                        status='passed',
                        details={
                            'method': 'regex_full_match',
                            'matched_text': match.group(0),
                            'pattern': pattern
                        },
                        reasoning=f'Pattern matched: {match.group(0)}'
                    )
            else:
                # No match found
                return EvaluationResult(
                    score=0.0,
                    status='failed',
                    details={
                        'method': 'regex_no_match',
                        'error': 'Pattern not found in response',
                        'pattern': pattern
                    },
                    reasoning=f'Regex pattern did not match response'
                )
                
        except Exception as e:
            return EvaluationResult(
                score=0.0,
                status='failed',
                details={
                    'method': 'regex_error',
                    'error': str(e)
                },
                reasoning=f'Regex evaluation failed: {str(e)}'
            )
    
    def _evaluate_hybrid(self, response: str, expected: Any, level: int) -> EvaluationResult:
        """
        Hybrid mode: LLM generates evaluation text, regex extracts score.
        
        Args:
            response: The LLM response to evaluate
            expected: Expected result or criteria
            level: Test level (1-5)
            
        Returns:
            EvaluationResult with score and details
        """
        try:
            # Build evaluation prompt
            eval_prompt_text = self.eval_prompt
            eval_prompt_text = eval_prompt_text.replace('{response}', response)
            
            if expected:
                expected_str = json.dumps(expected) if isinstance(expected, (dict, list)) else str(expected)
                eval_prompt_text = eval_prompt_text.replace('{expected}', expected_str)
            
            eval_prompt_text = eval_prompt_text.replace('{level}', str(level))
            
            # Send to LLM
            messages = [{"role": "user", "content": eval_prompt_text}]
            llm_response = self.llm_client.chat_completion(messages)
            llm_output = self.llm_client.extract_content(llm_response)
            
            # Now use regex to extract score from LLM output
            pattern = self.extraction_regex
            match = re.search(pattern, llm_output, re.IGNORECASE | re.DOTALL)
            
            if match and match.lastindex and match.lastindex >= 1:
                score_str = match.group(1)
                score = float(score_str)

                # Normalize score to 0-1 range
                max_score = self.evaluator_config.get('max_score')
                if max_score:
                    score = score / float(max_score)
                elif score > 1.0:
                    score = score / 100.0

                status = 'passed' if score >= 0.7 else 'failed'
                
                return EvaluationResult(
                    score=score,
                    status=status,
                    details={
                        'method': 'hybrid',
                        'llm_output': llm_output[:500],  # Truncate for storage
                        'extracted_score': score_str,
                        'pattern': pattern
                    },
                    reasoning=f'LLM evaluated → Regex extracted score {score_str} → {score*100:.0f}%'
                )
            else:
                return EvaluationResult(
                    score=0.0,
                    status='failed',
                    details={
                        'method': 'hybrid',
                        'llm_output': llm_output[:500],
                        'error': 'Regex failed to extract score from LLM output',
                        'pattern': pattern
                    },
                    reasoning='LLM generated evaluation but regex could not extract score'
                )
                
        except Exception as e:
            return EvaluationResult(
                score=0.0,
                status='failed',
                details={
                    'method': 'hybrid_error',
                    'error': str(e)
                },
                reasoning=f'Hybrid evaluation failed: {str(e)}'
            )
    
    def _evaluate_with_prompt(self, response: str, expected: Any, level: int) -> EvaluationResult:
        """Evaluate using LLM with custom prompt"""
        try:
            # Build evaluation prompt
            eval_prompt_text = self.eval_prompt
            
            # Replace placeholders
            eval_prompt_text = eval_prompt_text.replace('{response}', response)
            
            if expected:
                expected_str = json.dumps(expected) if isinstance(expected, (dict, list)) else str(expected)
                eval_prompt_text = eval_prompt_text.replace('{expected}', expected_str)
            
            eval_prompt_text = eval_prompt_text.replace('{level}', str(level))
            
            # Send to LLM
            messages = [{"role": "user", "content": eval_prompt_text}]
            llm_response = self.llm_client.chat_completion(messages)
            
            eval_response = self.llm_client.extract_content(llm_response)
            
            # Try to parse as JSON
            try:
                # Find JSON in response
                json_match = re.search(r'\{[^{}]*"score"[^{}]*\}', eval_response, re.DOTALL)
                if json_match:
                    result_json = json.loads(json_match.group(0))
                    score = float(result_json.get('score', 0))
                    reasoning = result_json.get('reasoning', '')
                    
                    # Normalize score to 0-1 range
                    max_score = self.evaluator_config.get('max_score')
                    if max_score:
                        score = score / float(max_score)
                    elif score > 5.0:
                        score = score / 100.0  # Assume percentage (0-100)
                    elif score > 1.0:
                        score = score / 5.0    # Assume 0-5 rating scale

                    status = 'passed' if score >= 0.7 else 'failed'

                    return EvaluationResult(
                        score=score,
                        status=status,
                        details={
                            'method': 'prompt',
                            'eval_response': eval_response[:500]  # Truncate for storage
                        },
                        reasoning=reasoning
                    )
            except json.JSONDecodeError:
                pass
            
            # Try to extract score directly
            # Look for patterns like "score: 85", "score is 85", "score 85"
            score_match = re.search(r'score[^\d]*(\d+(?:\.\d+)?)', eval_response, re.IGNORECASE)
            if score_match:
                score = float(score_match.group(1))
                # Normalize score to 0-1 range
                max_score = self.evaluator_config.get('max_score')
                if max_score:
                    score = score / float(max_score)
                elif score > 5.0:
                    score = score / 100.0
                elif score > 1.0:
                    score = score / 5.0
                
                status = 'passed' if score >= 0.7 else 'failed'
                
                return EvaluationResult(
                    score=score,
                    status=status,
                    details={
                        'method': 'prompt',
                        'eval_response': eval_response[:500]
                    },
                    reasoning='Score extracted from LLM evaluation response'
                )
            
            # Fallback: look for pass/fail in response
            if 'pass' in eval_response.lower():
                return EvaluationResult(
                    score=1.0,
                    status='passed',
                    details={'method': 'prompt', 'eval_response': eval_response[:500]},
                    reasoning='Pass keyword found in evaluation response'
                )
            elif 'fail' in eval_response.lower():
                return EvaluationResult(
                    score=0.0,
                    status='failed',
                    details={'method': 'prompt', 'eval_response': eval_response[:500]},
                    reasoning='Fail keyword found in evaluation response'
                )
            
            return EvaluationResult(
                score=0.0,
                status='failed',
                details={
                    'method': 'prompt',
                    'error': 'Could not parse evaluation response',
                    'eval_response': eval_response[:500]
                },
                reasoning='Failed to parse score from LLM evaluation'
            )
            
        except Exception as e:
            return EvaluationResult(
                score=0.0,
                status='failed',
                details={
                    'method': 'prompt',
                    'error': str(e)
                },
                reasoning=f'Prompt evaluation failed: {str(e)}'
            )


# Predefined evaluation prompts for common use cases
DEFAULT_EVAL_PROMPTS = {
    'numeric': """Evaluate if this response contains the correct numeric answer.

Expected answer: {expected}
Response: {response}

Rate the response on a scale of 0-5 where:
- 5: Exact correct answer
- 4: Close answer (within 5% tolerance)
- 3: Partially correct (right method, wrong final answer)
- 2: Wrong answer but shows understanding
- 1: Completely wrong
- 0: No numeric response

Return JSON: {"score": <0-5>, "reasoning": "<explanation>"}""",

    'factual': """Evaluate if this response contains accurate factual information.

Expected facts: {expected}
Response: {response}

Rate the response on a scale of 0-5 based on:
- Accuracy of information
- Completeness
- Relevance

Return JSON: {"score": <0-5>, "reasoning": "<explanation>"}""",

    'conversation': """Evaluate this conversational response.

Expected content: {expected}
Response: {response}

Rate on a scale of 0-5 based on:
- Relevance to the question
- Accuracy of information
- Fluency and naturalness

Return JSON: {"score": <0-5>, "reasoning": "<explanation>", "relevance": <0-1>, "accuracy": <0-1>, "fluency": <0-1>}""",

    'code': """Evaluate this code response.

Expected output: {expected}
Response: {response}

Rate on a scale of 0-5 based on:
- Correctness
- Code quality
- Efficiency

Return JSON: {"score": <0-5>, "reasoning": "<explanation>"}"""
}


def get_default_eval_prompt(prompt_type: str) -> Optional[str]:
    """Get a default evaluation prompt by type"""
    return DEFAULT_EVAL_PROMPTS.get(prompt_type)


def create_custom_evaluator(evaluator_type: str, config: Dict[str, Any] = None) -> CustomEvaluator:
    """Create a custom evaluator with default configuration"""
    config = config or {}
    
    # Merge with default prompt if specified
    if evaluator_type in DEFAULT_EVAL_PROMPTS and 'eval_prompt' not in config:
        config['eval_prompt'] = DEFAULT_EVAL_PROMPTS[evaluator_type]
    
    config['type'] = 'custom'
    
    return CustomEvaluator(config)