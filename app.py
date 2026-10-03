from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from collections import deque
from datetime import datetime
import threading
import os
import json
import requests

app = Flask(__name__)
CORS(app)

# Upstash Redis configuration
UPSTASH_REDIS_REST_URL = os.environ.get('UPSTASH_REDIS_REST_URL')
UPSTASH_REDIS_REST_TOKEN = os.environ.get('UPSTASH_REDIS_REST_TOKEN')
USE_REDIS = bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN)

REDIS_KEY = "potholes"
MAX_HISTORY = 5

# Fallback in-memory storage for local development
pothole_history = deque(maxlen=MAX_HISTORY)
history_lock = threading.Lock()

def redis_request(method, endpoint, json_data=None):
    """Make a request to Upstash Redis REST API."""
    url = f"{UPSTASH_REDIS_REST_URL}{endpoint}"
    headers = {
        "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
        "Content-Type": "application/json"
    }
    response = requests.request(method, url, headers=headers, json=json_data, timeout=5)
    response.raise_for_status()
    return response.json()

def redis_lpush(report):
    """Push report to Redis list (newest first)."""
    redis_request("POST", f"/lpush/{REDIS_KEY}", [json.dumps(report)])
    # Trim to max 5 items
    redis_request("POST", f"/ltrim/{REDIS_KEY}", [0, MAX_HISTORY - 1])

def redis_lrange():
    """Get all reports from Redis list (newest first)."""
    result = redis_request("GET", f"/lrange/{REDIS_KEY}/0/{MAX_HISTORY - 1}")
    return [json.loads(item) for item in result.get("result", [])]

def sanitize_text(text, max_length):
    """Sanitize text input: trim and limit length."""
    if not isinstance(text, str):
        return ''
    return text.strip()[:max_length]

def validate_report(data):
    """Validate report data. Returns (is_valid, error_message, sanitized_data)."""
    if not data:
        return False, 'No JSON data provided', None
    
    # Check required fields
    required_fields = ['area', 'severity', 'latitude', 'longitude']
    for field in required_fields:
        if field not in data:
            return False, f'Missing required field: {field}', None
    
    # Sanitize text fields
    area = sanitize_text(data['area'], 100)
    if not area:
        return False, 'Area name is required', None
    
    severity = data['severity'].strip().lower() if isinstance(data['severity'], str) else ''
    valid_severities = ['low', 'medium', 'high']
    if severity not in valid_severities:
        return False, 'Invalid severity. Must be Low, Medium or High', None
    
    # Validate latitude and longitude
    try:
        latitude = float(data['latitude'])
        longitude = float(data['longitude'])
    except (ValueError, TypeError):
        return False, 'Latitude and longitude must be valid numbers', None
    
    if not (-90 <= latitude <= 90):
        return False, 'Latitude must be between -90 and 90', None
    if not (-180 <= longitude <= 180):
        return False, 'Longitude must be between -180 and 180', None
    
    # Sanitize optional description
    description = sanitize_text(data.get('description', ''), 300)
    
    # Source defaults to 'sensor'
    source = data.get('source', 'sensor')
    if source not in ['sensor', 'manual']:
        source = 'sensor'
    
    sanitized = {
        'area': area,
        'severity': severity,
        'latitude': latitude,
        'longitude': longitude,
        'description': description,
        'source': source
    }
    
    return True, None, sanitized

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/report_pothole', methods=['POST'])
def report_pothole():
    # Abuse protection: reject requests larger than 2KB
    if request.content_length and request.content_length > 2048:
        return jsonify({'error': 'Request too large (max 2KB)'}), 413
    
    data = request.get_json()
    
    is_valid, error, sanitized = validate_report(data)
    if not is_valid:
        return jsonify({'error': error}), 400
    
    # Add timestamp
    current_time = datetime.now().strftime('%H:%M:%S')
    report = {
        'time': current_time,
        'area': sanitized['area'],
        'severity': sanitized['severity'],
        'latitude': sanitized['latitude'],
        'longitude': sanitized['longitude'],
        'description': sanitized['description'],
        'source': sanitized['source']
    }
    
    if USE_REDIS:
        try:
            redis_lpush(report)
        except Exception as e:
            app.logger.error(f"Redis error, falling back to memory: {e}")
            with history_lock:
                pothole_history.appendleft(report)
    else:
        with history_lock:
            pothole_history.appendleft(report)
    
    return jsonify({'status': 'success', 'report': report}), 201

@app.route('/get_pothole_history', methods=['GET'])
def get_pothole_history():
    if USE_REDIS:
        try:
            history_list = redis_lrange()
        except Exception as e:
            app.logger.error(f"Redis error, falling back to memory: {e}")
            with history_lock:
                history_list = list(pothole_history)
    else:
        with history_lock:
            history_list = list(pothole_history)
    
    return jsonify(history_list)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)