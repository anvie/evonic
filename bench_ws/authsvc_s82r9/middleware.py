import token_utils
import rate_limiter
from functools import wraps
from flask import request, jsonify

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        ip_address = request.remote_addr
        if rate_limiter.is_rate_limited(ip_address):
            return jsonify({"error": "Too Many Requests"}), 429

        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401

        token = auth_header.split(" ")[1]
        payload = token_utils.validate_token(token)

        if not payload:
            return jsonify({"error": "Invalid or expired token"}), 401

        # Optionally, you could store the payload in request.user
        # request.user = payload.get('sub')

        return f(*args, **kwargs)
    return decorated
