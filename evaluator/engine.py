"""
Evaluation Engine - Run LLM evaluations using configurable test definitions.

Supports both:
1. Legacy hardcoded tests (from tests/ module)
2. New configurable tests (from test_definitions/ directory)
"""

import time
import json
import os
import shutil
from typing import Dict, Any, List, Optional
import queue
from threading import Thread, Lock

from tests import get_test_class
from evaluator.llm_client import llm_client
from evaluator.scoring import scoring_engine
from evaluator.domain_evaluators import get_evaluator
from evaluator.test_loader import test_loader
from evaluator.test_manager import test_manager
from evaluator.score_aggregator import ScoreAggregator, TestResult
from evaluator.custom_evaluator import CustomEvaluator
from evaluator.logger import test_logger
from models.db import db
import config

# Maximum iterations for multi-turn tool calling (configurable via EVAL_MAX_TOOL_ITERATIONS env var)
MAX_TOOL_ITERATIONS = config.EVAL_MAX_TOOL_ITERATIONS


class EvaluationEngine:
    def __init__(self, use_configurable_tests: bool = False):
        """
        Initialize evaluation engine.
        
        Args:
            use_configurable_tests: If True, load tests from test_definitions/
                                   If False, use legacy hardcoded tests
        """
        self.current_run_id: Optional[int] = None
        self.is_running = False
        self.was_interrupted = False  # User clicked Stop
        self.has_error = False       # Error occurred during evaluation
        self.error_message: Optional[str] = None
        self.lock = Lock()
        self.thread: Optional[Thread] = None
        self.log_queue = queue.Queue()
        self.use_configurable_tests = use_configurable_tests
        self.total_tokens=0
        self.total_duration_ms = 0
        self.model_name: Optional[str] = None
        self.model_config = None

    def _log(self, message: str):
        """Log a message to the queue"""
        timestamp = time.strftime('%H:%M:%S')
        self.log_queue.put(f"[{timestamp}] {message}")
    
    def start_evaluation(self, model_name: str = None, domains: list = None) -> str:
        """Start a new evaluation run
        
        Args:
            model_name: Model to evaluate (None = use configured model)
            domains: List of domain names to test (None = all domains)
        """
        with self.lock:
            if self.is_running:
                raise Exception("Evaluation already running")

            # Resolve model: use selected model or fall back to global default
            if model_name and model_name != 'default':
                model_record = db.get_model_by_model_name(model_name)
                if model_record:
                    self.model_config = model_record
                else:
                    self.model_config = None
            else:
                # No specific model selected — use global default
                from evaluator.llm_client import llm_client
                model_name = llm_client.get_actual_model_name(force_refresh=True)
                self.model_config = None

            self.model_name = model_name
            self.selected_domains = domains  # Store selected domains
            self.current_run_id = db.create_evaluation_run(model_name)
            self.is_running = True
            self.was_interrupted = False
            self.has_error = False
            self.error_message = None
            self.total_tokens=0
            self.total_duration_ms = 0
            
            domains_str = ', '.join(domains) if domains else 'all'
            self._log(f'[INFO] Memulai evaluasi untuk model: {model_name}')
            self._log(f'[INFO] Domain yang dipilih: {domains_str}')
            
            # Start test logger
            test_logger.start_run(self.current_run_id, model_name)

            # Clear LLM API call log for fresh run
            try:
                from models.db import db as _db
                _log_file = _db.get_setting('llm_api_log_file') or os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'logs', 'llm_api_calls.md')
                with open(_log_file, 'w') as f:
                    f.write(f"# LLM API Calls — Run {self.current_run_id} — {model_name}\n\n")
            except Exception:
                pass
            
            # Start evaluation in background thread
            self.thread = Thread(target=self._run_evaluation, args=(self.current_run_id, model_name, domains, self.model_config))
            self.thread.daemon = True
            self.thread.start()
            
            return self.current_run_id
    
    def stop_evaluation(self):
        """Stop current evaluation"""
        with self.lock:
            self.is_running = False
            self.was_interrupted = True
    
    def reset_state(self):
        """Reset engine state to idle"""
        with self.lock:
            self.current_run_id = None
            self.is_running = False
            self.was_interrupted = False
            self.model_config = None
            self.total_tokens = 0
            self.total_duration_ms = 0
    
    def get_status(self) -> Dict[str, Any]:
        """Get current evaluation status"""
        with self.lock:
            if not self.current_run_id:
                return {"status": "idle"}
            
            run_info = db.get_evaluation_run(self.current_run_id)
            test_results = db.get_test_results(self.current_run_id)
            stats = db.get_run_stats(self.current_run_id)
            
            # Determine status
            if self.is_running:
                status = "running"
            elif self.has_error:
                status = "error"
            elif self.was_interrupted:
                status = "interrupted"
            else:
                status = "completed"
            
            # Calculate tok/s
            tok_per_sec = None
            if self.total_duration_ms > 0:
                tok_per_sec = (self.total_tokens / self.total_duration_ms) * 1000
            
            return {
                "status": status,
                "run_id": self.current_run_id,
                "run_info": run_info,
                "test_results": test_results,
                "stats": stats,
                "tok_per_sec": round(tok_per_sec, 1) if tok_per_sec else None,
                "error_message": self.error_message if self.has_error else None
            }
    
    def _run_evaluation(self, run_id: int, model_name: str, domains: list = None, model_config: dict = None):
        """Main evaluation loop - supports both legacy and configurable tests
        
        Args:
            run_id: Unique run identifier
            model_name: Model being evaluated
            domains: List of domain names to test (None = all domains)
        """
        self._log(f'[SYSTEM] Evaluation thread (run_id: {run_id}) dimulai.')

        # Create per-run LLM client if custom config
        if model_config:
            from evaluator.llm_client import LLMClient
            run_llm_client = LLMClient(model_config)
            self._log(f"[INFO] Using custom LLM endpoint: {model_config.get('base_url', 'N/A')}")
        else:
            from evaluator.llm_client import llm_client as run_llm_client

        
        try:
            if self.use_configurable_tests:
                self._run_configurable_evaluation(run_id, model_name, domains, run_llm_client)
            else:
                self._run_legacy_evaluation(run_id, model_name, domains, run_llm_client)
            
            # Generate summary after all tests
            if self.is_running:
                self._generate_summary(run_id, model_name, run_llm_client)
                
        except Exception as e:
            import traceback
            self._log(f'[ERROR] Evaluation error: {e}')
            self._log(f'[ERROR] Traceback: {traceback.format_exc()[-500:]}')
            print(f"Evaluation error: {e}")
            traceback.print_exc()
            # Mark as error (not user-interrupted)
            self.has_error = True
            self.error_message = str(e)
        finally:
            with self.lock:
                self.is_running = False
                # Capture under lock to prevent race with reset_state/start_evaluation
                was_interrupted = self.was_interrupted
                run_id_to_delete = self.current_run_id if was_interrupted else None
            
            if was_interrupted:
                # Interrupted/canceled — do NOT finalize the logger or save anything.
                # Immediately delete all artifacts: DB records AND logger files on disk.
                if run_id_to_delete:
                    # Delete logger files (logs/eval/<run_id>/)
                    run_dir = test_logger.get_run_dir()
                    if run_dir and os.path.isdir(run_dir):
                        try:
                            shutil.rmtree(run_dir)
                            self._log(f'[SYSTEM] Interrupted run {run_id_to_delete} logger files deleted')
                        except Exception as e:
                            self._log(f'[WARN] Failed to clean logger files for run {run_id_to_delete}: {e}')
                    
                    # Delete from database (cascades to test_results, level_scores, individual_test_results, etc.)
                    db.delete_run(run_id_to_delete)
                    self._log(f'[SYSTEM] Interrupted run {run_id_to_delete} deleted from history')
            else:
                # Normal completion or error — finalize the logger
                if self.has_error:
                    final_status = "error"
                else:
                    final_status = "completed"
                test_logger.finalize_run(status=final_status)
    
    def _run_legacy_evaluation(self, run_id: int, model_name: str, selected_domains: list = None, run_llm_client=None):
        """Run evaluation using legacy hardcoded tests
        
        Args:
            run_id: Unique run identifier
            model_name: Model being evaluated
            selected_domains: List of domain names to test (None = all domains)
        """
        all_domains = ["conversation", "math", "sql", "tool_calling", "reasoning", "health"]
        
        # Filter domains if selection provided
        if selected_domains:
            domains = [d for d in all_domains if d in selected_domains]
        else:
            domains = all_domains
        
        for domain in domains:
            for level in range(1, 6):  # Levels 1-5
                if not self.is_running:
                    break
                
                # Update status to running
                db.update_test_result(
                    run_id, domain, level,
                    status="running",
                    model_name=model_name
                )
                self._log(f'[TEST] Menjalankan tes: {domain} Level {level}')
                
                # Run the test
                test_result = self._run_single_legacy_test(domain, level, run_llm_client=run_llm_client)
                
                # Store result
                db.update_test_result(
                    run_id, domain, level,
                    prompt=test_result["prompt"],
                    response=test_result["response"],
                    expected=json.dumps(test_result["expected"]) if test_result["expected"] else None,
                    score=test_result["score"],
                    status=test_result["status"],
                    details=json.dumps(test_result["details"]) if test_result["details"] else None,
                    duration_ms=test_result["duration_ms"]
                )
                
                # Small delay between tests
                time.sleep(0.5)
    
    def _run_configurable_evaluation(self, run_id: int, model_name: str, selected_domains: list = None, run_llm_client=None):
        """Run evaluation using configurable test definitions
        
        Args:
            run_id: Unique run identifier
            model_name: Model being evaluated
            selected_domains: List of domain names to test (None = all domains)
        """
        # Clear global test_loader cache to ensure fresh data
        from evaluator.test_loader import test_loader
        test_loader.clear_cache()

        # Sync tests from files to database
        test_manager.sync_to_db()
        
        # Load all domains
        domains = test_manager.list_domains()
        
        for domain_data in domains:
            domain_id = domain_data['id']
            
            # Skip disabled domains
            if not domain_data.get('enabled', True):
                continue
            
            # Skip domains not in selection (if selection provided)
            if selected_domains and domain_id not in selected_domains:
                continue
            
            # Run tests for each level
            for level in range(1, 6):
                if not self.is_running:
                    break
                
                # Load tests for this domain/level
                tests = test_manager.list_tests(domain_id, level)
                
                if not tests:
                    self._log(f'[SKIP] No tests for {domain_id} Level {level}')
                    continue
                
                # Set status to "running" for this cell before starting
                db.update_test_result(
                    run_id, domain_id, level,
                    status="running",
                    model_name=model_name
                )
                
                self._log(f'[TEST] Running {len(tests)} test(s) for {domain_id} Level {level}')
                
                # Run all tests for this level
                test_results = []
                first_prompt = None
                first_response = None
                first_expected = None
                
                for test in tests:
                    if not self.is_running:
                        break
                    
                    if not test.get('enabled', True):
                        continue
                    
                    result = self._run_single_configurable_test(
                        test, domain_id, level, model_name, run_id, run_llm_client=run_llm_client
                    )
                    test_results.append(result)
                    
                    # Store first test's prompt/response for display
                    if first_prompt is None:
                        first_prompt = test.get('prompt', '')
                        first_expected = test.get('expected', {})
                
                # Calculate average score for this level
                # Only save if still running (not interrupted mid-level)
                if self.is_running and test_results:
                    level_score = ScoreAggregator.calculate_level_score(test_results)
                    
                    # Calculate total duration for this level
                    level_duration_ms = sum(
                        r.details.get('duration_ms', 0) if r.details else 0 
                        for r in test_results
                    )
                    
                    # Store level score
                    db.save_level_score(
                        run_id, domain_id, level,
                        level_score.average_score,
                        level_score.total_tests,
                        level_score.passed_tests
                    )
                    
                    # Also update the legacy test_results table for compatibility
                    avg_score = level_score.average_score
                    status = 'passed' if avg_score >= 0.7 else 'failed'
                    
                    # Get details from first test result (includes thinking, response, etc.)
                    first_details = test_results[0].details if test_results[0].details else {}
                    
                    db.update_test_result(
                        run_id, domain_id, level,
                        prompt=first_prompt,
                        response=first_details.get('response'),
                        expected=json.dumps(first_expected) if first_expected else None,
                        score=avg_score,
                        status=status,
                        details=json.dumps(first_details) if first_details else None,
                        model_name=model_name,
                        duration_ms=level_duration_ms
                    )
                
                time.sleep(0.5)
    
    def _run_single_legacy_test(self, domain: str, level: int, run_llm_client=None) -> Dict[str, Any]:
        """Run a single legacy test using domain-specific evaluator"""
        _client = run_llm_client or llm_client
        try:
            test_class = get_test_class(domain)
            if not test_class:
                self._log(f'[ERROR] Unknown domain: {domain}')
                return {
                    "prompt": "",
                    "response": "",
                    "expected": None,
                    "score": 0.0,
                    "status": "failed",
                    "details": f"Unknown domain: {domain}",
                    "duration_ms": 0
                }

            test_instance = test_class(level)
            prompt = test_instance.get_prompt()
            expected = test_instance.get_expected()

            # Log test start with input
            self._log(f'')
            self._log(f'═══════════════════════════════════════════════════════════════')
            self._log(f'[TEST] {domain.upper()} Level {level}')
            self._log(f'───────────────────────────────────────────────────────────────')
            
            # Truncate prompt for display
            prompt_display = prompt[:300] + '...' if len(prompt) > 300 else prompt
            prompt_display = prompt_display.replace('\n', ' ')
            self._log(f'[INPUT] {prompt_display}')
            
            if expected:
                expected_str = str(expected)[:100] + '...' if len(str(expected)) > 100 else str(expected)
                self._log(f'[EXPECTED] {expected_str}')

            # Handle tool_calling domain with multi-turn loop
            if domain == "tool_calling":
                from evaluator.tools import tool_framework
                tools = tool_framework.tools
                self._log(f'[TOOLS] Available: {[t["function"]["name"] for t in tools]}')
                
                # Run tool calling loop
                loop_result = self._run_tool_calling_loop(prompt, tools, system_prompt=system_prompt, run_llm_client=_client)

                duration_ms = loop_result["total_duration_ms"]
                total_tokens = loop_result["total_tokens"]
                thinking_content = loop_result["thinking"]

                # Log thinking first (before summary)
                if thinking_content:
                    if config.LOG_FULL_THINKING:
                        self._log(f'[THINKING] {thinking_content}')
                    else:
                        self._log(f'[THINKING] Model used thinking ({len(thinking_content)} chars)')

                # Build response content with all tool calls
                all_tool_calls = loop_result["all_tool_calls"]
                if all_tool_calls:
                    response_content = json.dumps({"tool_calls": all_tool_calls}, indent=2)
                else:
                    response_content = loop_result["final_response"]

                self._log(f'[LLM] Total: {duration_ms}ms, {total_tokens} tokens, {loop_result["iterations"]} iteration(s)')
                self._log(f'[TOOLS] Made {len(all_tool_calls)} tool call(s): {[tc["function"]["name"] for tc in all_tool_calls]}')

                # Accumulate tokens and duration
                self.total_tokens += total_tokens
                self.total_duration_ms += duration_ms

                error_info = None  # No single error for loop
            else:
                # Standard single-turn LLM call for other domains
                messages = [{"role": "user", "content": prompt}]
                tools = None

                self._log(f'[LLM] Sending request to model...')
                llm_response = _client.chat_completion(messages, tools)

                # Safely extract duration and tokens
                duration_ms = llm_response.get("duration_ms", 0) if isinstance(llm_response, dict) else 0
                total_tokens = llm_response.get("total_tokens", 0) if isinstance(llm_response, dict) else 0

                # Check for LLM errors
                error_info = _client.get_error_info(llm_response)
                if error_info:
                    self._log(f'[ERROR] LLM {error_info["type"]}: {error_info["message"]}')
                    if error_info["detail"]:
                        self._log(f'[ERROR] Detail: {error_info["detail"][:100]}')
                else:
                    self._log(f'[LLM] Response received in {duration_ms}ms, {total_tokens} tokens')

                # Accumulate tokens and duration for tok/s calculation
                self.total_tokens += total_tokens
                self.total_duration_ms += duration_ms

                # Extract content with thinking separation
                content_info = _client.extract_content_with_thinking(llm_response)
                response_content = content_info["content"]  # Final content (without thinking)
                thinking_content = content_info["thinking"]  # Thinking content (if present)

                # Log thinking for single-turn path
                if thinking_content:
                    if config.LOG_FULL_THINKING:
                        self._log(f'[THINKING] {thinking_content}')
                    else:
                        self._log(f'[THINKING] Model used thinking ({len(thinking_content)} chars)')

            # Log response
            if config.LOG_FULL_RESPONSE:
                self._log(f'[OUTPUT] {response_content}')
            else:
                response_display = response_content[:200] + '...' if len(response_content) > 200 else response_content
                response_display = response_display.replace('\n', ' ')
                self._log(f'[OUTPUT] {response_display}')

            # Get domain-specific evaluator
            evaluator = get_evaluator(domain)
            self._log(f'[EVAL] Using {evaluator.name} (PASS2: {evaluator.uses_pass2})')
            
            # Evaluate using domain-specific strategy (only final content, not thinking)
            # Pass the original prompt for context (helps PASS 2 extraction)
            self._log(f'[SCORING] Evaluating response...')
            result = evaluator.evaluate(response_content, expected, level, prompt)
            
            # Build details dict
            details = result.details
            if not isinstance(details, dict):
                details = {"details": str(details)}
            
            # Add evaluator info
            details["evaluator"] = evaluator.name
            details["uses_pass2"] = evaluator.uses_pass2
            
            # Add thinking content to details if present
            if thinking_content:
                details["thinking"] = thinking_content
            
            # Add LLM error info to details if present
            if error_info:
                details["llm_error"] = {
                    "type": error_info["type"],
                    "message": error_info["message"],
                    "detail": error_info["detail"]
                }
            
            # Log result
            status_icon = '✓' if result.status == 'passed' else '✗'
            self._log(f'[RESULT] {status_icon} Status: {result.status.upper()}, Score: {result.score*100:.0f}%')
            
            # Get details string for logging
            details_inner = details.get("details", "")
            if isinstance(details_inner, str):
                self._log(f'[DETAILS] {details_inner[:80]}')
            elif isinstance(details_inner, dict):
                # For conversation: show relevance/correctness/fluency
                if "relevance" in details_inner:
                    self._log(f'[DETAILS] R:{details_inner.get("relevance",0):.2f} C:{details_inner.get("correctness",0):.2f} F:{details_inner.get("fluency",0):.2f}')
                else:
                    self._log(f'[DETAILS] {str(details_inner)[:80]}')
            else:
                self._log(f'[DETAILS] {str(details_inner)[:80]}')
            
            self._log(f'═══════════════════════════════════════════════════════════════')

            # Log to JSON file
            test_logger.log_test(
                domain=domain,
                level=level,
                test_id=f"{domain}_L{level}",
                prompt=prompt,
                response=response_content,
                thinking=thinking_content,
                expected=expected,
                score=result.score,
                status=result.status,
                details=details,
                duration_ms=duration_ms,
                tokens=total_tokens,
                model_name=self.model_name,
                system_prompt=locals().get('system_prompt')
            )

            return {
                "prompt": prompt,
                "response": response_content,
                "expected": expected,
                "score": result.score,
                "status": result.status,
                "details": details,
                "duration_ms": duration_ms
            }
        except Exception as e:
            self._log(f'[ERROR] Exception: {type(e).__name__}: {str(e)[:100]}')
            return {
                "prompt": "",
                "response": "",
                "expected": None,
                "score": 0.0,
                "status": "failed",
                "details": f"Test error: {type(e).__name__}: {str(e)}",
                "duration_ms": 0
            }
    
    def _resolve_system_prompt(self, test: Dict[str, Any], domain_name: str) -> Optional[str]:
        """
        Resolve system prompt using 3-layer hierarchy:
        Domain-level → Level-level → Test-level with mode (overwrite/append)

        Args:
            test: Test dictionary (fresh from test_manager.list_tests)
            domain_name: Domain name to load

        Returns:
            Resolved system prompt or None
        """
        from evaluator.test_loader import test_loader, TestDefinition

        # Load domain first (always needed for fallback)
        domain = test_loader.load_domain(domain_name)

        # Load level definition
        level_num = test.get('level', 1)
        level_def = test_loader.load_level(domain_name, level_num)

        # Always build TestDefinition from the test dict (which is fresh from
        # test_manager.list_tests). Don't use test_loader.get_test() as it may
        # return stale cached data from a different TestLoader instance.
        test_def = TestDefinition(
            id=test.get('id', ''),
            name=test.get('name', ''),
            description=test.get('description', ''),
            prompt=test.get('prompt', ''),
            expected=test.get('expected', {}),
            evaluator_id=test.get('evaluator_id', ''),
            domain_id=domain_name,
            level=level_num,
            system_prompt=test.get('system_prompt'),
            system_prompt_mode=test.get('system_prompt_mode', 'overwrite')
        )

        # Use 3-layer hierarchy resolver
        resolved = test_loader.resolve_system_prompt(test_def, domain, level_def)

        # Determine source for logging
        if resolved:
            snippet = resolved.replace('\n', ' ').strip()[:100]
            if len(resolved) > 100:
                snippet += '...'

            sources = []
            if domain and domain.system_prompt:
                sources.append('DOMAIN')
            if level_def and level_def.system_prompt:
                sources.append(f'LEVEL(mode={level_def.system_prompt_mode})')
            if test_def.system_prompt:
                sources.append(f'TEST(mode={test_def.system_prompt_mode})')

            source = '+'.join(sources) if sources else 'UNKNOWN'

            self._log(f'[SYSTEM][{domain_name}][L{level_num}] Source: {source}')
            self._log(f'[SYSTEM][{domain_name}][L{level_num}] Prompt ({len(resolved)} chars): {snippet}')
        else:
            self._log(f'[SYSTEM][{domain_name}][L{level_num}] No system prompt at any level')

        return resolved

    def _resolve_registry_tools(self, test: Dict[str, Any], domain_name: str, level_num: int):
        """
        Resolve tools from the registry using 3-layer hierarchy (always append, deduplicated).

        Returns:
            (tools_list, mock_responses_dict, mock_response_types_dict)
        """
        from evaluator.test_loader import test_loader, TestDefinition

        domain = test_loader.load_domain(domain_name)
        level_def = test_loader.load_level(domain_name, level_num)

        test_def = TestDefinition(
            id=test.get('id', ''),
            name=test.get('name', ''),
            description=test.get('description', ''),
            prompt=test.get('prompt', ''),
            expected=test.get('expected', {}),
            evaluator_id=test.get('evaluator_id', ''),
            domain_id=domain_name,
            level=level_num,
            tool_ids=test.get('tool_ids')
        )

        resolved = test_loader.resolve_tools(test_def, domain, level_def)

        tools_list = []
        mock_responses = {}
        mock_response_types = {}
        no_mock_tools = set()

        for rt in resolved:
            tool_def = {"type": rt.get("type", "function"), "function": rt["function"]}
            tools_list.append(tool_def)

            func_name = rt["function"]["name"]
            if rt.get("no_mock"):
                no_mock_tools.add(func_name)
            elif rt.get("mock_response") is not None:
                mock_responses[func_name] = rt["mock_response"]
                mock_response_types[func_name] = rt.get("mock_response_type", "json")

        # Also check skill tools for no_mock — skill tool no_mock overrides test_definitions tool
        if tools_list:
            from backend.skills_manager import skills_manager
            resolved_fn_names = {t["function"]["name"] for t in tools_list}
            for skill_def in skills_manager.get_all_skill_tool_defs():
                if skill_def.get('no_mock'):
                    fn = skill_def.get('function', {}).get('name', '')
                    if fn in resolved_fn_names:
                        no_mock_tools.add(fn)
                        mock_responses.pop(fn, None)

        return tools_list, mock_responses, mock_response_types, no_mock_tools

    def _format_tool_result_text(self, data, indent=0):
        """Convert tool result dict/list to plain text format to save tokens"""
        if not isinstance(data, (dict, list)):
            return str(data)
        lines = []
        prefix = "  " * indent
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, dict):
                    lines.append(f"{prefix}{key}:")
                    lines.append(self._format_tool_result_text(value, indent + 1))
                elif isinstance(value, list):
                    lines.append(f"{prefix}{key}:")
                    for i, item in enumerate(value):
                        if isinstance(item, dict):
                            lines.append(f"{prefix}  [{i+1}]")
                            lines.append(self._format_tool_result_text(item, indent + 2))
                        else:
                            lines.append(f"{prefix}  - {item}")
                else:
                    lines.append(f"{prefix}{key}: {value}")
        elif isinstance(data, list):
            for i, item in enumerate(data):
                if isinstance(item, dict):
                    lines.append(f"{prefix}[{i+1}]")
                    lines.append(self._format_tool_result_text(item, indent + 1))
                else:
                    lines.append(f"{prefix}- {item}")
        return "\n".join(lines)

    def _execute_python_mock(self, py_code: str, args: dict):
        """Execute Python mock response via exec() with AST-based sandboxing.

        Before execution, the code is parsed and validated against a denylist
        of dangerous AST node patterns (dunder attribute access, imports, class
        definitions, etc.) that could escape the restricted namespace.
        """
        import math
        import ast

        # --- AST-based sandbox validation ---
        _DUNDER_DENIES = frozenset({
            '__class__', '__bases__', '__subclasses__', '__mro__',
            '__globals__', '__builtins__', '__import__', '__getattr__',
            '__getattribute__', '__setattr__', '__delattr__',
            '__reduce__', '__reduce_ex__', '__getstate__',
            '__code__', '__func__', '__self__',
        })

        try:
            tree = ast.parse(py_code, mode='exec')
        except SyntaxError as e:
            self._log(f'[PY-MOCK] Syntax error: {e}')
            return {"error": f"Python mock syntax error: {e}"}

        for node in ast.walk(tree):
            # Block import statements
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                return {"error": "Python mock: import statements are not allowed"}
            # Block class/function definitions
            if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef)):
                return {"error": "Python mock: class/async def not allowed"}
            # Block dunder attribute access (e.g. x.__class__.__bases__)
            if isinstance(node, ast.Attribute) and node.attr in _DUNDER_DENIES:
                return {"error": f"Python mock: access to '{node.attr}' is not allowed"}
            # Block exec()/eval()/compile() calls
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ('exec', 'eval', 'compile', '__import__'):
                    return {"error": f"Python mock: {node.func.id}() is not allowed"}

        namespace = {
            'args': args,
            'math': math,
            'json': json,
            're': __import__('re'),
            'result': None,
            # Safe builtins that mock code commonly needs
            'sum': sum, 'len': len, 'int': int, 'str': str,
            'list': list, 'dict': dict, 'tuple': tuple, 'set': set,
            'bool': bool, 'float': float, 'min': min, 'max': max,
            'abs': abs, 'round': round, 'range': range,
            'enumerate': enumerate, 'zip': zip, 'map': map,
            'filter': filter, 'sorted': sorted, 'any': any, 'all': all,
            'isinstance': isinstance, 'True': True, 'False': False, 'None': None,
        }
        try:
            exec(py_code, {'__builtins__': {}}, namespace)
            result = namespace.get('result')
            if result is None:
                return {"error": "mock did not set result"}
            return result
        except Exception as e:
            self._log(f'[PY-MOCK] Exception: {str(e)}')
            return {"error": f"Python mock failed: {str(e)}"}

    def _run_single_configurable_test(self, test: Dict[str, Any], domain: str,
                                       level: int, model_name: str, run_id: int, run_llm_client=None) -> TestResult:
        """Run a single configurable test"""
        _client = run_llm_client or llm_client
        test_id = test['id']
        prompt = test['prompt']
        expected = test.get('expected', {})
        weight = test.get('weight', 1.0)
        
        # Initialize variables for tool calling
        loop_result = None
        tools = None
        
        self._log(f'')
        self._log(f'═══════════════════════════════════════════════════════════════')
        self._log(f'[TEST][{domain.upper()}][L{level}] {test.get("name", test_id)}')
        self._log(f'───────────────────────────────────────────────────────────────')
        
        # Truncate prompt for display
        prompt_display = prompt[:300] + '...' if len(prompt) > 300 else prompt
        prompt_display = prompt_display.replace('\n', ' ')
        self._log(f'[INPUT] {prompt_display}')
        
        if expected:
            expected_str = str(expected)[:100] + '...' if len(str(expected)) > 100 else str(expected)
            self._log(f'[EXPECTED] {expected_str}')
        
        # Resolve system prompt using hierarchy (domain → test with mode)
        system_prompt = self._resolve_system_prompt(test, domain)

        # Resolve registry tools (domain → level → test, append + dedup)
        registry_tools, registry_mocks, registry_mock_types, registry_no_mock = self._resolve_registry_tools(test, domain, level)

        # Check if test has embedded tools OR is tool_calling domain OR uses tool_call evaluator
        test_tools = test.get('tools') or []  # Handle None explicitly
        has_embedded_tools = len(test_tools) > 0
        evaluator_id = test.get('evaluator_id', '')
        uses_tool_evaluator = evaluator_id == 'tool_call'
        has_registry_tools = len(registry_tools) > 0

        if domain == "tool_calling" or has_embedded_tools or uses_tool_evaluator or has_registry_tools:
            # Start with registry tools (if any)
            tools = list(registry_tools)
            mock_responses = dict(registry_mocks)
            mock_response_types = dict(registry_mock_types)
            no_mock_tools = set(registry_no_mock)

            if has_embedded_tools:
                # Merge embedded tools (override registry tools with same function name)
                embedded_func_names = set()
                for t in test_tools:
                    tool_def = {
                        "type": "function",
                        "function": t.get("function", t)
                    }
                    func_name = t.get("function", {}).get("name") or t.get("name")
                    embedded_func_names.add(func_name)

                    # Replace or append
                    existing_idx = next((i for i, rt in enumerate(tools) if rt.get("function", {}).get("name") == func_name), None)
                    if existing_idx is not None:
                        tools[existing_idx] = tool_def
                    else:
                        tools.append(tool_def)

                    if t.get("no_mock"):
                        no_mock_tools.add(func_name)
                        mock_responses.pop(func_name, None)
                    elif "mock_response" in t:
                        mock_responses[func_name] = t["mock_response"]
                        mock_response_types[func_name] = "json"  # embedded are always JSON

                self._log(f'[TOOLS] Merged: registry({len(registry_tools)}) + embedded({len(test_tools)}) = {len(tools)} tools')
            elif has_registry_tools:
                self._log(f'[TOOLS] Using registry tools: {[t["function"]["name"] for t in tools]}')
            elif not tools:
                from evaluator.tools import tool_framework
                tools = tool_framework.tools
                mock_responses = None
                mock_response_types = {}
                self._log(f'[TOOLS] Available: {[t["function"]["name"] for t in tools]}')

            if no_mock_tools:
                self._log(f'[TOOLS] No-mock (real backend): {sorted(no_mock_tools)}')

            # Run tool calling loop
            loop_result = self._run_tool_calling_loop(prompt, tools, mock_responses, system_prompt=system_prompt, mock_response_types=mock_response_types, no_mock_tools=no_mock_tools, run_llm_client=_client)

            duration_ms = loop_result["total_duration_ms"]
            total_tokens = loop_result["total_tokens"]
            thinking_content = loop_result["thinking"]

            # Log thinking first (before summary)
            if thinking_content:
                if config.LOG_FULL_THINKING:
                    self._log(f'[THINKING] {thinking_content}')
                else:
                    self._log(f'[THINKING] Model used thinking ({len(thinking_content)} chars)')

            # Build response content
            all_tool_calls = loop_result["all_tool_calls"]
            final_response = loop_result["final_response"]
            if evaluator_id == 'tool_call':
                # tool_call evaluator scores which tools were called
                response_content = json.dumps({"tool_calls": all_tool_calls}, indent=2) if all_tool_calls else final_response
            else:
                # All other evaluators score the final text response; fall back to tool_calls if no final response
                response_content = final_response if final_response else json.dumps({"tool_calls": all_tool_calls}, indent=2)

            self._log(f'[LLM] Total: {duration_ms}ms, {total_tokens} tokens, {loop_result["iterations"]} iteration(s)')
            self._log(f'[TOOLS] Made {len(all_tool_calls)} tool call(s): {[tc["function"]["name"] for tc in all_tool_calls]}')

            # Accumulate tokens and duration
            self.total_tokens += total_tokens
            self.total_duration_ms += duration_ms
        else:
            # Standard single-turn LLM call for other domains
            if system_prompt:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ]
                self._log(f'[SYSTEM] Using custom system prompt ({len(system_prompt)} chars)')
            else:
                messages = [{"role": "user", "content": prompt}]
            tools = None
            
            self._log(f'[LLM] Sending request to model...')
            llm_response = _client.chat_completion(messages, tools)

            duration_ms = llm_response.get("duration_ms", 0) if isinstance(llm_response, dict) else 0
            total_tokens = llm_response.get("total_tokens", 0) if isinstance(llm_response, dict) else 0
            self._log(f'[LLM] Response received in {duration_ms}ms, {total_tokens} tokens')

            # Accumulate tokens
            self.total_tokens += total_tokens
            self.total_duration_ms += duration_ms

            # Extract content with thinking separation
            content_info = _client.extract_content_with_thinking(llm_response)
            response_content = content_info["content"]
            thinking_content = content_info["thinking"]

            # Log thinking for single-turn path
            if thinking_content:
                if config.LOG_FULL_THINKING:
                    self._log(f'[THINKING] {thinking_content}')
                else:
                    self._log(f'[THINKING] Model used thinking ({len(thinking_content)} chars)')

        # Log response
        if config.LOG_FULL_RESPONSE:
            self._log(f'[OUTPUT] {response_content}')
        else:
            response_display = response_content[:200] + '...' if len(response_content) > 200 else response_content
            response_display = response_display.replace('\n', ' ')
            self._log(f'[OUTPUT] {response_display}')

        # Evaluate using appropriate evaluator
        evaluator_id = test.get('evaluator_id', '')
        
        # Check if we need to use a custom evaluator from test_definitions/evaluators/
        evaluator_config = test_loader.get_evaluator(evaluator_id) if evaluator_id else None
        
        if evaluator_config and evaluator_config.type in ('custom', 'regex', 'hybrid'):
            custom_eval = CustomEvaluator(evaluator_config.to_dict())
            self._log(f'[EVAL] Using custom evaluator: {evaluator_config.name} (type: {evaluator_config.type})')
            result = custom_eval.evaluate(response_content, expected, level)
        elif evaluator_id:
            # Use built-in evaluator type (tool_call, keyword, two_pass, sql_executor)
            evaluator = get_evaluator(domain, evaluator_type=evaluator_id)
            self._log(f'[EVAL] Using {evaluator.name} (type: {evaluator_id})')
            result = evaluator.evaluate(response_content, expected, level)
        else:
            # Fall back to domain default evaluator
            evaluator = get_evaluator(domain)
            self._log(f'[EVAL] Using {evaluator.name} (PASS2: {evaluator.uses_pass2})')
            result = evaluator.evaluate(response_content, expected, level)
        
        # Build details
        details = result.details if hasattr(result, 'details') else {}
        if not isinstance(details, dict):
            details = {"details": str(details)}
        
        # Add evaluator info
        if evaluator_config and evaluator_config.type == 'custom':
            details["evaluator"] = f"custom:{evaluator_config.name}"
        elif evaluator_config:
            details["evaluator"] = evaluator_config.name
            details["uses_pass2"] = evaluator_config.uses_pass2
        else:
            details["evaluator"] = "unknown"
        
        # Include response in details for modal display
        details['response'] = response_content
        details['duration_ms'] = duration_ms
        
        # Add thinking content to details if present
        if thinking_content:
            details["thinking"] = thinking_content
        
        # Add conversation log (for tool-calling tests or any test with multi-turn interaction)
        if loop_result and loop_result.get("conversation_log"):
            details["conversation_log"] = loop_result["conversation_log"]
        
        # Store tool definitions for UI display (only for tool-calling tests)
        if (domain == "tool_calling" or has_embedded_tools) and tools:
                details["tools_available"] = [
                    {
                        "name": t.get("function", {}).get("name", ""),
                        "description": t.get("function", {}).get("description", ""),
                        "parameters": t.get("function", {}).get("parameters", {})
                    }
                    for t in tools
                ]
        
        # Log result
        status_icon = '✓' if result.status == 'passed' else '✗'
        self._log(f'[RESULT] {status_icon} Status: {result.status.upper()}, Score: {result.score*100:.0f}%')
        
        # Save individual test result with resolved system_prompt and mode
        db.save_individual_test_result(
            run_id=run_id,
            test_id=test_id,
            domain=domain,
            level=level,
            prompt=prompt,
            response=response_content,
            expected=json.dumps(expected) if expected else None,
            score=result.score,
            status=result.status,
            details=json.dumps(details) if details else None,
            duration_ms=duration_ms,
            model_name=model_name,
            system_prompt=system_prompt,  # Save the resolved system prompt that was actually used
            system_prompt_mode=test.get('system_prompt_mode', 'overwrite')  # Save the mode
        )
        
        self._log(f'═══════════════════════════════════════════════════════════════')
        
        # Log to JSON file
        test_logger.log_test(
            domain=domain,
            level=level,
            test_id=test_id,
            prompt=prompt,
            response=response_content,
            thinking=thinking_content,
            expected=expected,
            score=result.score,
            status=result.status,
            details=details,
            duration_ms=duration_ms,
            tokens=total_tokens,
            model_name=model_name,
            system_prompt=system_prompt
        )
        
        return TestResult(
            test_id=test_id,
            domain=domain,
            level=level,
            score=result.score,
            status=result.status,
            weight=weight,
            details=details
        )
    
    def _generate_summary(self, run_id: int, model_name: str, run_llm_client=None):
        """Generate executive summary"""
        self._log('[INFO] Semua tes selesai. Membuat ringkasan...')
        test_results = db.get_test_results(run_id)
        
        # Convert database rows to dict format
        results_dict = []
        for result in test_results:
            results_dict.append({
                "domain": result["domain"],
                "level": result["level"],
                "score": result["score"],
                "status": result["status"]
            })
        
        # Calculate run stats
        overall_score = scoring_engine.calculate_overall_score(results_dict)
        tok_per_sec = 0
        if self.total_duration_ms > 0:
            tok_per_sec = round(self.total_tokens / (self.total_duration_ms / 1000), 1)

        run_stats = {
            "overall_score": overall_score,
            "total_tokens": self.total_tokens,
            "tok_per_sec": tok_per_sec,
            "total_duration_ms": self.total_duration_ms
        }

        # Generate summary with full stats
        summary = scoring_engine.generate_summary(results_dict, model_name, llm_client=run_llm_client or llm_client, run_stats=run_stats)
        
        # Store summary with token stats
        db.complete_evaluation_run(
            run_id, summary, overall_score,
            total_tokens=self.total_tokens,
            total_duration_ms=self.total_duration_ms
        )
    
    def _execute_js_mock(self, js_code: str, args: dict) -> dict:
        """Execute JavaScript mock response via node subprocess"""
        import subprocess
        args_json = json.dumps(args)
        full_code = f'const ARGS = {repr(args_json)};\n{js_code}'
        try:
            result = subprocess.run(
                ['node', '-e', full_code],
                capture_output=True, text=True, timeout=5
            )
            output = result.stdout.strip()
            return json.loads(output)
        except subprocess.TimeoutExpired:
            return {"error": "timed out"}
        except Exception as e:
            self._log(f'[JS-MOCK] Exception: {str(e)}')
            return {"error": f"JS mock failed: {str(e)}"}

    def _run_tool_calling_loop(self, prompt: str, tools: list, mock_responses: Dict[str, Any] = None, system_prompt: str = None, mock_response_types: Dict[str, str] = None, no_mock_tools: set = None, enable_planning: bool = True, run_llm_client=None) -> Dict[str, Any]:
        """
        Run multi-turn tool calling loop with mock execution.
        
        Args:
            prompt: User prompt
            tools: List of tool definitions (OpenAI format)
            mock_responses: Optional dict mapping tool name -> mock response
                           If provided, uses these instead of tool_framework
            system_prompt: Optional system prompt
        
        Continues until:
        - LLM returns final answer (no tool calls)
        - OR max iterations reached
        
        Returns:
            Dict with:
            - all_tool_calls: List of all tool calls made
            - final_response: Final text response
            - iterations: Number of iterations
            - total_duration_ms: Total duration
            - total_tokens: Total tokens used
            - thinking: Thinking content (if any)
            - messages: Full conversation history
        """
        from evaluator.tools import tool_framework
        _client = run_llm_client or llm_client

        # Read max tool iterations from DB (respects web UI override)
        max_tool_iterations = int(db.get_setting('max_tool_iterations', str(MAX_TOOL_ITERATIONS)))

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        all_tool_calls = []
        conversation_log = []  # Capture each turn's details
        seen_tool_calls = {}   # "func:args" → cached result (for duplicate detection)
        agent_state_searches = []  # [(query, top_3_lines), ...] — ordered search history
        agent_plan = []        # [(item_text, matched_query, matched_result), ...] — execution plan
        consecutive_dupes = 0  # Track consecutive duplicate-only iterations
        total_duration_ms = 0
        total_tokens = 0
        thinking_content = None
        final_response = ""

        # --- Planning phase: LLM creates execution checklist (1 call, no tools) ---
        if enable_planning and tools:  # Only plan when tools are available (agent mode)
            plan_messages = [
                {"role": "system", "content": (
                    "Read the user message below. Identify every item that needs to be "
                    "searched or resolved using tools.\n"
                    "Output ONLY a checklist, one item per line:\n"
                    "- [ ] <item description>\n\n"
                    "Be thorough — do not miss any item. Keep each item concise (under 10 words)."
                )},
                {"role": "user", "content": prompt}
            ]
            try:
                plan_response = _client.chat_completion(
                    plan_messages, tools=None, temperature=0.0,
                    enable_thinking=False, max_tokens=2048
                )
                if plan_response.get("success", False):
                    plan_info = _client.extract_content_with_thinking(plan_response)
                    plan_text = plan_info.get("content", "")
                    # Parse "- [ ] ..." lines
                    import re
                    for line in plan_text.split("\n"):
                        match = re.match(r'\s*[-*]\s*\[[ x]\]\s*(.+)', line.strip())
                        if match:
                            agent_plan.append([match.group(1).strip(), None, None])  # [item, query, result]
                    plan_duration = plan_response.get("duration_ms", 0)
                    plan_tokens = plan_response.get("total_tokens", 0)
                    total_duration_ms += plan_duration
                    total_tokens += plan_tokens
                    self._log(f'[TOOL-LOOP] Plan created: {len(agent_plan)} items ({plan_duration}ms, {plan_tokens}tok)')
                    for item in agent_plan:
                        self._log(f'[TOOL-LOOP]   - [ ] {item[0]}')
                else:
                    self._log(f'[TOOL-LOOP] Planning failed: {plan_response.get("error_type", "unknown")}')
            except Exception as e:
                self._log(f'[TOOL-LOOP] Planning error: {e}')

        # Inject plan into messages so LLM sees it from iteration 1
        if agent_plan:
            plan_lines = "\n".join(f"- [ ] {item[0]}" for item in agent_plan)
            plan_msg = (
                f"## Your execution plan ({len(agent_plan)} items):\n"
                f"{plan_lines}\n\n"
                f"Work through each unchecked item. Search one item per tool call.\n"
                f"When all items are done, output your final answer."
            )
            messages.append({"role": "user", "content": plan_msg})

        for iteration in range(max_tool_iterations):
            # Estimate prompt tokens from messages (rough: 1 token ≈ 4 chars)
            prompt_chars = sum(len(m.get("content", "") or "") for m in messages)
            est_tokens = prompt_chars // 4
            self._log(f'[TOOL-LOOP] Iteration {iteration + 1}/{max_tool_iterations} (~{est_tokens}tok)')
            
            # Initialize turn log
            turn_log = {
                "turn": iteration + 1,
                "thinking": None,
                "tool_calls": [],
                "tool_results": [],
                "response": None
            }
            
            # Send to LLM
            llm_response = _client.chat_completion(messages, tools)

            # Accumulate stats
            duration_ms = llm_response.get("duration_ms", 0) if isinstance(llm_response, dict) else 0
            tokens = llm_response.get("total_tokens", 0) if isinstance(llm_response, dict) else 0
            total_duration_ms += duration_ms
            total_tokens += tokens

            # Check for LLM errors — break loop on failure
            if not llm_response.get("success", False):
                error_type = llm_response.get("error_type", "unknown")
                self._log(f'[TOOL-LOOP] LLM error ({error_type}): {llm_response.get("error_detail", "")[:150]}')
                # On generation_timeout, try to salvage what thinking we have
                if error_type == "generation_timeout":
                    choices = llm_response.get("response", {}).get("choices", [])
                    if choices:
                        reasoning = choices[0].get("message", {}).get("reasoning_content") or ""
                        if reasoning:
                            turn_log["thinking"] = reasoning
                            thinking_content = thinking_content or reasoning
                self._log(f'[TOOL-LOOP] Breaking due to LLM error after {iteration + 1} iterations')
                conversation_log.append(turn_log)
                break

            # Extract content and thinking
            content_info = _client.extract_content_with_thinking(llm_response)
            response_content = content_info["content"]
            
            # Capture thinking for this turn
            turn_thinking = content_info.get("thinking")
            if turn_thinking:
                turn_log["thinking"] = turn_thinking
                # Also store first turn thinking as main thinking
                if iteration == 0:
                    thinking_content = turn_thinking
            
            # Check for tool calls
            tool_calls = content_info.get("tool_calls", [])
            
            # Also try to extract from response content if it's JSON
            if not tool_calls:
                try:
                    data = json.loads(response_content)
                    if isinstance(data, dict) and "tool_calls" in data:
                        tool_calls = data["tool_calls"]
                except (json.JSONDecodeError, TypeError):
                    pass
            
            # If no tool calls, we have the final answer
            if not tool_calls:
                # Detect "thinking-only" turn: model reasoned internally but emitted no text.
                # This is a known Qwen/thinking-model quirk. Nudge it to write its answer.
                if not response_content.strip() and turn_thinking:
                    self._log(f'[TOOL-LOOP] Empty response with thinking detected — nudging for output')
                    turn_log["response"] = ""
                    conversation_log.append(turn_log)
                    messages.append({"role": "user", "content": "Please output your final answer now."})
                    continue
                final_response = response_content
                turn_log["response"] = response_content
                conversation_log.append(turn_log)
                self._log(f'[TOOL-LOOP] Final answer received (no more tool calls)')
                break
            
            # Process tool calls
            self._log(f'[TOOL-LOOP] Got {len(tool_calls)} tool call(s)')
            iteration_has_new_call = False

            for tc in tool_calls:
                func_name = tc.get("function", {}).get("name", "unknown")
                func_args_str = tc.get("function", {}).get("arguments", "{}")
                tc_id = tc.get("id", f"call_{len(all_tool_calls)}")
                
                # Parse arguments
                try:
                    func_args = json.loads(func_args_str) if isinstance(func_args_str, str) else func_args_str
                except json.JSONDecodeError:
                    func_args = {}
                
                self._log(f'[TOOL-CALL] {func_name}({json.dumps(func_args)[:50]}...)')

                # Store tool call info
                tool_call_info = {
                    "id": tc_id,
                    "function": {
                        "name": func_name,
                        "arguments": func_args_str if isinstance(func_args_str, str) else json.dumps(func_args)
                    }
                }
                all_tool_calls.append(tool_call_info)

                # --- Duplicate tool call detection ---
                call_key = f"{func_name}:{json.dumps(func_args, sort_keys=True) if isinstance(func_args, dict) else func_args_str}"
                if call_key in seen_tool_calls:
                    cached_preview = seen_tool_calls[call_key]
                    result_str = (
                        f"[DUPLICATE SEARCH] You already searched this exact query. Previous top results:\n"
                        f"{cached_preview}\n\n"
                        f"Do NOT search this again. Either:\n"
                        f"1. Pick a code from these results, OR\n"
                        f"2. Search for something DIFFERENT you haven't searched yet, OR\n"
                        f"3. If you have all the information needed, output your final response."
                    )
                    self._log(f'[TOOL-LOOP] Skipped duplicate: {func_name}({json.dumps(func_args)[:50]})')
                    mock_result = {
                        "tool_call_id": tc_id,
                        "function_name": func_name,
                        "result": result_str,
                        "success": True
                    }
                    # Add to turn log + messages, then skip execution
                    turn_log["tool_calls"].append({"name": func_name, "arguments": func_args, "id": tc_id})
                    turn_log["tool_results"].append({"tool_call_id": tc_id, "function_name": func_name, "result": result_str})
                    messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": result_str})
                    continue

                # Execute tool — no_mock tools use real backend, others use mock
                if no_mock_tools and func_name in no_mock_tools:
                    from backend.tools.registry import ToolRegistry
                    _registry = ToolRegistry()
                    _module = _registry._load_tool_module(func_name)
                    if _module is None:
                        mock_result_data = {"error": f"No backend implementation for tool: {func_name}"}
                        self._log(f'[BACKEND] ERROR: no backend found for {func_name}')
                    elif not hasattr(_module, 'execute'):
                        mock_result_data = {"error": f"Tool backend '{func_name}' missing execute() function"}
                        self._log(f'[BACKEND] ERROR: {func_name}.execute() not found')
                    else:
                        try:
                            mock_result_data = _module.execute({}, func_args)
                        except Exception as e:
                            mock_result_data = {"error": f"Backend execution error: {str(e)}"}
                            self._log(f'[BACKEND] ERROR executing {func_name}: {e}')
                    mock_result = {
                        "tool_call_id": tc_id,
                        "function_name": func_name,
                        "result": mock_result_data,
                        "success": "error" not in mock_result_data
                    }
                elif mock_responses and func_name in mock_responses:
                    mock_value = mock_responses[func_name]
                    mock_type = (mock_response_types or {}).get(func_name, 'json')

                    if mock_type == 'javascript' and isinstance(mock_value, str):
                        mock_result_data = self._execute_js_mock(mock_value, func_args)
                        self._log(f'[MOCK] Executed JS mock for {func_name}')
                    elif mock_type == 'python' and isinstance(mock_value, str):
                        mock_result_data = self._execute_python_mock(mock_value, func_args)
                        self._log(f'[MOCK] Executed Python mock for {func_name}')
                    else:
                        mock_result_data = mock_value
                        self._log(f'[MOCK] Using mock response for {func_name}')

                    mock_result = {
                        "tool_call_id": tc_id,
                        "function_name": func_name,
                        "result": mock_result_data,
                        "success": True
                    }
                else:
                    # Fall back to tool_framework
                    mock_result = tool_framework.execute_tool({
                        "id": tc_id,
                        "function": {
                            "name": func_name,
                            "arguments": json.dumps(func_args) if isinstance(func_args, dict) else func_args_str
                        }
                    })
                
                result_str = self._format_tool_result_text(mock_result.get("result", {}))
                self._log(f'[TOOL-RESULT] {result_str[:100]}...')

                # Cache result for duplicate detection (first 3 lines as preview)
                result_lines = result_str.split("\n")
                top_3 = "\n".join(result_lines[:3])
                seen_tool_calls[call_key] = top_3
                iteration_has_new_call = True

                # Update agent state with search result
                query = func_args.get("query", "") if isinstance(func_args, dict) else ""
                if query:
                    agent_state_searches.append((query, top_3))

                    # Match against plan items — check off completed items
                    if agent_plan:
                        query_words = set(query.lower().split())
                        first_result = top_3.split("\n")[0] if top_3 else ""
                        for plan_item in agent_plan:
                            if plan_item[1] is not None:
                                continue  # already matched
                            item_words = set(plan_item[0].lower().split())
                            # Match if 2+ words overlap (or all query words match for short queries)
                            overlap = query_words & item_words
                            if len(overlap) >= min(2, len(query_words)):
                                plan_item[1] = query
                                plan_item[2] = first_result
                                break
                
                # Add to turn log
                turn_log["tool_calls"].append({
                    "name": func_name,
                    "arguments": func_args,
                    "id": tc_id
                })
                turn_log["tool_results"].append({
                    "tool_call_id": tc_id,
                    "function_name": func_name,
                    "result": mock_result.get("result", {})
                })
                
                # Add to conversation
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tc]
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result_str
                })
            
            # Build a text summary of this turn's tool calls + results for context
            if not turn_log.get("response") and turn_log.get("tool_calls"):
                turn_summary_lines = []
                for tc, tr in zip(turn_log.get("tool_calls", []), turn_log.get("tool_results", [])):
                    q = tc.get("arguments", {}).get("query", "") if isinstance(tc.get("arguments"), dict) else ""
                    result_text = str(tr.get("result", ""))
                    top_lines = "\n".join(result_text.split("\n")[:3])
                    turn_summary_lines.append(f"Searched: {tc.get('name', '?')}(\"{q}\")\nTop results: {top_lines}")
                turn_log["response"] = "\n".join(turn_summary_lines)

            # Add turn to conversation log
            conversation_log.append(turn_log)

            # --- Break on repeated duplicate loops ---
            if iteration_has_new_call:
                consecutive_dupes = 0
            else:
                consecutive_dupes += 1
                if consecutive_dupes >= 2:
                    self._log(f'[TOOL-LOOP] Breaking: {consecutive_dupes} consecutive duplicate-only iterations')
                    break

            # --- Context management: State Machine compaction (zero LLM calls) ---
            # When prompt tokens exceed threshold, rebuild messages as:
            #   [system] + [user] + [agent state] + [last 1 raw turn]
            #
            # State is built mechanically from agent_state_searches + latest thinking.
            # Instant, deterministic, no LLM overhead.
            COMPACT_THRESHOLD = 2500  # estimated tokens before triggering compaction
            _STATE_MARKER = "## Agent State"
            prompt_chars = sum(len(m.get("content", "") or "") for m in messages)
            est_tokens = prompt_chars // 4
            if est_tokens > COMPACT_THRESHOLD:
                # --- Build state table from search history ---
                table_lines = ["| # | Query | Top Results |", "|---|-------|-------------|"]
                for i, (query, top_3) in enumerate(agent_state_searches, 1):
                    # Flatten top_3 to single line for table
                    first_line = top_3.split("\n")[0] if top_3 else "(no results)"
                    table_lines.append(f"| {i} | \"{query}\" | {first_line} |")
                searches_table = "\n".join(table_lines)

                # --- Include latest thinking for reasoning continuity ---
                latest_thinking = (turn_thinking or "").strip()
                reasoning_section = ""
                if latest_thinking:
                    reasoning_section = f"\nLatest reasoning:\n> {latest_thinking[:1500]}\n"

                # --- Collect base messages (system + original user prompt) ---
                base_msgs = []
                for m in messages:
                    if m.get("role") in ("system", "user") and _STATE_MARKER not in m.get("content", ""):
                        base_msgs.append(m)
                    elif m.get("role") not in ("system", "user"):
                        break

                # --- Collect tail: last 1 assistant+tool pair ---
                tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
                if tool_indices:
                    cut_idx = tool_indices[-1]
                    if cut_idx > 0 and messages[cut_idx - 1].get("role") == "assistant":
                        cut_idx -= 1
                    tail_msgs = messages[cut_idx:]
                else:
                    tail_msgs = []

                # --- Build plan checklist (if available) ---
                plan_section = ""
                if agent_plan:
                    plan_lines = []
                    for item in agent_plan:
                        if item[1] is not None:
                            plan_lines.append(f"- [x] {item[0]} → {item[2] or 'done'}")
                        else:
                            plan_lines.append(f"- [ ] {item[0]}")
                    checked = sum(1 for p in agent_plan if p[1] is not None)
                    plan_section = f"Plan ({checked}/{len(agent_plan)} done):\n" + "\n".join(plan_lines) + "\n\n"

                # --- Rebuild messages ---
                state_content = (
                    f"{_STATE_MARKER}\n\n"
                    f"{plan_section}"
                    f"Searches completed (do NOT repeat these):\n"
                    f"{searches_table}\n"
                    f"{reasoning_section}\n"
                    f"Continue with next unchecked item or output your final answer.\n"
                    f"Do NOT repeat searches listed above."
                )
                state_msg = {"role": "user", "content": state_content}
                messages = base_msgs + [state_msg] + tail_msgs
                state_chars = len(state_content)
                self._log(f'[TOOL-LOOP] State compacted: {est_tokens}tok → ~{state_chars//4}tok ({len(agent_state_searches)} searches)')
                self._log(f'[TOOL-LOOP] State:\n{state_content[:500]}...')

        if not final_response and all_tool_calls:
            self._log(f'[TOOL-LOOP] No final response yet — forcing output with agent state')

            # Build plan summary for forced output
            plan_text = ""
            if agent_plan:
                plan_lines = []
                for item in agent_plan:
                    if item[1] is not None:
                        plan_lines.append(f"- [x] {item[0]} → {item[2] or 'done'}")
                    else:
                        plan_lines.append(f"- [ ] {item[0]} (not searched)")
                plan_text = "Plan:\n" + "\n".join(plan_lines) + "\n\n"

            # Build search results from agent state
            results_lines = []
            for query, top_3 in agent_state_searches:
                results_lines.append(f"- \"{query}\": {top_3.split(chr(10))[0]}")
            results_text = "\n".join(results_lines[-20:])

            # Collect latest thinking
            all_thinking = "\n".join(
                turn.get("thinking", "") or ""
                for turn in conversation_log[-3:]
                if turn.get("thinking")
            )

            force_messages = []
            if system_prompt:
                force_messages.append({"role": "system", "content": system_prompt})
            force_messages.append({"role": "user", "content": (
                f"{prompt}\n\n"
                f"## Your research is complete. Here is what you found:\n\n"
                f"{plan_text}"
                f"{results_text}\n\n"
                f"### Your reasoning:\n{all_thinking[-2000:]}\n\n"
                f"Now output your FINAL answer based on what you found. "
                f"Do NOT call any more tools. Output your answer now."
            )})

            try:
                force_response = _client.chat_completion(force_messages, tools=None, temperature=0.0)
                force_info = _client.extract_content_with_thinking(force_response)
                final_response = force_info.get("content", "").strip()
                force_duration = force_response.get("duration_ms", 0)
                force_tokens = force_response.get("total_tokens", 0)
                total_duration_ms += force_duration
                total_tokens += force_tokens
                self._log(f'[TOOL-LOOP] Forced output received ({len(final_response)} chars, {force_duration}ms)')
            except Exception as e:
                self._log(f'[TOOL-LOOP] Forced output failed: {e}')

        return {
            "all_tool_calls": all_tool_calls,
            "final_response": final_response,
            "iterations": iteration + 1,
            "total_duration_ms": total_duration_ms,
            "total_tokens": total_tokens,
            "thinking": thinking_content,
            "conversation_log": conversation_log,
            "messages": messages
        }
    
    def get_test_matrix(self, run_id: Optional[int] = None) -> Dict[str, Any]:
        """Get test matrix for UI display"""
        if not run_id:
            run_id = self.current_run_id

        if not run_id:
            return {"domains": {}, "status": "no_run"}

        test_results = db.get_test_results(run_id)

        # Organize by domain and level
        matrix = {}
        
        # Get domains from test definitions or use legacy
        if self.use_configurable_tests:
            domains = [d['id'] for d in test_manager.list_domains()]
        else:
            domains = ["conversation", "math", "sql", "tool_calling", "reasoning"]
        
        for domain in domains:
            matrix[domain] = {}
            for level in range(1, 6):
                matrix[domain][level] = {
                    "status": "pending",
                    "score": None,
                    "details": None,
                    "prompt": None,
                    "response": None,
                    "expected": None,
                    "duration_ms": None
                }

        # Fill with actual results
        for result in test_results:
            domain = result["domain"]
            level = result["level"]

            if domain in matrix and level in matrix[domain]:
                matrix[domain][level] = {
                    "status": result["status"],
                    "score": result["score"],
                    "details": json.loads(result["details"]) if result["details"] else None,
                    "prompt": result.get("prompt"),
                    "response": result.get("response"),
                    "expected": json.loads(result["expected"]) if result.get("expected") else None,
                    "duration_ms": result.get("duration_ms"),
                    "model_name": result.get("model_name")
                }

        # Get run info for model name
        run_info = db.get_evaluation_run(run_id)
        model_name = run_info.get("model_name") if run_info else None
        
        # Determine status
        if self.is_running:
            status = "running"
        elif self.has_error:
            status = "error"
        elif self.was_interrupted:
            status = "interrupted"
        else:
            status = "completed"
        
        # Calculate tok/s
        tok_per_sec = None
        if self.total_duration_ms > 0:
            tok_per_sec = (self.total_tokens / self.total_duration_ms) * 1000

        # Count completed individual tests for accurate progress
        individual_results = db.get_individual_test_results(run_id)
        completed_tests = len(individual_results)

        return {
            "domains": matrix,
            "run_id": run_id,
            "model_name": model_name,
            "status": status,
            "completed_tests": completed_tests,
            "overall_score": run_info.get("overall_score") if run_info else None,
            "tok_per_sec": round(tok_per_sec, 1) if tok_per_sec else None,
            "total_tokens": self.total_tokens,
            "total_duration_ms": self.total_duration_ms,
            "error_message": self.error_message if self.has_error else None
        }


# Global evaluation engine instance (uses legacy tests by default)
evaluation_engine = EvaluationEngine(use_configurable_tests=True)

# Configurable test engine (for when user wants to use JSON test definitions)
configurable_engine = EvaluationEngine(use_configurable_tests=True)
