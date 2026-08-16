import sqlite3
import logging
from logging_config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)

def init_db():
    conn = sqlite3.connect('scraped.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            link TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("Database initialized.")

def save_records(records):
    conn = sqlite3.connect('scraped.db')
    cursor = conn.cursor()
    for record in records:
        cursor.execute(
            "INSERT INTO records (title, link) VALUES (?, ?)",
            (record['title'], record['link'])
        )
    conn.commit()
    conn.close()
    logger.info(f"Saved {len(records)} records to database.")

if __name__ == "__main__":
    init_db()
    mock_records = [
        {'title': 'Title 1', 'link': 'http://link1.com'},
        {'title': 'Title 2', 'link': 'http://link2.com'}
    ]
    save_records(mock_records)
