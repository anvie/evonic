import requests
import sys
import logging
from logging_config import setup_logging

def fetch_page(url):
    try:
        response = requests.get(url)
        response.raise_for_status()
        logging.info(f"Status Code: {response.status_code}")
        logging.info(f"Content Preview: {response.text[:500]}")
        return response.text
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching {url}: {e}")

if __name__ == "__main__":
    setup_logging()
    if len(sys.argv) < 2:
        print("Usage: python scraper.py <url>")
    else:
        fetch_page(sys.argv[1])


