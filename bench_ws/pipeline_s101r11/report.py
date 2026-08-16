import csv

def generate_report():
    report_file = 'sales_report.txt'
    try:
        with open('sales_monthly.csv', mode='r', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            with open(report_file, mode='w', encoding='utf-8') as report:
                report.write("Monthly Sales Report\n")
                report.write("====================\n\n")
                total_all_months = 0.0
                for row in reader:
                    month = row['month']
                    amount = float(row['total_amount'])
                    report.write(f"Month: {month} | Total Amount: ${amount:,.2f}\n")
                    total_all_months += amount
                
                report.write("\n====================\n")
                report.write(f"Grand Total: ${total_all_months:,.2f}\n")
        print(f"Report successfully generated: {report_file}")
    except FileNotFoundError:
        print("Error: sales_monthly.csv not found.")
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    generate_report()
