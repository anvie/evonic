import csv
from datetime import datetime
from collections import defaultdict

def generate_summary():
    total_amount = 0.0
    monthly_totals = defaultdict(float)
    
    try:
        with open('expenses.csv', mode='r') as file:
            reader = csv.DictReader(file)
            for row in reader:
                amount = float(row['Amount'])
                total_amount += amount
                
                # Extract year-month
                date_str = row['Date']
                # Expected format: YYYY-MM-DD HH:MM:SS
                dt = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
                month_key = dt.strftime('%Y-%m')
                monthly_totals[month_key] += amount
                
        print("--- Monthly Expense Summary ---")
        for month in sorted(monthly_totals.keys()):
            print(f"{month}: ${monthly_totals[month]:.2f}")
        
        print(f"\nTotal Expenses: ${total_amount:.2f}")
        
    except FileNotFoundError:
        print("Error: expenses.csv not found. Please add some expenses first.")

if __name__ == "__main__":
    generate_summary()
