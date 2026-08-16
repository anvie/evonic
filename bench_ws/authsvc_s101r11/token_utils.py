import secrets
import time
import auth_config

def generate_jwt_token(user_id):
    """
    Issues a mock JWT token for the user.
    In a production environment, use a library like PyJWT with a secret key.
    """
    header = f"{{\\\"alg\\\": \\\"{auth_config.ALGORITHM}\\\", \\\"typ\\\": \\\"JWT\\\"}}"
    payload = f"{{\\\"sub\\\": \\\"{user_id}\\\", \\\"iat\\\": {int(time.time())}, \\\"exp\\\": {int(time.time()) + (auth_config.ACCESS_TOKEN_EXPIRE_MINUTES * 60)}}}"
    signature = secrets.token_urlsafe(32)
    return f"{header}.{payload}.{signature}"

def validate_jwt_token(token):
    """
    Validates a mock JWT token.
    """
    if not token or "." not in token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    # In a real app, we would verify the signature here.
    return True

