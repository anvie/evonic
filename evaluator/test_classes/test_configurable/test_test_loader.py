"""
Unit tests for test_loader module
"""

import pytest
import json
import tempfile
import os
from pathlib import Path

from evaluator.test_loader import (
    TestLoader, TestDefinition, DomainDefinition, EvaluatorDefinition
)


@pytest.fixture
def temp_test_dirs():
    """Create temporary test directories"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tests_dir = Path(tmpdir) / "tests"
        custom_dir = Path(tmpdir) / "custom_tests"
        evaluators_dir = Path(tmpdir) / "evaluators"
        
        # Create directory structure
        (tests_dir / "math" / "level_1").mkdir(parents=True)
        (tests_dir / "conversation" / "level_1").mkdir(parents=True)
        evaluators_dir.mkdir(parents=True)
        custom_dir.mkdir(parents=True)
        
        # Create domain.json files
        math_domain = {
            "id": "math",
            "name": "Mathematics",
            "description": "Math test domain",
            "icon": "calculator",
            "color": "#10B981",
            "evaluator_id": "two_pass",
            "enabled": True
        }
        with open(tests_dir / "math" / "domain.json", 'w') as f:
            json.dump(math_domain, f)
        
        conv_domain = {
            "id": "conversation",
            "name": "Conversation",
            "description": "Conversation test domain",
            "icon": "chat",
            "color": "#3B82F6",
            "evaluator_id": "keyword",
            "enabled": True
        }
        with open(tests_dir / "conversation" / "domain.json", 'w') as f:
            json.dump(conv_domain, f)
        
        # Create test files
        math_test = {
            "id": "math_add_1",
            "name": "Simple Addition",
            "description": "Test basic addition",
            "prompt": "What is 2 + 2?",
            "expected": {"answer": 4},
            "evaluator_id": "two_pass",
            "timeout_ms": 30000,
            "weight": 1.0,
            "enabled": True
        }
        with open(tests_dir / "math" / "level_1" / "addition.json", 'w') as f:
            json.dump(math_test, f)
        
        conv_test = {
            "id": "conv_greeting_1",
            "name": "Basic Greeting",
            "description": "Test greeting response",
            "prompt": "Hello, how are you?",
            "expected": {"keywords": ["hello", "good"]},
            "evaluator_id": "keyword",
            "timeout_ms": 30000,
            "weight": 1.0,
            "enabled": True
        }
        with open(tests_dir / "conversation" / "level_1" / "greeting.json", 'w') as f:
            json.dump(conv_test, f)
        
        # Create evaluator file
        evaluator = {
            "id": "two_pass",
            "name": "Two Pass Evaluator",
            "type": "predefined",
            "description": "Extracts answer then evaluates",
            "uses_pass2": True
        }
        with open(evaluators_dir / "two_pass.json", 'w') as f:
            json.dump(evaluator, f)
        
        yield {
            'tests_dir': str(tests_dir),
            'custom_dir': str(custom_dir),
            'evaluators_dir': str(evaluators_dir),
            'tmpdir': tmpdir
        }


class TestTestLoader:
    """Tests for TestLoader class"""
    
    def test_scan_domains(self, temp_test_dirs):
        """Test scanning domain directories"""
        loader = TestLoader(
            tests_dir=temp_test_dirs['tests_dir'],
            custom_dir=temp_test_dirs['custom_dir'],
            evaluators_dir=temp_test_dirs['evaluators_dir']
        )
        
        domains = loader.scan_domains()
        
        assert len(domains) == 2
        domain_ids = [d.id for d in domains]
        assert 'math' in domain_ids
        assert 'conversation' in domain_ids
    
    def test_load_domain(self, temp_test_dirs):
        """Test loading a single domain"""
        loader = TestLoader(
            tests_dir=temp_test_dirs['tests_dir'],
            custom_dir=temp_test_dirs['custom_dir'],
            evaluators_dir=temp_test_dirs['evaluators_dir']
        )
        
        domain = loader.load_domain('math')
        
        assert domain is not None
        assert domain.id == 'math'
        assert domain.name == 'Mathematics'
        assert domain.evaluator_id == 'two_pass'
    
    def test_load_nonexistent_domain(self, temp_test_dirs):
        """Test loading a domain that doesn't exist"""
        loader = TestLoader(
            tests_dir=temp_test_dirs['tests_dir'],
            custom_dir=temp_test_dirs['custom_dir'],
            evaluators_dir=temp_test_dirs['evaluators_dir']
        )
        
        domain = loader.load_domain('nonexistent')
        
        assert domain is None
    
    def test_load_tests_by_level(self, temp_test_dirs):
        """Test loading tests for a specific level"""
        loader = TestLoader(
            tests_dir=temp_test_dirs['tests_dir'],
            custom_dir=temp_test_dirs['custom_dir'],
            evaluators_dir=temp_test_dirs['evaluators_dir']
        )
        
        tests = loader.load_tests_by_level('math', 1)
        
        assert len(tests) == 1
        assert tests[0].id == 'math_add_1'
        assert tests[0].name == 'Simple Addition'
    
    def test_load_tests_empty_level(self, temp_test_dirs):
        """Test loading tests for a level with no tests"""
        loader = TestLoader(
            tests_dir=temp_test_dirs['tests_dir'],
            custom_dir=temp_test_dirs['custom_dir'],
            evaluators_dir=temp_test_dirs['evaluators_dir']
        )
        
        tests = loader.load_tests_by_level('math', 5)
        
        assert len(tests) == 0
    
    def test_load_all_tests(self, temp_test_dirs):
        """Test loading all tests"""
        loader = TestLoader(
            tests_dir=temp_test_dirs['tests_dir'],
            custom_dir=temp_test_dirs['custom_dir'],
            evaluators_dir=temp_test_dirs['evaluators_dir']
        )
        
        tests = loader.load_all_tests()
        
        assert len(tests) == 2
    
    def test_get_test(self, temp_test_dirs):
        """Test getting a single test by ID"""
        loader = TestLoader(
            tests_dir=temp_test_dirs['tests_dir'],
            custom_dir=temp_test_dirs['custom_dir'],
            evaluators_dir=temp_test_dirs['evaluators_dir']
        )
        
        test = loader.get_test('math_add_1')
        
        assert test is not None
        assert test.id == 'math_add_1'
        assert test.prompt == 'What is 2 + 2?'
    
    def test_load_evaluators(self, temp_test_dirs):
        """Test loading evaluators"""
        loader = TestLoader(
            tests_dir=temp_test_dirs['tests_dir'],
            custom_dir=temp_test_dirs['custom_dir'],
            evaluators_dir=temp_test_dirs['evaluators_dir']
        )
        
        evaluators = loader.load_evaluators()
        
        assert len(evaluators) == 1
        assert evaluators[0].id == 'two_pass'
    
    def test_validate_test(self, temp_test_dirs):
        """Test test validation"""
        loader = TestLoader(
            tests_dir=temp_test_dirs['tests_dir'],
            custom_dir=temp_test_dirs['custom_dir'],
            evaluators_dir=temp_test_dirs['evaluators_dir']
        )
        
        # Valid test
        valid_test = TestDefinition(
            id='test1',
            name='Test',
            description='A test',
            prompt='Question?',
            expected={},
            evaluator_id='two_pass',
            domain_id='math',
            level=1
        )
        errors = loader.validate_test(valid_test)
        assert len(errors) == 0
        
        # Invalid test - missing evaluator
        invalid_test = TestDefinition(
            id='test2',
            name='Test',
            description='A test',
            prompt='Question?',
            expected={},
            evaluator_id='nonexistent',
            domain_id='math',
            level=1
        )
        errors = loader.validate_test(invalid_test)
        assert len(errors) > 0
    
    def test_validate_domain(self, temp_test_dirs):
        """Test domain validation"""
        loader = TestLoader(
            tests_dir=temp_test_dirs['tests_dir'],
            custom_dir=temp_test_dirs['custom_dir'],
            evaluators_dir=temp_test_dirs['evaluators_dir']
        )
        
        # Valid domain
        valid_domain = DomainDefinition(
            id='test_domain',
            name='Test Domain',
            description='A test domain'
        )
        errors = loader.validate_domain(valid_domain)
        assert len(errors) == 0
        
        # Invalid domain - invalid ID characters
        invalid_domain = DomainDefinition(
            id='Test Domain!',
            name='Test',
            description='Invalid'
        )
        errors = loader.validate_domain(invalid_domain)
        assert len(errors) > 0
    
    def test_clear_cache(self, temp_test_dirs):
        """Test clearing cache"""
        loader = TestLoader(
            tests_dir=temp_test_dirs['tests_dir'],
            custom_dir=temp_test_dirs['custom_dir'],
            evaluators_dir=temp_test_dirs['evaluators_dir']
        )
        
        # Load something to cache
        loader.scan_domains()
        loader.load_tests_by_level('math', 1)
        
        # Clear cache
        loader.clear_cache()
        
        assert len(loader._domains_cache) == 0
        assert len(loader._tests_cache) == 0


class TestDomainDefinition:
    """Tests for DomainDefinition dataclass"""
    
    def test_to_dict(self):
        """Test converting domain to dictionary"""
        domain = DomainDefinition(
            id='test',
            name='Test Domain',
            description='A test domain',
            icon='calculator',
            color='#FF0000',
            evaluator_id='two_pass',
            enabled=True,
            path='/path/to/domain'
        )
        
        d = domain.to_dict()
        
        assert d['id'] == 'test'
        assert d['name'] == 'Test Domain'
        assert d['color'] == '#FF0000'
    
    def test_from_dict(self):
        """Test creating domain from dictionary"""
        data = {
            'id': 'math',
            'name': 'Mathematics',
            'description': 'Math tests',
            'icon': 'calculator',
            'color': '#10B981',
            'evaluator_id': 'two_pass',
            'enabled': True
        }
        
        domain = DomainDefinition.from_dict(data, '/path/to/math')
        
        assert domain.id == 'math'
        assert domain.name == 'Mathematics'
        assert domain.path == '/path/to/math'


class TestTestDefinition:
    """Tests for TestDefinition dataclass"""
    
    def test_to_dict(self):
        """Test converting test to dictionary"""
        test = TestDefinition(
            id='test1',
            name='Test',
            description='A test',
            prompt='Question?',
            expected={'answer': 42},
            evaluator_id='two_pass',
            domain_id='math',
            level=1,
            timeout_ms=60000,
            weight=2.0
        )
        
        d = test.to_dict()
        
        assert d['id'] == 'test1'
        assert d['weight'] == 2.0
        assert d['timeout_ms'] == 60000
    
    def test_from_dict(self):
        """Test creating test from dictionary"""
        data = {
            'id': 'test1',
            'name': 'Test',
            'description': 'A test',
            'prompt': 'Question?',
            'expected': {'answer': 42},
            'evaluator_id': 'two_pass',
            'timeout_ms': 30000,
            'weight': 1.0,
            'enabled': True
        }
        
        test = TestDefinition.from_dict(data, 'math', 1, '/path/to/test.json')
        
        assert test.id == 'test1'
        assert test.domain_id == 'math'
        assert test.level == 1


class TestEvaluatorDefinition:
    """Tests for EvaluatorDefinition dataclass"""
    
    def test_to_dict(self):
        """Test converting evaluator to dictionary"""
        evaluator = EvaluatorDefinition(
            id='custom_eval',
            name='Custom Evaluator',
            type='custom',
            description='A custom evaluator',
            eval_prompt='Evaluate: {response}',
            extraction_regex='SCORE: (\\d+)',
            config={'param': 'value'}
        )
        
        d = evaluator.to_dict()
        
        assert d['id'] == 'custom_eval'
        assert d['type'] == 'custom'
        assert d['eval_prompt'] == 'Evaluate: {response}'
    
    def test_from_dict(self):
        """Test creating evaluator from dictionary"""
        data = {
            'id': 'two_pass',
            'name': 'Two Pass',
            'type': 'predefined',
            'description': 'Two pass evaluation',
            'uses_pass2': True,
            'config': {'tolerance': 0.01}
        }
        
        evaluator = EvaluatorDefinition.from_dict(data, '/path/to/eval.json')
        
        assert evaluator.id == 'two_pass'
        assert evaluator.type == 'predefined'
        assert evaluator.uses_pass2 == True