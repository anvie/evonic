from .base import BaseTest
from typing import Dict, Any
import re


class CodingTest(BaseTest):
    """Python coding tests - expects clean output from PASS 2"""

    def get_prompt(self) -> str:
        # Prompts are loaded from test_definitions/coding/ JSON files
        return ""

    def get_expected(self) -> Any:
        # Expected values are loaded from test_definitions/coding/ JSON files
        return ""

    def score_response(self, response: str, expected: Any) -> Dict[str, Any]:
        """
        Score response for coding tests.

        Supports two expected types:
        - numeric: compare as float with tolerance
        - exact_match: compare as string (case-sensitive)
        """
        clean = response.strip()

        # Determine expected type
        if isinstance(expected, dict):
            exp_type = expected.get("type", "numeric")
            exp_value = expected.get("answer", expected.get("value", ""))
        elif isinstance(expected, (int, float)):
            exp_type = "numeric"
            exp_value = float(expected)
        else:
            exp_type = "exact_match"
            exp_value = str(expected)

        if exp_type == "numeric":
            return self._score_numeric(clean, float(exp_value))
        else:
            return self._score_exact(clean, str(exp_value))

    def _score_numeric(self, response: str, expected: float) -> Dict[str, Any]:
        """Score numeric answer"""
        # Try direct parse
        try:
            actual = float(response)
            return self._compare_numeric(actual, expected)
        except ValueError:
            pass

        # Extract numbers from response
        numbers = re.findall(r'[-+]?\d+\.?\d*', response)
        if numbers:
            for num_str in numbers:
                try:
                    actual = float(num_str)
                    result = self._compare_numeric(actual, expected)
                    if result["score"] == 1.0:
                        return result
                except ValueError:
                    continue
            # Return first number as attempt
            try:
                first = float(numbers[0])
                return self._compare_numeric(first, expected)
            except ValueError:
                pass

        return {
            "score": 0.0,
            "details": f"Could not extract number from: '{response[:100]}'",
            "actual": None,
            "expected": expected
        }

    def _compare_numeric(self, actual: float, expected: float) -> Dict[str, Any]:
        """Compare numeric values with tolerance"""
        if abs(actual - expected) < 0.01:
            return {
                "score": 1.0,
                "details": f"Correct: {actual}",
                "actual": actual,
                "expected": expected
            }
        return {
            "score": 0.0,
            "details": f"Wrong: expected {expected}, got {actual}",
            "actual": actual,
            "expected": expected
        }

    def _score_exact(self, response: str, expected: str) -> Dict[str, Any]:
        """Score exact string match"""
        # Try exact match first
        if response == expected:
            return {
                "score": 1.0,
                "details": f"Correct: {response}",
                "actual": response,
                "expected": expected
            }

        # Try case-insensitive
        if response.lower() == expected.lower():
            return {
                "score": 0.9,
                "details": f"Correct (case-insensitive): {response}",
                "actual": response,
                "expected": expected
            }

        # Try stripping quotes/backticks
        stripped = response.strip('`"\'').strip()
        if stripped == expected:
            return {
                "score": 1.0,
                "details": f"Correct (after stripping): {stripped}",
                "actual": stripped,
                "expected": expected
            }

        # Try normalizing whitespace around commas (e.g. "A, B, C" vs "A,B,C")
        norm_resp = re.sub(r'\s*,\s*', ',', response)
        norm_exp = re.sub(r'\s*,\s*', ',', expected)
        if norm_resp == norm_exp:
            return {
                "score": 0.95,
                "details": f"Correct (comma-whitespace normalized): {response}",
                "actual": response,
                "expected": expected
            }

        # Check if expected is contained in response (for multi-line output)
        if expected in response:
            return {
                "score": 0.8,
                "details": f"Expected found in response",
                "actual": response[:200],
                "expected": expected
            }

        return {
            "score": 0.0,
            "details": f"Wrong: expected '{expected}', got '{response[:200]}'",
            "actual": response[:200],
            "expected": expected
        }
