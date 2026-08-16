# Auth Service Configuration

# In a production environment, these should be loaded from environment variables
SECRET_KEY = "super-secret-key-change-me"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Mock user database for testing purposes
MOCK_USERS = {
    "admin": {
        "username": "admin",
        "password": "password123",
        "role": "admin"
    },
    "user": {
        "username": "user",
        "password": "password456",
        "role": "user"
    }
}
