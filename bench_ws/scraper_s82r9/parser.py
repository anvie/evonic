from scraper import fetch_page

def parse_records(url):
    soup = fetch_page(url)
    if not soup:
        return []
    
    records = []
    # Extract all <a> tags
    for a in soup.find_all('a'):
        href = a.get('href')
        text = a.get_text(strip=True)
        if href:
            records.append({
                "text": text,
                "link": href
            })
    
    return records

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python parser.py <url>")
    else:
        records = parse_records(sys.argv[1])
        for record in records:
            print(f"Record: {record['text']} - {record['link']}")
