"""Test server startup and basic endpoint."""
import traceback
from fastapi.testclient import TestClient

try:
    from api.main import app
    print("✓ App imported successfully")
    
    client = TestClient(app)
    print("✓ Test client created")
    
    # Test health endpoint
    try:
        response = client.get("/health")
        print(f"✓ Health endpoint: {response.status_code}")
        print(f"  Response: {response.json()}")
    except Exception as e:
        print(f"✗ Health endpoint failed: {e}")
        traceback.print_exc()
    
    # Test root endpoint
    try:
        response = client.get("/")
        print(f"✓ Root endpoint: {response.status_code}")
        print(f"  Response: {response.json()}")
    except Exception as e:
        print(f"✗ Root endpoint failed: {e}")
        traceback.print_exc()
    
    # Test protected endpoint
    try:
        response = client.post("/api/v1/optimize", json={"drive_folder": "test"})
        print(f"✓ Optimize endpoint (no auth): {response.status_code}")
        print(f"  Response: {response.json()}")
    except Exception as e:
        print(f"✗ Optimize endpoint failed: {e}")
        traceback.print_exc()
        
except Exception as e:
    print(f"✗ Failed to import app: {e}")
    traceback.print_exc()

