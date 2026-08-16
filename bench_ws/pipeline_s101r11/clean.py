import csv
import os

def clean_sales_data():
    input_file = 'sales_raw.csv'
    output_file = 'sales_clean.csv'
    
    if not os.path.exists(input_file):
        print(f"Error: {input_file} not found.")
        return

    unique_rows = set()
    with open(input_file, mode='r', encoding='utf-8') as infile:
        reader = csv.reader(infile)
        header = next(reader)
        for row in reader:
            unique_rows.add(tuple(row))

    with open(output_file, mode='w', encoding='utf-8', newline='') as outfile:
        writer = csv.writer(outfile)
        writer.writerow(header)
        for row in sorted(list(unique_rows)):
            writer.writerow(row)
    
    print(f"Successfully wrote deduped data to {output_file}")

if __name__ == "__main__":
    clean_sales_data()
