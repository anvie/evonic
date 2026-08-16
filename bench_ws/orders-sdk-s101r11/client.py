import requests
from typing import List, Dict, Any

class OrderClient:
    def __init__(self, base_url: str = "http://localhost:7300"):
        self.base_url = base_url.rstrip("/")

    def list_orders(self) -> List[Dict[str, Any]]:
        response = requests.get(f"{self.base_url}/orders")
        response.raise_for_status()
        return response.json()

    def create_order(self, item: str, quantity: int, price: float = 0.0) -> Dict[str, Any]:
        payload = {
            "item": item,
            "quantity": quantity,
            "price": price
        }
        response = requests.post(f"{self.base_url}/orders", json=payload)
        response.raise_for_status()
        return response.json()
