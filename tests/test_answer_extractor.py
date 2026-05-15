"""
Unit tests for the Answer Extractor module (two-pass evaluation)
"""

import pytest
from evaluator.answer_extractor import answer_extractor


class TestTwoPassEnabled:
    """Runtime toggle for Pass 2 extraction."""

    def test_is_enabled_respects_config_default(self, monkeypatch):
        monkeypatch.setattr('evaluator.answer_extractor.config.TWO_PASS_ENABLED', True)
        # No DB in unit test — should fall back to config
        assert answer_extractor.is_enabled() is True

    def test_is_enabled_when_disabled_in_config(self, monkeypatch):
        monkeypatch.setattr('evaluator.answer_extractor.config.TWO_PASS_ENABLED', False)
        assert answer_extractor.is_enabled() is False


class TestFormatValidation:
    """Test format validation for PASS 2 output"""
    
    def test_validate_number_clean(self):
        """Clean number should validate"""
        result = answer_extractor._validate_format("36", "number")
        assert result["valid"] is True
        assert result["cleaned"] == "36"
    
    def test_validate_number_with_decimals(self):
        """Float number should validate"""
        result = answer_extractor._validate_format("820800", "number")
        assert result["valid"] is True
    
    def test_validate_number_large(self):
        """Large number should validate"""
        result = answer_extractor._validate_format("11236000", "number")
        assert result["valid"] is True
    
    def test_validate_number_negative(self):
        """Negative number should validate"""
        result = answer_extractor._validate_format("-42", "number")
        assert result["valid"] is True
    
    def test_validate_boolean_ya(self):
        """'ya' should validate"""
        result = answer_extractor._validate_format("ya", "boolean")
        assert result["valid"] is True
        assert result["cleaned"] == "ya"
    
    def test_validate_boolean_tidak(self):
        """'tidak' should validate"""
        result = answer_extractor._validate_format("tidak", "boolean")
        assert result["valid"] is True
        assert result["cleaned"] == "tidak"
    
    def test_validate_boolean_wrong(self):
        """Wrong boolean should fail"""
        result = answer_extractor._validate_format("yes", "boolean")
        assert result["valid"] is False
    
    def test_validate_sequence(self):
        """Number sequence should validate"""
        result = answer_extractor._validate_format("3, 7, 15, 18, 22", "sequence")
        assert result["valid"] is True
        assert result["cleaned"] == "3, 7, 15, 18, 22"
    
    def test_validate_sequence_no_spaces(self):
        """Sequence without spaces should validate"""
        result = answer_extractor._validate_format("3,7,15,18,22", "sequence")
        assert result["valid"] is True
    
    def test_validate_sequence_with_brackets(self):
        """Sequence with brackets should validate"""
        result = answer_extractor._validate_format("[3, 7, 15, 18, 22]", "sequence")
        assert result["valid"] is True
    
    def test_validate_statements(self):
        """Statement numbers should validate"""
        result = answer_extractor._validate_format("2, 4", "statements")
        assert result["valid"] is True
    
    def test_validate_sql(self):
        """SQL query should validate"""
        result = answer_extractor._validate_format(
            "SELECT name, email FROM customers WHERE city = 'Jakarta'",
            "sql"
        )
        assert result["valid"] is True
    
    def test_validate_sql_lowercase(self):
        """Lowercase SQL should validate"""
        result = answer_extractor._validate_format(
            "select * from users",
            "sql"
        )
        assert result["valid"] is True
    
    def test_validate_sql_no_select(self):
        """Non-SQL should fail"""
        result = answer_extractor._validate_format("This is not SQL", "sql")
        assert result["valid"] is False
    
    def test_validate_tools(self):
        """Tool names should validate"""
        result = answer_extractor._validate_format("get_weather, get_time", "tools")
        assert result["valid"] is True
    
    def test_validate_rubric(self):
        """Rubric scores should validate"""
        result = answer_extractor._validate_format("0.8,0.9,0.7", "rubric")
        assert result["valid"] is True
    
    def test_validate_rubric_wrong_count(self):
        """Wrong number of scores should fail"""
        result = answer_extractor._validate_format("0.8,0.9", "rubric")
        assert result["valid"] is False
    
    def test_validate_rubric_out_of_range(self):
        """Out of range scores should fail"""
        result = answer_extractor._validate_format("1.5,0.9,0.7", "rubric")
        assert result["valid"] is False


class TestExtractionPrompts:
    """Test that extraction prompts are generated correctly"""
    
    def test_math_prompt(self):
        """Math extraction prompt should ask for number only"""
        prompt_data = answer_extractor._get_extraction_prompt("math", 2, "some answer")
        assert prompt_data is not None
        assert "number" in prompt_data["prompt"].lower()
        assert prompt_data["expected_format"] == "number"
    
    def test_reasoning_l1_prompt(self):
        """Reasoning L1 should ask for ya/tidak"""
        prompt_data = answer_extractor._get_extraction_prompt("reasoning", 1, "some answer")
        assert prompt_data is not None
        assert "ya" in prompt_data["prompt"].lower() or "tidak" in prompt_data["prompt"].lower()
        assert prompt_data["expected_format"] == "boolean"
    
    def test_reasoning_l2_prompt(self):
        """Reasoning L2 should ask for boolean (ya/tidak)"""
        prompt_data = answer_extractor._get_extraction_prompt("reasoning", 2, "some answer")
        assert prompt_data is not None
        assert prompt_data["expected_format"] == "boolean"
    
    def test_reasoning_l3_prompt(self):
        """Reasoning L3 should ask for text (analogy answers)"""
        prompt_data = answer_extractor._get_extraction_prompt("reasoning", 3, "some answer")
        assert prompt_data is not None
        assert prompt_data["expected_format"] == "text"
    
    def test_reasoning_l4_prompt(self):
        """Reasoning L4 should ask for boolean (causal reasoning)"""
        prompt_data = answer_extractor._get_extraction_prompt("reasoning", 4, "some answer")
        assert prompt_data is not None
        assert prompt_data["expected_format"] == "boolean"
    
    def test_reasoning_l5_prompt(self):
        """Reasoning L5 should ask for flexible format (number or text)"""
        prompt_data = answer_extractor._get_extraction_prompt("reasoning", 5, "some answer")
        assert prompt_data is not None
        assert prompt_data["expected_format"] == "flexible"
    
    def test_sql_prompt(self):
        """SQL extraction prompt should ask for SQL"""
        prompt_data = answer_extractor._get_extraction_prompt("sql", 1, "some answer")
        assert prompt_data is not None
        assert prompt_data["expected_format"] == "sql"
    
    def test_unknown_domain(self):
        """Unknown domain should return None"""
        prompt_data = answer_extractor._get_extraction_prompt("unknown", 1, "some answer")
        assert prompt_data is None


class TestMathScoring:
    """Test math test scoring with clean inputs"""
    
    def test_math_correct(self):
        """Correct math answer should score 1.0"""
        from tests.math import MathTest
        test = MathTest(2)  # 15% of 240 = 36
        result = test.score_response("36", 36.0)
        assert result["score"] == 1.0
    
    def test_math_wrong(self):
        """Wrong math answer should score 0.0"""
        from tests.math import MathTest
        test = MathTest(2)
        result = test.score_response("35", 36.0)
        assert result["score"] == 0.0
    
    def test_math_with_explanation_fallback(self):
        """Math with explanation should still extract"""
        from tests.math import MathTest
        test = MathTest(2)
        result = test.score_response("36", 36.0)  # Clean from PASS 2
        assert result["score"] == 1.0


class TestReasoningScoring:
    """Test reasoning test scoring with clean inputs"""
    
    def test_reasoning_l1_correct(self):
        """Correct boolean answer should score 1.0"""
        from tests.reasoning import ReasoningTest
        test = ReasoningTest(1)
        result = test.score_response("ya", "ya")
        assert result["score"] == 1.0
    
    def test_reasoning_l2_correct(self):
        """Correct sequence should score 1.0"""
        from tests.reasoning import ReasoningTest
        test = ReasoningTest(2)
        result = test.score_response("3, 7, 15, 18, 22", [3, 7, 15, 18, 22])
        assert result["score"] == 1.0
    
    def test_reasoning_l2_wrong(self):
        """Wrong sequence should score 0.0"""
        from tests.reasoning import ReasoningTest
        test = ReasoningTest(2)
        result = test.score_response("1, 2, 3, 4, 5", [3, 7, 15, 18, 22])
        assert result["score"] == 0.0
    
    def test_reasoning_l3_correct(self):
        """Correct team count should score 1.0"""
        from tests.reasoning import ReasoningTest
        test = ReasoningTest(3)
        result = test.score_response("17", 17)
        assert result["score"] == 1.0
    
    def test_reasoning_l4_correct(self):
        """Correct statements should score 1.0"""
        from tests.reasoning import ReasoningTest
        test = ReasoningTest(4)
        result = test.score_response("2, 4", [2, 4])
        assert result["score"] == 1.0
    
    def test_reasoning_l5_correct(self):
        """Correct currency should score 1.0"""
        from tests.reasoning import ReasoningTest
        test = ReasoningTest(5)
        result = test.score_response("820800", 820800.0)
        assert result["score"] == 1.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
