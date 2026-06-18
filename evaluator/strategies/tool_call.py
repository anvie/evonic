"""
Tool Call Evaluator

Two-pass extraction + tool call validation.
Used for tool_calling domain.
"""

from typing import Any, Dict, List
from .base import BaseEvaluator, EvaluationResult
from evaluator.answer_extractor import answer_extractor
import json
import re


class ToolCallEvaluator(BaseEvaluator):
    """
    Tool call evaluation with validation.
    
    1. PASS2: Extract clean tool names
    2. Validate tool calls match expected
    """
    
    def __init__(self, domain: str = "tool_calling"):
        self.domain = domain
        self.extractor = answer_extractor
    
    @property
    def name(self) -> str:
        return "tool_call"
    
    @property
    def uses_pass2(self) -> bool:
        return True
    
    def evaluate(self, response: str, expected: Any, level: int, prompt: str = "") -> EvaluationResult:
        """Evaluate tool calls"""
        
        # Check if response already has tool_calls (from LLM with tools)
        tool_calls = self._extract_tool_calls(response)
        
        if tool_calls:
            # Direct tool calls from LLM response
            return self._validate_tool_calls(tool_calls, expected, level)
        
        # Otherwise, use PASS2 to extract tool names
        extraction = self.extractor.extract(self.domain, level, response)
        
        if not extraction["success"]:
            return EvaluationResult(
                score=0.0,
                status="failed",
                details={
                    "error": extraction.get("parse_error", "Tool extraction failed"),
                    "pass2": {
                        "success": False,
                        "error": extraction.get("parse_error")
                    }
                },
                extracted_answer=extraction.get("extracted"),
                pass2_used=True
            )
        
        # Parse extracted tool names
        extracted = extraction["extracted"]
        tool_names = [t.strip() for t in extracted.split(',')]
        
        return self._validate_tool_names(tool_names, expected, level, extraction)
    
    def _extract_tool_calls(self, response: str) -> List[Dict]:
        """Extract tool calls from LLM response"""
        # Try JSON format first (OpenAI style)
        try:
            data = json.loads(response)
            if isinstance(data, dict) and "tool_calls" in data:
                return data["tool_calls"]
        except json.JSONDecodeError:
            pass
        
        # Try Gemma 4 format: <|tool_call>function{args}<|tool_call|>
        from evaluator.gemma4_parser import extract_gemma4_tool_calls, gemma4_tool_calls_to_openai_format
        
        gemma4_calls = extract_gemma4_tool_calls(response)
        if gemma4_calls:
            return gemma4_tool_calls_to_openai_format(gemma4_calls)

        # Try Qwen XML format: <tool_call><function=name><parameter=key>val</parameter></function></tool_call>
        from evaluator.qwen_parser import extract_qwen_tool_calls, qwen_tool_calls_to_openai_format
        
        qwen_calls = extract_qwen_tool_calls(response)
        if qwen_calls:
            return qwen_tool_calls_to_openai_format(qwen_calls)
        
        return []
    
    def _validate_tool_calls(self, tool_calls: List[Dict], expected: Any, level: int) -> EvaluationResult:
        """Validate tool calls from LLM response"""

        called_tools = [tc.get("function", {}).get("name", "") for tc in tool_calls]

        expected_tools = []
        is_chain = False
        if isinstance(expected, dict):
            if "tool" in expected:
                expected_tools = [expected["tool"]]
            elif "tools_used" in expected:
                expected_tools = expected.get("tools_used", [])
            elif "tools" in expected:
                expected_tools = expected.get("tools", [])
            elif "chain" in expected:
                expected_tools = expected.get("chain", [])
                is_chain = True
        elif isinstance(expected, list):
            expected_tools = expected

        # Calculate score
        if not expected_tools:
            score = 0.0
        else:
            # Check if expected tools were called
            expected_set = set(expected_tools)
            called_set = set(called_tools)

            correct = expected_set & called_set

            score = len(correct) / len(expected_set) if expected_set else 1.0

            # For chains, also verify sequential order
            if is_chain and score > 0:
                if not self._check_chain_order(called_tools, expected_tools):
                    score *= 0.5  # Penalize wrong order

        status = "passed" if score >= 0.8 else "failed"

        return EvaluationResult(
            score=score,
            status=status,
            details={
                "called_tools": called_tools,
                "expected_tools": expected_tools,
                "missing_tools": list(set(expected_tools) - set(called_tools)),
                "scoring_method": "chain_validation" if is_chain else "tool_validation",
                "chain_order_correct": self._check_chain_order(called_tools, expected_tools) if is_chain else None,
                "pass2": {
                    "success": True,
                    "format": "direct_tool_calls"
                }
            },
            extracted_answer=', '.join(called_tools),
            pass2_used=False  # No PASS2 needed for direct tool calls
        )
    
    @staticmethod
    def _check_chain_order(called: List[str], chain: List[str]) -> bool:
        """Check that chain tools appear as a subsequence in called tools."""
        it = iter(called)
        return all(tool in it for tool in chain)

    def _validate_tool_names(self, tool_names: List[str], expected: Any,
                             level: int, extraction: Dict) -> EvaluationResult:
        """Validate extracted tool names"""

        expected_tools = []
        is_chain = False
        if isinstance(expected, dict):
            # Handle both "tool" (singular) and "tools" (plural)
            if "tool" in expected:
                expected_tools = [expected["tool"]]
            elif "tools" in expected:
                expected_tools = expected.get("tools", [])
            elif "chain" in expected:
                expected_tools = expected.get("chain", [])
                is_chain = True
        elif isinstance(expected, list):
            expected_tools = expected

        # Calculate score
        if not expected_tools:
            score = 0.0
        else:
            expected_set = set(expected_tools)
            called_set = set(tool_names)

            correct = expected_set & called_set

            score = len(correct) / len(expected_set) if expected_set else 1.0

            # For chains, also verify sequential order
            if is_chain and score > 0:
                if not self._check_chain_order(tool_names, expected_tools):
                    score *= 0.5  # Penalize wrong order

        status = "passed" if score >= 0.8 else "failed"

        return EvaluationResult(
            score=score,
            status=status,
            details={
                "called_tools": tool_names,
                "expected_tools": expected_tools,
                "missing_tools": list(set(expected_tools) - set(tool_names)),
                "scoring_method": "chain_validation" if is_chain else "tool_validation",
                "chain_order_correct": self._check_chain_order(tool_names, expected_tools) if is_chain else None,
                "pass2": {
                    "success": True,
                    "format": extraction.get("expected_format")
                }
            },
            extracted_answer=', '.join(tool_names),
            pass2_used=True
        )
