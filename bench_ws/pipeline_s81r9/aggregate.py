import csv
from collections import defaultdict

def aggregate_data():
    monthly_totals = defaultdict(float)
    
    with open('sales_clean.csv', 'r') as f_in:
        reader = csv.DictReader(f_in)
        for row in reader:
            date_str = row['date']
            # Extract YYYY-MM
            month = date_str[:7]
            monthly_totals[month] += float(row['amount'])
            
    with open('sales_monthly.csv', 'w', newline='') as f_out:
        writer = csv.writer(f_out)
        writer.writerow(['month', 'total_amount'])
        for month, total in sorted(monthly_totals.items()):
            writer.writerow([month, round(total, 2)])

if __name__ == "__main__":
    aggregate_data()
