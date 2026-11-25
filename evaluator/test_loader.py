"""
Test Loader - Load test definitions from directory structure.

Directory structure:
evaluator/test_definitions/
├── conversation/
│   ├── domain.json
│   ├── level_1/
│   │   ├── test_1.json
│   │   └── test_2.json
│   └── level_2/
│       └── test_1.json
├── math/
│   ├── domain.json
│   └── level_1/
│       └── addition.json
└── evaluators/
    ├── two_pass.json
    └── keyword.json

custom_tests/
└── my_domain/
    ├── domain.json
    └── level_1/
        └── custom_test.json
"""

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from datetime import datetime


@dataclass
class TestDefinition:
    """Represents a single test definition"""
    id: str
    name: str
    description: str
    prompt: str
    expected: Dict[str, Any]
    evaluator_id: str
    domain_id: str
    level: int
    system_prompt: Optional[str] = None
    system_prompt_mode: str = "overwrite"
    timeout_ms: int = 30000
    weight: float = 1.0
    enabled: bool = True
    path: str = ""
    created_at: str = ""
    updated_at: str = ""
    tools: List[Dict[str, Any]] = None  # Embedded tool definitions with mock responses
    tool_ids: Optional[List[str]] = None  # Registry tool IDs attached to this test
    vault: Optional[Dict[str, Any]] = None  # {"files": {path: content}} — run the tools against a real temp vault
    max_tool_turns: Optional[int] = None  # Per-test tool-loop step limit (vault tests)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], domain_id: str, level: int, path: str) -> 'TestDefinition':
        """Create from dictionary"""
        return cls(
            id=data.get('id', ''),
            name=data.get('name', ''),
            description=data.get('description', ''),
            system_prompt=data.get('system_prompt', None),
            system_prompt_mode=data.get('system_prompt_mode', 'overwrite'),
            prompt=data.get('prompt', ''),
            expected=data.get('expected', {}),
            evaluator_id=data.get('evaluator_id', ''),
            domain_id=domain_id,
            level=level,
            timeout_ms=data.get('timeout_ms', 30000),
            weight=data.get('weight', 1.0),
            enabled=data.get('enabled', True),
            path=path,
            created_at=data.get('created_at', datetime.now().isoformat()),
            updated_at=data.get('updated_at', datetime.now().isoformat()),
            tools=data.get('tools', None),
            tool_ids=data.get('tool_ids', None),
            vault=data.get('vault', None),
            max_tool_turns=data.get('max_tool_turns', None)
        )


@dataclass
class DomainDefinition:
    """Represents a domain definition"""
    id: str
    name: str
    description: str
    icon: str = "file"
    color: str = "#3B82F6"
    evaluator_id: str = ""
    system_prompt: Optional[str] = None
    system_prompt_mode: str = "overwrite"
    enabled: bool = True
    path: str = ""
    created_at: str = ""
    updated_at: str = ""
    tool_ids: Optional[List[str]] = None  # Registry tool IDs attached to this domain

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], path: str) -> 'DomainDefinition':
        """Create from dictionary"""
        return cls(
            id=data.get('id', ''),
            name=data.get('name', ''),
            description=data.get('description', ''),
            icon=data.get('icon', 'file'),
            color=data.get('color', '#3B82F6'),
            evaluator_id=data.get('evaluator_id', ''),
            system_prompt=data.get('system_prompt', None),
            system_prompt_mode=data.get('system_prompt_mode', 'overwrite'),
            enabled=data.get('enabled', True),
            path=path,
            created_at=data.get('created_at', datetime.now().isoformat()),
            updated_at=data.get('updated_at', datetime.now().isoformat()),
            tool_ids=data.get('tool_ids', None)
        )


@dataclass
class LevelDefinition:
    """Represents a level definition within a domain"""
    domain_id: str
    level: int
    system_prompt: Optional[str] = None
    system_prompt_mode: str = "overwrite"
    path: str = ""
    tool_ids: Optional[List[str]] = None  # Registry tool IDs attached to this level

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvaluatorDefinition:
    """Represents an evaluator definition"""
    id: str
    name: str
    type: str  # 'predefined' or 'custom'
    description: str = ""
    eval_prompt: Optional[str] = None
    extraction_regex: Optional[str] = None
    uses_pass2: bool = False
    config: Dict[str, Any] = None
    path: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        d = asdict(self)
        if d['config'] is None:
            d['config'] = {}
        return d
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any], path: str) -> 'EvaluatorDefinition':
        """Create from dictionary"""
        return cls(
            id=data.get('id', ''),
            name=data.get('name', ''),
            type=data.get('type', 'predefined'),
            description=data.get('description', ''),
            eval_prompt=data.get('eval_prompt'),
            extraction_regex=data.get('extraction_regex'),
            uses_pass2=data.get('uses_pass2', False),
            config=data.get('config', {}),
            path=path
        )


@dataclass
class ToolDefinition:
    """Represents a tool definition in the registry"""
    id: str
    name: str
    description: str = ""
    function: Dict[str, Any] = None  # OpenAI function schema
    mock_response: Any = None  # JSON object or JS code string
    mock_response_type: str = "json"  # 'json', 'javascript', or 'python'
    no_mock: bool = False  # If True, use real backend instead of mock (fail if no backend)
    path: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], path: str = "") -> 'ToolDefinition':
        """Create from dictionary"""
        return cls(
            id=data.get('id', ''),
            name=data.get('name', ''),
            description=data.get('description', ''),
            function=data.get('function'),
            mock_response=data.get('mock_response'),
            mock_response_type=data.get('mock_response_type', 'json'),
            no_mock=data.get('no_mock', False),
            path=path,
            created_at=data.get('created_at', ''),
            updated_at=data.get('updated_at', '')
        )


class TestLoader:
    """Load test definitions from directory structure"""
    
    def __init__(self,
                 tests_dir: str = "evaluator/test_definitions",
                 custom_dir: str = "custom_tests",
                 evaluators_dir: str = "evaluator/test_definitions/evaluators",
                 custom_evaluators_dir: str = "custom_evaluators",
                 tools_dir: str = "tools"):
        # Use absolute paths based on this file's directory (evaluator/)
        base_dir = Path(__file__).parent.parent
        self.tests_dir = base_dir / tests_dir
        self.custom_dir = base_dir / custom_dir
        self.evaluators_dir = base_dir / evaluators_dir
        self.custom_evaluators_dir = base_dir / custom_evaluators_dir

        # Tools directories
        self.tools_dir = base_dir / tools_dir
        self.custom_tools_dir = base_dir / custom_dir / "tools"

        # Cache
        self._domains_cache: Dict[str, DomainDefinition] = {}
        self._levels_cache: Dict[str, LevelDefinition] = {}
        self._tests_cache: Dict[str, List[TestDefinition]] = {}
        self._evaluators_cache: Dict[str, EvaluatorDefinition] = {}
        self._tools_cache: Dict[str, ToolDefinition] = {}
    
    def scan_domains(self) -> List[DomainDefinition]:
        """Scan all domain directories and return list of domains"""
        domains = []
        
        # Define preferred domain order
        domain_order = ["conversation", "math", "tool_calling", "reasoning", "health"]
        
        # Scan default tests directory
        if self.tests_dir.exists():
            for domain_path in self.tests_dir.iterdir():
                if domain_path.is_dir() and domain_path.name != 'evaluators':
                    domain = self._load_domain(domain_path)
                    if domain:
                        domains.append(domain)
                        self._domains_cache[domain.id] = domain
        
        # Scan custom tests directory
        if self.custom_dir.exists():
            for domain_path in self.custom_dir.iterdir():
                if domain_path.is_dir():
                    domain = self._load_domain(domain_path)
                    if domain:
                        # Don't override default domains
                        if domain.id not in self._domains_cache:
                            domains.append(domain)
                            self._domains_cache[domain.id] = domain
        
        # Sort domains: known domains first (in order), then custom domains alphabetically
        def sort_key(d):
            if d.id in domain_order:
                return (0, domain_order.index(d.id))
            return (1, d.name.lower())
        
        domains.sort(key=sort_key)
        return domains
    
    def _load_domain(self, domain_path: Path) -> Optional[DomainDefinition]:
        """Load domain from directory"""
        domain_file = domain_path / "domain.json"
        if not domain_file.exists():
            return None
        
        try:
            with open(domain_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return DomainDefinition.from_dict(data, str(domain_path))
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Error loading domain {domain_path}: {e}")
            return None
    
    def load_domain(self, domain_id: str) -> Optional[DomainDefinition]:
        """Load a single domain by ID"""
        if domain_id in self._domains_cache:
            return self._domains_cache[domain_id]
        
        # Try to find domain
        for base_dir in [self.tests_dir, self.custom_dir]:
            domain_path = base_dir / domain_id
            if domain_path.exists():
                domain = self._load_domain(domain_path)
                if domain:
                    self._domains_cache[domain_id] = domain
                    return domain
        
        return None
    
    def load_level(self, domain_id: str, level: int) -> Optional[LevelDefinition]:
        """Load level definition from level.json if it exists"""
        cache_key = f"{domain_id}:{level}"
        if cache_key in self._levels_cache:
            return self._levels_cache[cache_key]

        for base_dir in [self.tests_dir, self.custom_dir]:
            level_path = base_dir / domain_id / f"level_{level}" / "level.json"
            if level_path.exists():
                try:
                    with open(level_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    level_def = LevelDefinition(
                        domain_id=domain_id,
                        level=level,
                        system_prompt=data.get('system_prompt'),
                        system_prompt_mode=data.get('system_prompt_mode', 'overwrite'),
                        path=str(level_path),
                        tool_ids=data.get('tool_ids')
                    )
                    self._levels_cache[cache_key] = level_def
                    return level_def
                except (json.JSONDecodeError, IOError):
                    pass

        return None

    def load_tests_by_level(self, domain_id: str, level: int) -> List[TestDefinition]:
        """Load all tests for a specific domain and level"""
        cache_key = f"{domain_id}:{level}"
        if cache_key in self._tests_cache:
            return self._tests_cache[cache_key]
        
        tests = []
        
        # Find domain path
        domain_path = None
        for base_dir in [self.tests_dir, self.custom_dir]:
            potential_path = base_dir / domain_id
            if potential_path.exists():
                domain_path = potential_path
                break
        
        if not domain_path:
            return tests
        
        # Load tests from level directory
        level_path = domain_path / f"level_{level}"
        if level_path.exists():
            for test_file in level_path.glob("*.json"):
                if test_file.name == "level.json":
                    continue
                test = self._load_test(test_file, domain_id, level)
                if test:
                    tests.append(test)
        
        # Sort by name
        tests.sort(key=lambda t: t.name)
        self._tests_cache[cache_key] = tests
        
        return tests
    
    def load_all_tests(self, domain_id: str = None) -> List[TestDefinition]:
        """Load all tests, optionally filtered by domain"""
        all_tests = []
        
        domains = [domain_id] if domain_id else [d.id for d in self.scan_domains()]
        
        for domain in domains:
            for level in range(1, 6):  # Levels 1-5
                tests = self.load_tests_by_level(domain, level)
                all_tests.extend(tests)
        
        return all_tests
    
    def _load_test(self, test_file: Path, domain_id: str, level: int) -> Optional[TestDefinition]:
        """Load a single test from JSON file"""
        try:
            with open(test_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return TestDefinition.from_dict(data, domain_id, level, str(test_file))
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Error loading test {test_file}: {e}")
            return None
    
    def get_test(self, test_id: str) -> Optional[TestDefinition]:
        """Get a single test by ID"""
        # Search in all domains and levels
        for domain in self.scan_domains():
            for level in range(1, 6):
                tests = self.load_tests_by_level(domain.id, level)
                for test in tests:
                    if test.id == test_id:
                        return test
        return None
    
    def load_evaluators(self) -> List[EvaluatorDefinition]:
        """Load all evaluators from default and custom directories"""
        evaluators = []
        
        # Load from default evaluators directory
        if self.evaluators_dir.exists():
            for eval_file in self.evaluators_dir.glob("*.json"):
                evaluator = self._load_evaluator(eval_file)
                if evaluator:
                    evaluators.append(evaluator)
                    self._evaluators_cache[evaluator.id] = evaluator
        
        # Load from custom evaluators directory
        if self.custom_evaluators_dir.exists():
            for eval_file in self.custom_evaluators_dir.glob("*.json"):
                evaluator = self._load_evaluator(eval_file)
                if evaluator:
                    evaluators.append(evaluator)
                    self._evaluators_cache[evaluator.id] = evaluator
        
        return evaluators
    
    def _load_evaluator(self, eval_file: Path) -> Optional[EvaluatorDefinition]:
        """Load a single evaluator from JSON file"""
        try:
            with open(eval_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return EvaluatorDefinition.from_dict(data, str(eval_file))
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Error loading evaluator {eval_file}: {e}")
            return None
    
    def get_evaluator(self, evaluator_id: str) -> Optional[EvaluatorDefinition]:
        """Get a single evaluator by ID"""
        if evaluator_id in self._evaluators_cache:
            return self._evaluators_cache[evaluator_id]
        
        # Load evaluators if not cached
        self.load_evaluators()
        return self._evaluators_cache.get(evaluator_id)
    
    def validate_test(self, test: TestDefinition) -> List[str]:
        """Validate a test definition, return list of errors"""
        errors = []
        
        if not test.id:
            errors.append("Test ID is required")
        if not test.name:
            errors.append("Test name is required")
        if not test.prompt:
            errors.append("Test prompt is required")
        if not test.evaluator_id:
            errors.append("Evaluator ID is required")
        if test.level < 1 or test.level > 5:
            errors.append("Level must be between 1 and 5")
        
        # Check if evaluator exists
        evaluator = self.get_evaluator(test.evaluator_id)
        if not evaluator:
            errors.append(f"Evaluator '{test.evaluator_id}' not found")
        
        return errors
    
    def validate_domain(self, domain: DomainDefinition) -> List[str]:
        """Validate a domain definition, return list of errors"""
        errors = []
        
        if not domain.id:
            errors.append("Domain ID is required")
        if not domain.name:
            errors.append("Domain name is required")
        
        # Check for valid characters in ID
        import re
        if domain.id and not re.match(r'^[a-z0-9_]+$', domain.id):
            errors.append("Domain ID must contain only lowercase letters, numbers, and underscores")
        
        return errors
    
    def clear_cache(self):
        """Clear all caches"""
        self._domains_cache.clear()
        self._levels_cache.clear()
        self._tests_cache.clear()
        self._evaluators_cache.clear()
        self._tools_cache.clear()
    
    # ==================== Tool Operations ====================

    def scan_tools(self) -> List[ToolDefinition]:
        """Scan all tool JSON files and return list of tools"""
        for tools_dir in [self.tools_dir, self.custom_tools_dir]:
            if tools_dir.exists():
                for tool_file in tools_dir.glob("*.json"):
                    tool = self._load_tool(tool_file)
                    if tool and tool.id not in self._tools_cache:
                        self._tools_cache[tool.id] = tool

        tools = list(self._tools_cache.values())
        tools.sort(key=lambda t: t.name)
        return tools

    def _load_tool(self, tool_file: Path) -> Optional[ToolDefinition]:
        """Load a single tool from JSON file"""
        try:
            with open(tool_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return ToolDefinition.from_dict(data, str(tool_file))
        except (json.JSONDecodeError, KeyError) as e:
            print(f"Error loading tool {tool_file}: {e}")
            return None

    def get_tool(self, tool_id: str) -> Optional[ToolDefinition]:
        """Get a single tool by ID"""
        if tool_id in self._tools_cache:
            return self._tools_cache[tool_id]

        # Load all tools if not cached
        self.scan_tools()
        return self._tools_cache.get(tool_id)

    def resolve_tools(self, test: TestDefinition, domain: DomainDefinition = None,
                      level_def: LevelDefinition = None) -> List[Dict[str, Any]]:
        """
        Resolve tools using 3-layer hierarchy (always append, deduplicate by function name).
        Domain tool_ids + Level tool_ids + Test tool_ids

        Returns list of dicts: [{"type": "function", "function": {...}, "mock_response": ..., "mock_response_type": ...}, ...]
        """
        if domain is None:
            domain = self.load_domain(test.domain_id)
        if level_def is None:
            level_def = self.load_level(test.domain_id, test.level)

        # Collect tool_ids in order: domain -> level -> test
        all_tool_ids = []
        if domain and domain.tool_ids:
            all_tool_ids.extend(domain.tool_ids)
        if level_def and level_def.tool_ids:
            all_tool_ids.extend(level_def.tool_ids)
        if test.tool_ids:
            all_tool_ids.extend(test.tool_ids)

        if not all_tool_ids:
            return []

        # Load tools and deduplicate by function name (last wins)
        tools_by_func_name = {}
        for tool_id in all_tool_ids:
            if tool_id.startswith('skill:'):
                # Resolve skill tool from skills_manager
                from backend.skills_manager import skills_manager
                for skill_def in skills_manager.get_all_skill_tool_defs():
                    if skill_def.get('id') == tool_id:
                        func = skill_def.get('function', {})
                        func_name = func.get('name', '')
                        if func_name:
                            tools_by_func_name[func_name] = {
                                "type": "function",
                                "function": func,
                                "mock_response": skill_def.get('mock_response'),
                                "mock_response_type": skill_def.get('mock_response_type', 'json'),
                                "no_mock": skill_def.get('no_mock', False)
                            }
                        break
            elif tool_id.startswith('plugin:'):
                # Resolve plugin tool from plugin_manager
                from backend.plugin_manager import plugin_manager
                for plugin_def in plugin_manager.get_all_plugin_tool_defs():
                    if plugin_def.get('id') == tool_id:
                        func = plugin_def.get('function', {})
                        func_name = func.get('name', '')
                        if func_name:
                            tools_by_func_name[func_name] = {
                                "type": "function",
                                "function": func,
                                "mock_response": plugin_def.get('mock_response'),
                                "mock_response_type": plugin_def.get('mock_response_type', 'json'),
                                "no_mock": plugin_def.get('no_mock', False)
                            }
                        break
            else:
                tool_def = self.get_tool(tool_id)
                if tool_def and tool_def.function:
                    func_name = tool_def.function.get('name', tool_def.id)
                    tools_by_func_name[func_name] = {
                        "type": "function",
                        "function": tool_def.function,
                        "mock_response": tool_def.mock_response,
                        "mock_response_type": tool_def.mock_response_type,
                        "no_mock": tool_def.no_mock
                    }

        return list(tools_by_func_name.values())

    def resolve_system_prompt(self, test: TestDefinition, domain: DomainDefinition = None, level_def: LevelDefinition = None) -> Optional[str]:
        """
        Resolve system prompt using 3-layer hierarchy:
        Domain-level → Level-level → Test-level with mode (overwrite/append)

        Stage 1: Domain + Level → base_prompt
        Stage 2: base_prompt + Test → final prompt

        Args:
            test: Test definition
            domain: Optional domain definition (will load if not provided)
            level_def: Optional level definition (will load if not provided)

        Returns:
            Resolved system prompt or None
        """
        # Load domain if not provided
        if domain is None:
            domain = self.load_domain(test.domain_id)

        # Load level if not provided
        if level_def is None:
            level_def = self.load_level(test.domain_id, test.level)

        # Stage 1: Domain → Level → base_prompt
        domain_prompt = domain.system_prompt if domain else None
        base_prompt = domain_prompt

        if level_def and level_def.system_prompt:
            level_mode = level_def.system_prompt_mode or 'overwrite'
            if level_mode == 'append' and base_prompt:
                base_prompt = f"{base_prompt}\n\n{level_def.system_prompt}"
            else:
                # overwrite, or no domain prompt to append to
                base_prompt = level_def.system_prompt

        # Stage 2: base_prompt + Test → final prompt
        test_prompt = test.system_prompt

        if not base_prompt and not test_prompt:
            return None

        if base_prompt and not test_prompt:
            return base_prompt

        if test_prompt and not base_prompt:
            return test_prompt

        # Both exist - apply test mode
        test_mode = test.system_prompt_mode or 'overwrite'

        if test_mode == 'append':
            return f"{base_prompt}\n\n{test_prompt}"
        else:
            return test_prompt


# Global loader instance
test_loader = TestLoader()