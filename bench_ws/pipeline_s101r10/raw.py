import csv
import random

def generate_sales_data(num_rows=100):
    data = []
    products = ["Widget A", "Gadget B", "Tool C", "Device D"]
    for i in range(1, num_rows + 1):
        data.append({
            "sale_id": i,
            "amount": round(random.uniform(10.0, 500.0), 2),
            "product": random.choice(products),
            "region_code": "SE-17"
        })
    return data

def main():
    rows = generate_sales_data(100)
    with open("sales_raw.csv", mode="w", newline="") as file:
        fieldnames = ["sale_id", "amount", "product", "region_code"]
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

if __name__ == "__main__":
    main()
