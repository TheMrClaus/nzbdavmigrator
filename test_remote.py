#!/usr/bin/env python3
"""
Test script for remote access to the web server
"""

import urllib.request
import urllib.error
import socket

def test_port_open(host, port):
    """Test if port is open from network perspective"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception as e:
        print(f"Socket test error: {e}")
        return False

def test_remote_access(host, port=9999):
    """Test remote access to the server"""
    print(f"=== Testing Remote Access to {host}:{port} ===")

    # Test if port is open
    print(f"Testing if port {port} is open...")
    if test_port_open(host, port):
        print("✓ Port is open and accepting connections")
    else:
        print("✗ Port appears to be closed or blocked")
        print("This could be due to:")
        print("  - Firewall blocking the port")
        print("  - Server not actually binding to external interface")
        print("  - Network routing issues")
        return False

    # Test HTTP access
    url = f"http://{host}:{port}/"
    print(f"Testing HTTP access to {url}")

    try:
        response = urllib.request.urlopen(url, timeout=10)
        print(f"✓ HTTP request successful: {response.getcode()}")

        content = response.read(500).decode('utf-8', errors='ignore')
        if "NZBDAVMigrator" in content:
            print("✓ Server is serving the correct application")
            return True
        else:
            print("⚠ Server responded but content doesn't look right")
            return False

    except urllib.error.URLError as e:
        print(f"✗ HTTP request failed: {e}")
        return False
    except Exception as e:
        print(f"✗ Unexpected error: {e}")
        return False

if __name__ == "__main__":
    # Test the specific server
    success = test_remote_access("95.216.9.228", 9999)

    if not success:
        print("\n=== Troubleshooting Steps ===")
        print("1. Check if firewall is blocking port 9999:")
        print("   sudo ufw status")
        print("   sudo ufw allow 9999")
        print("")
        print("2. Check if server is binding to all interfaces:")
        print("   netstat -tlnp | grep 9999")
        print("   ss -tlnp | grep 9999")
        print("")
        print("3. Test from the server itself:")
        print("   curl http://localhost:9999")
        print("   curl http://0.0.0.0:9999")
        print("")
        print("4. Check server logs for any errors")