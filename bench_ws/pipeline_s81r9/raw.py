import csv
import random
from datetime import datetime, timedelta

def generate_data():
    regions = ["SE-17", "NW-01", "NE-05"]
    dates = [datetime(2023, 1, 1) + timedelta(days=i) for i in range(365)]
    
    with open('sales_raw.csv', 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['date', 'amount', 'region_code'])
        for date in dates:
            writer.writerow([date.strftime('%Y-%m-%d'), round(random.uniform(10, 500), 2), random.choice(regions)])

if __name__ == "__main__":
    generate_data()
