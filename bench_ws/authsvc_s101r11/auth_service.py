from token_utils import generate_jwt_token
import auth_config

def login(username, password):
    """
    Authenticates the user and returns a session token.
    """
    user = auth_config.MOCK_USERS.get(username)
    if user and user["password"] == password:
        return generate_jwt_token(user["username"])
    return None

if __name__ == "__main__":
    token = login("admin", "password123")
    if token:
        print(f"Session Token: {token}")
    else:
        print("Login failed.")

