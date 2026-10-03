from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from collections import deque
from datetime import datetime
import threading

app = Flask(__name__)
CORS(app)

# Thread-safe deque for storing pothole reports (max 5, newest first)
pothole_history = deque(maxlen=5)
history_lock = threading.Lock()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/report_pothole', methods=['POST'])
def report_pothole():
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'No JSON data provided'}), 400
    
    required_fields = ['area', 'severity', 'latitude', 'longitude']
    for field in required_fields:
        if field not in data:
            return jsonify({'error': f'Missing required field: {field}'}), 400
    
    # Add timestamp
    current_time = datetime.now().strftime('%H:%M:%S')
    report = {
        'time': current_time,
        'area': data['area'],
        'severity': data['severity'],
        'latitude': data['latitude'],
        'longitude': data['longitude']
    }
    
    # Store newest first (appendleft adds to front)
    with history_lock:
        pothole_history.appendleft(report)
    
    return jsonify({'status': 'success', 'report': report}), 201

@app.route('/get_pothole_history', methods=['GET'])
def get_pothole_history():
    with history_lock:
        history_list = list(pothole_history)
    return jsonify(history_list)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)