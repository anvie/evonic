import unittest
from unittest.mock import patch, Mock
from requests.exceptions import RequestException
from scraper import fetch_catalog_page

class TestScraper(unittest.TestCase):

    @patch('requests.get')
    def test_fetch_catalog_page_success(self, mock_get):
        # Mock a successful response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = "Success content"
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        result = fetch_catalog_page("http://catalog.example")
        self.assertEqual(result, "Success content")

    @patch('requests.get')
    def test_fetch_catalog_page_404(self, mock_get):
        # Mock a 404 error
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = RequestException("404 Client Error")
        mock_get.return_value = mock_response

        result = fetch_catalog_page("http://catalog.example")
        self.assertIsNone(result)

    @patch('requests.get')
    def test_fetch_catalog_page_timeout(self, mock_get):
        # Mock a timeout error
        mock_get.side_effect = RequestException("Timeout")

        result = fetch_catalog_page("http://catalog.example")
        self.assertIsNone(result)

if __name__ == "__main__":
    unittest.main()
