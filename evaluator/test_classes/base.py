from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class BaseTest(ABC):
    """Base class for all test types"""
    
    def __init__(self, level: int):
        self.level = level
        self.domain = self.__class__.__name__.replace("Test", "").lower()
    
    @abstractmethod
    def get_prompt(self) -> str:
        """Get the prompt to send to LLM"""
        pass
    
    @abstractmethod
    def get_expected(self) -> Any:
        """Get expected output or validation criteria"""
        pass
    
    @abstractmethod
    def score_response(self, response: str, expected: Any) -> Dict[str, Any]:
        """Score the LLM response (expects clean extracted answer from PASS 2)"""
        pass
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert test to dictionary"""
        return {
            "domain": self.domain,
            "level": self.level,
            "prompt": self.get_prompt(),
            "expected": self.get_expected()
        }
    
    def run(self) -> Dict[str, Any]:
        """Run the test and return results"""
        prompt = self.get_prompt()
        expected = self.get_expected()
        
        return {
            "domain": self.domain,
            "level": self.level,
            "prompt": prompt,
            "expected": expected
        }


# Concrete test classes are in separate files:
# - tests/conversation.py - ConversationTest
# - tests/math.py - MathTest
# - tests/sql_gen.py - SQLGenTest
# - tests/tool_calling.py - ToolCallingTest
# - tests/reasoning.py - ReasoningTest
