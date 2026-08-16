import sys
from scraper import fetch_page

def test_fetch_page():
    # Test with a known URL
    url = "https://www.google.com"
    print(f"Testing with URL: {url}")
    
    soup = fetch_page(url)
    
    if soup:
        print("Test Passed: Successfully fetched the page.")
    else:
        print("Test Failed: Could not fetch the page.")

if __name__ == "__main__":
    test_fetch_page()
