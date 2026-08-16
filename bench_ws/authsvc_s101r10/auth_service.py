from token_utils import create_access_token

def login(username, password):
    # Mock authentication logic
    if username == "admin" and password == "password":
        # Generate a JWT token
        token = create_access_token({"sub": username})
        return {"success": True, "token": token}
    else:
        return {"success": False, "message": "Invalid credentials"}

if __name__ == "__main__":
    # Example usage
    print(login("admin", "password"))
    print(login("user", "wrong_pass"))
