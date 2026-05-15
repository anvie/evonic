"""
Answer Extractor Module - Two-Pass LLM Evaluation

PASS 1: LLM generates answer with reasoning
PASS 2: LLM extracts ONLY the final answer in strict format

This module handles PASS 2 - extracting clean answers from verbose LLM responses.
"""

from typing import Dict, Any, Optional
from evaluator.llm_client import llm_client, strip_thinking_tags
import config
import re


# Extraction prompt templates per domain/level
# Each template instructs LLM to output ONLY the answer in specific format
EXTRACTION_PROMPTS = {
    "math": {
        "template": """You are given a question and an AI's response. Extract ONLY the final numeric answer.

---BEGIN RESPONSE---
{response}
---END RESPONSE---

Rules:
1. Return ONLY the number (no text, no explanation, no boxes)
2. Remove any formatting like commas, dots, or currency symbols
3. If the response contains a calculation, return only the final result

Your answer (number only):""",
        "expected_format": "number"
    },
    
    "reasoning": {
        1: {
            "template": """You are given a question and an AI's response. Extract ONLY the final answer: "ya" or "tidak".

---BEGIN RESPONSE---
{response}
---END RESPONSE---

Rules:
1. Return ONLY "ya" or "tidak" (one word, lowercase)
2. No explanation, no reasoning, no other text

Your answer (ya/tidak only):""",
            "expected_format": "boolean"
        },
        
        2: {
            "template": """You are given a question and an AI's response. Extract ONLY the final answer: "ya" or "tidak".

---BEGIN RESPONSE---
{response}
---END RESPONSE---

Rules:
1. Return ONLY "ya" or "tidak" (one word, lowercase)
2. No explanation, no reasoning, no other text

Your answer (ya/tidak only):""",
            "expected_format": "boolean"
        },
        
        3: {
            "template": """You are given a question and an AI's response. Extract ONLY the final answer (a word, phrase, or short sentence).

---BEGIN RESPONSE---
{response}
---END RESPONSE---

Rules:
1. Return ONLY the answer itself (no explanation, no punctuation at the end)
2. If it's an analogy completion, return only the missing word
3. If it's a deduction or causal question, return the full conclusion sentence
4. Lowercase only

Your answer:""",
            "expected_format": "text"
        },
        
        4: {
            "template": """You are given a question about causality and an AI's response. Evaluate if the response correctly considers alternative explanations.

---BEGIN RESPONSE---
{response}
---END RESPONSE---

Rules:
1. Return "ya" if the response considers multiple factors/alternatives
2. Return "tidak" if the response only considers one factor
3. Just one word, lowercase

Your answer (ya/tidak only):""",
            "expected_format": "boolean"
        },
        
        5: {
            "template": """You are given a question and an AI's response. Extract ONLY the final answer (number or single word).

---BEGIN RESPONSE---
{response}
---END RESPONSE---

Rules:
1. If the answer is a number, return ONLY the number
2. If the answer is a word, return ONLY that word (lowercase)
3. No explanation, no units, no other text

Your answer:""",
            "expected_format": "flexible"
        }
    },
    
    "sql": {
        "template": """You are given a question and an AI's response containing SQL. Extract ONLY the SQL query.

---BEGIN RESPONSE---
{response}
---END RESPONSE---

Rules:
1. Return ONLY the SQL statement (no markdown, no explanation)
2. The SQL should end with semicolon
3. No text before or after

Your answer (SQL only):""",
        "expected_format": "sql"
    },
    
    "tool_calling": {
        "template": """You are given a question and an AI's response containing tool calls. Extract ONLY the tool names.

---BEGIN RESPONSE---
{response}
---END RESPONSE---

Rules:
1. Return ONLY tool names separated by comma: tool1, tool2, tool3
2. No brackets, no explanation, no other text
3. Just the tool names

Your answer (tool names only):""",
        "expected_format": "tools"
    },
    
    "conversation": {
        "template": """Rate this conversation response.

Answer ONLY with three numbers (0.0 to 1.0) in this exact format:
relevance,correctness,fluency

Example: 0.8,0.9,0.7

No explanation. Just three numbers.

---BEGIN ANSWER---
{response}
---END ANSWER---

Your answer (three numbers only):""",
        "expected_format": "rubric"
    },
    
    "health": {
        "template": """You are given a health-related question and an AI's response. Extract the final answer.

---BEGIN RESPONSE---
{response}
---END RESPONSE---

Rules:
1. If the answer contains BMI and category, return in format: "BMI: X.XX, Kategori: ..."
2. If the answer is a single number, return ONLY the number
3. If the answer is yes/no, return ONLY "ya" or "tidak" (lowercase)
4. Preserve important information like BMI value AND category

Your answer:""",
        "expected_format": "health"
    }
}


class AnswerExtractor:
    """Extract clean final answers from LLM responses"""
    
    def __init__(self):
        self.client = llm_client
        self.temperature = getattr(config, 'TWO_PASS_TEMPERATURE', 0.0)

    def is_enabled(self) -> bool:
        """Whether Pass 2 extraction runs (DB setting overrides env default)."""
        try:
            from models.db import db
            stored = db.get_setting('two_pass_enabled', None)
            if stored is not None:
                return stored == '1'
        except Exception:
            pass
        return getattr(config, 'TWO_PASS_ENABLED', True)
    
    def extract(self, domain: str, level: int, response: str, question: str = "") -> Dict[str, Any]:
        """
        Extract final answer using LLM with strict format instructions.
        
        Uses multi-layer extraction:
        - Layer 1: LLM Extraction (primary)
        - Layer 2: Regex Fallback (for common patterns)
        - Layer 3: Domain heuristics (last resort)
        
        Args:
            domain: Test domain (math, reasoning, sql, etc.)
            level: Test level (1-5)
            response: Raw LLM response from PASS 1
            question: Original question/prompt from PASS 1 (for context)
            
        Returns:
            {
                "success": bool,
                "extracted": str,           # Clean answer from PASS 2
                "expected_format": str,     # What format was expected
                "raw_pass2": str,           # Raw PASS 2 output
                "pass2_prompt": str,        # Prompt used for PASS 2
                "parse_error": Optional[str],
                "extraction_method": str    # How extraction was done
            }
        """
        # Check if two-pass is enabled
        if not self.is_enabled():
            return {
                "success": True,
                "extracted": response,
                "expected_format": "raw",
                "raw_pass2": "",
                "pass2_prompt": "",
                "parse_error": None,
                "extraction_method": "disabled"
            }
        
        # Get extraction prompt (include question for context)
        prompt_data = self._get_extraction_prompt(domain, level, response, question)
        
        if not prompt_data:
            return {
                "success": True,
                "extracted": response,
                "expected_format": "raw",
                "raw_pass2": "",
                "pass2_prompt": "",
                "parse_error": None,
                "extraction_method": "no_prompt"
            }

        prompt = prompt_data["prompt"]
        expected_format = prompt_data["expected_format"]
        
        # LAYER 1: PASS 2 - Call LLM to extract clean answer
        messages = [{"role": "user", "content": prompt}]
        
        try:
            llm_response = self.client.chat_completion(
                messages,
                temperature=self.temperature,
                tools=None
            )
            
            # Use extract_content_with_thinking to handle both:
            # 1. llama.cpp --reasoning mode (reasoning_content field)
            # 2. Tag-based thinking (<think> tags in content)
            content_info = self.client.extract_content_with_thinking(llm_response)
            cleaned_pass2 = content_info["content"].strip()
            thinking_pass2 = content_info["thinking"]
            raw_pass2 = content_info["raw"] or cleaned_pass2
            
            # Validate the format (use cleaned version without thinking)
            validated = self._validate_format(cleaned_pass2, expected_format)
            
            if validated["valid"]:
                return {
                    "success": True,
                    "extracted": validated["cleaned"],
                    "expected_format": expected_format,
                    "raw_pass2": cleaned_pass2,
                    "pass2_prompt": prompt,
                    "pass2_thinking": thinking_pass2,
                    "parse_error": None,
                    "extraction_method": "llm"
                }
            else:
                # LLM extraction failed - TRY LAYER 2: Regex fallback
                fallback_result = self._try_regex_fallback(response, expected_format, domain)
                
                if fallback_result["success"]:
                    return {
                        "success": True,
                        "extracted": fallback_result["extracted"],
                        "expected_format": expected_format,
                        "raw_pass2": cleaned_pass2,
                        "pass2_prompt": prompt,
                        "pass2_thinking": thinking_pass2,
                        "parse_error": None,
                        "extraction_method": fallback_result["method"]
                    }
                else:
                    # All extraction methods failed
                    return {
                        "success": False,
                        "extracted": cleaned_pass2,
                        "expected_format": expected_format,
                        "raw_pass2": cleaned_pass2,
                        "pass2_prompt": prompt,
                        "pass2_thinking": thinking_pass2,
                        "parse_error": validated["error"],
                        "extraction_method": "failed"
                    }
                
        except Exception as e:
            # LLM call failed - try regex fallback
            fallback_result = self._try_regex_fallback(response, expected_format, domain)
            
            if fallback_result["success"]:
                return {
                    "success": True,
                    "extracted": fallback_result["extracted"],
                    "expected_format": expected_format,
                    "raw_pass2": "",
                    "pass2_prompt": prompt,
                    "parse_error": None,
                    "extraction_method": fallback_result["method"]
                }
            else:
                return {
                    "success": False,
                    "extracted": response,
                    "expected_format": expected_format,
                    "raw_pass2": "",
                    "pass2_prompt": prompt,
                    "parse_error": f"Extraction error: {str(e)}",
                    "extraction_method": "error"
                }
    
    def _try_regex_fallback(self, response: str, expected_format: str, domain: str) -> Dict[str, Any]:
        """
        Layer 2 & 3: Try regex patterns and domain heuristics.
        
        Returns:
            {"success": bool, "extracted": str, "method": str}
        """
        
        # For number format, try regex patterns
        if expected_format == "number":
            # Pattern 1: \boxed{n} (LaTeX)
            match = re.search(r'\\boxed\{(\d+(?:\.\d+)?)\}', response)
            if match:
                return {"success": True, "extracted": match.group(1), "method": "regex_boxed"}
            
            # Pattern 2: "answer is X" or "hasil adalah X"
            match = re.search(r'(?:answer|hasil|jawaban)\s*(?:is|nya|ialah|adalah)[:\s]*(\d+(?:\.\d+)?)', response, re.IGNORECASE)
            if match:
                return {"success": True, "extracted": match.group(1), "method": "regex_answer_is"}
            
            # Pattern 3: "= X" at end
            match = re.search(r'=\s*(\d+(?:\.\d+)?)\s*[.\s]*$', response)
            if match:
                return {"success": True, "extracted": match.group(1), "method": "regex_equals_end"}
            
            # Pattern 4: Last number in response (for math)
            all_numbers = re.findall(r'\d+(?:\.\d+)?', response)
            if all_numbers and domain == "math":
                return {"success": True, "extracted": all_numbers[-1], "method": "heuristic_last_number"}
        
        elif expected_format == "boolean":
            lower = response.lower()
            if "ya" in lower or "yes" in lower:
                return {"success": True, "extracted": "ya", "method": "heuristic_boolean"}
            if "tidak" in lower or "no" in lower:
                return {"success": True, "extracted": "tidak", "method": "heuristic_boolean"}
        
        elif expected_format == "sql":
            match = re.search(r'(SELECT\s+.*?(?:;|$))', response, re.IGNORECASE | re.DOTALL)
            if match:
                return {"success": True, "extracted": match.group(1).strip(), "method": "regex_sql"}

        elif expected_format == "health":
            lower = response.lower()
            # Boolean answers
            if re.search(r'\bya\b', lower):
                return {"success": True, "extracted": "ya", "method": "heuristic_health_boolean"}
            if re.search(r'\btidak\b', lower):
                return {"success": True, "extracted": "tidak", "method": "heuristic_health_boolean"}
            # Structured BMI format
            bmi_match = re.search(r'bmi[:\s]*(\d+\.?\d*)', lower)
            if bmi_match:
                kategori_match = re.search(r'kategori[:\s]*([^\n,.]+)', lower)
                result = f"BMI: {bmi_match.group(1)}"
                if kategori_match:
                    result += f", Kategori: {kategori_match.group(1).strip()}"
                return {"success": True, "extracted": result, "method": "regex_health_bmi"}
            # Numeric answer (heart rate, weight, etc.)
            numbers = re.findall(r'[-+]?\d+\.?\d*', response)
            if numbers:
                return {"success": True, "extracted": numbers[-1], "method": "heuristic_health_number"}

        # No fallback worked
        return {"success": False, "extracted": response, "method": "no_fallback"}
    
    def _get_extraction_prompt(self, domain: str, level: int, response: str, question: str = "") -> Optional[Dict]:
        """Get extraction prompt and expected format for domain/level
        
        Args:
            domain: Test domain
            level: Test level
            response: Model response from PASS 1
            question: Original question for context
        """
        
        # Build question context section if available
        question_context = ""
        if question:
            question_context = f"""
---ORIGINAL QUESTION---
{question[:1000]}
---END QUESTION---

"""
        
        if domain == "reasoning":
            # Reasoning has level-specific prompts
            level_prompts = EXTRACTION_PROMPTS.get("reasoning", {})
            if level in level_prompts:
                data = level_prompts[level]
                template = data["template"]
                # Replace {response} placeholder, add question context before it
                prompt = question_context + template.format(response=response)
                return {
                    "prompt": prompt,
                    "expected_format": data["expected_format"]
                }
        elif domain in EXTRACTION_PROMPTS:
            data = EXTRACTION_PROMPTS[domain]
            if "template" in data:
                template = data["template"]
                # Replace {response} placeholder, add question context before it
                prompt = question_context + template.format(response=response)
                return {
                    "prompt": prompt,
                    "expected_format": data["expected_format"]
                }
        
        # No extraction prompt - return original response
        return None
    
    def _validate_format(self, raw: str, expected_format: str) -> Dict[str, Any]:
        """
        Validate that PASS 2 output follows expected format.
        
        Returns:
            {
                "valid": bool,
                "cleaned": str,    # Cleaned/normalized answer
                "error": str       # Error message if invalid
            }
        """
        
        raw = raw.strip()
        
        if expected_format == "number":
            # Should be a single number (integer or float)
            # Remove common artifacts
            cleaned = raw.replace('Rp', '').replace('rp', '').strip()
            cleaned = cleaned.replace(',', '').replace('.', '')  # Remove separators for Indonesian format
            
            # Try to extract number
            match = re.match(r'^[-+]?\d+$', cleaned)
            if match:
                return {"valid": True, "cleaned": cleaned, "error": ""}
            
            # Try float pattern
            match = re.match(r'^[-+]?\d+\.?\d*$', cleaned)
            if match:
                return {"valid": True, "cleaned": cleaned, "error": ""}
            
            # Maybe has explanation - try to extract first number
            numbers = re.findall(r'[-+]?\d+\.?\d*', cleaned)
            if numbers and len(numbers) == 1:
                return {"valid": True, "cleaned": numbers[0], "error": ""}
            
            return {"valid": False, "cleaned": raw, "error": f"Expected single number, got: {raw[:100]}"}
        
        elif expected_format == "boolean":
            # Should be "ya" or "tidak"
            lower = raw.lower().strip()
            if lower in ["ya", "tidak"]:
                return {"valid": True, "cleaned": lower, "error": ""}
            return {"valid": False, "cleaned": raw, "error": f"Expected 'ya' or 'tidak', got: {raw[:50]}"}
        
        elif expected_format == "sequence":
            # Should be: 3, 7, 15, 18, 22
            # Remove brackets if present
            cleaned = raw.replace('[', '').replace(']', '').strip()
            
            # Try to parse as comma-separated numbers
            parts = [p.strip() for p in cleaned.split(',')]
            try:
                numbers = [int(p) for p in parts if p]
                if len(numbers) >= 2:
                    return {"valid": True, "cleaned": ', '.join(map(str, numbers)), "error": ""}
            except ValueError:
                pass
            
            return {"valid": False, "cleaned": raw, "error": f"Expected number sequence, got: {raw[:100]}"}
        
        elif expected_format == "statements":
            # Should be: 2, 4 or similar
            parts = [p.strip() for p in raw.split(',')]
            try:
                numbers = [int(p) for p in parts if p]
                if numbers:
                    return {"valid": True, "cleaned": ', '.join(map(str, numbers)), "error": ""}
            except ValueError:
                pass
            return {"valid": False, "cleaned": raw, "error": f"Expected statement numbers, got: {raw[:50]}"}
        
        elif expected_format == "sql":
            # Strip markdown code fences if present
            cleaned_sql = re.sub(r'```(?:sql)?\s*', '', raw)
            cleaned_sql = cleaned_sql.replace('```', '').strip()

            # Normalize typographic quotes to straight ASCII quotes (SQL requires ASCII delimiters)
            # This fixes PASS 2 LLM extraction where normalize_llm_text() converts ' to ’
            cleaned_sql = (
                cleaned_sql
                .replace('‘', "'")  # left single quotation mark → '
                .replace('’', "'")  # right single quotation mark → '
                .replace('“', '"')  # left double quotation mark → "
                .replace('”', '"')  # right double quotation mark → "
            )

            upper = cleaned_sql.upper()
            if "SELECT" in upper:
                return {"valid": True, "cleaned": cleaned_sql, "error": ""}
            return {"valid": False, "cleaned": cleaned_sql, "error": "Expected SQL query"}
        
        elif expected_format == "tools":
            # Should be: tool1, tool2
            parts = [p.strip() for p in raw.split(',') if p.strip()]
            if parts:
                return {"valid": True, "cleaned": ', '.join(parts), "error": ""}
            return {"valid": False, "cleaned": raw, "error": "Expected tool names"}
        
        elif expected_format == "rubric":
            # Should be: 0.8,0.9,0.7
            parts = raw.split(',')
            if len(parts) == 3:
                try:
                    scores = [float(p.strip()) for p in parts]
                    if all(0 <= s <= 1 for s in scores):
                        return {"valid": True, "cleaned": raw, "error": ""}
                except ValueError:
                    pass
            return {"valid": False, "cleaned": raw, "error": "Expected three scores (0.0-1.0)"}
        
        elif expected_format == "text":
            # Should be a single word or short phrase (for analogy, word completion, etc.)
            # Clean up common artifacts
            cleaned = raw.strip().lower()
            # Remove quotes, periods, extra punctuation
            cleaned = cleaned.strip('"\'.,!?')
            # Take first word if multiple words (model might add explanation)
            if ' ' in cleaned:
                # Check if it's just the word with some fluff
                words = cleaned.split()
                # First word is likely the answer
                cleaned = words[0].strip('"\'.,!?')
            if cleaned:
                return {"valid": True, "cleaned": cleaned, "error": ""}
            return {"valid": False, "cleaned": raw, "error": f"Expected text answer, got empty"}
        
        elif expected_format == "health":
            # Health answers can be numeric, boolean, or structured text (BMI + category)
            cleaned = raw.strip()
            cleaned_lower = cleaned.lower()
            
            # Check for boolean first
            if cleaned_lower in ["ya", "tidak"]:
                return {"valid": True, "cleaned": cleaned_lower, "error": ""}
            
            # Check for structured format (BMI: X.XX, Kategori: ...)
            if "bmi" in cleaned_lower or "kategori" in cleaned_lower:
                return {"valid": True, "cleaned": cleaned, "error": ""}
            
            # Check for number (including decimals)
            # Remove common units
            num_cleaned = cleaned.replace('kg', '').replace('cm', '').replace('bpm', '').replace('liter', '').strip()
            match = re.match(r'^[-+]?\d+\.?\d*$', num_cleaned)
            if match:
                return {"valid": True, "cleaned": num_cleaned, "error": ""}
            
            # Try to extract first number
            numbers = re.findall(r'[-+]?\d+\.?\d*', cleaned)
            if numbers:
                return {"valid": True, "cleaned": numbers[0], "error": ""}
            
            return {"valid": False, "cleaned": raw, "error": f"Expected number or ya/tidak, got: {raw[:50]}"}
        
        elif expected_format == "flexible":
            # Flexible format - can be number or word
            cleaned = raw.strip().lower()
            # Remove common punctuation
            cleaned = cleaned.strip('"\'.,!?')
            
            # If it's a number, return as-is
            try:
                float(cleaned)
                return {"valid": True, "cleaned": cleaned, "error": ""}
            except ValueError:
                pass
            
            # If it's a single word, return it
            if ' ' not in cleaned and cleaned:
                return {"valid": True, "cleaned": cleaned, "error": ""}
            
            # Try to get first word/number
            words = cleaned.split()
            if words:
                first = words[0].strip('"\'.,!?')
                return {"valid": True, "cleaned": first, "error": ""}
            
            return {"valid": False, "cleaned": raw, "error": f"Could not extract answer from: {raw[:50]}"}
        
        # Default: accept any text
        return {"valid": True, "cleaned": raw, "error": ""}


# Global extractor instance
answer_extractor = AnswerExtractor()
