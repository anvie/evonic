from .base import BaseTest
from typing import Dict, Any
import json
from evaluator.tools import tool_framework

class ToolCallingTest(BaseTest):
    """Tool calling tests"""
    
    def get_prompt(self) -> str:
        prompts = {
            1: "Hitung 125 * 8 menggunakan kalkulator",
            2: "Ambil data semua produk dari database yang harganya di atas Rp 1.000.000",
            3: "Panggil API endpoint https://api.example.com/users untuk mendapatkan data pengguna",
            4: "Buat file laporan.txt yang berisi 'Laporan Penjualan Harian' dan tambahkan timestamp",
            5: """Lakukan analisis penjualan multi-langkah:
1. Ambil data order dari database
2. Hitung total penjualan per hari
3. Simpan hasil analisis ke file sales_report.txt
4. Berikan ringkasan hasil"""
        }
        return prompts.get(self.level, "")
    
    def get_expected(self) -> Dict[str, Any]:
        expected = {
            1: {"tool": "calculator", "result": 1000},
            2: {"tool": "database_query", "row_count": 8},
            3: {"tool": "api_call", "status": "success"},
            4: {"tool": "file_create", "status": "created"},
            5: {"multi_step": True, "tools_used": ["database_query", "calculator", "file_create"]}
        }
        return expected.get(self.level, {})
    
    def score_response(self, response: str, expected: Dict[str, Any]) -> Dict[str, Any]:
        # Extract tool calls from response
        tool_calls = self._extract_tool_calls(response)
        
        if not tool_calls:
            return {
                "score": 0.0,
                "details": "No tool calls found in response",
                "tool_calls": []
            }
        
        # Execute tool calls
        execution_results = []
        for tool_call in tool_calls:
            result = tool_framework.execute_tool(tool_call)
            execution_results.append(result)
        
        # Score based on level
        if self.level <= 4:
            # Single tool call expected
            if len(execution_results) != 1:
                return {
                    "score": 0.0,
                    "details": f"Expected 1 tool call, got {len(execution_results)}",
                    "tool_calls": execution_results
                }
            
            result = execution_results[0]
            expected_tool = expected.get("tool", "")
            
            if result["function_name"] != expected_tool:
                return {
                    "score": 0.0,
                    "details": f"Expected tool {expected_tool}, got {result['function_name']}",
                    "tool_calls": execution_results
                }
            
            if result["success"]:
                return {
                    "score": 1.0,
                    "details": "Tool call executed successfully",
                    "tool_calls": execution_results
                }
            else:
                return {
                    "score": 0.0,
                    "details": f"Tool execution failed: {result['result'].get('error', 'Unknown error')}",
                    "tool_calls": execution_results
                }
        
        else:  # Level 5: Multi-step
            if len(execution_results) < 2:
                return {
                    "score": 0.0,
                    "details": f"Multi-step expected, got only {len(execution_results)} tool calls",
                    "tool_calls": execution_results
                }
            
            # Check if expected tools were used
            expected_tools = expected.get("tools_used", [])
            used_tools = [r["function_name"] for r in execution_results]
            
            missing_tools = set(expected_tools) - set(used_tools)
            if missing_tools:
                return {
                    "score": 0.5,  # Partial credit
                    "details": f"Missing tools: {missing_tools}",
                    "tool_calls": execution_results
                }
            
            # Check if all executions were successful
            failed_executions = [r for r in execution_results if not r["success"]]
            if failed_executions:
                return {
                    "score": 0.7,  # Partial credit
                    "details": f"Some tool executions failed: {len(failed_executions)}/{len(execution_results)}",
                    "tool_calls": execution_results
                }
            
            return {
                "score": 1.0,
                "details": "Multi-step tool calling executed successfully",
                "tool_calls": execution_results
            }
    
    def _extract_tool_calls(self, response: str) -> list:
        """Extract tool calls from LLM response"""
        import re
        import json
        
        # Look for JSON tool calls
        try:
            # Check if response is already a JSON tool call
            data = json.loads(response)
            if "tool_calls" in data:
                return data["tool_calls"]
        except json.JSONDecodeError:
            pass
        
        # Look for JSON blocks
        json_blocks = re.findall(r'```json\s*(.*?)\s*```', response, re.DOTALL | re.IGNORECASE)
        for block in json_blocks:
            try:
                data = json.loads(block)
                if "tool_calls" in data:
                    return data["tool_calls"]
            except json.JSONDecodeError:
                continue
        
        # Look for function call patterns
        function_patterns = [
            r'{\s*"name"\s*:\s*"(.*?)"\s*,\s*"arguments"\s*:\s*{(.*?)}\s*}',
            r'function_call\s*[\{\[].*?[\}\]]'
        ]
        
        # This is simplified - actual implementation would need more sophisticated parsing
        return []