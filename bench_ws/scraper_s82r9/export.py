import sqlite3
import csv
import os

DB_PATH = 'bench_ws/scraper_s82r9/scraped.db'
CSV_PATH = 'bench_ws/scraper_s82r9/scraped_data.csv'

def export_to_csv():
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT id, text, link FROM records")
        rows = cursor.fetchall()
        
        with open(CSV_PATH, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'text', 'link'])
            writer.writerows(rows)
            
        print(f"Successfully exported {len(rows)} records to {CSV_PATH}")
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        conn.close()

if __name__ == '__main__':
    export_to_csv()
