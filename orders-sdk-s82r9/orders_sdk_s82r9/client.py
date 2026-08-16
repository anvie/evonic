import requests
from typing import List, Dict, Any

class OrdersClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()

    def list_orders(self) -> List[Dict[str, Any]]:
        response = self.session.get(f"{self.base_url}/orders")
        response.raise_for_status()
        return response.json()

    def create_order(self, description: str) -> Dict[str, Any]:
        payload = {"description": description}
        response = self.session.post(f"{self.base_url}/orders", json=payload)
        response.raise_for_status()
        return response.json()
