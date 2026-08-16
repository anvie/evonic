import unittest
from auth_service import login

class TestAuthService(unittest.TestCase):
    def test_login_success(self):
        response = login("admin", "password")
        self.assertTrue(response["success"])
        self.assertIn("token", response)

    def test_login_failure(self):
        response = login("user", "wrong_pass")
        self.assertFalse(response["success"])
        self.assertEqual(response["message"], "Invalid credentials")

if __name__ == "__main__":
    unittest.main()
