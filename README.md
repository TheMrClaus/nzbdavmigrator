# NZBDAV Migrator

NZBDAV Migrator helps you migrating from nzbdav by reading your existing nzbdav SQLite database and coordinating with Radarr/Sonarr over their APIs.

## Features
- Web dashboard to review and queue items stored in the nzbdav database
- Tracks processed items separately in `/app/data` so you can pick up where you left off
- Optional Radarr/Sonarr integration to trigger re-downloads automatically
- Health check and restart policy baked into the provided Docker Compose file

## Prerequisites
- Docker Engine 20.10+ and the `docker compose` plugin (or legacy `docker-compose`)
- A copy of your nzbdav SQLite database on the host (defaults to `/opt/nzbdav/db.sqlite`)
- Two external Docker networks named `mediaserver` and `infra`, or update `compose.yml` to match your environment

Create the external networks once if you do not already have them:

```bash
docker network create mediaserver
docker network create infra
```

## Quick Start (Docker Compose)
1. From the project root (`/opt/nzbdavmigrator`), ensure a persistent data directory exists:
   ```bash
   mkdir -p data
   ```
2. Review `compose.yml` and adjust any environment variables, volume paths, or networks as needed. The most important value is `NZB_DB`, which should point at the mounted nzbdav database inside the container.
3. Build and launch the container:
   ```bash
   docker compose -f compose.yml up --build -d
   ```
4. Open the web UI at `http://localhost:9999` (or replace `localhost` with your host IP). Configure Radarr/Sonarr endpoints through the UI if you did not set them via environment variables.

## Alternative: helper script
If you prefer the legacy `docker-compose` workflow, the repository includes `docker-run.sh` which wraps the same steps:

```bash
chmod +x docker-run.sh
./docker-run.sh
```

The script creates the `data/` directory, warns if the nzbdav source database is missing, builds the image, and runs `docker-compose up --build -d`.

## Configuration
The container accepts the following environment variables (see `compose.yml` for examples):

- `NZB_DB` – absolute path to the nzbdav SQLite file inside the container (defaults to `/app/nzbdav_source.sqlite`).
- `RADARR_URL`, `RADARR_API_KEY` – optional Radarr connection details.
- `SONARR_URL`, `SONARR_API_KEY` – optional Sonarr connection details.
- `BATCH_SIZE` – number of items processed per run (default `10`).
- `MAX_BATCH_SIZE` – hard upper limit to prevent overload (default `50`).
- `API_DELAY` – delay in seconds between external API calls (default `2.0`).
- `PORT` and `HOST` – web server binding, default `9999`/`0.0.0.0`.
- `TZ` – container timezone (example: `Europe/Madrid`).

Persisted app state (including `nzbdav_status.db` and configuration snapshots) lives under the `data/` volume mount. The nzbdav source database is mounted read-only by default to avoid accidental modifications.

## Managing the Container
- View live logs:
  ```bash
  docker compose -f compose.yml logs -f
  ```
- Restart after configuration changes:
  ```bash
  docker compose -f compose.yml restart
  ```
- Stop and remove the stack:
  ```bash
  docker compose -f compose.yml down
  ```
- Enter the container shell (for troubleshooting):
  ```bash
  docker compose -f compose.yml exec nzbdav-migrator bash
  ```

## Updating
Pull the latest repository changes, review `compose.yml` for new options, then rebuild:

```bash
git pull
docker compose -f compose.yml up --build -d
```

