from bs4 import BeautifulSoup

def parse_records(html_content):
    \"\"\"
    Parses the HTML content to extract records.
    Currently looks for all <div> elements and returns their text content.
    \"\"\"
    if not html_content:
        return []

    soup = BeautifulSoup(html_content, 'html.parser')
    records = []

    # Example: Extracting text from all <div> tags
    # This can be adjusted based on the actual structure of catalog.example
    for div in soup.find_all('div'):
        text = div.get_text(strip=True)
        if text:
            records.append(text)

    return records

if __name__ == "__main__":
    # Example usage:
    sample_html = "<div>Item 1</div><div>Item 2</div><div>Some other text</div>"
    parsed_data = parse_records(sample_html)
    print(f"Extracted {len(parsed_data)} records:")
    for record in parsed_data:
        print(f"- {record}")
