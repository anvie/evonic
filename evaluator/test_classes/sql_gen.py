from .base import BaseTest
from typing import Dict, Any
import json
from evaluator.sql_executor import sql_executor

class SQLGenTest(BaseTest):
    """SQL generation tests"""
    
    def get_prompt(self) -> str:
        full_schema = """
COMPLETE DATABASE SCHEMA:

TABLE: customers
- id (INTEGER, PRIMARY KEY)
- name (TEXT) - customer name
- email (TEXT) - customer email  
- city (TEXT) - customer city
- created_at (DATETIME) - creation timestamp

TABLE: products
- id (INTEGER, PRIMARY KEY)
- name (TEXT) - product name
- price (DECIMAL) - product price
- category (TEXT) - product category
- stock (INTEGER) - available stock

TABLE: orders
- id (INTEGER, PRIMARY KEY)
- customer_id (INTEGER, FOREIGN KEY to customers.id)
- total (DECIMAL) - order total amount
- status (TEXT) - order status
- order_date (DATE) - order date

TABLE: order_items
- id (INTEGER, PRIMARY KEY)
- order_id (INTEGER, FOREIGN KEY to orders.id)
- product_id (INTEGER, FOREIGN KEY to products.id)
- quantity (INTEGER) - quantity ordered
- unit_price (DECIMAL) - price per unit

TABLE: employees
- id (INTEGER, PRIMARY KEY)
- name (TEXT) - employee name
- department (TEXT) - department
- salary (DECIMAL) - salary amount
- hire_date (DATE) - hire date

KEY RELATIONSHIPS:
- orders.customer_id → customers.id
- order_items.order_id → orders.id
- order_items.product_id → products.id
"""
        
        prompts = {
            1: f"""Buat query SQL untuk mengambil semua data dari tabel customers.

PENTING: Jawab HANYA dengan query SQL mentah. JANGAN ada penjelasan, komentar, atau markdown.

{full_schema}""",
            
            2: f"""Buat query SQL untuk mengambil nama dan email customer dari kota Jakarta.

Gunakan kolom: name (bukan nama), email, city (bukan kota)

PENTING: Jawab HANYA dengan query SQL mentah. JANGAN ada penjelasan, komentar, atau markdown.

{full_schema}""",
            
            3: f"""Buat query SQL untuk menghitung total penjualan per kota customer.

Hint: Join tables orders dan customers, group by city

PENTING: Jawab HANYA dengan query SQL mentah. JANGAN ada penjelasan, komentar, atau markdown.

{full_schema}""",
            
            4: f"""Buat query SQL untuk menampilkan 5 produk dengan harga tertinggi beserta kategorinya.

Gunakan tabel: products dengan kolom name, price, category

PENTING: Jawab HANYA dengan query SQL mentah. JANGAN ada penjelasan, komentar, atau markdown.

{full_schema}""",
            
            5: f"""Buat query SQL untuk analisis customer:
- Tampilkan nama customer dan total belanja mereka
- Urutkan berdasarkan total belanja descending
- Gunakan JOIN antara customers dan orders

PENTING: Jawab HANYA dengan query SQL mentah. JANGAN ada penjelasan, komentar, atau markdown.

{full_schema}"""
        }
        return prompts.get(self.level, "")
    
    def get_expected(self) -> Dict[str, Any]:
        """Expected results based on actual seed database"""
        expected_results = {
            1: {
                "description": "All customers from database",
                "min_rows": 15,
                "required_columns": ["id", "name", "email", "city"],
            },
            2: {
                "description": "Jakarta customers - name and email only",
                "min_rows": 1,
                "required_columns": ["name", "email"],
                "forbidden_columns": ["id", "created_at"],
            },
            3: {
                "description": "Total sales per city",
                "min_rows": 1,
                "required_columns": ["city", "total_sales"],
                "aggregation_check": True
            },
            4: {
                "description": "Top 5 expensive products",
                "max_rows": 5,
                "required_columns": ["name", "price", "category"],
                "order_check": "DESC"
            },
            5: {
                "description": "Customer spending analysis",
                "min_rows": 1,
                "required_columns": ["customer_name", "total_spent"],
                "join_check": True
            }
        }
        return expected_results.get(self.level, {})
    
    def score_response(self, response: str, expected: dict) -> Dict[str, Any]:
        """Improved SQL scoring with comprehensive validation"""
        try:
            # Step 1: Extract SQL from response
            sql_query = self._extract_sql_query(response)
            if not sql_query:
                return {
                    "score": 0.0, 
                    "details": "No SQL query found in response",
                    "sql_query": "",
                    "breakdown": {"extraction": 0.0}
                }

            # Step 2: Execute the SQL
            execution_result = sql_executor.execute_safe_query(sql_query)
            
            if not execution_result.get("success"):
                error_msg = execution_result.get("error", "Unknown error")
                return {
                    "score": 0.0,
                    "details": f"SQL execution failed: {error_msg}",
                    "sql_query": sql_query,
                    "breakdown": {"execution": 0.0}
                }

            actual_result = execution_result.get("result", [])
            columns = execution_result.get("columns", [])
            
            # Step 3: Multi-factor scoring
            breakdown = {}
            total_score = 0.0
            details_parts = []
            
            # 3a. Query Execution (baseline)
            breakdown["execution"] = 1.0
            details_parts.append("✓ Query executed successfully")
            
            # 3b. Result Quality Check
            if not actual_result:
                breakdown["results"] = 0.0
                details_parts.append("✗ Query returned no results")
            else:
                breakdown["results"] = 1.0
                details_parts.append(f"✓ Returned {len(actual_result)} rows")
            
            # 3c. Column Validation
            if expected and actual_result:
                required_cols = expected.get("required_columns", [])
                forbidden_cols = expected.get("forbidden_columns", [])
                actual_cols = set(columns)
                
                col_score = 0.0
                if required_cols:
                    required_found = sum(1 for c in required_cols if c.lower() in [col.lower() for col in actual_cols])
                    col_score = required_found / len(required_cols)
                    
                    if col_score >= 1.0:
                        details_parts.append(f"✓ All required columns present: {required_cols}")
                    elif col_score > 0:
                        details_parts.append(f"⚠ Partial columns: {required_found}/{len(required_cols)} required found")
                    else:
                        details_parts.append(f"✗ Missing required columns: {required_cols}")
                
                # Check forbidden columns
                if forbidden_cols:
                    forbidden_found = [c for c in forbidden_cols if c.lower() in [col.lower() for col in actual_cols]]
                    if forbidden_found:
                        col_score *= 0.7  # Penalty
                        details_parts.append(f"⚠ Unwanted columns selected: {forbidden_found}")
                
                breakdown["columns"] = col_score
            
            # 3d. Row Count Validation
            if expected:
                min_rows = expected.get("min_rows")
                max_rows = expected.get("max_rows")
                
                row_score = 1.0
                if min_rows and len(actual_result) < min_rows:
                    row_score = len(actual_result) / min_rows
                    details_parts.append(f"⚠ Too few rows: {len(actual_result)} (expected ≥{min_rows})")
                elif max_rows and len(actual_result) > max_rows:
                    row_score = max_rows / len(actual_result)
                    details_parts.append(f"⚠ Too many rows: {len(actual_result)} (expected ≤{max_rows})")
                elif actual_result:
                    details_parts.append(f"✓ Row count acceptable")
                
                breakdown["row_count"] = row_score
            
            # 3e. Data Validation (sample check)
            if expected and actual_result:
                data_score = 1.0
                
                # Basic email format check for level 2
                if self.level == 2 and 'email' in columns:
                    valid_emails = sum(1 for r in actual_result if '@' in str(r.get('email', '')))
                    data_score *= (valid_emails / len(actual_result)) if actual_result else 0
                    if data_score >= 0.9:
                        details_parts.append("✓ Email format valid")
                    else:
                        details_parts.append(f"⚠ Some emails invalid: {valid_emails}/{len(actual_result)}")
                
                breakdown["data_quality"] = data_score
            
            # Step 4: Calculate weighted final score
            weights = {
                "execution": 0.2,    # Must execute
                "results": 0.2,     # Must return data
                "columns": 0.3,     # Correct columns
                "row_count": 0.15,  # Reasonable row count
                "data_quality": 0.15  # Data looks correct
            }
            
            for key, weight in weights.items():
                if key in breakdown:
                    total_score += breakdown[key] * weight
                else:
                    breakdown[key] = 0.0
            
            # Determine status
            if total_score >= 0.8:
                status = "passed"
            elif total_score >= 0.5:
                status = "partial"
            else:
                status = "failed"
            
            return {
                "score": round(total_score, 3),
                "status": status,
                "details": " | ".join(details_parts),
                "sql_query": sql_query,
                "row_count": len(actual_result),
                "columns": columns,
                "breakdown": breakdown,
                "actual_result_preview": actual_result[:3] if actual_result else []
            }
            
        except Exception as e:
            return {
                "score": 0.0,
                "details": f"Scoring error: {type(e).__name__}: {str(e)}",
                "sql_query": "",
                "breakdown": {"error": str(e)}
            }
    
    def _extract_sql_query(self, response: str) -> str:
        """Extract SQL query from LLM response (PASS 2 output should be clean SQL)"""
        import re
        
        # PASS 2 should return clean SQL - try direct parse first
        clean = response.strip()
        
        # Remove markdown if present
        clean = re.sub(r'```sql\s*', '', clean, flags=re.IGNORECASE)
        clean = re.sub(r'```\s*', '', clean)
        clean = clean.strip()
        
        # If it starts with SELECT, it's likely clean SQL from PASS 2
        if clean.upper().startswith('SELECT'):
            return clean
        
        # Fallback: Look for SQL code blocks
        sql_blocks = re.findall(r'```sql\s*(.*?)\s*```', response, re.DOTALL | re.IGNORECASE)
        if sql_blocks:
            return sql_blocks[0].strip()
        
        # Fallback: Look for SQL without code blocks
        sql_patterns = [
            r'(SELECT\s+.*?\s+FROM\s+.*?(?:WHERE.*?)?(?:GROUP\s+BY.*?)?(?:HAVING.*?)?(?:ORDER\s+BY.*?)?(?:LIMIT.*?)?)',
            r'(SELECT\s+.*?\s+FROM\s+\w+)'
        ]
        
        for pattern in sql_patterns:
            matches = re.findall(pattern, response, re.DOTALL | re.IGNORECASE)
            if matches:
                return matches[0].strip()
        
        return ""
