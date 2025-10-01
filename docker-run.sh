#!/bin/bash
# Quick start script for Docker deployment

set -e

# Create data directory for persistent storage
mkdir -p ./data

# Check if database exists
if [ ! -f "/opt/nzbdav/db.sqlite" ]; then
    echo "Warning: Database not found at /opt/nzbdav/db.sqlite"
    echo "Please ensure the nzbdav database is available at that location"
    echo "You can also modify the docker-compose.yml to point to a different location"
fi

echo "Building and starting NZBDAVMigrator Docker container..."

# Build and start the container
docker-compose up --build -d

echo ""
echo "✓ Container started successfully!"
echo ""
echo "Access the web interface at: http://localhost:9999"
echo "or http://YOUR_SERVER_IP:9999"
echo ""
echo "Useful commands:"
echo "  View logs:    docker-compose logs -f"
echo "  Stop:         docker-compose down"
echo "  Restart:      docker-compose restart"
echo "  Status:       docker-compose ps"
echo ""
echo "Configuration files are stored in ./data/"