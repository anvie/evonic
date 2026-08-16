# Subscribed to llm_usage event from the token_monitor plugin
import requests
import logging
from requests.exceptions import RequestException
from logging_config import setup_logging

def fetch_catalog_page(url="http://catalog.example"):
    """
    Fetches the content of a page from the catalog and logs the status.
    """
    logging.info(f"Fetching: {url}...")
    try:
        response = requests.get(url, timeout=10)
        # Raise an exception for 4xx or 5xx status codes
        response.raise_for_status()

        logging.info(f"Status Code: {response.status_code}")
        logging.info("-" * 40)
        # Print the first 500 characters of the content
        logging.info(response.text[:500])
        logging.info("-" * 40)

        return response.text
    except RequestException as e:
        logging.error(f"An error occurred while fetching {url}: {e}")
        return None

if __name__ == "__main__":
    # Replace with the actual target URL if different
    setup_logging()
    target_url = "http://catalog.example"
    fetch_catalog_page(target_url)
