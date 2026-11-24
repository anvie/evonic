"""
Test Logger Module

Creates JSON log files for each test containing:
- input (prompt)
- thinking (if present)
- output (response)
- evaluation result
"""

import json
import os
from datetime import datetime
from typing import Dict, Any, Optional
from pathlib import Path

from evaluator.points import earned_points, max_points


class TestLogger:
    """Handles per-test logging to JSON files"""
    
    def __init__(self, base_dir: str = "logs/eval"):
        self.base_dir = base_dir
        self.current_run_dir: Optional[Path] = None
        self.run_id: Optional[int] = None
        self.test_count = 0
        self.passed_count = 0
        self.total_score = 0.0
        self.total_points = 0.0
        self.max_points = 0.0
        
    def start_run(self, run_id: int, model_name: str):
        """Initialize a new run directory"""
        self.run_id = run_id
        self.current_run_dir = Path(self.base_dir) / str(run_id)
        self.current_run_dir.mkdir(parents=True, exist_ok=True)
        self.test_count = 0
        self.passed_count = 0
        self.total_score = 0.0
        self.total_points = 0.0
        self.max_points = 0.0
        
        # Write run metadata
        metadata = {
            "run_id": run_id,
            "model": model_name,
            "started_at": datetime.now().isoformat(),
            "status": "running"
        }
        metadata_path = self.current_run_dir / "_run_meta.json"
        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
    
    def log_test(self,
                 domain: str,
                 level: int,
                 test_id: str,
                 prompt: str,
                 response: str,
                 thinking: Optional[str],
                 expected: Any,
                 score: float,
                 status: str,
                 details: Dict[str, Any],
                 duration_ms: int,
                 tokens: int,
                 model_name: str,
                 system_prompt: Optional[str] = None,
                 weight: float = 1.0) -> str:
        """
        Log a single test result to JSON file.
        
        Returns the path to the log file.
        """
        if not self.current_run_dir:
            return ""
        
        self.test_count += 1
        self.total_score += score
        self.total_points += earned_points(score, level, weight)
        self.max_points += max_points(level, weight)
        if status == 'passed':
            self.passed_count += 1
        
        # Build log entry
        log_data = {
            "test_info": {
                "domain": domain,
                "level": level,
                "test_id": test_id,
                "run_id": self.run_id,
                "model": model_name,
                "timestamp": datetime.now().isoformat()
            },
            "system_prompt": system_prompt,
            "input": prompt,
            "thinking": thinking,  # null if not present
            "output": response,
            "evaluation": {
                "expected": expected,
                "score": score,
                "status": status,
                "details": details
            },
            "timing": {
                "duration_ms": duration_ms,
                "tokens": tokens
            }
        }
        
        # Generate filename: math_L1_001.json
        file_num = self.test_count
        filename = f"{domain}_L{level}_{file_num:03d}.json"
        filepath = self.current_run_dir / filename
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(log_data, f, indent=2, ensure_ascii=False)
        
        return str(filepath)
    
    def finalize_run(self, status: str = "completed"):
        """
        Create summary file and update metadata at run completion.
        
        Args:
            status: "completed" or "interrupted"
        """
        if not self.current_run_dir:
            return
        
        # Update run metadata
        metadata_path = self.current_run_dir / "_run_meta.json"
        if metadata_path.exists():
            with open(metadata_path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            
            metadata["status"] = status
            metadata["completed_at"] = datetime.now().isoformat()
            metadata["total_tests"] = self.test_count
            metadata["passed_tests"] = self.passed_count
            
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
        
        # Create summary
        avg_score = (self.total_score / self.test_count) if self.test_count > 0 else 0.0
        
        summary = {
            "run_id": self.run_id,
            "status": status,
            "completed_at": datetime.now().isoformat(),
            "statistics": {
                "total_tests": self.test_count,
                "passed_tests": self.passed_count,
                "failed_tests": self.test_count - self.passed_count,
                "average_score": round(avg_score, 4),
                "total_points": round(self.total_points, 4),
                "max_points": round(self.max_points, 4),
                "pass_rate": round(self.passed_count / self.test_count, 4) if self.test_count > 0 else 0.0
            }
        }
        
        summary_path = self.current_run_dir / "_summary.json"
        with open(summary_path, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        
        return str(summary_path)
    
    def get_run_dir(self) -> Optional[str]:
        """Get the current run directory path"""
        return str(self.current_run_dir) if self.current_run_dir else None


# Global logger instance
test_logger = TestLogger()
