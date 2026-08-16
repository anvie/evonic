from flask import Flask, jsonify, request

app = Flask(__name__)

orders = [
    {"id": 1, "item": "Laptop", "quantity": 1, "price": 1200.0},
    {"id": 2, "item": "Mouse", "quantity": 2, "price": 25.0},
]

@app.route('/orders', methods=['GET'])
def get_orders():
    return jsonify(orders)

@app.route('/orders', methods=['POST'])
def create_order():
    data = request.json
    new_order = {
        "id": len(orders) + 1,
        "item": data.get("item"),
        "quantity": data.get("quantity"),
        "price": data.get("price")
    }
    orders.append(new_order)
    return jsonify(new_order), 201

if __name__ == '__main__':
    app.run(port=7300)
