"""Test modules for LLM evaluation"""

from .base import BaseTest
from .conversation import ConversationTest
from .math import MathTest
from .sql_gen import SQLGenTest
from .tool_calling import ToolCallingTest
from .reasoning import ReasoningTest
from .health import HealthTest
from .coding import CodingTest

# Test registry
TEST_CLASSES = {
    "conversation": ConversationTest,
    "math": MathTest,
    "sql": SQLGenTest,
    "tool_calling": ToolCallingTest,
    "reasoning": ReasoningTest,
    "health": HealthTest,
    "coding": CodingTest
}

def get_test_class(domain: str):
    """Get test class for domain"""
    return TEST_CLASSES.get(domain.lower())
