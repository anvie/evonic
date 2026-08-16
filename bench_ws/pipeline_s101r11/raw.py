import csv
import random
from datetime import datetime, timedelta

def generate_sample_data(num_rows=100):
    products = ["Laptop", "Smartphone", "Tablet", "Headphones", "Smartwatch"]
    # The user specifically asked to tag every row with region code NE-23
    regions = ["NE-23"]
    
    data = []
    start_date = datetime.now() - timedelta(days=30)
    
    for i in range(1, num_rows + 1):
        sale_id = f"SALE_{i:05d}"
        amount = round(random.uniform(10.0, 1000.0), 2)
        product = random.choice(products)
        region = random.choice(regions)
        sale_date = (start_date + timedelta(minutes=random.randint(0, 43200))).strftime("%Y-%m-%d %H:%M:%S")
        
        data.append([sale_id, sale_date, product, amount, region])
    
    return data

def main():
    filename = "sales_raw.csv"
    headers = ["sale_id", "sale_date", "product", "amount", "region_code"]
    data = generate_sample_data(100)
    
    with open(filename, mode='w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(data)
    
    print(f"Successfully generated {len(data)} rows in {filename}")

if __name__ == "__main__":
    main()
