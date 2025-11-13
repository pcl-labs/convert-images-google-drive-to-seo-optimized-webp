"""
Local testing script for the API.
Tests endpoints without requiring Cloudflare bindings.
"""

import requests
import json
import time

BASE_URL = "http://localhost:8000"

def test_health():
    """Test health endpoint."""
    print("\n=== Testing Health Endpoint ===")
    response = requests.get(f"{BASE_URL}/health")
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    return response.status_code == 200

def test_root():
    """Test root endpoint."""
    print("\n=== Testing Root Endpoint ===")
    response = requests.get(f"{BASE_URL}/")
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    return response.status_code == 200

def test_docs():
    """Test docs endpoint."""
    print("\n=== Testing Docs Endpoint ===")
    response = requests.get(f"{BASE_URL}/docs")
    print(f"Status: {response.status_code}")
    return response.status_code == 200

def test_optimize_requires_auth():
    """Test that optimize endpoint requires authentication."""
    print("\n=== Testing Optimize Endpoint (No Auth) ===")
    response = requests.post(
        f"{BASE_URL}/api/v1/optimize",
        json={
            "drive_folder": "test-folder-id"
        }
    )
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    return response.status_code == 401

def test_jobs_requires_auth():
    """Test that jobs endpoint requires authentication."""
    print("\n=== Testing Jobs Endpoint (No Auth) ===")
    response = requests.get(f"{BASE_URL}/api/v1/jobs")
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    return response.status_code == 401

def test_github_auth():
    """Test GitHub auth endpoint."""
    print("\n=== Testing GitHub Auth Endpoint ===")
    try:
        response = requests.get(f"{BASE_URL}/auth/github", allow_redirects=False)
        print(f"Status: {response.status_code}")
        if response.status_code == 302:
            print(f"Redirect URL: {response.headers.get('Location', 'N/A')}")
        else:
            print(f"Response: {json.dumps(response.json(), indent=2)}")
        return True
    except Exception as e:
        print(f"Error: {e}")
        return False

def main():
    """Run all tests."""
    print("Starting local API tests...")
    print(f"Testing API at: {BASE_URL}")
    print("\nMake sure the server is running: python run_api.py")
    
    results = []
    
    try:
        results.append(("Health", test_health()))
        results.append(("Root", test_root()))
        results.append(("Docs", test_docs()))
        results.append(("Optimize Auth Required", test_optimize_requires_auth()))
        results.append(("Jobs Auth Required", test_jobs_requires_auth()))
        results.append(("GitHub Auth", test_github_auth()))
    except requests.exceptions.ConnectionError:
        print("\n❌ ERROR: Could not connect to server.")
        print("Please start the server first: python run_api.py")
        return
    
    print("\n=== Test Results ===")
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    print(f"\nTotal: {passed}/{total} tests passed")

if __name__ == "__main__":
    main()

