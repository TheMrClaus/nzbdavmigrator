# Docker Deployment Guide

## Overview

The NZBDAVMigrator reads from your existing nzbdav SQLite database to get the list of movies/series, then allows you to selectively trigger re-downloads in Radarr/Sonarr.

**What it does:**
- Reads `db.sqlite` (your nzbdav database) to get the movie/series list
- Creates its own small status database (`data/nzbdav_status.db`) to track what's been processed
- Provides a web interface to select and process items
- Calls Radarr/Sonarr APIs to trigger re-downloads

## Quick Start

## Docker Compose (Recommended)

The `docker-compose.yml` file includes:
- Automatic container rebuild
- Persistent data storage
- Environment variable configuration

## Networking

Make sure nzbdavmigrator is in the same network as your Arrs

## Troubleshooting

### Database not found:
- Ensure `db.sqlite` exists on the host
- Check file permissions (readable by UID 1000)
- Verify the volume mount in docker-compose.yml

### Configuration not persisting:
- Ensure `./data` directory exists and is writable
- Check volume mount: `docker inspect nzbdav-migrator`

## Security Notes

- Container runs as non-root user (UID 1000)
- Database mounted read-only
- No sensitive data in the image
- Configuration stored in mounted volume
