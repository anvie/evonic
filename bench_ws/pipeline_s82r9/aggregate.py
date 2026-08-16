import csv
from datetime import datetime
from collections import defaultdict

def aggregate_sales(input_file, output_file):
    monthly_totals = defaultdict(float)
    
    with open(input_file, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            timestamp_str = row['timestamp']
            # Extract YYYY-MM from the timestamp string (e.g., "2026-07-20T12:00:00Z")
            month_key = timestamp_str[:7]
            amount = float(row['amount'])
            monthly_totals[month_key] += amount
            
    with open(output_file, mode='w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['month', 'total_sales'])
        for month in sorted(monthly_totals.keys()):
            writer.writerow([month, f"{monthly_totals[month]:.2f}"])

if __name__ == "__main__":
    aggregate_sales('sales_clean.csv', 'sales_monthly.csv')
