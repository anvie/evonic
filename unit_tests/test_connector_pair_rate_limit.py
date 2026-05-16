"""Tests for Evonet connector pairing rate limiter."""

import unittest

from backend import connector_pair_rate_limit as rate_limit


class TestConnectorPairRateLimit(unittest.TestCase):
    def setUp(self):
        rate_limit.reset_for_tests()

    def test_allows_attempts_under_limit(self):
        ip = '10.0.0.1'
        limit = rate_limit._max_attempts()
        for _ in range(limit - 1):
            rate_limit.record_attempt(ip)
            self.assertFalse(rate_limit.is_rate_limited(ip))

    def test_blocks_at_limit(self):
        ip = '10.0.0.2'
        limit = rate_limit._max_attempts()
        for _ in range(limit):
            rate_limit.record_attempt(ip)
        self.assertTrue(rate_limit.is_rate_limited(ip))

    def test_limits_are_per_ip(self):
        limit = rate_limit._max_attempts()
        for _ in range(limit):
            rate_limit.record_attempt('10.0.0.3')
        self.assertTrue(rate_limit.is_rate_limited('10.0.0.3'))
        self.assertFalse(rate_limit.is_rate_limited('10.0.0.4'))


if __name__ == '__main__':
    unittest.main()
