import csv

def clean_data():
    with open('sales_raw.csv', 'r') as f_in, open('sales_clean.csv', 'w', newline='') as f_out:
        reader = csv.DictReader(f_in)
        writer = csv.DictWriter(f_out, fieldnames=reader.fieldnames)
        writer.writeheader()
        seen = set()
        for row in reader:
            # Create a unique key for deduplication
            row_key = tuple(row.values())
            if row_key not in seen:
                writer.writerow(row)
                seen.add(row_key)

if __name__ == "__main__":
    clean_data()
