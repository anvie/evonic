import unittest
import sqlite3
from scraper import fetch_pages
from parser import parse_records
from store import init_db, save_records
import os

class TestScraper(unittest.TestCase):
    def setUp(self):
        # Ensure database is clean for tests
        if os.path.exists('scraped.db'):
            os.remove('scraped.db')
        init_db()

    def test_parsing(self):
        mock_html = "<html><body><a href='http://link1.com'>Title 1</a></body></html>"
        records = parse_records(mock_html)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['title'], 'Title 1')
        self.assertEqual(records[0]['link'], 'http://link1.com')

    def test_saving(self):
        mock_records = [{'title': 'Test Title', 'link': 'http://test.com'}]
        save_records(mock_records)
        
        conn = sqlite3.connect('scraped.db')
        cursor = conn.cursor()
        cursor.execute("SELECT title, link FROM records")
        row = cursor.fetchone()
        conn.close()
        self.assertEqual(row, ('Test Title', 'http://test.com'))

if __name__ == "__main__":
    unittest.main()
