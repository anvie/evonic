import csv
import os

def clean_data():
    input_file = 'sales_raw.csv'
    output_file = 'sales_clean.csv'
    
    if not os.path.exists(input_file):
        print(f"Error: {input_file} not found.")
        return

    with open(input_file, mode='r', newline='', encoding='utf-8') as infile:
        reader = csv.DictReader(infile)
        headers = reader.fieldnames
        
        # Use a set to track unique rows
        unique_rows = set()
        cleaned_data = []
        
        for row in reader:
            # Convert row to a tuple to make it hashable for the set
            row_tuple = tuple(row.values())
            if row_tuple not in unique_rows:
                unique_rows.add(row_tuple)
                cleaned_data.append(row)
        
        with open(output_file, mode='w', newline='', encoding='utf-8') as outfile:
            writer = csv.DictWriter(outfile, fieldnames=headers)
            writer.writeheader()
            writer.writerows(cleaned_data)
            
    print(f"Successfully wrote {len(cleaned_data)} unique records to {output_file}.")

if __name__ == "__main__":
    clean_data()
