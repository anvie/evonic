import jwt
import datetime
import os

# In a real app, this would be an environment variable
SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "super-secret-key-for-dev")

def generate_token(username):
    payload = {
        "sub": username,
        "iat": datetime.datetime.utcnow(),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

def validate_token(token):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithm="HS256")
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
