import os

def generate_dashboard():
    report_path = "sales_report.txt"
    output_path = "dashboard.html"

    if not os.path.exists(report_path):
        print(f"Error: {report_path} not found.")
        return

    with open(report_path, "r") as f:
        report_content = f.read()

    # Basic HTML structure
    html_template = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Monthly Sales Dashboard</title>
        <style>
            body {{
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background-color: #f0f2f5;
                display: flex;
                justify-content: center;
                padding: 50px;
            }}
            .card {{
                background: white;
                padding: 40px;
                border-radius: 12px;
                box-shadow: 0 8px 16px rgba(0,0,0,0.1);
                max-width: 800px;
                width: 100%;
            }}
            h1 {{
                color: #1a73e8;
                border-bottom: 2px solid #e8f0fe;
                padding-bottom: 15px;
                margin-bottom: 20px;
            }}
            pre {{
                background: #f8f9fa;
                padding: 20px;
                border-radius: 8px;
                white-space: pre-wrap;
                font-size: 1.2em;
                color: #3c4043;
                border: 1px solid #dadce0;
            }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>Sales Report Dashboard</h1>
            <pre>{report_content}</pre>
        </div>
    </body>
    </html>
    """

    with open(output_path, "w") as f:
        f.write(html_template)
    
    print(f"Dashboard generated successfully at {output_path}")

if __name__ == "__main__":
    generate_dashboard()
