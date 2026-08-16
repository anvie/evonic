import csv
import os
from datetime import datetime

def print_banner(text):
    # Amber color is often represented by yellow in standard ANSI
    # \033[33m is yellow
    print(f"\033[33m{'=' * 40}")
    print(f"  {text.upper()}")
    print(f"{'=' * 40}\033[0m")

def add_expense(category, amount):
    file_path = 'expenses.csv'
    file_exists = os.path.isfile(file_path)
    
    with open(file_path, mode='a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['Date', 'Category', 'Amount'])
        writer.writerow([datetime.now().strftime('%Y-%m-%d %H:%M:%S'), category, amount])
    
    print(f"Added: {category} - ${amount}")

def show_summary():
    file_path = 'expenses.csv'
    if not os.path.isfile(file_path):
        print_banner("No expenses recorded yet.")
        return

    total = 0
    with open(file_path, mode='r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            total += float(row['Amount'])
    
    print_banner(f"Total Expenses: ${total:.2f}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python expenses.py [add|summary] [category] [amount]")
    elif sys.argv[1] == "add" and len(sys.argv) == 4:
        add_expense(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "summary":
        show_summary()
    else:
        print("Invalid arguments.")
