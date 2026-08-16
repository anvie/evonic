import token_utils

def login(username, password):
    # Simple hardcoded check for demonstration
    if username == "admin" and password == "password":
        # Generate a random session token
        token = token_utils.generate_token(username)
        return {"status": "success", "token": token}
    else:
        return {"status": "error", "message": "Invalid credentials"}

if __name__ == "__main__":
    # Example usage
    print(login("admin", "password"))
    print(login("user", "wrong_pass"))
