#!/bin/bash
echo "=== Docker Container Debug ==="

echo "1. Checking if container is running:"
docker ps | grep nzbdav-migrator

echo -e "\n2. Container status:"
docker ps

echo -e "\n3. Container logs (last 20 lines):"
docker compose logs --tail=20

echo -e "\n4. Testing if container responds internally:"
docker compose exec nzbdav-migrator python3 -c "
import urllib.request
try:
    response = urllib.request.urlopen('http://localhost:9999/', timeout=5)
    print(f'✓ Internal access works: {response.getcode()}')
except Exception as e:
    print(f'✗ Internal access failed: {e}')
" 2>/dev/null || echo "Could not execute command in container"

echo -e "\n5. Checking if port is exposed:"
docker port nzbdav-migrator 2>/dev/null || echo "Container not found or no ports exposed"

echo -e "\n6. Network connectivity test:"
python3 -c "
import socket
try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    result = sock.connect_ex(('localhost', 9999))
    sock.close()
    if result == 0:
        print('✓ Port 9999 is accessible')
    else:
        print('✗ Port 9999 is not accessible')
except Exception as e:
    print(f'✗ Network test failed: {e}')
"

echo -e "\n7. Container filesystem check:"
docker compose exec nzbdav-migrator ls -la /app/ 2>/dev/null || echo "Could not list container files"

echo -e "\n8. Process check inside container:"
docker compose exec nzbdav-migrator ps aux 2>/dev/null || echo "Could not check processes"