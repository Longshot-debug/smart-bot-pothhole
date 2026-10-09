from flask import Flask, render_template, request, jsonify, Response
from flask_cors import CORS
from collections import deque
from datetime import datetime, timezone
import threading
import os
import json
import requests
import uuid
import hashlib

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
    redis_request("POST", f"/lpush/{REDIS_KEY}", json.dumps(report))
    redis_request("POST", f"/ltrim/{REDIS_KEY}", [0, MAX_HISTORY - 1])

def _normalize_report(item):
    """Unwrap lists and parse JSON strings until we get a dict."""
    while True:
        if isinstance(item, list):
            if not item:
                return None
            item = item[0]
        elif isinstance(item, str):
            try:
                item = json.loads(item)
            except (ValueError, TypeError):
                return None
        else:
            return item if isinstance(item, dict) else None

def redis_lrange():
    """Get all reports from Redis list (newest first)."""
    result = redis_request("GET", f"/lrange/{REDIS_KEY}/0/{MAX_HISTORY - 1}")
    reports = []
    for item in result.get("result", []):
        report = _normalize_report(item)
        if report is not None:
            reports.append(report)
    return reports

def sanitize_text(text, max_length):
    """Sanitize text input: trim and limit length."""
    if not isinstance(text, str):
        return ''
    return text.strip()[:max_length]

def validate_report(data):
    """Validate report data. Returns (is_valid, error_message, sanitized_data)."""
    if not data:
        return False, 'No JSON data provided', None
    
    required_fields = ['area', 'severity', 'latitude', 'longitude']
    for field in required_fields:
        if field not in data:
            return False, f'Missing required field: {field}', None
    
    area = sanitize_text(data['area'], 100)
    if not area:
        return False, 'Area name is required', None
    
    severity = data['severity'].strip().lower() if isinstance(data['severity'], str) else ''
    valid_severities = ['low', 'medium', 'high']
    if severity not in valid_severities:
        return False, 'Invalid severity. Must be Low, Medium or High', None
    
    try:
        latitude = float(data['latitude'])
        longitude = float(data['longitude'])
    except (ValueError, TypeError):
        return False, 'Latitude and longitude must be valid numbers', None
    
    if not (-90 <= latitude <= 90):
        return False, 'Latitude must be between -90 and 90', None
    if not (-180 <= longitude <= 180):
        return False, 'Longitude must be between -180 and 180', None
    
    description = sanitize_text(data.get('description', ''), 300)
    
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
    
    photo = data.get('photo')
    if source == 'manual':
        if not photo or not isinstance(photo, str) or not photo.startswith('data:image/'):
            return False, 'A valid photo data URL is required for manual reports', None
        sanitized['photo'] = photo
    elif photo and isinstance(photo, str) and photo.startswith('data:image/'):
        sanitized['photo'] = photo
    
    return True, None, sanitized

def make_etag(data):
    """Generate ETag from data."""
    return hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/report_pothole', methods=['POST'])
def report_pothole():
    if request.content_length and request.content_length > 300 * 1024:
        return jsonify({'error': 'Request too large (max 300KB)'}), 413
    
    data = request.get_json()
    
    is_valid, error, sanitized = validate_report(data)
    if not is_valid:
        return jsonify({'error': error}), 400
    
    # Add UTC ISO timestamp and unique id
    current_time = datetime.now(timezone.utc).isoformat()
    report = {
        'id': uuid.uuid4().hex,
        'time': current_time,
        'area': sanitized['area'],
        'severity': sanitized['severity'],
        'latitude': sanitized['latitude'],
        'longitude': sanitized['longitude'],
        'description': sanitized['description'],
        'source': sanitized['source']
    }
    if 'photo' in sanitized:
        report['photo'] = sanitized['photo']
    
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
    
    # Strip photo from the list response; include has_photo flag
    safe_list = []
    for r in history_list:
        r = dict(r)
        r['has_photo'] = bool(r.get('photo'))
        r.pop('photo', None)
        safe_list.append(r)
    
    # Generate ETag
    etag = make_etag(safe_list)
    
    # Check If-None-Match header
    if_none_match = request.headers.get('If-None-Match')
    if if_none_match and if_none_match.strip('"') == etag:
        return '', 304
    
    response = jsonify(safe_list)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['ETag'] = f'"{etag}"'
    return response

@app.route('/photo/<report_id>')
def get_photo(report_id):
    report = None
    if USE_REDIS:
        try:
            history_list = redis_lrange()
            report = next((r for r in history_list if r.get('id') == report_id), None)
        except Exception as e:
            app.logger.error(f"Redis error, falling back to memory: {e}")
    if report is None:
        with history_lock:
            report = next((r for r in pothole_history if r.get('id') == report_id), None)
    if report is None or not report.get('photo'):
        return jsonify({'error': 'Photo not found'}), 404
    photo = report['photo']
    try:
        header, b64 = photo.split(',', 1)
        mime = header.split(':')[1].split(';')[0] if ':' in header else 'image/jpeg'
        import base64
        return Response(base64.b64decode(b64), mimetype=mime)
    except Exception:
        return Response(photo, mimetype='text/plain')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)