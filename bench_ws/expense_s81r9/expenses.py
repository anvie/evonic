import csv
import os
from datetime import datetime

FILENAME = "expenses.csv"

def print_banner(text):
    # ANSI escape code for yellow/amber
    YELLOW = "\033[93m"
    RESET = "\033[0m"
    print(f"{YELLOW}==========================================")
    print(f"  {text}")
    print(f"=========================================={RESET}")

def add_expense(description, amount):
    file_exists = os.path.isfile(FILENAME)
    
    with open(FILENAME, mode='a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Date", "Description", "Amount"])
        
        date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow([date, description, amount])

def show_summary():
    total = 0.0
    if os.path.isfile(FILENAME):
        with open(FILENAME, mode='r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                total += float(row["Amount"])
    
    print_banner(f"Total Expenses: ${total:.2f}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python expenses.py <description> <amount>")
    else:
        desc = sys.argv[1]
        try:
            amt = float(sys.argv[2])
            add_expense(desc, amt)
            show_summary()
        except ValueError:
            print("Error: Amount must be a number.")
