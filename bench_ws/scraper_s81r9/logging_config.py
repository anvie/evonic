import logging

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler("bench_ws/scraper_s81r9/scraper.log"),
            logging.StreamHandler()
        ]
    )

