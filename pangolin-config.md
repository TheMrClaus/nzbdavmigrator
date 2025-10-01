# Pangolin Reverse Proxy Configuration for NZBDAVMigrator

## Issue
NZBDAVMigrator works on `http://95.216.9.228:9999` but not through Pangolin reverse proxy.

## Common Solutions

### 1. Network Connectivity
Make sure Pangolin can reach the NZBDAVMigrator container:

**Option A: Same Docker Network**
```yaml
# In your docker-compose.yml, add to networks section
networks:
  - pangolin_network  # or whatever network Pangolin uses

# Or connect to existing network
docker network connect pangolin_network nzbdav-migrator
```

**Option B: Use Container Name**
In Pangolin config, use the container name instead of localhost:
- Instead of: `http://localhost:9999`
- Use: `http://nzbdav-migrator:9999`

### 2. Pangolin Configuration Examples

**Basic HTTP Proxy:**
```yaml
# Pangolin config
upstream:
  - name: nzbdav
    target: http://nzbdav-migrator:9999

routes:
  - path: /nzbdav
    upstream: nzbdav
    strip_prefix: true
```

**With Custom Domain:**
```yaml
# If using custom domain like nzbdav.yourdomain.com
routes:
  - host: nzbdav.yourdomain.com
    upstream: nzbdav
```

### 3. Docker Compose Network Fix

Add this to your docker-compose.yml:

```yaml
services:
  nzbdav-migrator:
    # ... existing config ...
    networks:
      - default
      - pangolin_network

networks:
  pangolin_network:
    external: true
    name: pangolin_pangolin_network  # Adjust to match Pangolin's network name
```

### 4. Common Issues & Solutions

**Issue: 502 Bad Gateway**
- Check if containers are on same network
- Verify target URL in Pangolin config
- Check container logs: `docker-compose logs nzbdav-migrator`

**Issue: 404 Not Found**
- Check path mapping in Pangolin
- Verify route configuration
- Check if `strip_prefix` is needed

**Issue: Connection Refused**
- Verify container is running: `docker ps | grep nzbdav`
- Check if port 9999 is exposed within Docker network
- Test direct container access: `docker exec pangolin_container curl http://nzbdav-migrator:9999`

### 5. Debugging Steps

1. **Check Networks:**
   ```bash
   docker network ls
   docker network inspect pangolin_pangolin_network
   ```

2. **Test Container Connectivity:**
   ```bash
   # From Pangolin container
   docker exec -it pangolin curl http://nzbdav-migrator:9999
   ```

3. **Check Pangolin Logs:**
   ```bash
   docker logs pangolin
   ```

4. **Verify NZBDAVMigrator is accessible:**
   ```bash
   curl http://nzbdav-migrator:9999  # From another container on same network
   ```

## Recommended Configuration

Since you have many containers running, use Docker networks:

```yaml
# Add to your docker-compose.yml
version: '3.8'

services:
  nzbdav-migrator:
    build: .
    container_name: nzbdav-migrator
    ports:
      - "9999:9999"  # Keep for direct access if needed
    volumes:
      - /opt/nzbdav/db.sqlite:/app/nzbdav_source.sqlite:ro
      - ./data:/app/data
    environment:
      - NZB_DB=/app/nzbdav_source.sqlite
    restart: unless-stopped
    networks:
      - default
      - proxy_network  # Add to Pangolin's network

networks:
  proxy_network:
    external: true
    name: pangolin_pangolin_network  # Adjust name as needed
```

Then configure Pangolin to route to `http://nzbdav-migrator:9999`.