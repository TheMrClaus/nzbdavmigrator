#!/bin/bash
set -e

echo "=== NZBDAVMigrator Container Starting ==="
echo "Container started at: $(date)"
echo "Environment variables:"
env | grep -E "(NZB_|RADARR_|SONARR_|PORT|HOST)" || echo "No relevant env vars set"

echo "Checking file permissions..."
ls -la /app/
ls -la /app/data/ || echo "Data directory not accessible"

echo "Ensuring data directory has correct ownership..."
# Try to fix ownership if running as root, otherwise just check permissions
if [ "$(id -u)" = "0" ]; then
    chown -R nzbuser:nzbuser /app/data/
    echo "Fixed ownership as root"
else
    echo "Running as non-root user, checking permissions..."
fi

echo "Testing config file creation..."
if [ -w /app/data ]; then
    echo "Data directory is writable"
    if [ -f /app/data/nzbdav_web_config.json ]; then
        echo "Config file exists: $(ls -la /app/data/nzbdav_web_config.json)"
    else
        echo "Config file does not exist yet - will be created on first save"
    fi
else
    echo "WARNING: Data directory is not writable!"
fi

echo "Testing Python import..."
python3 -c "import sqlite3, json, threading; print('Core modules OK')" || {
    echo "Python module test failed"
    exit 1
}

echo "Starting application..."
exec python3 nzbdav_web.py