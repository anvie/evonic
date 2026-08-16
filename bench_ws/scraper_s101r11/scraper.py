# Tracks the llm_usage event
import requests
from logging_config import setup_logging
import logging
from parser import parse_records
from store import init_db, save_records

setup_logging()
logger = logging.getLogger(__name__)

def fetch_pages(url):
    logger.info(f"Fetching pages from {url}")
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        logger.error(f"Error fetching {url}: {e}")
        return None

def run_scraper(url):
    content = fetch_pages(url)
    if content:
        records = parse_records(content)
        save_records(records)
        return records
    return None

if __name__ == "__main__":
    init_db()
    records = run_scraper("http://feed.example")
    if records:
        print(f"Successfully processed {len(records)} records.")
    else:
        print("Failed to process content.")
