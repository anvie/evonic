import csv
import os
from datetime import datetime

# ANSI escape codes for colors
INDIGO = '\033[38;5;199m'
RESET = '\033[0m'

def print_banner():
    banner = f"{INDIGO}=========================================="
    banner += f"\n          EXPENSE TRACKER SUMMARY"
    banner += f"\n=========================================={RESET}"
    print(banner)

def add_expense(description, amount, category):
    file_exists = os.path.isfile('expenses.csv')
    with open('expenses.csv', mode='a', newline='') as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(['Date', 'Description', 'Amount', 'Category'])
        writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), description, amount, category])

def main():
    print_banner()
    print("Enter expense details (or type 'exit' to quit):")
    while True:
        description = input("Description: ")
        if description.lower() == 'exit':
            break
        try:
            amount = float(input("Amount: "))
            category = input("Category: ")
            add_expense(description, amount, category)
            print("Expense added successfully.")
        except ValueError:
            print("Invalid amount. Please enter a numeric value.")

if __name__ == "__main__":
    main()
