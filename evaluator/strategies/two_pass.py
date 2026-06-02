"""
Two-Pass Evaluator

PASS 1: LLM generates answer with reasoning
PASS 2: LLM extracts ONLY the final answer in strict format

Used for: math, reasoning domains
"""

from typing import Any, Dict
from .base import BaseEvaluator, EvaluationResult
from evaluator.answer_extractor import answer_extractor
from evaluator.test_classes import get_test_class


class TwoPassEvaluator(BaseEvaluator):
    """
    Two-pass evaluation for domains that need clean answer extraction.
    
    Used for: math, reasoning
    """
    
    def __init__(self, domain: str):
        self.domain = domain
        self.extractor = answer_extractor
    
    @property
    def name(self) -> str:
        return f"two_pass_{self.domain}"
    
    @property
    def uses_pass2(self) -> bool:
        return True
    
    def evaluate(self, response: str, expected: Any, level: int, prompt: str = "") -> EvaluationResult:
        """
        Evaluate using two-pass extraction.
        
        1. Extract clean answer via PASS2
        2. Score the clean answer using domain-specific test class
        
        Args:
            response: Model response from PASS 1
            expected: Expected answer (can be float, dict with 'answer' key, or other types)
            level: Test level (1-5)
            prompt: Original question/prompt for context
        """
        # Handle expected in different formats
        # Configurable tests pass expected as dict: {"answer": 10.0, "type": "numeric"}
        expected_value = expected
        if isinstance(expected, dict):
            expected_value = expected.get("answer", expected.get("value", expected))
        
        # PASS 2: Extract clean answer (include original question for context)
        extraction = self.extractor.extract(self.domain, level, response, prompt)
        
        if not extraction["success"]:
            pass2_details = {
                "success": False,
                "format": extraction.get("expected_format"),
                "raw_output": extraction.get("raw_pass2", ""),
                "prompt": extraction.get("pass2_prompt", ""),
                "error": extraction.get("parse_error"),
                "extracted_attempt": extraction.get("extracted", "")
            }
            
            # Add PASS2 thinking if present
            if extraction.get("pass2_thinking"):
                pass2_details["thinking"] = extraction["pass2_thinking"]
            
            return EvaluationResult(
                score=0.0,
                status="failed",
                details={
                    "error": extraction.get("parse_error", "Extraction failed"),
                    "raw_output": extraction.get("raw_pass2", ""),
                    "input_response": response[:500] if len(response) > 500 else response,
                    "pass2": pass2_details
                },
                extracted_answer=extraction.get("extracted"),
                pass2_used=True
            )
        
        # Score the extracted answer using domain test class
        extracted = extraction["extracted"]
        
        test_class = get_test_class(self.domain)
        if test_class:
            test_instance = test_class(level)
            score_result = test_instance.score_response(extracted, expected_value)

            # If extraction picked wrong answer, retry with raw PASS1 response
            # This leverages multi-number matching in score_response
            if score_result.get("score", 0) < 1.0 and response != extracted:
                raw_result = test_instance.score_response(response, expected_value)
                if raw_result.get("score", 0) > score_result.get("score", 0):
                    score_result = raw_result
                    score_result["extraction_note"] = "Matched from raw PASS1 response"
        else:
            score_result = {"score": 0.0, "details": f"Unknown domain: {self.domain}"}
        
        # Determine status
        score = score_result.get("score", 0.0)
        status = score_result.get("status", "passed" if score >= 0.8 else "failed")
        
        # Build details
        details = score_result.get("details", {})
        if isinstance(details, str):
            details = {"details": details}
        
        # Add PASS2 metadata
        details["pass2"] = {
            "success": True,
            "format": extraction["expected_format"],
            "raw_output": extraction.get("raw_pass2", ""),
            "prompt": extraction.get("pass2_prompt", ""),
            "input_response": response[:500] if len(response) > 500 else response,
            "extracted_answer": extracted
        }
        
        # Add PASS2 thinking if present
        if extraction.get("pass2_thinking"):
            details["pass2"]["thinking"] = extraction["pass2_thinking"]
        
        return EvaluationResult(
            score=score,
            status=status,
            details=details,
            extracted_answer=extracted,
            pass2_used=True
        )
