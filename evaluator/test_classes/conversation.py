from .base import BaseTest
from typing import Dict, Any


class ConversationTest(BaseTest):
    """Indonesian conversational Q&A tests
    
    Note: This test class is NOT used by KeywordEvaluator.
    KeywordEvaluator handles scoring directly with keyword matching.
    This class only provides prompts and expected keywords.
    """
    
    def get_prompt(self) -> str:
        prompts = {
            1: "Halo! Bisa tolong perkenalkan diri Anda?",
            2: "Apa ibu kota Indonesia dan mengapa kota tersebut penting?",
            3: "Jelaskan apa yang dimaksud dengan 'startup' dalam konteks bisnis teknologi.",
            4: "Saya sedang mempertimbangkan untuk memulai bisnis e-commerce di Indonesia. Bisa berikan saran tentang platform yang cocok dan strategi pemasaran yang efektif?",
            5: "Sebagai konsultan teknologi, bagaimana Anda akan menyarankan perusahaan tradisional untuk beradaptasi dengan transformasi digital? Berikan contoh konkret untuk sektor retail."
        }
        return prompts.get(self.level, "")
    
    def get_expected(self) -> Dict[str, Any]:
        """Expected keywords for scoring"""
        return {
            "relevance_weight": 0.3,
            "correctness_weight": 0.4,
            "fluency_weight": 0.3,
            "keywords": self._get_expected_keywords()
        }
    
    def _get_expected_keywords(self) -> list:
        """Get expected keywords for each level"""
        keywords = {
            1: ["ai", "assistant", "membantu", "help", "asisten", "saya", "i am", "qwen", "alibaba", "llm", "model"],
            2: ["jakarta", "ibu kota", "indonesia", "pusat", "pemerintahan", "nusantara", "ikn"],
            3: ["startup", "teknologi", "bisnis", "inovasi", "perusahaan", "skala", "pertumbuhan"],
            4: ["e-commerce", "tokopedia", "shopee", "pemasaran", "digital", "marketplace", "strategi"],
            5: ["transformasi digital", "retail", "teknologi", "adaptasi", "contoh", "online", "omnichannel"]
        }
        return keywords.get(self.level, [])
    
    def score_response(self, response: str, expected: Dict[str, Any]) -> Dict[str, Any]:
        """
        Score response - used as fallback if KeywordEvaluator is not available.
        
        KeywordEvaluator handles scoring directly, but this method is kept
        for compatibility and testing purposes.
        """
        # This is now handled by KeywordEvaluator, but kept for fallback
        from evaluator.strategies.keyword import KeywordEvaluator
        evaluator = KeywordEvaluator("conversation")
        result = evaluator.evaluate(response, expected, self.level)
        
        return {
            "score": result.score,
            "status": result.status,
            "details": result.details
        }
