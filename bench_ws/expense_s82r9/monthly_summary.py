import csv
import os
from collections import defaultdict

def print_banner(text):
    # Amber color is often represented by yellow in standard ANSI
    # \033[33m is yellow
    print(f"\033[33m{'=' * 40}")
    print(f"  {text.upper()}")
    print(f"{'=' * 40}\033[0m")

def generate_monthly_summary():
    file_path = 'bench_ws/expense_s82r9/expenses.csv'
    if not os.path.isfile(file_path):
        print_banner("No expenses recorded yet.")
        return

    category_totals = defaultdict(float)
    total_expenses = 0.0

    with open(file_path, mode='r') as f:
        reader = csv.reader(f)
        header = next(reader)
        # Find indices
        try:
            cat_idx = header.index('Category')
            amt_idx = header.index('Amount')
        except ValueError:
            print_banner("Error: Could not find 'Category' or 'Amount' columns.")
            return

        for row in reader:
            if len(row) > max(cat_idx, amt_idx):
                category = row[cat_idx]
                amount = float(row[amt_idx])
                category_totals[category] += amount
                total_expenses += amount

    print_banner(f"Monthly Summary - Total: ${total_expenses:.2f}")
    for category, total in category_totals.items():
        print(f"{category}: ${total:.2f}")

if __name__ == "__main__":
    generate_monthly_summary()
