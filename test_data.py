import requests
import time
import os

# Test pothole data around Ludhiana
TEST_POTHOLES = [
    {
        "area": "Civil Lines",
        "severity": "high",
        "latitude": 30.900965,
        "longitude": 75.857277
    },
    {
        "area": "Model Town",
        "severity": "medium",
        "latitude": 30.915678,
        "longitude": 75.842156
    },
    {
        "area": "Sarabha Nagar",
        "severity": "low",
        "latitude": 30.892345,
        "longitude": 75.871234
    },
    {
        "area": "BRS Nagar",
        "severity": "high",
        "latitude": 30.878912,
        "longitude": 75.856789
    },
    {
        "area": "Dugri",
        "severity": "medium",
        "latitude": 30.865432,
        "longitude": 75.889012
    }
]

API_URL = os.environ.get('API_URL', 'http://localhost:5000/report_pothole')

def post_potholes():
    print(f"Posting {len(TEST_POTHOLES)} test potholes to {API_URL}...\n")
    
    for i, pothole in enumerate(TEST_POTHOLES, 1):
        try:
            response = requests.post(API_URL, json=pothole, timeout=5)
            if response.status_code == 201:
                data = response.json()
                print(f"✓ [{i}/{len(TEST_POTHOLES)}] Posted: {pothole['area']} ({pothole['severity'].upper()}) at {data['report']['time']}")
            else:
                print(f"✗ [{i}/{len(TEST_POTHOLES)}] Failed: {response.status_code} - {response.text}")
        except requests.exceptions.ConnectionError:
            print(f"✗ [{i}/{len(TEST_POTHOLES)}] Connection failed - Is the server running at {API_URL}?")
            break
        except Exception as e:
            print(f"✗ [{i}/{len(TEST_POTHOLES)}] Error: {e}")
        
        time.sleep(0.5)  # Small delay between posts
    
    print(f"\nDone! Check the dashboard at {API_URL.replace('/report_pothole', '')}")

if __name__ == "__main__":
    post_potholes()