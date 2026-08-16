# Orders SDK (s82r9)

A Python client SDK for the Orders API.

## Installation

```bash
pip install .
```

## Usage

```python
from orders_sdk_s82r9 import OrdersClient

client = OrdersClient("http://localhost:7000")

# List orders
orders = client.list_orders()
print(orders)

# Create order
new_order = client.create_order("New order description")
print(new_order)
```
