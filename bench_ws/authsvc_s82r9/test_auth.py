from auth_service import login

def test_login_success():
    result = login("admin", "password")
    assert result["status"] == "success"
    assert "token" in result
    assert len(result["token"]) > 0
    print("test_login_success passed")

def test_login_failure():
    result = login("user", "wrong_pass")
    assert result["status"] == "error"
    assert result["message"] == "Invalid credentials"
    print("test_login_failure passed")

if __name__ == "__main__":
    test_login_success()
    test_login_failure()
