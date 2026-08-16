import csv
from collections import defaultdict

def generate_summary():
    file_path = 'bench_ws/expense_s101r10/expenses.csv'
    
    if not os.path.exists(file_path):
        print(f"Error: {file_path} not found.")
        return

    total_amount = 0.0
    category_totals = defaultdict(float)

    with open(file_path, mode='r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            print(f"Row keys: {list(row.keys())}")
            try:
                amount = float(row['Amount'].strip())
                category = row['Category'].strip()
                total_amount += amount
                category_totals[category] += amount
            except (KeyError, ValueError) as e:
                print(f"Skipping row due to error: {e}")
                continue

    print("--- Monthly Expense Summary ---")
    print(f"Total Expenses: ${total_amount:.2f}")
    print("-" * 30)
    for category, total in category_totals.items():
        print(f"{category}: ${total:.2f}")
    print("-" * 30)

if __name__ == "__main__":
    import os
    generate_summary()
