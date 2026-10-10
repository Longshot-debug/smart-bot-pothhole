from flask import Flask, render_template, request, jsonify, Response
from flask_cors import CORS
from collections import deque
from datetime import datetime, timezone, timedelta
import threading
import os
import json
import base64
import hashlib
import re
import time
import requests
import uuid

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

# ---------------------------------------------------------------------------
# Twilio SMS notifications
#
# Credentials are read from the environment and never leave the server. The
# frontend only ever sees the safe settings: whether alerts are on, and the
# recipient number in E.164 form.
#
# Vercel runs serverless, so there are no background threads: the SMS is sent
# inline (5 second timeout) before the report response is returned, and any
# failure is swallowed so the report is never lost or blocked.
# ---------------------------------------------------------------------------

TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '').strip()
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '').strip()
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER', '').strip()
TWILIO_MESSAGES_URL = (
    'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json'
    .format(sid=TWILIO_ACCOUNT_SID)
)

# Link printed at the bottom of every alert. Override if the dashboard moves.
PUBLIC_BASE_URL = os.environ.get(
    'PUBLIC_BASE_URL', 'https://smart-bot-pot.vercel.app'
).strip().rstrip('/')

SMS_TIMEOUT_SECONDS = 5        # never block a report for longer than this
SMS_THROTTLE_SECONDS = 300     # at most 1 SMS per recipient per 5 minutes
SMS_TEST_RATE_LIMIT = 3        # at most 3 manual test messages per hour
SMS_TEST_RATE_WINDOW = 3600
SMS_SENT_TTL = 7 * 24 * 3600   # remember a texted report for 7 days

# Only these severities are worth a text message.
SMS_SEVERITIES = ('high', 'medium')

# E.164, e.g. +919876543210.
E164_PATTERN = re.compile(r'^\+[1-9]\d{9,14}$')

# India is a fixed UTC+05:30 all year, so computing it by hand avoids needing
# a tzdata package inside Vercel's Python runtime.
IST_OFFSET = timezone(timedelta(hours=5, minutes=30))
MONTH_ABBR = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

# Twilio trial accounts refuse some destinations. Map the codes we know about so
# the dashboard can show a message a human can act on.
TWILIO_ERROR_HINTS = {
    21211: 'That phone number is not a valid phone number.',
    21408: 'Twilio cannot send SMS to that country or region for this account.',
    21606: 'The sending number is not a valid Twilio number.',
    21608: 'That number is not verified. Trial accounts can only text verified '
           'numbers - verify it in the Twilio console or upgrade the account.',
    21610: 'That number is not verified for your Twilio trial account.',
    21614: 'That number cannot receive SMS messages.',
}

# Redis keys (mirrored in memory when Redis is not configured).
SMS_SETTINGS_KEY = 'sms_settings'
SMS_SENT_PREFIX = 'sms_sent:'      # + report id
SMS_LAST_PREFIX = 'sms_last:'      # + hashed recipient
SMS_TEST_KEY = 'sms_test_log'

# In-memory fallback so local development works without Redis.
sms_memory = {
    'settings': None,
    'sent': {},       # report id -> epoch seconds
    'last': {},       # hashed recipient -> epoch seconds
    'tests': [],      # [epoch seconds, ...]
}
sms_memory_lock = threading.Lock()


def twilio_configured():
    """True only when all three Twilio env vars are present."""
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER)


# --- value helpers (keeps any payload safe inside a Redis URL path) ---------

def _encode_payload(text):
    return base64.urlsafe_b64encode(text.encode('utf-8')).decode('ascii')


def _decode_payload(token):
    try:
        return base64.urlsafe_b64decode(token.encode('ascii')).decode('utf-8')
    except Exception:
        return None


def _redis_get(key):
    return redis_request('GET', '/get/%s' % key).get('result')


def _redis_set(key, value, ttl_seconds=None):
    token = _encode_payload(value)
    if ttl_seconds:
        redis_request('POST', '/setex/%s/%d/%s' % (key, int(ttl_seconds), token))
    else:
        redis_request('POST', '/set/%s/%s' % (key, token))


def _redis_claim(report_id, ttl_seconds):
    """Atomic 'set if absent' so a report is only ever texted once."""
    key = SMS_SENT_PREFIX + report_id
    claimed = redis_request(
        'POST', '/setnx/%s/%s' % (key, _encode_payload('1'))
    ).get('result')
    if claimed != 1:
        return False
    redis_request('POST', '/expire/%s/%d' % (key, int(ttl_seconds)))
    return True


def _recipient_hash(phone):
    """Phone numbers are hashed for the throttle keys, not stored in key names."""
    return hashlib.sha256(phone.encode('utf-8')).hexdigest()[:32]


# --- settings ---------------------------------------------------------------

def _settings_from_doc(doc):
    doc = doc if isinstance(doc, dict) else {}
    phone = str(doc.get('phone') or '').strip()
    return {
        'sms_enabled': doc.get('sms_enabled') is True or doc.get('sms_enabled') == 'true',
        'phone': phone,
        'updated_at': doc.get('updated_at'),
    }


def get_sms_settings():
    """Read the alert settings: Redis first, in-memory fallback."""
    if USE_REDIS:
        try:
            raw = _redis_get(SMS_SETTINGS_KEY)
            doc = json.loads(_decode_payload(raw) or 'null') if raw else None
            return _settings_from_doc(doc)
        except Exception as e:
            app.logger.error('Redis error reading SMS settings: %s', type(e).__name__)
    with sms_memory_lock:
        return _settings_from_doc(sms_memory['settings'])


def save_sms_settings(settings):
    """Persist the alert settings. Returns False only when Redis has failed."""
    if USE_REDIS:
        try:
            _redis_set(SMS_SETTINGS_KEY, json.dumps(settings))
            return True
        except Exception as e:
            app.logger.error('Redis error saving SMS settings: %s', type(e).__name__)
            return False
    with sms_memory_lock:
        sms_memory['settings'] = settings
    return True


# --- duplicate + throttle guards --------------------------------------------

def _report_already_texted(report_id):
    """Claims a report id. Returns False when it was already texted."""
    if not report_id:
        return True
    report_id = str(report_id)

    if USE_REDIS:
        try:
            return _redis_claim(report_id, SMS_SENT_TTL)
        except Exception as e:
            app.logger.error('Redis error claiming report SMS: %s', type(e).__name__)
            return False

    with sms_memory_lock:
        if report_id in sms_memory['sent']:
            return False
        sms_memory['sent'][report_id] = time.time()
        return True


def _recipient_throttled(phone):
    """True when this recipient already got an SMS in the last 5 minutes."""
    now = time.time()
    token = _recipient_hash(phone)

    if USE_REDIS:
        try:
            raw = _redis_get(SMS_LAST_PREFIX + token)
            if raw and (now - float(_decode_payload(raw))) < SMS_THROTTLE_SECONDS:
                return True
            return False
        except Exception as e:
            app.logger.error('Redis error reading SMS throttle: %s', type(e).__name__)
            return True

    with sms_memory_lock:
        return (now - sms_memory['last'].get(token, 0)) < SMS_THROTTLE_SECONDS


def _mark_recipient_sent(phone):
    token = _recipient_hash(phone)
    stamp = _encode_payload('%.3f' % time.time())

    if USE_REDIS:
        try:
            _redis_set(SMS_LAST_PREFIX + token, stamp, SMS_THROTTLE_SECONDS)
            return
        except Exception as e:
            app.logger.error('Redis error writing SMS throttle: %s', type(e).__name__)
            return

    with sms_memory_lock:
        sms_memory['last'][token] = time.time()


def _record_test_attempt():
    """Returns True while the manual test button is still under 3 per hour."""
    now = time.time()

    if USE_REDIS:
        try:
            raw = _redis_get(SMS_TEST_KEY)
            stamps = json.loads(_decode_payload(raw) or '[]')
            stamps = [s for s in stamps
                      if isinstance(s, (int, float)) and (now - s) < SMS_TEST_RATE_WINDOW]
            if len(stamps) >= SMS_TEST_RATE_LIMIT:
                return False
            stamps.append(now)
            _redis_set(SMS_TEST_KEY, json.dumps(stamps), SMS_TEST_RATE_WINDOW * 2)
            return True
        except Exception as e:
            app.logger.error('Redis error recording SMS test: %s', type(e).__name__)
            return False

    with sms_memory_lock:
        stamps = [s for s in sms_memory['tests']
                  if (now - s) < SMS_TEST_RATE_WINDOW]
        if len(stamps) >= SMS_TEST_RATE_LIMIT:
            sms_memory['tests'] = stamps
            return False
        stamps.append(now)
        sms_memory['tests'] = stamps
        return True


# --- message -----------------------------------------------------------------

def format_ist_label(iso_time):
    """e.g. "9 Oct, 11:54 PM IST" from an ISO timestamp."""
    dt = None
    if iso_time:
        try:
            dt = datetime.fromisoformat(str(iso_time).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            dt = None
    if dt is None:
        return 'Unknown time'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    local = dt.astimezone(IST_OFFSET)
    hour = local.hour % 12 or 12
    meridiem = 'AM' if local.hour < 12 else 'PM'
    return '%d %s, %d:%02d %s IST' % (
        local.day, MONTH_ABBR[local.month - 1], hour, local.minute, meridiem
    )


def _coord_text(value):
    try:
        return '%.6f' % float(value)
    except (ValueError, TypeError):
        return '0'


def build_sms_body(area, severity, latitude, longitude, reported_at=None):
    """Plain ASCII body - Twilio rejects most non-ASCII content."""
    location = sanitize_text(area, 60) or 'Unknown area'
    lines = (
        'POTHOLE ALERT',
        'Severity: %s' % str(severity or '').upper(),
        'Location: %s' % location,
        'Time: %s' % format_ist_label(reported_at),
        'Map: https://maps.google.com/?q=%s,%s'
        % (_coord_text(latitude), _coord_text(longitude)),
        'Report: %s' % PUBLIC_BASE_URL,
    )
    return '\n'.join(lines).encode('ascii', 'ignore').decode('ascii')


# --- Twilio REST call --------------------------------------------------------

def twilio_send_sms(to_phone, body):
    """
    Send one SMS through the Twilio REST API using plain requests.

    Returns (ok, detail). Never raises, and never logs the credentials.
    """
    if not twilio_configured():
        return False, {
            'status': 'skipped',
            'code': 'not_configured',
            'message': 'Twilio credentials are not set on the server.',
        }

    payload = {
        'From': TWILIO_PHONE_NUMBER,
        'To': to_phone,
        'Body': body,
    }

    try:
        response = requests.post(
            TWILIO_MESSAGES_URL,
            data=payload,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=SMS_TIMEOUT_SECONDS,
        )
    except Exception as e:
        app.logger.error('Twilio request failed: %s', type(e).__name__)
        return False, {
            'status': 'failed',
            'code': 'twilio_unreachable',
            'message': 'Could not reach Twilio. Please try again shortly.',
        }

    if 200 <= response.status_code < 300:
        return True, {'status': 'sent'}

    detail = twilio_error_detail(response)
    app.logger.error('Twilio rejected the SMS: http=%s code=%s message=%s',
                     response.status_code,
                     detail.get('code'),
                     detail.get('message'))
    return False, detail


def twilio_error_detail(response):
    """Turn a Twilio error response into {code, message} safe to show."""
    code = None
    message = None
    try:
        data = response.json()
        if isinstance(data, dict):
            code = data.get('code')
            message = data.get('message')
    except Exception:
        data = None

    if code is None:
        code = 'http_%s' % response.status_code
    if not message:
        message = (response.text or '')[:160] or 'Twilio returned an error.'
    if isinstance(code, int) and code in TWILIO_ERROR_HINTS:
        message = TWILIO_ERROR_HINTS[code]
    return {'status': 'failed', 'code': code, 'message': message}


# --- report alerts -----------------------------------------------------------

def maybe_send_report_sms(report):
    """
    Alert the saved recipient about a new report. Called only after the report
    has been stored. Never raises and never blocks for more than the timeout:
    the caller returns the report either way.
    """
    try:
        settings = get_sms_settings()
        if not settings.get('sms_enabled'):
            return {'status': 'skipped', 'reason': 'disabled'}

        phone = str(settings.get('phone') or '').strip()
        if not phone or not E164_PATTERN.match(phone):
            return {'status': 'skipped', 'reason': 'invalid_recipient'}

        severity = str(report.get('severity') or '').lower()
        if severity not in SMS_SEVERITIES:
            return {'status': 'skipped', 'reason': 'low_severity'}

        if not _report_already_texted(report.get('id')):
            return {'status': 'skipped', 'reason': 'duplicate'}

        if _recipient_throttled(phone):
            return {'status': 'skipped', 'reason': 'throttled'}

        body = build_sms_body(
            report.get('area'),
            report.get('severity'),
            report.get('latitude'),
            report.get('longitude'),
            report.get('time'),
        )

        ok, detail = twilio_send_sms(phone, body)
        if ok:
            _mark_recipient_sent(phone)
            return {'status': 'sent'}
        return detail
    except Exception as e:
        app.logger.error('SMS alert pipeline error: %s', type(e).__name__)
        return {'status': 'skipped', 'reason': 'internal_error'}

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
    
    # Text the saved recipient. Runs inline (Vercel has no background threads)
    # and is fully guarded: the report above is already stored, and this can
    # neither block it for long nor lose it.
    sms_result = maybe_send_report_sms(report)
    
    return jsonify({'status': 'success', 'report': report, 'sms': sms_result}), 201


@app.route('/sms_settings', methods=['GET'])
def sms_settings_get():
    """Return the safe alert settings for the logged-out dashboard."""
    return jsonify(get_sms_settings())


@app.route('/sms_settings', methods=['POST'])
def sms_settings_post():
    """Store the alert toggle and recipient number (E.164) server-side."""
    data = request.get_json(silent=True) or {}
    enabled = data.get('sms_enabled') is True or data.get('sms_enabled') == 'true'
    phone = str(data.get('phone') or '').strip()

    if enabled and not E164_PATTERN.match(phone):
        return jsonify({
            'error': 'Enter a valid phone number in international format, '
                     'e.g. +919876543210.'
        }), 400

    settings = {
        'sms_enabled': enabled,
        'phone': phone if enabled else '',
        'updated_at': datetime.now(timezone.utc).isoformat(),
    }

    if not save_sms_settings(settings):
        return jsonify({'error': 'Could not save the settings on the server.'}), 500

    return jsonify({'status': 'success', 'settings': settings})


@app.route('/sms_test', methods=['POST'])
def sms_test():
    """
    Send a test message to the saved recipient. Only ever triggered by the
    Settings button, never automatically. Rate limited to 3 per hour.
    """
    # Rate limit first, so repeated clicks cannot be used to probe Twilio.
    if not _record_test_attempt():
        return jsonify({
            'status': 'failed',
            'code': 'rate_limited',
            'message': 'Too many test messages. Try again in an hour.',
        }), 429

    settings = get_sms_settings()
    phone = str(settings.get('phone') or '').strip()

    if not E164_PATTERN.match(phone):
        return jsonify({
            'status': 'failed',
            'code': 'invalid_recipient',
            'message': 'Save a valid phone number in international format '
                       '(e.g. +919876543210) first.',
        }), 400

    if not twilio_configured():
        # Nothing to send with, and nothing to complain about either.
        return jsonify({
            'status': 'skipped',
            'code': 'not_configured',
            'message': 'Twilio is not configured on the server, so no message '
                       'was sent.',
        }), 200

    body = '\n'.join((
        'POTHOLE ALERT',
        'This is a test message from your Pothole Detection dashboard.',
        'Report: %s' % PUBLIC_BASE_URL,
    )).encode('ascii', 'ignore').decode('ascii')

    ok, detail = twilio_send_sms(phone, body)
    if not ok:
        return jsonify(detail), 502
    return jsonify({'status': 'sent'})

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