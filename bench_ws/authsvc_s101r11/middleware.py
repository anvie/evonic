from flask import request, jsonify
from token_utils import validate_jwt_token
from rate_limiter import is_rate_limited

def auth_middleware():
    """
    Middleware to validate JWT tokens on each request.
    """
    ip_address = request.remote_addr
    if is_rate_limited(ip_address):
        return jsonify({"message": "Too many requests"}), 429

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return jsonify({"message": "Missing or invalid Authorization header"}), 401
    
    token = auth_header.split(" ")[1]
    if not validate_jwt_token(token):
        return jsonify({"message": "Invalid token"}), 401
    
    return None
