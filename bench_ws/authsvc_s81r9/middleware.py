from flask import request, jsonify
from token_utils import validate_token

def jwt_middleware(f):
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({"message": "Missing or invalid token"}), 401
        
        token = auth_header.split(" ")[1]
        payload = validate_token(token)
        
        if not payload:
            return jsonify({"message": "Invalid or expired token"}), 401
        
        # Optionally, you can attach the user info to the request context
        # request.user = payload["sub"]
        
        return f(*args, **kwargs)
    return wrapper
