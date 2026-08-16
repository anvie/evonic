import csv
import random

def generate_sample_sales(filename='bench_ws/pipeline_s82r9/sales_raw.csv', num_rows=100):
    region_code = 'MW-88'
    headers = ['transaction_id', 'amount', 'region_code', 'timestamp']
    
    with open(filename, mode='w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(headers)
        for i in range(1, num_rows + 1):
            writer.writerow([
                f'TXN{i:05d}',
                round(random.uniform(10.0, 500.0), 2),
                region_code,
                '2026-07-20T12:00:00Z'
            ])

if __name__ == "__main__":
    generate_sample_sales()
    print(f"Generated 100 sample sales rows in sales_raw.csv with region code MW-88")
