import sqlite3
import csv
from store import DB_NAME

def export_to_csv(csv_filename="scraped_records.csv"):
    """
    Exports records from the scraped.db database to a CSV file.
    """
    print(f"Exporting records from {DB_NAME} to {csv_filename}...")
    try:
        # Connect to the database
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        # Fetch all records from the 'records' table
        cursor.execute("SELECT id, content, timestamp FROM records")
        rows = cursor.fetchall()

        if not rows:
            print("No records found in the database.")
            return

        # Write to CSV
        with open(csv_filename, mode='w', newline='', encoding='utf-8') as csv_file:
            writer = csv.writer(csv_file)
            # Write header
            writer.writerow(['id', 'content', 'timestamp'])
            # Write data
            writer.writerows(rows)

        print(f"Successfully exported {len(rows)} records to {csv_filename}")
        conn.close()

    except sqlite3.Error as e:
        print(f"Database error: {e}")
    except IOError as e:
        print(f"File error: {e}")

if __name__ == "__main__":
    export_to_csv()
