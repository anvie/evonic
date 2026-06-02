from typing import Dict, Any, Optional
import json
from evaluator.test_classes import get_test_class

class ScoringEngine:
    def __init__(self):
        pass
    
    def score_test(self, domain: str, level: int, response: str, expected: Any) -> Dict[str, Any]:
        """Score a test response"""
        test_class = get_test_class(domain)
        if not test_class:
            return {
                "score": 0.0,
                "details": {"error": f"Unknown domain: {domain}"},
                "status": "failed"
            }
        
        test_instance = test_class(level)
        scoring_result = test_instance.score_response(response, expected)
        
        # Determine status based on score
        score = scoring_result.get("score", 0.0)
        status = scoring_result.get("status", "passed" if score >= 0.8 else "failed")
        
        # Build details object with all relevant info
        details = {
            "details": scoring_result.get("details", ""),
        }
        
        # Include breakdown if available (for SQL tests)
        if "breakdown" in scoring_result:
            details["breakdown"] = scoring_result["breakdown"]
        if "sql_query" in scoring_result:
            details["sql_query"] = scoring_result["sql_query"]
        if "columns" in scoring_result:
            details["columns"] = scoring_result["columns"]
        if "row_count" in scoring_result:
            details["row_count"] = scoring_result["row_count"]
        if "actual_result_preview" in scoring_result:
            details["actual_result_preview"] = scoring_result["actual_result_preview"]
        
        # For conversation tests, include relevance/correctness/fluency
        if "relevance" in scoring_result:
            details["relevance"] = scoring_result["relevance"]
        if "correctness" in scoring_result:
            details["correctness"] = scoring_result["correctness"]
        if "fluency" in scoring_result:
            details["fluency"] = scoring_result["fluency"]
        if "keywords_found" in scoring_result:
            details["keywords_found"] = scoring_result["keywords_found"]
        
        return {
            "score": score,
            "details": details,
            "status": status
        }
    
    def validate_tool_calls(self, tool_calls: list, expected_tools: list) -> Dict[str, Any]:
        """Validate tool calls against expected tools"""
        if not tool_calls:
            return {
                "valid": False,
                "error": "No tool calls found"
            }
        
        called_tools = [call["function"]["name"] for call in tool_calls]
        
        # Check if all expected tools were called
        missing_tools = set(expected_tools) - set(called_tools)
        extra_tools = set(called_tools) - set(expected_tools)
        
        return {
            "valid": len(missing_tools) == 0,
            "called_tools": called_tools,
            "missing_tools": list(missing_tools),
            "extra_tools": list(extra_tools)
        }
    
    def calculate_overall_score(self, test_results: list) -> float:
        """Calculate overall score as average of domain averages"""
        if not test_results:
            return 0.0

        # Group scores by domain
        domain_scores = {}
        for result in test_results:
            if result.get("score") is None or result.get("status") == "skipped":
                continue
            domain = result.get("domain", "unknown")
            domain_scores.setdefault(domain, []).append(result["score"])

        if not domain_scores:
            return 0.0

        # Average each domain, then average across domains
        domain_avgs = [sum(scores) / len(scores) for scores in domain_scores.values()]
        return sum(domain_avgs) / len(domain_avgs)
    
    def generate_summary(self, test_results: list, model_name: str, llm_client=None, run_stats: dict = None) -> str:
        """Generate executive summary using LLM or fallback to rule-based

        Args:
            test_results: list of {domain, level, score, status}
            model_name: model name for fallback context
            llm_client: optional LLM client for rich summary
            run_stats: optional dict with overall_score, total_tokens, tok_per_sec, total_duration_ms
        """
        if not test_results:
            return "No tests completed"

        # Count results by domain
        domain_scores = {}
        domain_counts = {}
        domain_passed = {}

        for result in test_results:
            domain = result.get("domain")
            score = result.get("score", 0.0)
            status = result.get("status", "failed")

            if domain not in domain_scores:
                domain_scores[domain] = 0.0
                domain_counts[domain] = 0
                domain_passed[domain] = 0

            domain_scores[domain] += score
            domain_counts[domain] += 1
            if status == "passed":
                domain_passed[domain] += 1

        # Calculate average per domain
        domain_avgs = {}
        for domain in domain_scores:
            if domain_counts[domain] > 0:
                domain_avgs[domain] = domain_scores[domain] / domain_counts[domain]

        # Try LLM-generated summary first
        if llm_client and domain_avgs:
            llm_summary = self._generate_llm_summary(domain_avgs, domain_counts, domain_passed, llm_client, run_stats)
            if llm_summary:
                return llm_summary

        # Fallback: rule-based summary
        return self._generate_rule_based_summary(domain_avgs, domain_counts, domain_passed, model_name, run_stats)

    def _generate_rule_based_summary(self, domain_avgs: dict, domain_counts: dict, domain_passed: dict, model_name: str, run_stats: dict = None) -> str:
        """Generate detailed rule-based summary when LLM fails"""
        if not domain_avgs:
            return "Telah diuji tetapi tidak ada data domain yang cukup."

        sorted_domains = sorted(domain_avgs.items(), key=lambda x: x[1], reverse=True)
        strongest = sorted_domains[0]
        weakest = sorted_domains[-1]
        overall = sum(s * domain_counts[d] for d, s in domain_avgs.items()) / sum(domain_counts.values())

        # Performance label
        if overall >= 0.8:
            label = "sangat baik"
        elif overall >= 0.6:
            label = "cukup baik"
        elif overall >= 0.4:
            label = "menengah"
        else:
            label = "perlu peningkatan"

        sentences = []

        # Sentence 1: overall + label
        sentences.append(f"Performa keseluruhan {label} dengan skor {overall*100:.0f}%.")

        # Sentence 2: highlight terkuat & terlemah
        if strongest[1] > weakest[1]:
            sentences.append(f"Paling kuat di {strongest[0]} ({strongest[1]*100:.0f}%), area yang perlu ditingkatkan: {weakest[0]} ({weakest[1]*100:.0f}%).")
        else:
            sentences.append(f"Performa konsisten di semua domain.")

        # Sentence 3: speed info if available
        if run_stats:
            speed = run_stats.get("tok_per_sec", 0)
            dur_min = run_stats.get("total_duration_ms", 0) / 60000
            sentences.append(f"Evaluasi selesai dalam {dur_min:.1f} menit dengan kecepatan {speed} tok/s.")

        return " ".join(sentences)

    def _generate_llm_summary(self, domain_avgs: dict, domain_counts: dict, domain_passed: dict, llm_client, run_stats: dict = None) -> str:
        """Generate natural summary using the tested LLM"""
        try:
            # Build domain performance data with pass/fail breakdown
            sorted_domains = sorted(domain_avgs.items(), key=lambda x: x[1], reverse=True)
            strongest = sorted_domains[0]
            weakest = sorted_domains[-1]

            lines = []
            for domain, avg in sorted_domains:
                total = domain_counts.get(domain, 0)
                passed = domain_passed.get(domain, 0)
                failed = total - passed
                pct = avg * 100
                lines.append(f"- {domain}: {pct:.0f}% rata-rata ({passed} lulus, {failed} gagal dari {total} tes)")

            domain_data = "\n".join(lines)
            total_tests = sum(domain_counts.values())
            total_passed = sum(domain_passed.values())
            overall_avg = sum(s * domain_counts[d] for d, s in domain_avgs.items()) / total_tests if total_tests > 0 else 0

            # Build run stats info
            stats_info = ""
            if run_stats:
                tok = run_stats.get("total_tokens", 0)
                speed = run_stats.get("tok_per_sec", 0)
                dur = run_stats.get("total_duration_ms", 0) / 1000
                stats_info = f"""
Run Performance:
- Total tokens: {tok:,}
- Kecepatan: {speed} tokens/detik
- Durasi total: {dur:.1f} detik"""

            prompt = f"""Berikut adalah hasil evaluasi LLM di beberapa domain:

{domain_data}

Overall: {total_passed}/{total_tests} tes lulus, rata-rata keseluruhan {overall_avg*100:.0f}%.
Domain terkuat: {strongest[0]} ({strongest[1]*100:.0f}%)
Domain terlemah: {weakest[0]} ({weakest[1]*100:.0f}%){stats_info}

Tulis ringkasan evaluasi dalam 2-3 kalimat bahasa Indonesia yang natural dan informatif. Sertakan:
- Kalimat 1: Overview performa (overall score, sebutkan domain terkuat dan terlemah)
- Kalimat 2: Insight spesifik tentang area yang perlu ditingkatkan atau keunggulan
- Kalimat 3: Comment tentang kecepatan/efisiensi (jika data tersedia)
JANGAN menyebut nama model. JANGAN gunakan emoji. JANGAN gunakan bullet/list. Langsung tulis ringkasannya saja tanpa pembuka."""

            response = llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                enable_thinking=False
            )

            if response.get("success") and response.get("response"):
                from evaluator.llm_client import strip_thinking_tags
                content = llm_client.extract_content(response)
                if content:
                    content, _ = strip_thinking_tags(content)
                    content = content.strip().strip('"').strip()
                    if len(content) > 20:
                        return content

            return None
        except Exception:
            return None

# Global scoring engine instance
scoring_engine = ScoringEngine()