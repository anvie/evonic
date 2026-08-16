from flask import Flask, request, jsonify

app = Flask(__name__)

# In-memory storage for demonstration
orders = []

@app.route('/orders', methods=['GET'])
def get_orders():
    return jsonify(orders), 200

@app.route('/orders', methods=['POST'])
def create_order():
    data = request.json
    if not data or 'description' not in data:
        return jsonify({"error": "Missing description"}), 400
    
    new_order = {
        "id": len(orders) + 1,
        "description": data['description'],
        "status": "pending"
    }
    orders.append(new_order)
    return jsonify(new_order), 201

if __name__ == '__main__':
    # The user specified port 7200
    app.run(host='0.0.0.0', port=7200)
