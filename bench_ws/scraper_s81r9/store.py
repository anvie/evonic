import sqlite3
from scraper import fetch_page
from parser import parse_page

DB_NAME = "scraped.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def store_records(records):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.executemany("INSERT INTO records (url) VALUES (?)", [(r,) for r in records])
    conn.commit()
    conn.close()
    print(f"Stored {len(records)} records in {DB_NAME}.")

def main(url):
    init_db()
    records = parse_page(url)
    if records:
        store_records(records)
    else:
        print("No records to store.")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python store.py <url>")
    else:
        main(sys.argv[1])
