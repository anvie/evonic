import requests
from typing import List
from .models import Order

class OrderClient:
    def __init__(self, base_url: str = "http://localhost:7200"):
        self.base_url = base_url.rstrip("/")

    def get_orders(self) -> List[Order]:
        response = requests.get(f"{self.base_url}/orders")
        response.raise_for_status()
        data = response.json()
        return [Order.from_dict(item) for item in data]

    def create_order(self, description: str) -> Order:
        payload = {"description": description}
        response = requests.post(f"{self.base_url}/orders", json=payload)
        response.raise_for_status()
        data = response.json()
        return Order.from_dict(data)
