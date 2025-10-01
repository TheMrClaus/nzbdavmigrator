#!/bin/bash
echo "Fixing permissions for Docker container..."

# Check current ownership
echo "Current data directory ownership:"
ls -la data/

# Fix ownership for container user (UID 1000)
echo "Changing ownership to UID 1000 (nzbuser)..."
chown -R 1000:1000 data/

# Make sure directory is writable
chmod -R 755 data/

echo "Fixed permissions:"
ls -la data/

echo "Done! Now restart the container:"
echo "docker-compose down && docker-compose up -d"