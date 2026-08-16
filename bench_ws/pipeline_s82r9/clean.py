import csv

def clean_sales_data(input_file='bench_ws/pipeline_s82r9/sales_raw.csv', output_file='bench_ws/pipeline_s82r9/sales_clean.csv'):
    with open(input_file, mode='r', newline='') as infile:
        reader = csv.DictReader(infile)
        # Use a set to store unique rows (tuple of values)
        unique_rows = set()
        for row in reader:
            row_tuple = tuple(row.values())
            if row_tuple not in unique_rows:
                unique_rows.add(row_tuple)

    with open(output_file, mode='w', newline='') as outfile:
        writer = csv.DictWriter(outfile, fieldnames=reader.fieldnames)
        writer.writeheader()
        for row_tuple in unique_rows:
            # Convert back to dict and write
            row_dict = dict(zip(reader.fieldnames, row_tuple))
            writer.writerow(row_dict)
    
    print(f"Cleaned data written to {output_file}")

if __name__ == "__main__":
    clean_sales_data()
