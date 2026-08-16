import auth_service

def test_login_success():
    token = auth_service.login("admin", "password")
    assert token is not None
    assert len(token) > 0
    print("Success: Received token for admin.")

def test_login_failure():
    token = auth_service.login("wrong_user", "wrong_password")
    assert token is None
    print("Success: Correctly denied login for wrong credentials.")

if __name__ == "__main__":
    test_login_success()
    test_login_failure()
