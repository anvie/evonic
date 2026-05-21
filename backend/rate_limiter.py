"""
Rate limiter for protecting endpoints from brute-force attacks.

Implements a simple in-memory sliding window rate limiter with configurable
limits per IP address. Designed for authentication endpoints and other
security-sensitive routes.
"""

import time
from collections import defaultdict
from threading import Lock
from functools import wraps
from flask import request, render_template


class RateLimiter:
    """
    Thread-safe in-memory rate limiter using sliding window algorithm.
    
    Tracks request timestamps per identifier (typically IP address) and
    enforces configurable rate limits.
    """
    
    def __init__(self):
        # {identifier: [timestamp1, timestamp2, ...]}
        self._requests = defaultdict(list)
        self._lock = Lock()
    
    def is_allowed(self, identifier, max_requests, window_seconds):
        """
        Check if a request from the given identifier is allowed.
        
        Args:
            identifier: Unique identifier (e.g., IP address)
            max_requests: Maximum requests allowed in the window
            window_seconds: Time window in seconds
            
        Returns:
            bool: True if request is allowed, False if rate limit exceeded
        """
        now = time.time()
        cutoff = now - window_seconds
        
        with self._lock:
            # Remove timestamps outside the window
            self._requests[identifier] = [
                ts for ts in self._requests[identifier]
                if ts > cutoff
            ]
            
            # Check if limit exceeded
            if len(self._requests[identifier]) >= max_requests:
                return False
            
            # Record this request
            self._requests[identifier].append(now)
            return True
    
    def get_remaining_attempts(self, identifier, max_requests, window_seconds):
        """
        Get the number of remaining attempts for an identifier.
        
        Args:
            identifier: Unique identifier (e.g., IP address)
            max_requests: Maximum requests allowed in the window
            window_seconds: Time window in seconds
            
        Returns:
            int: Number of remaining attempts (0 if limit exceeded)
        """
        now = time.time()
        cutoff = now - window_seconds
        
        with self._lock:
            # Count recent requests
            recent = [ts for ts in self._requests[identifier] if ts > cutoff]
            remaining = max(0, max_requests - len(recent))
            return remaining
    
    def reset(self, identifier):
        """
        Reset rate limit for a specific identifier.
        
        Args:
            identifier: Unique identifier to reset
        """
        with self._lock:
            if identifier in self._requests:
                del self._requests[identifier]
    
    def clear_all(self):
        """Clear all rate limit data. Useful for testing."""
        with self._lock:
            self._requests.clear()


# Global rate limiter instance
_rate_limiter = RateLimiter()


def get_rate_limiter():
    """Get the global rate limiter instance."""
    return _rate_limiter


def rate_limit(max_requests=5, window_seconds=300, identifier_func=None):
    """
    Decorator to apply rate limiting to a Flask route.
    
    Args:
        max_requests: Maximum requests allowed in the window (default: 5)
        window_seconds: Time window in seconds (default: 300 = 5 minutes)
        identifier_func: Optional function to extract identifier from request.
                        Defaults to using request.remote_addr (IP address).
    
    Example:
        @app.route('/login', methods=['POST'])
        @rate_limit(max_requests=5, window_seconds=300)
        def login():
            # Login logic here
            pass
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            # Get identifier (default to IP address)
            if identifier_func:
                identifier = identifier_func(request)
            else:
                identifier = request.remote_addr or 'unknown'
            
            # Check rate limit
            limiter = get_rate_limiter()
            if not limiter.is_allowed(identifier, max_requests, window_seconds):
                # Rate limit exceeded
                remaining = limiter.get_remaining_attempts(
                    identifier, max_requests, window_seconds
                )
                
                # Return appropriate response based on request type
                if request.is_json:
                    from flask import jsonify
                    return jsonify({
                        'error': 'Rate limit exceeded. Please try again later.',
                        'retry_after': window_seconds
                    }), 429
                else:
                    # For HTML forms, render the same template with error
                    # This is specifically for login page
                    try:
                        import config
                        turnstile_site_key = getattr(config, 'TURNSTILE_SITE_KEY', None)
                    except Exception:
                        turnstile_site_key = None
                    
                    if turnstile_site_key:
                        return render_template(
                            'login.html',
                            turnstile_site_key=turnstile_site_key,
                            error=f'Too many login attempts. Please try again in {window_seconds // 60} minutes.'
                        ), 429
                    else:
                        return 'Rate limit exceeded. Please try again later.', 429
            
            # Request allowed, proceed
            return f(*args, **kwargs)
        
        return wrapped
    return decorator
