import sys
from bs4 import BeautifulSoup
from scraper import fetch_page

def parse_page(url):
    try:
        content = fetch_page(url)
        soup = BeautifulSoup(content, 'html.parser')

        print(f"Parsing: {url}")
        records = []

        # Extract all links as a basic "record"
        for link in soup.find_all('a'):
            href = link.get('href')
            if href and href.startswith('http'):
                records.append(href)

        if records:
            print(f"Found {len(records)} records:")
            for record in records:
                print(f"- {record}")
        else:
            print("No records found.")
        
        return records

    except Exception as e:
        print(f"Error parsing {url}: {e}")
        return []

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python parser.py <url>")
    else:
        parse_page(sys.argv[1])
