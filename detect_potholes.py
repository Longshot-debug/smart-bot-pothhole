import cv2
import requests
import time
import random
import os

API_URL = os.environ.get('API_URL', 'http://localhost:5000/report_pothole')

# Sample areas around Ludhiana for simulated detection
LUDHIANA_AREAS = [
    {"name": "Civil Lines", "lat": 30.900965, "lon": 75.857277},
    {"name": "Model Town", "lat": 30.915678, "lon": 75.842156},
    {"name": "Sarabha Nagar", "lat": 30.892345, "lon": 75.871234},
    {"name": "BRS Nagar", "lat": 30.878912, "lon": 75.856789},
    {"name": "Dugri", "lat": 30.865432, "lon": 75.889012},
    {"name": "Gill Road", "lat": 30.908765, "lon": 75.863421},
    {"name": "Ferozepur Road", "lat": 30.899876, "lon": 75.845678},
    {"name": "Jalandhar Bypass", "lat": 30.923456, "lon": 75.834567},
]

SEVERITIES = ["high", "medium", "low"]

def simulate_detection():
    """Simulate a pothole detection with random location and severity."""
    area = random.choice(LUDHIANA_AREAS)
    severity = random.choice(SEVERITIES)
    
    # Add small random offset to coordinates
    lat = area["lat"] + random.uniform(-0.005, 0.005)
    lon = area["lon"] + random.uniform(-0.005, 0.005)
    
    return {
        "area": area["name"],
        "severity": severity,
        "latitude": round(lat, 6),
        "longitude": round(lon, 6)
    }

def post_detection(detection):
    """POST detection to the Flask API."""
    try:
        response = requests.post(API_URL, json=detection, timeout=5)
        if response.status_code == 201:
            data = response.json()
            print(f"✓ Detection reported: {detection['area']} ({detection['severity'].upper()}) at {data['report']['time']}")
            return True
        else:
            print(f"✗ Failed: {response.status_code} - {response.text}")
            return False
    except requests.exceptions.ConnectionError:
        print(f"✗ Connection failed - Is the server running at {API_URL}?")
        return False
    except Exception as e:
        print(f"✗ Error: {e}")
        return False

def run_detection_loop(interval=10):
    """Run continuous detection simulation."""
    print(f"Starting pothole detection simulation (interval: {interval}s)")
    print(f"Posting to: {API_URL}")
    print("Press Ctrl+C to stop\n")
    
    try:
        while True:
            detection = simulate_detection()
            post_detection(detection)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n\nDetection stopped by user.")

def run_single_detection():
    """Run a single detection (for testing)."""
    detection = simulate_detection()
    print(f"Simulated detection: {detection}")
    post_detection(detection)

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--loop":
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        run_detection_loop(interval)
    else:
        run_single_detection()

# TODO: Replace simulate_detection() with actual OpenCV detection logic
# Example structure for real implementation:
#
# def detect_potholes_from_camera(camera_index=0):
#     cap = cv2.VideoCapture(camera_index)
#     
#     # Load your trained model (YOLO, SSD, custom CNN, etc.)
#     # model = cv2.dnn.readNetFromONNX("pothole_model.onnx")
#     
#     while True:
#         ret, frame = cap.read()
#         if not ret:
#             break
#         
#         # Preprocess frame
#         # blob = cv2.dnn.blobFromImage(frame, ...)
#         # model.setInput(blob)
#         # detections = model.forward()
#         
#         # Process detections
#         # for detection in detections:
#         #     confidence = detection[2]
#         #     if confidence > 0.5:
#         #         # Get bbox, class, etc.
#         #         # Map to GPS coordinates (requires GPS integration)
#         #         # Determine severity based on size/depth
#         #         post_detection({...})
#         
#         cv2.imshow('Pothole Detection', frame)
#         if cv2.waitKey(1) & 0xFF == ord('q'):
#             break
#     
#     cap.release()
#     cv2.destroyAllWindows()