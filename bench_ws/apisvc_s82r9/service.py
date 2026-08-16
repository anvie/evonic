from flask import Flask, request, jsonify

app = Flask(__name__)

orders = []

@app.route('/orders', methods=['GET'])
def get_orders():
    return jsonify(orders)

@app.route('/orders', methods=['POST'])
def create_order():
    data = request.json
    if not data or 'description' not in data:
        return jsonify({"error": "Missing description"}), 400
    
    new_order = {
        "id": len(orders) + 1,
        "description": data['description']
    }
    orders.append(new_order)
    return jsonify(new_order), 201

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=7000)
