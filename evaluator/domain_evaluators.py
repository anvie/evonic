"""
Domain Evaluator Registry

Maps each domain to its preferred evaluation strategy.
"""

from typing import Dict, Type, Optional
from .strategies.base import BaseEvaluator
from .strategies.two_pass import TwoPassEvaluator
from .strategies.keyword import KeywordEvaluator
from .strategies.sql_executor import SQLExecutorEvaluator
from .strategies.tool_call import ToolCallEvaluator
from .strategies.icd_code import IcdCodeEvaluator
from .strategies.knowledge_builder import KnowledgeBuilderEvaluator
from .strategies.kb_organizer import KBOrganizerEvaluator


# Domain → Evaluator mapping
DOMAIN_EVALUATORS: Dict[str, Type[BaseEvaluator]] = {
    "math": TwoPassEvaluator,
    "reasoning": TwoPassEvaluator,
    "sql": SQLExecutorEvaluator,
    "conversation": KeywordEvaluator,  # No PASS2 needed - uses keyword matching
    "tool_calling": ToolCallEvaluator,
    "health": TwoPassEvaluator,  # Health domain - uses two-pass for numeric/text extraction
    "icd_coding": IcdCodeEvaluator,
    "knowledge_builder": KnowledgeBuilderEvaluator,  # Deterministic author-docs JSON validator
}

# Evaluator type name → class mapping for config overrides
EVALUATOR_TYPES = {
    "two_pass": TwoPassEvaluator,
    "keyword": KeywordEvaluator,
    "sql_executor": SQLExecutorEvaluator,
    "tool_call": ToolCallEvaluator,
    "icd_code_judge": IcdCodeEvaluator,
    "knowledge_builder": KnowledgeBuilderEvaluator,
    "kb_organizer": KBOrganizerEvaluator,
}


# Cache for evaluator instances
_evaluator_cache: Dict[str, BaseEvaluator] = {}


def get_evaluator(domain: str, evaluator_type: Optional[str] = None) -> BaseEvaluator:
    """
    Get the appropriate evaluator for a domain.
    
    Args:
        domain: Test domain name
        evaluator_type: Optional override for evaluator type
        
    Returns:
        Evaluator instance for the domain
    """
    cache_key = f"{domain}:{evaluator_type or 'default'}"
    
    if cache_key in _evaluator_cache:
        return _evaluator_cache[cache_key]
    
    # Determine evaluator class
    if evaluator_type and evaluator_type in EVALUATOR_TYPES:
        evaluator_class = EVALUATOR_TYPES[evaluator_type]
    else:
        evaluator_class = DOMAIN_EVALUATORS.get(domain, TwoPassEvaluator)
    
    # Instantiate with domain name
    evaluator = evaluator_class(domain)
    
    # Cache it
    _evaluator_cache[cache_key] = evaluator
    
    return evaluator


def get_evaluator_info(domain: str) -> Dict:
    """Get information about the evaluator for a domain"""
    evaluator = get_evaluator(domain)
    return {
        "name": evaluator.name,
        "uses_pass2": evaluator.uses_pass2,
        "domain": domain
    }


def list_evaluators() -> Dict[str, Dict]:
    """List all domain-evaluator mappings"""
    return {
        domain: get_evaluator_info(domain)
        for domain in DOMAIN_EVALUATORS
    }


def clear_cache():
    """Clear evaluator cache"""
    global _evaluator_cache
    _evaluator_cache = {}
