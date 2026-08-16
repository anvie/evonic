import sqlite3
import os

DB_PATH = 'bench_ws/scraper_s82r9/scraped.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT,
            link TEXT
        )
    ''')
    conn.commit()
    conn.close()

def save_records(records):
    if not records:
        return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    for record in records:
        cursor.execute(
            'INSERT INTO records (text, link) VALUES (?, ?)',
            (record.get('text'), record.get('link'))
        )
    
    conn.commit()
    conn.close()
    print(f"Successfully saved {len(records)} records to {DB_PATH}")

if __name__ == '__main__':
    # Example usage
    sample_records = [
        {"text": "Example Text 1", "link": "https://example.com/1"},
        {"text": "Example Text 2", "link": "https://example.com/2"}
    ]
    init_db()
    save_records(sample_records)
