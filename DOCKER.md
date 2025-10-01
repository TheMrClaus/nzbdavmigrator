# Docker Deployment Guide

## Overview

The NZBDAVMigrator reads from your existing nzbdav SQLite database to get the list of movies/series, then allows you to selectively trigger re-downloads in Radarr/Sonarr.

**What it does:**
- Reads `/opt/nzbdav/db.sqlite` (your nzbdav database) to get the movie/series list
- Creates its own small status database (`data/nzbdav_status.db`) to track what's been processed
- Provides a web interface to select and process items
- Calls Radarr/Sonarr APIs to trigger re-downloads

## Quick Start

1. **Build and run with Docker Compose:**
   ```bash
   chmod +x docker-run.sh
   ./docker-run.sh
   ```

2. **Access the web interface:**
   - http://localhost:9999
   - http://YOUR_SERVER_IP:9999

## Manual Docker Commands

### Build the image:
```bash
docker build -t nzbdav-migrator .
```

### Run the container:
```bash
docker run -d \
  --name nzbdav-migrator \
  -p 9999:9999 \
  -v /opt/nzbdav/db.sqlite:/app/nzbdav_source.sqlite:ro \
  -v $(pwd)/data:/app/data \
  -e NZB_DB=/app/nzbdav_source.sqlite \
  nzbdav-migrator
```

## Docker Compose (Recommended)

The `docker-compose.yml` file includes:
- Automatic container rebuild
- Health checks
- Persistent data storage
- Environment variable configuration

### Environment Variables

You can configure the application using environment variables in `docker-compose.yml`:

```yaml
environment:
  - NZB_DB=/app/nzbdav_source.sqlite
  - RADARR_URL=http://radarr:7878
  - RADARR_API_KEY=your_radarr_api_key
  - SONARR_URL=http://sonarr:8989
  - SONARR_API_KEY=your_sonarr_api_key
  - BATCH_SIZE=10
  - MAX_BATCH_SIZE=50
  - API_DELAY=2.0
  - PORT=9999
  - HOST=0.0.0.0
```

## Volume Mounts

- **nzbdav Database**: `/opt/nzbdav/db.sqlite:/app/nzbdav_source.sqlite:ro` (read-only source data)
- **App Data**: `./data:/app/data` (config, status tracking database, logs)

## Useful Commands

```bash
# View logs
docker-compose logs -f

# Stop the container
docker-compose down

# Restart the container
docker-compose restart

# Check container status
docker-compose ps

# Enter the container for debugging
docker-compose exec nzbdav-migrator bash

# Update and rebuild
docker-compose down
docker-compose up --build -d
```

## Health Checks

The container includes health checks that verify:
- Web server is responding
- API endpoints are accessible
- Application is functioning correctly

## Networking

If running with other services (Radarr, Sonarr), consider:

1. **Using Docker networks:**
   ```yaml
   networks:
     - media
   ```

2. **Connecting to existing containers:**
   ```bash
   docker network connect existing_network nzbdav-migrator
   ```

## Troubleshooting

### Database not found:
- Ensure `/opt/nzbdav/db.sqlite` exists on the host
- Check file permissions (readable by UID 1000)
- Verify the volume mount in docker-compose.yml

### Cannot access web interface:
- Check if port 9999 is exposed: `docker port nzbdav-migrator`
- Verify firewall settings: `sudo ufw allow 9999`
- Check container logs: `docker-compose logs`

### Configuration not persisting:
- Ensure `./data` directory exists and is writable
- Check volume mount: `docker inspect nzbdav-migrator`

## Security Notes

- Container runs as non-root user (UID 1000)
- Database mounted read-only
- No sensitive data in the image
- Configuration stored in mounted volume

## Production Considerations

1. **Use a reverse proxy** (nginx example included in docker-compose.yml)
2. **Enable SSL/TLS** for external access
3. **Set up log rotation** for container logs
4. **Configure backup** for the `./data` directory
5. **Monitor container health** and restart policies