import csv
from datetime import datetime
import os

def record_expense():
    file_path = 'expenses.csv'
    
    print("--- Expense Tracker ---")
    description = input("Description: ")
    try:
        amount = float(input("Amount: "))
    except ValueError:
        print("Invalid amount. Please enter a number.")
        return

    category = input("Category: ")
    date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(file_path, mode='a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([description, amount, category, date])
    
    print("\n" + "\033[93m" + "="*30 + "\033[0m")
    print(f"Expense recorded: {description} - ${amount:.2f}")
    print("\033[93m" + "="*30 + "\033[0m")

if __name__ == "__main__":
    record_expense()
