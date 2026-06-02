"""
Unit tests for score_aggregator module
"""

import pytest
from evaluator.score_aggregator import (
    ScoreAggregator, TestResult, LevelScore, DomainScore,
    calculate_level_score, aggregate_all_results
)


class TestTestResult:
    """Tests for TestResult dataclass"""
    
    def test_create_test_result(self):
        """Test creating a test result"""
        result = TestResult(
            test_id='test1',
            domain='math',
            level=1,
            score=0.85,
            status='passed',
            weight=1.0,
            details={'answer': 42}
        )
        
        assert result.test_id == 'test1'
        assert result.domain == 'math'
        assert result.level == 1
        assert result.score == 0.85
        assert result.status == 'passed'
    
    def test_create_failed_result(self):
        """Test creating a failed result"""
        result = TestResult(
            test_id='test2',
            domain='conversation',
            level=2,
            score=0.3,
            status='failed',
            weight=1.0,
            details={'error': 'Wrong answer'}
        )
        
        assert result.status == 'failed'
        assert result.score < 0.5


class TestLevelScore:
    """Tests for LevelScore dataclass"""
    
    def test_create_level_score(self):
        """Test creating a level score"""
        score = LevelScore(
            domain='math',
            level=1,
            average_score=0.85,
            total_tests=3,
            passed_tests=2
        )
        
        assert score.domain == 'math'
        assert score.level == 1
        assert score.average_score == 0.85
        assert score.total_tests == 3
        assert score.passed_tests == 2
    
    def test_to_dict(self):
        """Test converting to dictionary"""
        score = LevelScore(
            domain='math',
            level=1,
            average_score=0.85,
            total_tests=3,
            passed_tests=2
        )
        
        d = score.to_dict()
        
        assert d['domain'] == 'math'
        assert d['average_score'] == 0.85
        assert d['total_tests'] == 3


class TestDomainScore:
    """Tests for DomainScore dataclass"""
    
    def test_create_domain_score(self):
        """Test creating a domain score"""
        level_scores = {
            1: LevelScore('math', 1, 0.9, 2, 2),
            2: LevelScore('math', 2, 0.8, 2, 1)
        }
        
        score = DomainScore(
            domain='math',
            average_score=0.85,
            total_tests=4,
            passed_tests=3,
            levels=level_scores
        )
        
        assert score.domain == 'math'
        assert score.average_score == 0.85
        assert score.total_tests == 4
        assert len(score.levels) == 2
    
    def test_to_dict(self):
        """Test converting to dictionary"""
        level_scores = {
            1: LevelScore('math', 1, 0.9, 2, 2)
        }
        
        score = DomainScore(
            domain='math',
            average_score=0.9,
            total_tests=2,
            passed_tests=2,
            levels=level_scores
        )
        
        d = score.to_dict()
        
        assert d['domain'] == 'math'
        assert d['average_score'] == 0.9
        assert '1' in d['levels']


class TestScoreAggregator:
    """Tests for ScoreAggregator class"""
    
    def test_calculate_level_score_single(self):
        """Test calculating level score with single test"""
        results = [
            TestResult('test1', 'math', 1, 0.9, 'passed', 1.0)
        ]
        
        score = ScoreAggregator.calculate_level_score(results)
        
        assert score.domain == 'math'
        assert score.level == 1
        assert score.average_score == 0.9
        assert score.total_tests == 1
        assert score.passed_tests == 1
    
    def test_calculate_level_score_multiple(self):
        """Test calculating level score with multiple tests"""
        results = [
            TestResult('test1', 'math', 1, 0.9, 'passed', 1.0),
            TestResult('test2', 'math', 1, 0.7, 'passed', 1.0),
            TestResult('test3', 'math', 1, 0.5, 'failed', 1.0)
        ]
        
        score = ScoreAggregator.calculate_level_score(results)
        
        # Average: (0.9 + 0.7 + 0.5) / 3 = 0.7
        assert abs(score.average_score - 0.7) < 0.001
        assert score.total_tests == 3
        assert score.passed_tests == 2  # 0.9 and 0.7 >= 0.7 are passed
    
    def test_calculate_level_score_weighted(self):
        """Test calculating weighted average"""
        results = [
            TestResult('test1', 'math', 1, 1.0, 'passed', 2.0),  # weight=2
            TestResult('test2', 'math', 1, 0.5, 'failed', 1.0)   # weight=1
        ]
        
        score = ScoreAggregator.calculate_level_score(results)
        
        # Weighted: (1.0 * 2.0 + 0.5 * 1.0) / 3.0 = 0.833
        assert abs(score.average_score - 0.833) < 0.01
    
    def test_calculate_level_score_empty(self):
        """Test calculating level score with no tests"""
        results = []
        
        score = ScoreAggregator.calculate_level_score(results)
        
        assert score.average_score == 0.0
        assert score.total_tests == 0
        assert score.passed_tests == 0
    
    def test_calculate_domain_score(self):
        """Test calculating domain score"""
        level_scores = [
            LevelScore('math', 1, 0.9, 2, 2),
            LevelScore('math', 2, 0.8, 2, 1),
            LevelScore('math', 3, 0.7, 2, 1)
        ]
        
        score = ScoreAggregator.calculate_domain_score(level_scores)
        
        # Average: (0.9 + 0.8 + 0.7) / 3 = 0.8
        assert abs(score.average_score - 0.8) < 0.001
        assert score.total_tests == 6
        assert score.passed_tests == 4
    
    def test_calculate_domain_score_empty(self):
        """Test calculating domain score with no levels"""
        level_scores = []
        
        score = ScoreAggregator.calculate_domain_score(level_scores)
        
        assert score.average_score == 0.0
        assert score.total_tests == 0
    
    def test_calculate_overall_score(self):
        """Test calculating overall score"""
        domain_scores = [
            DomainScore('math', 0.85, 10, 8, {1: LevelScore('math', 1, 0.9, 2, 2)}),
            DomainScore('conversation', 0.75, 10, 6, {1: LevelScore('conversation', 1, 0.7, 2, 1)})
        ]
        
        overall = ScoreAggregator.calculate_overall_score(domain_scores)
        
        # Average: (0.85 + 0.75) / 2 = 0.8
        assert abs(overall['overall_score'] - 0.8) < 0.001
        assert overall['total_tests'] == 20
        assert overall['passed_tests'] == 14
        assert 'math' in overall['domains']
        assert 'conversation' in overall['domains']
    
    def test_aggregate_results(self):
        """Test aggregating all results"""
        test_results = [
            TestResult('test1', 'math', 1, 0.9, 'passed', 1.0),
            TestResult('test2', 'math', 1, 0.7, 'passed', 1.0),
            TestResult('test3', 'math', 2, 0.8, 'passed', 1.0),
            TestResult('test4', 'conversation', 1, 0.6, 'failed', 1.0)
        ]
        
        aggregation = ScoreAggregator.aggregate_results(test_results)
        
        assert 'overall' in aggregation
        assert 'domains' in aggregation
        assert 'levels' in aggregation
        assert 'math' in aggregation['domains']
        assert 'conversation' in aggregation['domains']
    
    def test_format_score_report(self):
        """Test formatting score report"""
        domain_scores = [
            DomainScore('math', 0.85, 3, 2, {
                1: LevelScore('math', 1, 0.9, 2, 2),
                2: LevelScore('math', 2, 0.8, 1, 0)
            })
        ]
        
        overall = ScoreAggregator.calculate_overall_score(domain_scores)
        # Add domains to the aggregation for the report
        aggregation = {
            'overall': overall,
            'domains': overall['domains'],
            'levels': {}
        }
        report = ScoreAggregator.format_score_report(aggregation)
        
        assert 'Overall Score: 85.00%' in report
        # Check that report contains domain info
        assert 'DOMAIN BREAKDOWN' in report


class TestConvenienceFunctions:
    """Tests for convenience functions"""
    
    def test_calculate_level_score_func(self):
        """Test convenience function for level score"""
        results = [
            {'test_id': 'test1', 'domain': 'math', 'level': 1, 'score': 0.9, 'status': 'passed', 'weight': 1.0},
            {'test_id': 'test2', 'domain': 'math', 'level': 1, 'score': 0.7, 'status': 'passed', 'weight': 1.0}
        ]
        
        score = calculate_level_score(results)
        
        assert 'average_score' in score
        assert 'total_tests' in score
        assert score['total_tests'] == 2
    
    def test_aggregate_all_results_func(self):
        """Test convenience function for aggregation"""
        results = [
            {'test_id': 'test1', 'domain': 'math', 'level': 1, 'score': 0.9, 'status': 'passed', 'weight': 1.0},
            {'test_id': 'test2', 'domain': 'math', 'level': 2, 'score': 0.8, 'status': 'passed', 'weight': 1.0}
        ]
        
        aggregation = aggregate_all_results(results)
        
        assert 'overall' in aggregation
        assert 'domains' in aggregation
        assert 'levels' in aggregation
        assert aggregation['overall']['total_tests'] == 2


class TestEdgeCases:
    """Tests for edge cases"""
    
    def test_all_zero_scores(self):
        """Test with all zero scores"""
        results = [
            TestResult('test1', 'math', 1, 0.0, 'failed', 1.0),
            TestResult('test2', 'math', 1, 0.0, 'failed', 1.0)
        ]
        
        score = ScoreAggregator.calculate_level_score(results)
        
        assert score.average_score == 0.0
        assert score.passed_tests == 0
    
    def test_all_perfect_scores(self):
        """Test with all perfect scores"""
        results = [
            TestResult('test1', 'math', 1, 1.0, 'passed', 1.0),
            TestResult('test2', 'math', 1, 1.0, 'passed', 1.0)
        ]
        
        score = ScoreAggregator.calculate_level_score(results)
        
        assert score.average_score == 1.0
        assert score.passed_tests == 2
    
    def test_mixed_weights(self):
        """Test with mixed weights including zero"""
        results = [
            TestResult('test1', 'math', 1, 0.8, 'passed', 2.0),
            TestResult('test2', 'math', 1, 0.6, 'failed', 0.5),
            TestResult('test3', 'math', 1, 0.0, 'failed', 1.0)
        ]
        
        score = ScoreAggregator.calculate_level_score(results)
        
        # Weighted: (0.8 * 2.0 + 0.6 * 0.5 + 0.0 * 1.0) / (2.0 + 0.5 + 1.0) = 1.9 / 3.5 = 0.543
        assert 0.5 < score.average_score < 0.6
    
    def test_single_domain(self):
        """Test aggregation with single domain"""
        test_results = [
            TestResult('test1', 'math', 1, 0.9, 'passed', 1.0)
        ]
        
        aggregation = ScoreAggregator.aggregate_results(test_results)
        
        assert len(aggregation['domains']) == 1
        assert 'math' in aggregation['domains']
        assert aggregation['overall']['total_tests'] == 1
    
    def test_single_level(self):
        """Test aggregation with single level"""
        test_results = [
            TestResult('test1', 'math', 1, 0.9, 'passed', 1.0),
            TestResult('test2', 'math', 1, 0.8, 'passed', 1.0)
        ]
        
        aggregation = ScoreAggregator.aggregate_results(test_results)
        
        assert 'math_1' in aggregation['levels']
        assert len(aggregation['domains']['math']['levels']) == 1