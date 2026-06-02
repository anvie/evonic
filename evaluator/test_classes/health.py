"""
Health Domain Test Class

Handles scoring for health-related questions:
- BMI calculations
- Daily intake recommendations
- Normal range checks
"""

from typing import Dict, Any
from .base import BaseTest


class HealthTest(BaseTest):
    """Health domain test - numeric and boolean answers with tolerance"""
    
    def __init__(self, level: int):
        self.level = level
    
    def get_prompt(self) -> str:
        """Health tests use configurable prompts"""
        return ""
    
    def get_expected(self) -> Any:
        """Health tests use configurable expected values"""
        return None
    
    def score_response(self, response: str, expected: Any) -> Dict[str, Any]:
        """
        Score health domain response.
        
        Expected format:
        {
            "answer": 22.86 or "ya",
            "type": "numeric" or "text",
            "tolerance": 0.1  # optional for numeric
            "keywords": ["keyword1", "keyword2"]  # optional for text
        }
        """
        # Handle expected in different formats
        if isinstance(expected, dict):
            expected_value = expected.get("answer", expected.get("value"))
            expected_type = expected.get("type", "numeric")
            tolerance = expected.get("tolerance", 0.01)
            keywords = expected.get("keywords", [])
        else:
            expected_value = expected
            expected_type = "text" if isinstance(expected, str) else "numeric"
            tolerance = 0.01
            keywords = []
        
        response_clean = response.strip().lower()
        
        # Keyword-based scoring (if keywords provided)
        if keywords:
            found_keywords = []
            missing_keywords = []
            for kw in keywords:
                if kw.lower() in response_clean:
                    found_keywords.append(kw)
                else:
                    missing_keywords.append(kw)
            
            score = len(found_keywords) / len(keywords) if keywords else 0
            status = "passed" if score >= 0.8 else "failed"
            
            return {
                "score": score,
                "status": status,
                "details": f"Keywords found: {found_keywords}, missing: {missing_keywords}"
            }
        
        # Text/boolean comparison (ya/tidak)
        if expected_type == "text" or isinstance(expected_value, str):
            expected_clean = str(expected_value).strip().lower()
            if response_clean == expected_clean:
                return {"score": 1.0, "status": "passed", "details": f"Correct: {response_clean}"}
            else:
                return {"score": 0.0, "status": "failed", "details": f"Wrong: expected '{expected_clean}', got '{response_clean}'"}
        
        # Numeric comparison with tolerance
        try:
            actual = float(response.strip())
            expected_num = float(expected_value)
            
            diff = abs(actual - expected_num)
            if diff <= tolerance:
                return {"score": 1.0, "status": "passed", "details": f"Correct: {actual} (expected {expected_num}, tolerance {tolerance})"}
            else:
                # Partial score based on how close
                if expected_num != 0:
                    error_pct = diff / abs(expected_num)
                    partial_score = max(0, 1 - error_pct)
                else:
                    partial_score = 0
                
                return {
                    "score": partial_score,
                    "status": "failed" if partial_score < 0.8 else "passed",
                    "details": f"Difference: {diff:.4f} (expected {expected_num}, got {actual}, tolerance {tolerance})"
                }
        except ValueError:
            return {"score": 0.0, "status": "failed", "details": f"Not a number: '{response}'"}
