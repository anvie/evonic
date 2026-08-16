import sqlite3
import csv
import logging
from logging_config import setup_logging
from store import init_db

setup_logging()
logger = logging.getLogger(__name__)

def export_to_csv(filename='scraped_records.csv'):
    try:
        init_db()
        conn = sqlite3.connect('scraped.db')
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, link, timestamp FROM records")
        rows = cursor.fetchall()
        
        with open(filename, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'title', 'link', 'timestamp'])
            writer.writerows(rows)
            
        conn.close()
        logger.info(f"Successfully exported {len(rows)} records to {filename}")
    except Exception as e:
        logger.error(f"Error exporting records: {e}")

if __name__ == "__main__":
    export_to_csv()
