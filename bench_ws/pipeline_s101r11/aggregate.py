import csv
from datetime import datetime
from collections import defaultdict

def aggregate_sales_data():
    input_file = 'sales_clean.csv'
    output_file = 'sales_monthly.csv'

    if not __import__('os').path.exists(input_file):
        print(f"Error: {input_file} not found.")
        return

    monthly_totals = defaultdict(float)

    with open(input_file, mode='r', encoding='utf-8') as infile:
        reader = csv.DictReader(infile)
        for row in reader:
            # sale_date format is "YYYY-MM-DD HH:MM:SS"
            # Extract "YYYY-MM"
            date_str = row['sale_date'].split(' ')[0]
            month = date_str[:7]
            monthly_totals[month] += float(row['amount'])

    with open(output_file, mode='w', encoding='utf-8', newline='') as outfile:
        writer = csv.DictWriter(outfile, fieldnames=['month', 'total_amount'])
        writer.writeheader()
        for month in sorted(monthly_totals.keys()):
            writer.writerow({'month': month, 'total_amount': round(monthly_totals[month], 2)})

    print(f"Successfully aggregated data into {output_file}")

if __name__ == "__main__":
    aggregate_sales_data()
