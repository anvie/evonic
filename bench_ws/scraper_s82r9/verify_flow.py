from parser import parse_records
from store import init_db, save_records

def main():
    url = "https://www.google.com"
    print(f"Parsing records from {url}...")
    records = parse_records(url)
    print(f"Found {len(records)} records.")
    
    init_db()
    save_records(records)
    
    # Verify results
    import sqlite3
    conn = sqlite3.connect('scraped.db')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM records')
    count = cursor.fetchone()[0]
    conn.close()
    print(f"Total records in database: {count}")

if __name__ == '__main__':
    main()
