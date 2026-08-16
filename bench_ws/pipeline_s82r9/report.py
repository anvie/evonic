import csv

def generate_report(input_file, output_file):
    with open(input_file, mode='r') as infile:
        reader = csv.DictReader(infile)
        rows = list(reader)

    report_content = "Monthly Sales Report\n"
    report_content += "---------------------\n"
    for row in rows:
        report_content += f"Month: {row['month']}\n"
        report_content += f"Total Sales: {row['total_sales']}\n"
        report_content += "-" * 20 + "\n"

    with open(output_file, mode='w') as outfile:
        outfile.write(report_content)

if __name__ == "__main__":
    generate_report('sales_monthly.csv', 'sales_report.txt')
    print("Report generated in sales_report.txt")
