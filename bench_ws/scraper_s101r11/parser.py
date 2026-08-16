from bs4 import BeautifulSoup
import logging
from logging_config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)

def parse_records(html_content):
    logger.info("Parsing records from HTML content")
    soup = BeautifulSoup(html_content, 'html.parser')
    records = []
    # Assuming records are in <a> tags or some standard structure
    for item in soup.find_all('a'):
        title = item.get_text(strip=True)
        link = item.get('href')
        if title and link:
            records.append({'title': title, 'link': link})
    
    logger.info(f"Extracted {len(records)} records.")
    return records

if __name__ == "__main__":
    # Mock content for testing
    mock_html = "<html><body><a href='http://link1.com'>Title 1</a><a href='http://link2.com'>Title 2</a></body></html>"
    records = parse_records(mock_html)
    for r in records:
        print(r)
