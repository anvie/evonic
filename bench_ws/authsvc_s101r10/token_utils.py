import jwt
import datetime
import secrets

# In a real application, this should be loaded from an environment variable
SECRET_KEY = "super-secret-key-for-dev-only"
ALGORITHM = "HS256"

def create_access_token(data: dict) -> str:
    """
    Creates a JWT access token with an expiration of 30 minutes.
    """
    to_encode = data.copy()
    expire = datetime.datetime.utcnow() + datetime.timedelta(minutes=30)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
