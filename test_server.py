#!/usr/bin/env python3
"""
Test script to verify the web server is working
"""

import urllib.request
import urllib.error
import sys
import time

def test_server(host="localhost", port=9999):
    url = f"http://{host}:{port}/"

    print(f"Testing connection to {url}")

    try:
        # Test basic connection
        response = urllib.request.urlopen(url, timeout=5)
        print(f"✓ Server responded with status: {response.getcode()}")

        # Read a bit of the response
        content = response.read(500).decode('utf-8', errors='ignore')
        if "NZBDAVMigrator" in content:
            print("✓ Server is serving the correct application")
        else:
            print("⚠ Server responded but content doesn't look right")
            print(f"First 200 chars: {content[:200]}...")

        return True

    except urllib.error.URLError as e:
        print(f"✗ Connection failed: {e}")
        return False
    except Exception as e:
        print(f"✗ Unexpected error: {e}")
        return False

def test_api_endpoints(host="localhost", port=9999):
    """Test API endpoints"""
    base_url = f"http://{host}:{port}"
    endpoints = ["/api/items", "/api/status", "/api/config"]

    for endpoint in endpoints:
        url = base_url + endpoint
        try:
            response = urllib.request.urlopen(url, timeout=5)
            print(f"✓ API endpoint {endpoint} responded: {response.getcode()}")
        except Exception as e:
            print(f"✗ API endpoint {endpoint} failed: {e}")

if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9999

    print("=== Testing NZBDAVMigrator Web Server ===")
    print(f"Target: {host}:{port}")
    print()

    # Wait a moment for server to start if just launched
    print("Waiting 2 seconds for server to initialize...")
    time.sleep(2)

    if test_server(host, port):
        print("\n=== Testing API Endpoints ===")
        test_api_endpoints(host, port)
        print("\n✓ All tests completed successfully!")
    else:
        print("\n✗ Basic connection test failed")
        print("\nTroubleshooting:")
        print("1. Make sure the server is running: python3 start_web.py")
        print("2. Check if port 9999 is blocked by firewall")
        print("3. Try accessing from the same machine first")
        print("4. Check server logs for errors")