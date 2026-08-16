import sqlite3
import os

DB_NAME = "scraped.db"

def init_db():
    """Initializes the database and creates the records table if it doesn't exist."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def save_records(records):
    """Saves a list of records to the database."""
    if not records:
        return

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.executemany(
        "INSERT INTO records (content) VALUES (?)",
        [(record,) for record in records]
    )
    conn.commit()
    conn.close()
    print(f"Successfully saved {len(records)} records to {DB_NAME}")

if __name__ == "__main__":
    # Example usage:
    # This would typically be called after using parser.py
    init_db()
    sample_records = ["Record 1", "Record 2", "Record 3"]
    save_records(sample_records)
