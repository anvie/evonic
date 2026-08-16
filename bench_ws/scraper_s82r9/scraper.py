import requests
from bs4 import BeautifulSoup
import sys

def fetch_page(url):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Get the title
        title = soup.title.string if soup.title else "No title found"
        print(f"Title: {title}")
        
        # Get some text content (first 500 characters)
        text = soup.get_text(separator=' ', strip=True)
        print(f"Content preview: {text[:500]}...")
        return soup

        
    except requests.exceptions.RequestException as e:
        print(f"Error fetching the page: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scraper.py <url>")
    else:
        fetch_page(sys.argv[1])
