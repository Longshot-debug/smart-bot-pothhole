import cv2
import serial
import requests
import time
import numpy as np
import random

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================
ARDUINO_PORT = 'COM3'  # Update to your Arduino port
BAUD_RATE = 9600

# THE FIX: Pointing to your actual live website
WEBSITE_URL = 'https://smart-bot-pot.vercel.app/report_pothole' # <--- PASTE YOUR ACTUAL WEBSITE LINK HERE

# Virtual Line Position (Y-coordinate on the screen)
LINE_Y = 240

# ==========================================
# 2. FETCH REAL LAPTOP LOCATION (VIA WI-FI/IP)
# ==========================================
print("Fetching real location coordinates via Wi-Fi...")
try:
    loc_data = requests.get('http://ip-api.com/json/').json()
    base_lat = loc_data.get('lat', 28.6139)
    base_lon = loc_data.get('lon', 77.2090)
    print(f"✅ Base Location found! Lat: {base_lat}, Lon: {base_lon}")
except Exception as e:
    print("⚠️ Could not fetch live location. Using default coordinates.")
    base_lat, base_lon = 28.6139, 77.2090

# Set the current location markers
current_lat, current_lon = base_lat, base_lon

# ==========================================
# 3. CONNECT TO ARDUINO
# ==========================================
try:
    arduino = serial.Serial(ARDUINO_PORT, BAUD_RATE, timeout=1)
    time.sleep(2)
    print(f"✅ Connected to Arduino on {ARDUINO_PORT}")
except Exception as e:
    print(f"❌ ERROR: Could not connect to Arduino. ({e})")
    arduino = None

# ==========================================
# 4. WEBCAM & VISION LOGIC
# ==========================================
cap = cv2.VideoCapture(2)
last_status = "clear"    

print("Starting Smart Pothole Vision System...")

while True:
    ret, frame = cap.read()
    if not ret:
        break

    # Grayscale & Binary Threshold
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    line_broken = False

    # Draw the default blue virtual line
    cv2.line(frame, (0, LINE_Y), (frame.shape[1], LINE_Y), (255, 0, 0), 2)

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area > 500:
            x, y, w, h = cv2.boundingRect(cnt)
            cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
           
            # --- LINE BREAKING LOGIC ---
            if y < LINE_Y and (y + h) > LINE_Y:
                line_broken = True
               
                # Highlight in red when it touches the line
                cv2.putText(frame, "POTHOLE TRIGGERED", (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
                cv2.line(frame, (0, LINE_Y), (frame.shape[1], LINE_Y), (0, 0, 255), 4)
                cv2.putText(frame, "LINE BROKEN!", (10, LINE_Y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
            else:
                # Green text before it hits the line
                cv2.putText(frame, "POTHOLE APPROACHING", (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

    # Trigger the system only if the line is actually broken
    current_status = "pothole" if line_broken else "clear"

  # ==========================================
    # 5. SEND DATA WITH RANDOMIZED LOCATION
    # ==========================================
    if current_status != last_status:
       
        if current_status == "pothole":
            # Shift the location slightly for every new pothole (approx max 100 meters)
            current_lat += random.uniform(-0.0009, 0.0009)
            current_lon += random.uniform(-0.0009, 0.0009)

            payload = {
                "status": current_status,
                "line_broken": line_broken,
                "location": {
                    "latitude": round(current_lat, 6),
                    "longitude": round(current_lon, 6)
                }
            }

            print(f"Action: Pothole on Line! Sending 'P' & Location ({round(current_lat, 5)}, {round(current_lon, 5)})")
            if arduino:
                arduino.write(b'P')
            try:
                # INCREASED TIMEOUT TO 5 SECONDS FOR LIVE WEBSITES
                response = requests.post(WEBSITE_URL, json=payload, timeout=5)
                print(f"Website Reply: {response.status_code} - {response.text}")
            except Exception as e:
                print(f"Website Warning: {e}")
       
        else:
            payload = {
                "status": current_status,
                "line_broken": line_broken,
                "location": {
                    "latitude": round(current_lat, 6),
                    "longitude": round(current_lon, 6)
                }
            }

            print("Action: Road Clear! Sending 'C' to Arduino & Website")
            if arduino:
                arduino.write(b'C')
            try:
                response = requests.post(WEBSITE_URL, json=payload, timeout=5)
                print(f"Website Reply: {response.status_code}")
            except Exception as e:
                print(f"Website Warning: {e}")
           
        last_status = current_status

    # Show the live camera feed
    cv2.imshow("Smart Pothole System", frame)

    # Press 'q' to quit the program
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
if arduino:
    arduino.close()
