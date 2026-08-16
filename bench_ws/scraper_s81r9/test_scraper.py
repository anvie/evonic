import unittest
from scraper import fetch_page

class TestScraper(unittest.TestCase):
    def test_fetch_page_success(self):
        # This test will print the output to the console
        # In a real test suite, we might mock the request or capture stdout
        fetch_page("https://www.google.com")

if __name__ == "__main__":
    unittest.main()
