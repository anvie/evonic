from .base import BaseTest
from typing import Dict, Any
import re


class ReasoningTest(BaseTest):
    """Logical reasoning tests - expects clean format from PASS 2"""
    
    def get_prompt(self) -> str:
        prompts = {
            1: "Jika hari ini hujan, maka saya akan membawa payung. Hari ini hujan. Apakah saya akan membawa payung?",
            2: "Urutkan angka berikut dari terkecil ke terbesar: 15, 3, 22, 7, 18",
            3: """Sebuah perusahaan memiliki 3 tim: A, B, dan C. 
- Tim A memiliki 5 anggota
- Tim B memiliki 3 anggota lebih banyak dari Tim A
- Tim C memiliki setengah anggota Tim B
Berapa total anggota semua tim?""",
            4: """Dari pernyataan berikut, mana yang benar?
1. Semua burung bisa terbang
2. Beberapa burung bisa terbang
3. Tidak ada burung yang bisa terbang
4. Penguin adalah burung yang tidak bisa terbang""",
            5: """Sebuah toko memberikan diskon bertingkat:
- Diskon 20% untuk pembelian di atas Rp 500.000
- Diskon tambahan 10% untuk pembelian di atas Rp 1.000.000
- Diskon tambahan 5% untuk member loyal

Seorang member loyal membeli produk seharga Rp 1.200.000. Berapa harga yang harus dibayar setelah semua diskon?"""
        }
        return prompts.get(self.level, "")
    
    def get_expected(self) -> Any:
        expected = {
            1: "ya",
            2: [3, 7, 15, 18, 22],
            3: 17,  # Tim A=5, Tim B=5+3=8, Tim C=8/2=4, Total=5+8+4=17
            4: [2, 4],  # Pernyataan 2 dan 4 benar
            5: 820800.0  # 1,200,000 * 0.8 * 0.9 * 0.95
        }
        return expected.get(self.level, "")
    
    def score_response(self, response: str, expected: Any) -> Dict[str, Any]:
        """
        Score response - expects clean format from PASS 2.
        
        Level 1: "ya" or "tidak"
        Level 2: "3, 7, 15, 18, 22"
        Level 3: "17"
        Level 4: "2, 4"
        Level 5: "820800"
        """
        
        if self.level == 1:
            return self._score_level_1(response, expected)
        elif self.level == 2:
            return self._score_level_2(response, expected)
        elif self.level == 3:
            return self._score_level_3(response, expected)
        elif self.level == 4:
            return self._score_level_4(response, expected)
        elif self.level == 5:
            return self._score_level_5(response, expected)
        
        return {"score": 0.0, "details": "Unknown level"}
    
    def _score_level_1(self, response: str, expected: Any) -> Dict[str, Any]:
        """Level 1: Boolean - expects 'ya' or 'tidak'"""
        clean = response.strip().lower()
        
        # Handle expected in different formats (dict from configurable tests)
        expected_value = expected
        if isinstance(expected, dict):
            expected_value = expected.get('answer', expected.get('value', expected))
        
        # Case-insensitive comparison
        expected_clean = str(expected_value).strip().lower()
        
        if clean == expected_clean:
            return {"score": 1.0, "status": "passed", "details": f"Correct: {clean}"}
        else:
            return {"score": 0.0, "status": "failed", "details": f"Wrong: expected '{expected_clean}', got '{clean}'"}
    
    def _score_level_2(self, response: str, expected: Any) -> Dict[str, Any]:
        """Level 2: Boolean or Sequence - can be 'ya/tidak' or number sequence"""
        clean = response.strip().lower()
        
        # Handle expected in different formats (dict from configurable tests)
        expected_value = expected
        if isinstance(expected, dict):
            expected_value = expected.get('answer', expected.get('value', expected))
        
        # If expected is string (boolean answer like "Ya")
        if isinstance(expected_value, str):
            expected_clean = expected_value.strip().lower()
            if clean == expected_clean:
                return {"score": 1.0, "status": "passed", "details": f"Correct: {clean}"}
            else:
                return {"score": 0.0, "status": "failed", "details": f"Wrong: expected '{expected_clean}', got '{clean}'"}
        
        # If expected is list (sequence)
        if isinstance(expected_value, list):
            try:
                numbers = [int(n.strip()) for n in response.split(',')]
                if numbers == expected_value:
                    return {"score": 1.0, "status": "passed", "details": f"Correct: {numbers}"}
                else:
                    return {"score": 0.0, "status": "failed", "details": f"Wrong: expected {expected_value}, got {numbers}"}
            except ValueError:
                return {"score": 0.0, "status": "failed", "details": f"Invalid format: '{response}'"}
        
        return {"score": 0.0, "status": "failed", "details": f"Unknown expected type: {type(expected_value)}"}
    
    def _score_level_3(self, response: str, expected: Any) -> Dict[str, Any]:
        """Level 3: Number or Text - expects '17' or 'gelap' (analogy)"""
        # Handle expected in different formats (dict from configurable tests)
        expected_value = expected
        expected_type = "number"  # default
        if isinstance(expected, dict):
            expected_value = expected.get('answer', expected.get('value', expected))
            expected_type = expected.get('type', 'number')
        
        response_clean = response.strip().lower()
        
        # Text comparison (for analogy tests)
        if expected_type == "text" or isinstance(expected_value, str):
            expected_clean = str(expected_value).strip().lower()
            if response_clean == expected_clean:
                return {"score": 1.0, "details": f"Correct: {response_clean}"}
            else:
                return {"score": 0.0, "details": f"Wrong: expected '{expected_clean}', got '{response_clean}'"}
        
        # Number comparison (default)
        try:
            actual = int(response.strip())
            expected_num = int(expected_value) if not isinstance(expected_value, int) else expected_value
            if actual == expected_num:
                return {"score": 1.0, "details": f"Correct: {actual}"}
            else:
                return {"score": 0.0, "details": f"Wrong: expected {expected_num}, got {actual}"}
        except ValueError:
            return {"score": 0.0, "details": f"Not a number: '{response}'"}
    
    def _score_level_4(self, response: str, expected: Any) -> Dict[str, Any]:
        """Level 4: Causal reasoning - checks if response considers alternatives"""
        clean = response.strip().lower()
        
        # Handle expected in different formats (dict from configurable tests)
        if isinstance(expected, dict):
            # Causal reasoning test - check if alternatives are considered
            if expected.get('consider_alternatives'):
                # Check if response is "ya" (considers alternatives)
                if clean in ['ya', 'yes', 'true']:
                    return {"score": 1.0, "status": "passed", "details": "Response considers alternative explanations"}
                elif clean in ['tidak', 'no', 'false']:
                    return {"score": 0.0, "status": "failed", "details": "Response does not consider alternatives"}
                else:
                    # For verbose responses, check keywords
                    alternatives_keywords = ['alternatif', 'kemungkinan', 'faktor', 'plasebo', 'alami', 'lain']
                    found = sum(1 for kw in alternatives_keywords if kw in clean)
                    if found >= 2:
                        return {"score": 1.0, "status": "passed", "details": f"Found {found} alternative consideration keywords"}
                    else:
                        return {"score": 0.5, "status": "partial", "details": f"Partial: found {found} keywords"}
            
            # If expected has specific statements
            expected_list = expected.get('answer', expected.get('statements', []))
            if isinstance(expected_list, list):
                try:
                    statements = [int(n.strip()) for n in response.split(',')]
                    if all(s in statements for s in expected_list):
                        return {"score": 1.0, "status": "passed", "details": f"Correct: statements {statements}"}
                    else:
                        return {"score": 0.0, "status": "failed", "details": f"Wrong: expected {expected_list}, got {statements}"}
                except ValueError:
                    pass
        
        # Fallback: try to parse as statement numbers
        try:
            statements = [int(n.strip()) for n in response.split(',')]
            if isinstance(expected, list) and all(s in statements for s in expected):
                return {"score": 1.0, "status": "passed", "details": f"Correct: statements {statements}"}
            return {"score": 0.0, "status": "failed", "details": f"Got statements: {statements}"}
        except ValueError:
            return {"score": 0.0, "status": "failed", "details": f"Could not parse: '{response[:50]}'"}
    
    def _score_level_5(self, response: str, expected: Any) -> Dict[str, Any]:
        """Level 5: Flexible - can be number (combinatorics) or string"""
        response_clean = response.strip().lower()
        
        # Handle expected in different formats (dict from configurable tests)
        expected_value = expected
        expected_type = None
        if isinstance(expected, dict):
            expected_value = expected.get('answer', expected.get('value'))
            expected_type = expected.get('type')
        
        # String comparison
        if expected_type == "string" or isinstance(expected_value, str):
            expected_clean = str(expected_value).strip().lower()
            if response_clean == expected_clean:
                return {"score": 1.0, "status": "passed", "details": f"Correct: {response_clean}"}
            else:
                return {"score": 0.0, "status": "failed", "details": f"Wrong: expected '{expected_clean}', got '{response_clean}'"}
        
        # Numeric comparison
        try:
            # Try to parse expected as number
            expected_num = float(expected_value) if expected_value is not None else 0
        except (TypeError, ValueError):
            expected_num = 0
        
        # Clean response for numeric comparison
        clean = response.strip()
        clean = clean.replace(',', '').replace('Rp', '').replace('rp', '').strip()
        # Handle Indonesian decimal format (dots as thousands separator)
        if '.' in clean and clean.count('.') > 1:
            clean = clean.replace('.', '')
        
        try:
            actual = float(clean)
            if abs(actual - expected_num) < 1:  # Tolerance
                return {"score": 1.0, "status": "passed", "details": f"Correct: {actual}"}
            else:
                return {"score": 0.0, "status": "failed", "details": f"Wrong: expected {expected_num}, got {actual}"}
        except ValueError:
            return {"score": 0.0, "status": "failed", "details": f"Not a number: '{response}'"}
