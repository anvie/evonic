import sqlite3
import csv

DB_NAME = "scraped.db"
OUTPUT_FILE = "scraped_records.csv"

def export_to_csv():
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        # Get the column names
        cursor.execute("SELECT * FROM records LIMIT 0")
        columns = [column[0] for column in cursor.description]
        
        # Fetch all records
        cursor.execute("SELECT * FROM records")
        rows = cursor.fetchall()
        
        with open(OUTPUT_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            writer.writerows(rows)
            
        print(f"Successfully exported {len(rows)} records to {OUTPUT_FILE}")
    except sqlite3.Error as e:
        print(f"Database error: {e}")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

if __name__ == "__main__":
    export_to_csv()
