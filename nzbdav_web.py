#!/usr/bin/env python3
"""
NZBDAVMigrator Web Application
A web-based interface for managing NZB migrations with Radarr/Sonarr integration.
"""

import os
import sys
import json
import sqlite3
import threading
import time
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import traceback

# Flask imports - using minimal dependencies
try:
    from urllib.parse import unquote
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from urllib.parse import urlparse, parse_qs
    import socketserver
except ImportError as e:
    print(f"Error importing required modules: {e}")
    sys.exit(1)

# Import from the existing CLI tool
try:
    from export_nzb import (
        parse_release_dir, is_series, is_movie,
        extract_series_title, extract_movie_title,
        trigger_radarr_searches, trigger_sonarr_searches,
        _api_request
    )
except ImportError:
    print("Error: Cannot import from export_nzb.py. Make sure it's in the same directory.")
    sys.exit(1)


class Config:
    """Configuration management for the web application"""

    def __init__(self):
        # Store config in data directory for Docker persistence
        config_dir = Path("data")
        config_dir.mkdir(exist_ok=True)
        self.config_file = config_dir / "nzbdav_web_config.json"

        # Ensure the data directory is writable
        if not os.access(config_dir, os.W_OK):
            print(f"Warning: Config directory {config_dir} is not writable", flush=True)
        self.defaults = {
            "database_path": os.getenv("NZB_DB", "db.sqlite"),
            "radarr_url": os.getenv("RADARR_URL", ""),
            "radarr_api_key": os.getenv("RADARR_API_KEY", ""),
            "sonarr_url": os.getenv("SONARR_URL", ""),
            "sonarr_api_key": os.getenv("SONARR_API_KEY", ""),
            "batch_size": int(os.getenv("BATCH_SIZE", "10")),
            "max_batch_size": int(os.getenv("MAX_BATCH_SIZE", "50")),
            "api_delay": float(os.getenv("API_DELAY", "2.0")),
            "sonarr_delete_whole_season": os.getenv("SONARR_DELETE_WHOLE_SEASON", "true").lower() in ("true", "1", "yes"),
            "status_db": os.getenv("STATUS_DB", "data/nzbdav_status.db"),
            "port": int(os.getenv("PORT", "9999")),
            "host": os.getenv("HOST", "0.0.0.0"),
            # Scheduled tasks
            "schedule_movies_enabled": False,
            "schedule_movies_count": 5,
            "schedule_movies_interval": 60,
            "schedule_series_enabled": False,
            "schedule_series_count": 3,
            "schedule_series_interval": 90,
            "schedule_check_found_enabled": False,
            "schedule_check_found_interval": 30
        }
        self.data = self.load()

    def load(self) -> Dict:
        """Load configuration from file"""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                merged = self.defaults.copy()
                merged.update(config)
                return merged
            except (json.JSONDecodeError, IOError) as e:
                print(f"Warning: Failed to load config: {e}")
        return self.defaults.copy()

    def save(self):
        """Save configuration to file"""
        try:
            print(f"Saving config to: {self.config_file}", flush=True)
            with open(self.config_file, 'w') as f:
                json.dump(self.data, f, indent=2)
            print(f"Config saved successfully", flush=True)
        except IOError as e:
            print(f"Warning: Failed to save config to {self.config_file}: {e}", flush=True)
            import traceback
            traceback.print_exc()

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value):
        self.data[key] = value


class StatusDatabase:
    """Database for tracking processing status"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        # Ensure directory exists
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize status tracking database"""
        print(f"Debug: Initializing database at: {self.db_path}")
        print(f"Debug: Database directory exists: {os.path.exists(os.path.dirname(self.db_path))}")
        print(f"Debug: Current working directory: {os.getcwd()}")
        print(f"Debug: Directory permissions: {oct(os.stat(os.path.dirname(self.db_path)).st_mode)}")

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS processed_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        category TEXT NOT NULL,
                        release_path TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        status TEXT DEFAULT 'processed',
                        error_message TEXT,
                        found_in_arr BOOLEAN DEFAULT NULL,
                        UNIQUE(title, category, release_path)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS scheduler_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_name TEXT NOT NULL,
                        started_at TIMESTAMP NOT NULL,
                        completed_at TIMESTAMP,
                        items_processed INTEGER DEFAULT 0,
                        status TEXT DEFAULT 'running',
                        error_message TEXT
                    )
                """)
                conn.commit()

                # Migration: Add found_in_arr column if it doesn't exist
                try:
                    conn.execute("ALTER TABLE processed_items ADD COLUMN found_in_arr BOOLEAN DEFAULT NULL")
                    conn.commit()
                    print(f"Debug: Added found_in_arr column to database")
                except sqlite3.OperationalError:
                    # Column already exists
                    pass

                print(f"Debug: Database initialized successfully")
        except Exception as e:
            print(f"Debug: Database initialization failed: {e}")
            print(f"Debug: Trying to create database in /tmp as fallback")
            fallback_path = "/tmp/nzbdav_status.db"
            self.db_path = fallback_path
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS processed_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        category TEXT NOT NULL,
                        release_path TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        status TEXT DEFAULT 'processed',
                        error_message TEXT,
                        found_in_arr BOOLEAN DEFAULT NULL,
                        UNIQUE(title, category, release_path)
                    )
                """)
                conn.commit()

                # Migration: Add found_in_arr column if it doesn't exist (fallback)
                try:
                    conn.execute("ALTER TABLE processed_items ADD COLUMN found_in_arr BOOLEAN DEFAULT NULL")
                    conn.commit()
                    print(f"Debug: Added found_in_arr column to fallback database")
                except sqlite3.OperationalError:
                    # Column already exists
                    pass

                print(f"Debug: Fallback database created at {fallback_path}")

    def add_processed(self, title: str, category: str, release_path: str,
                     media_type: str, status: str = 'processed', error: str = None, found_in_arr: bool = None):
        """Add a processed item"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO processed_items
                (title, category, release_path, media_type, status, error_message, found_in_arr)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (title, category, release_path, media_type, status, error, found_in_arr))
            conn.commit()

    def is_processed(self, title: str, category: str, release_path: str) -> bool:
        """Check if an item has been processed"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT 1 FROM processed_items
                WHERE title = ? AND category = ? AND release_path = ?
            """, (title, category, release_path))
            return cursor.fetchone() is not None

    def get_processed_items(self) -> List[Tuple]:
        """Get all processed items"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT title, category, release_path, media_type, processed_at, status, error_message, found_in_arr
                FROM processed_items ORDER BY processed_at DESC
            """)
            return cursor.fetchall()

    def remove_processed(self, title: str, category: str, release_path: str):
        """Remove a specific processed item"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                DELETE FROM processed_items
                WHERE title = ? AND category = ? AND release_path = ?
            """, (title, category, release_path))
            conn.commit()
            return cursor.rowcount > 0

    def update_found_status(self, title: str, category: str, release_path: str, found: bool):
        """Update the found_in_arr status for a specific item"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                UPDATE processed_items
                SET found_in_arr = ?
                WHERE title = ? AND category = ? AND release_path = ?
            """, (found, title, category, release_path))
            conn.commit()
            return cursor.rowcount > 0

    def clear_processed(self, title: str = None, category: str = None, release_path: str = None):
        """Clear processed status for specific items or all items"""
        with sqlite3.connect(self.db_path) as conn:
            if title and category and release_path:
                conn.execute("""
                    DELETE FROM processed_items
                    WHERE title = ? AND category = ? AND release_path = ?
                """, (title, category, release_path))
            else:
                conn.execute("DELETE FROM processed_items")
            conn.commit()

    def log_scheduler_run(self, task_name: str, started_at: str, completed_at: str = None, items_processed: int = 0, status: str = 'completed', error_message: str = None):
        """Log a scheduler run"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO scheduler_history (task_name, started_at, completed_at, items_processed, status, error_message)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (task_name, started_at, completed_at, items_processed, status, error_message))
            conn.commit()

    def get_scheduler_history(self, limit: int = 20):
        """Get scheduler run history"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT task_name, started_at, completed_at, items_processed, status, error_message
                FROM scheduler_history
                ORDER BY started_at DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]


class NZBDAVMigratorApp:
    """Main application class"""

    def __init__(self):
        self.config = Config()
        self.status_db = StatusDatabase(self.config.get("status_db"))
        self.items_data = []
        self.schedulers = {}
        self.scheduler_status = {
            "movies": {"enabled": False, "last_run": None, "next_run": None, "running": False},
            "series": {"enabled": False, "last_run": None, "next_run": None, "running": False},
            "check_found": {"enabled": False, "last_run": None, "next_run": None, "running": False}
        }
        self.processing_status = {"active": False, "message": "", "progress": 0}
        self._stop_event = threading.Event()
        self._background_threads: List[threading.Thread] = []

        # Prime the item cache before background schedulers spin up so the
        # first automation pass works with a populated dataset.
        try:
            preload_result = self.refresh_items()
            if preload_result.get("error"):
                print(
                    f"Warning: Initial item load failed: {preload_result['error']}",
                    flush=True
                )
        except Exception as exc:  # Defensive: refresh_items should not raise
            print(f"Warning: Unexpected error during initial load: {exc}", flush=True)

        self._init_schedulers()

    def get_database_items(self) -> List[Dict]:
        """Get items from the nzbdav database"""
        db_path = self.config.get("database_path")
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"Database file not found: {db_path}")

        print(f"Loading items from database: {db_path}", flush=True)
        items = []

        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                # Simpler query - just get all paths and process in Python
                print("Executing database query...", flush=True)
                cursor.execute("""
                    SELECT DISTINCT Path
                    FROM DavItems
                    WHERE Id IN (SELECT Id FROM DavNzbFiles UNION SELECT Id FROM DavRarFiles)
                    AND Path IS NOT NULL
                    ORDER BY Path
                """)

                release_dirs = {}
                rows = cursor.fetchall()
                print(f"Processing {len(rows)} database rows...", flush=True)

                for row in rows:
                    path = row['Path']
                    try:
                        rel_dir, category, release_name = parse_release_dir(path)
                        if rel_dir and rel_dir not in release_dirs:
                            release_dirs[rel_dir] = {
                                'category': category,
                                'release_name': release_name
                            }
                    except Exception as e:
                        # Skip problematic paths
                        print(f"Warning: Could not parse path '{path}': {e}", flush=True)
                        continue

                print(f"Found {len(release_dirs)} unique releases", flush=True)

                # Get all processed items at once for faster lookup
                processed_items = set()
                found_status = {}  # Store found_in_arr status
                try:
                    processed_rows = self.status_db.get_processed_items()
                    for row in processed_rows:
                        # row format: title, category, release_path, media_type, processed_at, status, error_message, found_in_arr
                        title, category, release_path = row[0], row[1], row[2]
                        raw_found = row[7] if len(row) > 7 else None  # Handle old database format

                        if raw_found is None:
                            found_in_arr = None
                        elif isinstance(raw_found, str):
                            lowered = raw_found.strip().lower()
                            found_in_arr = lowered in ('1', 'true', 'yes', 'on')
                        else:
                            found_in_arr = bool(raw_found)

                        processed_items.add((title, category, release_path))
                        found_status[(title, category, release_path)] = found_in_arr
                except Exception as e:
                    print(f"Warning: Could not load processed items: {e}", flush=True)

                # Process releases
                for rel_dir, info in release_dirs.items():
                    category = info['category']
                    release_name = info['release_name']

                    # Quick classification
                    if is_series(release_name, category):
                        media_type = "series"
                        clean_title = extract_series_title(release_name)
                    elif is_movie(release_name, category):
                        media_type = "movie"
                        clean_title = extract_movie_title(release_name)
                    else:
                        continue

                    # Debug all title extractions for now
                    if release_name in ["(Yellowbird.2014.DVDRip.XviD-EVO-U", "1917.2019.2160p.UHD.Blu-ray.Remux"] or not clean_title or clean_title == "unnamed" or "(" in clean_title[:1]:
                        print(f"DEBUG: Title extraction - Original: '{release_name}' -> Cleaned: '{clean_title}' (Type: {media_type})", flush=True)

                    # Fast lookup in processed set
                    key = (clean_title, category, rel_dir)
                    is_processed = key in processed_items
                    found_in_arr = found_status.get(key)

                    items.append({
                        'title': clean_title,
                        'category': category,
                        'release_name': release_name,
                        'release_path': rel_dir,
                        'media_path': "Unknown",
                        'media_type': media_type,
                        'is_processed': is_processed,
                        'found_in_arr': found_in_arr,
                        'selected': False
                    })

                print(f"Loaded {len(items)} items successfully", flush=True)

        except sqlite3.Error as e:
            raise Exception(f"Database error: {e}")

        return items

    def process_items(self, item_indices: List[int]):
        """Process selected items"""
        if self.processing_status["active"]:
            return {"error": "Processing already in progress"}

        selected_items = [self.items_data[i] for i in item_indices if i < len(self.items_data)]

        if len(selected_items) > self.config.get("max_batch_size"):
            return {"error": f"Too many items selected. Maximum: {self.config.get('max_batch_size')}"}

        # Start processing in background thread
        thread = threading.Thread(target=self._process_items_worker, args=(selected_items,))
        thread.daemon = True
        thread.start()

        return {"success": f"Started processing {len(selected_items)} items"}

    def _process_items_worker(self, items: List[Dict]):
        """Worker function for processing items"""
        try:
            self.processing_status["active"] = True
            self.processing_status["progress"] = 0
            total = len(items)

            # Separate movies and series
            movies = [item for item in items if item['media_type'] == 'movie']
            series = [item for item in items if item['media_type'] == 'series']

            # Process movies
            if movies and self.config.get("radarr_url"):
                self.processing_status["message"] = f"Processing {len(movies)} movies..."
                movie_titles = [item['title'] for item in movies]

                try:
                    success = trigger_radarr_searches(
                        movie_titles,
                        self.config.get("radarr_url"),
                        self.config.get("radarr_api_key"),
                        delay=self.config.get("api_delay"),
                        timeout=15.0
                    )

                    for item in movies:
                        status = 'processed' if item['title'] in success else 'failed'
                        self.status_db.add_processed(
                            item['title'], item['category'], item['release_path'],
                            item['media_type'], status
                        )
                        item['is_processed'] = (status == 'processed')

                except Exception as e:
                    for item in movies:
                        self.status_db.add_processed(
                            item['title'], item['category'], item['release_path'],
                            item['media_type'], 'failed', str(e)
                        )

            # Process series
            if series and self.config.get("sonarr_url"):
                self.processing_status["message"] = f"Processing {len(series)} series..."

                # Build episode-specific data
                from export_nzb import parse_season_episode_from_release
                episode_data = {}
                delete_whole_season = self.config.get("sonarr_delete_whole_season", True)

                for item in series:
                    title = item['title']
                    release_name = item.get('release_name', '')

                    # Parse season/episode from release name
                    ep_info = parse_season_episode_from_release(release_name)
                    if ep_info:
                        if title not in episode_data:
                            episode_data[title] = []

                        # If whole season mode is enabled, clear episode list to delete entire season
                        if delete_whole_season and ep_info.get('season') is not None:
                            ep_info_to_add = {'season': ep_info['season'], 'episodes': []}  # Empty episodes = whole season
                            # Check if we already have this season
                            season_exists = any(
                                e.get('season') == ep_info['season'] and not e.get('episodes')
                                for e in episode_data[title]
                            )
                            if not season_exists:
                                episode_data[title].append(ep_info_to_add)
                                print(f"DEBUG: Added whole season for '{title}' S{ep_info['season']:02d} (delete_whole_season=True)", flush=True)
                        else:
                            episode_data[title].append(ep_info)
                            print(f"DEBUG: Parsed episode data for '{title}': {ep_info} from '{release_name}'", flush=True)
                    else:
                        print(f"DEBUG: Could not parse episode data from '{release_name}'", flush=True)

                series_titles = list(set(item['title'] for item in series))  # Unique titles
                print(f"DEBUG: Processing {len(series_titles)} unique series with episode data: {list(episode_data.keys())}", flush=True)
                print(f"DEBUG: Delete whole season mode: {delete_whole_season}", flush=True)

                try:
                    success = trigger_sonarr_searches(
                        series_titles,
                        self.config.get("sonarr_url"),
                        self.config.get("sonarr_api_key"),
                        delay=self.config.get("api_delay"),
                        timeout=15.0,
                        episode_data=episode_data if episode_data else None
                    )

                    for item in series:
                        status = 'processed' if item['title'] in success else 'failed'
                        self.status_db.add_processed(
                            item['title'], item['category'], item['release_path'],
                            item['media_type'], status
                        )
                        item['is_processed'] = (status == 'processed')

                except Exception as e:
                    for item in series:
                        self.status_db.add_processed(
                            item['title'], item['category'], item['release_path'],
                            item['media_type'], 'failed', str(e)
                        )

            self.processing_status["message"] = f"Completed processing {total} items"
            self.processing_status["progress"] = 100

        except Exception as e:
            self.processing_status["message"] = f"Error: {e}"
        finally:
            time.sleep(2)  # Keep message visible
            self.processing_status["active"] = False

    def mark_not_processed(self, item_indices: List[int]):
        """Mark selected items as not processed"""
        print(f"DEBUG: mark_not_processed called with indices: {item_indices}", flush=True)

        if self.processing_status["active"]:
            return {"error": "Cannot mark items while processing is active"}

        selected_items = [self.items_data[i] for i in item_indices if i < len(self.items_data)]
        print(f"DEBUG: Selected {len(selected_items)} items to mark as not processed", flush=True)

        if not selected_items:
            return {"error": "No valid items selected"}

        try:
            removed_count = 0
            # Remove from status database (mark as not processed)
            for item in selected_items:
                print(f"DEBUG: Removing processed status for: title='{item['title']}', category='{item['category']}', release_path='{item['release_path']}'", flush=True)
                if self.status_db.remove_processed(item['title'], item['category'], item['release_path']):
                    removed_count += 1
                    item['is_processed'] = False
                    item['found_in_arr'] = None  # Reset found status too
                    print(f"DEBUG: Successfully removed item from database", flush=True)
                else:
                    print(f"DEBUG: Item was not found in database (may not have been processed)", flush=True)

            print(f"DEBUG: Successfully removed {removed_count} items from processed status", flush=True)
            return {"success": f"Marked {len(selected_items)} items as not processed (removed {removed_count} from database)"}
        except Exception as e:
            print(f"DEBUG: Error in mark_not_processed: {e}", flush=True)
            return {"error": f"Failed to mark items as not processed: {e}"}

    def refresh_items(self):
        """Refresh items from database"""
        try:
            self.items_data = self.get_database_items()
            return {"success": f"Loaded {len(self.items_data)} items"}
        except Exception as e:
            return {"error": str(e)}

    def test_connections(self, config: dict) -> dict:
        """Test Radarr and Sonarr connections"""
        results = {}

        # Test Radarr
        if config.get('radarr_url') and config.get('radarr_api_key'):
            try:
                response = _api_request(
                    config['radarr_url'],
                    config['radarr_api_key'],
                    'api/v3/system/status',
                    timeout=10
                )
                if response:
                    results['radarr'] = {
                        'success': True,
                        'version': response.get('version', 'unknown')
                    }
                else:
                    results['radarr'] = {
                        'success': False,
                        'error': 'No response from Radarr'
                    }
            except Exception as e:
                results['radarr'] = {
                    'success': False,
                    'error': str(e)
                }

        # Test Sonarr
        if config.get('sonarr_url') and config.get('sonarr_api_key'):
            try:
                response = _api_request(
                    config['sonarr_url'],
                    config['sonarr_api_key'],
                    'api/v3/system/status',
                    timeout=10
                )
                if response:
                    results['sonarr'] = {
                        'success': True,
                        'version': response.get('version', 'unknown')
                    }
                else:
                    results['sonarr'] = {
                        'success': False,
                        'error': 'No response from Sonarr'
                    }
            except Exception as e:
                results['sonarr'] = {
                    'success': False,
                    'error': str(e)
                }

        return results

    def check_item_exists_in_radarr(self, title: str) -> bool:
        """Check if a movie exists in Radarr"""
        try:
            base_url = self.config.get('radarr_url')
            api_key = self.config.get('radarr_api_key')
            print(f"DEBUG: Checking Radarr for '{title}' at {base_url}", flush=True)

            if not base_url or not api_key:
                print(f"DEBUG: Radarr not configured - URL: {bool(base_url)}, API Key: {bool(api_key)}", flush=True)
                return False

            response = _api_request(base_url, api_key, 'api/v3/movie', timeout=10)
            print(f"DEBUG: Radarr API response type: {type(response)}, length: {len(response) if response else 0}", flush=True)

            if response:
                movies = response  # _api_request returns parsed JSON directly
                # Look for movie with matching title
                clean_title = title.lower().replace(' ', '').replace('(', '').replace(')', '')
                print(f"DEBUG: Looking for clean title '{clean_title}' in {len(movies)} movies", flush=True)

                for movie in movies:
                    movie_title = movie.get('title', '').lower().replace(' ', '').replace('(', '').replace(')', '')
                    # Exact match or very close match (one contains the other but with minimum length)
                    if (clean_title == movie_title or
                        (len(clean_title) > 6 and len(movie_title) > 6 and
                         (clean_title in movie_title or movie_title in clean_title))):

                        # Check if movie has downloaded files
                        has_file = movie.get('hasFile', False)
                        downloaded = movie.get('downloaded', False)
                        print(f"DEBUG: Found match in Radarr: '{title}' -> '{movie.get('title', '')}' (hasFile: {has_file}, downloaded: {downloaded})", flush=True)

                        # Only return True if the movie actually has files downloaded
                        return has_file or downloaded
                print(f"DEBUG: No match found in Radarr for '{title}'", flush=True)
            return False
        except Exception as e:
            print(f"DEBUG: Error checking Radarr for {title}: {e}", flush=True)
            return False

    def check_item_exists_in_sonarr(self, title: str) -> bool:
        """Check if a series exists in Sonarr"""
        try:
            base_url = self.config.get('sonarr_url')
            api_key = self.config.get('sonarr_api_key')
            response = _api_request(base_url, api_key, 'api/v3/series', timeout=10)

            if response:
                series = response  # _api_request returns parsed JSON directly
                # Look for series with matching title
                clean_title = title.lower().replace(' ', '').replace('(', '').replace(')', '')
                for show in series:
                    show_title = show.get('title', '').lower().replace(' ', '').replace('(', '').replace(')', '')
                    # Exact match or very close match (one contains the other but with minimum length)
                    if (clean_title == show_title or
                        (len(clean_title) > 6 and len(show_title) > 6 and
                         (clean_title in show_title or show_title in clean_title))):

                        # Check if series has downloaded episodes
                        # For Sonarr, we need to check statistics or episode files
                        statistics = show.get('statistics', {})
                        episode_file_count = statistics.get('episodeFileCount', 0)
                        print(f"DEBUG: Found match in Sonarr: '{title}' -> '{show.get('title', '')}' (episodeFileCount: {episode_file_count})", flush=True)

                        # Only return True if the series has downloaded episodes
                        return episode_file_count > 0
            return False
        except Exception as e:
            print(f"Debug: Error checking Sonarr for {title}: {e}")
            return False

    def check_found_status(self, item_indices: List[int]):
        """Check if processed items were found in Radarr/Sonarr and update their status"""
        if self.processing_status["active"]:
            return {"error": "Cannot check status while processing is active"}

        selected_items = [self.items_data[i] for i in item_indices if i < len(self.items_data)]

        if not selected_items:
            return {"error": "No valid items selected"}

        # Only check processed items
        processed_items = [item for item in selected_items if item.get('is_processed')]

        if not processed_items:
            return {"error": "No processed items selected"}

        try:
            updated_count = 0
            for item in processed_items:
                found = False
                if item['media_type'] == 'movie' and self.config.get('radarr_url'):
                    found = self.check_item_exists_in_radarr(item['title'])
                elif item['media_type'] == 'series' and self.config.get('sonarr_url'):
                    found = self.check_item_exists_in_sonarr(item['title'])

                # Update database
                if self.status_db.update_found_status(item['title'], item['category'], item['release_path'], found):
                    updated_count += 1

            return {"success": f"Checked {len(processed_items)} items, updated {updated_count} records"}
        except Exception as e:
            return {"error": f"Failed to check found status: {e}"}

    def auto_check_found_status(self):
        """Automatically check ALL processed items with unknown found status"""
        if self.processing_status["active"]:
            print("DEBUG: auto_check_found_status skipped - processing is active", flush=True)
            return 0

        # Find all processed items with unknown status (found_in_arr is None)
        pending_checks = [
            i for i, item in enumerate(self.items_data)
            if item.get('is_processed') and item.get('found_in_arr') is None
        ]

        if not pending_checks:
            print("DEBUG: auto_check_found_status - no items need checking", flush=True)
            return 0

        print(f"DEBUG: auto_check_found_status - checking {len(pending_checks)} items", flush=True)
        result = self.check_found_status(pending_checks)
        print(f"DEBUG: auto_check_found_status result: {result}", flush=True)
        return len(pending_checks)

    # ------------------------------------------------------------------
    # Background scheduling helpers

    def start_background_tasks(self):
        """Start periodic background tasks for auto-processing and status checks"""
        if self._stop_event.is_set():
            self._stop_event.clear()
        self._background_threads = []

        self._start_periodic_task(
            interval_seconds=20 * 60,
            task=self._automatic_process_random_movies,
            name="AutoProcessMovies"
        )
        self._start_periodic_task(
            interval_seconds=10 * 60,
            task=self._automatic_check_pending_found_status,
            name="AutoCheckFound"
        )

    def stop_background_tasks(self):
        """Signal background tasks to stop"""
        self._stop_event.set()
        for thread in self._background_threads:
            if thread.is_alive():
                thread.join(timeout=2.0)

    def _start_periodic_task(self, interval_seconds: int, task, name: str):
        """Helper to start a periodic daemon thread"""

        def runner():
            while not self._stop_event.wait(interval_seconds):
                try:
                    task()
                except Exception as exc:
                    print(f"DEBUG: {name} task error: {exc}", flush=True)
                    traceback.print_exc()

        thread = threading.Thread(target=runner, name=name, daemon=True)
        thread.start()
        self._background_threads.append(thread)

    def _automatic_process_random_movies(self):
        """Automatically process up to 10 random pending movies"""
        if self.processing_status.get("active"):
            print("DEBUG: AutoProcess skipped - processing already active", flush=True)
            return

        try:
            refresh_result = self.refresh_items()
            if refresh_result.get("error"):
                print(f"DEBUG: AutoProcess refresh failed: {refresh_result['error']}", flush=True)
                return
        except Exception as exc:
            print(f"DEBUG: AutoProcess refresh raised error: {exc}", flush=True)
            traceback.print_exc()
            return

        pending_movies = [
            index for index, item in enumerate(self.items_data)
            if item.get('media_type') == 'movie' and not item.get('is_processed')
        ]

        if not pending_movies:
            print("DEBUG: AutoProcess skipped - no pending movies available", flush=True)
            return

        max_batch = self.config.get('max_batch_size', 10)
        target_count = min(10, len(pending_movies), max_batch)

        if target_count <= 0:
            print("DEBUG: AutoProcess skipped - max batch size prevented selection", flush=True)
            return

        selected_indices = random.sample(pending_movies, target_count)
        selected_titles = [self.items_data[i]['title'] for i in selected_indices]
        print(
            f"DEBUG: AutoProcess scheduling {target_count} movie(s): {selected_titles}",
            flush=True
        )

        result = self.process_items(selected_indices)
        print(f"DEBUG: AutoProcess result: {result}", flush=True)

    def _automatic_check_pending_found_status(self):
        """Automatically check Radarr/Sonarr status for processed items with unknown state"""
        if self.processing_status.get("active"):
            print("DEBUG: AutoCheckFound skipped - processing active", flush=True)
            return

        try:
            refresh_result = self.refresh_items()
            if refresh_result.get("error"):
                print(f"DEBUG: AutoCheckFound refresh failed: {refresh_result['error']}", flush=True)
                return
        except Exception as exc:
            print(f"DEBUG: AutoCheckFound refresh raised error: {exc}", flush=True)
            traceback.print_exc()
            return

        pending_checks = [
            index for index, item in enumerate(self.items_data)
            if item.get('is_processed') and item.get('found_in_arr') is None
        ]

        if not pending_checks:
            print("DEBUG: AutoCheckFound skipped - no processed items pending status", flush=True)
            return

        print(
            f"DEBUG: AutoCheckFound checking {len(pending_checks)} processed item(s) for status",
            flush=True
        )

        result = self.check_found_status(pending_checks)
        print(f"DEBUG: AutoCheckFound result: {result}", flush=True)

    def _run_scheduled_task(self, task_name, task_func, interval_minutes):
        """Run a scheduled task in a loop"""
        import time
        from datetime import datetime, timedelta

        while not self._stop_event.is_set():
            started_at = None
            items_processed = 0
            try:
                if self.scheduler_status[task_name]["enabled"]:
                    self.scheduler_status[task_name]["running"] = True
                    started_at = datetime.now().isoformat()
                    self.scheduler_status[task_name]["last_run"] = started_at

                    print(f"Running scheduled task: {task_name}", flush=True)

                    # Call task and get count of items processed
                    result = task_func()
                    items_processed = result if result is not None else 0

                    completed_at = datetime.now().isoformat()
                    self.scheduler_status[task_name]["running"] = False
                    next_run = datetime.now() + timedelta(minutes=interval_minutes)
                    self.scheduler_status[task_name]["next_run"] = next_run.isoformat()

                    # Log successful run
                    self.status_db.log_scheduler_run(
                        task_name=task_name,
                        started_at=started_at,
                        completed_at=completed_at,
                        items_processed=items_processed,
                        status='completed'
                    )

                # Sleep in small increments to allow quick shutdown
                sleep_time = interval_minutes * 60
                for _ in range(int(sleep_time)):
                    if self._stop_event.is_set():
                        break
                    time.sleep(1)
            except Exception as e:
                error_msg = str(e)
                print(f"Error in scheduled task {task_name}: {error_msg}", flush=True)
                self.scheduler_status[task_name]["running"] = False

                # Log failed run
                if started_at:
                    self.status_db.log_scheduler_run(
                        task_name=task_name,
                        started_at=started_at,
                        completed_at=datetime.now().isoformat(),
                        items_processed=items_processed,
                        status='error',
                        error_message=error_msg
                    )

                time.sleep(60)  # Wait a minute before retry

    def _init_schedulers(self):
        """Initialize scheduled tasks based on configuration"""
        # Start scheduler threads
        self._update_schedulers()

    def _update_schedulers(self):
        """Update schedulers based on current configuration"""
        import random
        from datetime import datetime, timedelta

        # Movies scheduler
        if self.config.get("schedule_movies_enabled"):
            if "movies" not in self.schedulers or not self.schedulers["movies"].is_alive():
                count = self.config.get("schedule_movies_count", 5)

                def scheduled_movies_task():
                    refresh_result = self.refresh_items()
                    if refresh_result.get("error"):
                        print(
                            f"DEBUG: Scheduled movies refresh failed: {refresh_result['error']}",
                            flush=True
                        )
                        return 0

                    movies = [
                        item for item in self.items_data
                        if item['media_type'] == 'movie' and not item['is_processed']
                    ]

                    if not movies:
                        return 0

                    task_count = self.config.get("schedule_movies_count", 5)
                    selected = random.sample(movies, min(task_count, len(movies)))
                    indices = [self.items_data.index(item) for item in selected]

                    process_result = self.process_items(indices)
                    if isinstance(process_result, dict) and process_result.get("error"):
                        print(
                            f"DEBUG: Scheduled movies processing skipped: {process_result['error']}",
                            flush=True
                        )
                        return 0

                    return len(selected)

                interval = self.config.get("schedule_movies_interval", 60)
                thread = threading.Thread(target=self._run_scheduled_task, args=("movies", scheduled_movies_task, interval), daemon=True)
                self.schedulers["movies"] = thread
                self.scheduler_status["movies"]["enabled"] = True
                self.scheduler_status["movies"]["next_run"] = (datetime.now() + timedelta(minutes=interval)).isoformat()
                thread.start()
                print(f"Started movies scheduler: {count} movies every {interval} minutes", flush=True)
        else:
            self.scheduler_status["movies"]["enabled"] = False

        # Series scheduler
        if self.config.get("schedule_series_enabled"):
            if "series" not in self.schedulers or not self.schedulers["series"].is_alive():
                count = self.config.get("schedule_series_count", 3)

                def scheduled_series_task():
                    refresh_result = self.refresh_items()
                    if refresh_result.get("error"):
                        print(
                            f"DEBUG: Scheduled series refresh failed: {refresh_result['error']}",
                            flush=True
                        )
                        return 0

                    series_items = [
                        item for item in self.items_data
                        if item['media_type'] == 'series' and not item['is_processed']
                    ]

                    if not series_items:
                        return 0

                    task_count = self.config.get("schedule_series_count", 3)
                    selected = random.sample(series_items, min(task_count, len(series_items)))
                    indices = [self.items_data.index(item) for item in selected]

                    process_result = self.process_items(indices)
                    if isinstance(process_result, dict) and process_result.get("error"):
                        print(
                            f"DEBUG: Scheduled series processing skipped: {process_result['error']}",
                            flush=True
                        )
                        return 0

                    return len(selected)

                interval = self.config.get("schedule_series_interval", 90)
                thread = threading.Thread(target=self._run_scheduled_task, args=("series", scheduled_series_task, interval), daemon=True)
                self.schedulers["series"] = thread
                self.scheduler_status["series"]["enabled"] = True
                self.scheduler_status["series"]["next_run"] = (datetime.now() + timedelta(minutes=interval)).isoformat()
                thread.start()
                print(f"Started series scheduler: {count} series every {interval} minutes", flush=True)
        else:
            self.scheduler_status["series"]["enabled"] = False

        # Check found scheduler
        if self.config.get("schedule_check_found_enabled"):
            if "check_found" not in self.schedulers or not self.schedulers["check_found"].is_alive():
                def scheduled_check_found_task():
                    return self.auto_check_found_status()

                interval = self.config.get("schedule_check_found_interval", 30)
                thread = threading.Thread(target=self._run_scheduled_task, args=("check_found", scheduled_check_found_task, interval), daemon=True)
                self.schedulers["check_found"] = thread
                self.scheduler_status["check_found"]["enabled"] = True
                self.scheduler_status["check_found"]["next_run"] = (datetime.now() + timedelta(minutes=interval)).isoformat()
                thread.start()
                print(f"Started check found scheduler: every {interval} minutes", flush=True)
        else:
            self.scheduler_status["check_found"]["enabled"] = False


class NZBDAVWebHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the web interface"""

    def do_GET(self):
        """Handle GET requests"""
        print(f"DEBUG: GET request for {self.path}")
        parsed_path = urlparse(self.path)
        path = parsed_path.path
        query = parse_qs(parsed_path.query)

        print(f"DEBUG: Parsed path: {path}")

        if path == "/" or path == "/index.html":
            print("DEBUG: Serving index page")
            self.serve_index()
        elif path == "/api/items":
            print("DEBUG: Serving items API")
            self.serve_items()
        elif path == "/api/status":
            print("DEBUG: Serving status API")
            self.serve_status()
        elif path == "/api/config":
            print("DEBUG: Serving config API")
            self.serve_config()
        elif path == "/api/scheduler_status":
            print("DEBUG: Serving scheduler status API")
            self.serve_scheduler_status()
        elif path == "/api/scheduler_history":
            print("DEBUG: Serving scheduler history API")
            self.serve_scheduler_history()
        elif path == "/style.css":
            print("DEBUG: Serving CSS")
            self.serve_css()
        else:
            print(f"DEBUG: Path not found, sending 404 for: {path}")
            self.send_error(404)

    def do_POST(self):
        """Handle POST requests"""
        parsed_path = urlparse(self.path)
        path = parsed_path.path

        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length).decode('utf-8')

        try:
            data = json.loads(post_data) if post_data else {}
        except json.JSONDecodeError:
            data = {}

        if path == "/api/refresh":
            result = self.app.refresh_items()
            self.send_json_response(result)
        elif path == "/api/process":
            indices = data.get("indices", [])
            result = self.app.process_items(indices)
            self.send_json_response(result)
        elif path == "/api/mark_not_processed":
            print(f"DEBUG: mark_not_processed API called with data: {data}", flush=True)
            indices = data.get("indices", [])
            result = self.app.mark_not_processed(indices)
            print(f"DEBUG: mark_not_processed result: {result}", flush=True)
            self.send_json_response(result)
        elif path == "/api/check_found":
            print(f"DEBUG: check_found API called with data: {data}", flush=True)
            indices = data.get("indices", [])
            print(f"DEBUG: check_found indices: {indices}", flush=True)
            result = self.app.check_found_status(indices)
            print(f"DEBUG: check_found result: {result}", flush=True)
            self.send_json_response(result)
        elif path == "/api/config":
            # Update config
            updated_keys = []
            for key, value in data.items():
                if key in self.app.config.defaults:
                    self.app.config.set(key, value)
                    updated_keys.append(key)

            print(f"DEBUG: Updating config keys: {updated_keys}", flush=True)
            self.app.config.save()

            # Verify save worked by reading it back
            try:
                self.app.config.load()
                print("DEBUG: Config reloaded successfully after save", flush=True)

                # Update schedulers if any schedule-related config changed
                schedule_keys = [k for k in updated_keys if k.startswith('schedule_')]
                if schedule_keys:
                    print(f"DEBUG: Schedule config changed, updating schedulers: {schedule_keys}", flush=True)
                    self.app._update_schedulers()

                self.send_json_response({"success": f"Configuration updated: {', '.join(updated_keys)}"})
            except Exception as e:
                print(f"DEBUG: Config reload failed: {e}", flush=True)
                self.send_json_response({"error": f"Config saved but reload failed: {e}"})
        elif path == "/api/test_connections":
            print(f"DEBUG: test_connections API called with data: {data}", flush=True)
            result = self.app.test_connections(data)
            print(f"DEBUG: test_connections result: {result}", flush=True)
            self.send_json_response(result)
        else:
            self.send_error(404)

    def serve_index(self):
        """Serve the main HTML page"""
        html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NZBDAVMigrator</title>
    <link rel="stylesheet" href="/style.css">
</head>
<body>
    <div class="container">
        <header>
            <h1>NZBDAVMigrator</h1>
            <div class="toolbar">
                <button onclick="refreshItems()">Refresh</button>
                <button onclick="selectAll()">Select All</button>
                <button onclick="selectNone()">Select None</button>
                <button onclick="processSelected()">Process Selected</button>
                <button onclick="markSelectedAsNotProcessed()">Mark Not Processed</button>
                <button onclick="checkFoundStatus()">Check Found Status</button>
                <button onclick="openSettings()">Settings</button>
            </div>
        </header>

        <div class="filters">
            <input type="text" id="filterInput" placeholder="Filter items..." onkeyup="applyFilter()">
            <label for="pageSizeSelect">Items per page:</label>
            <select id="pageSizeSelect" onchange="changePageSize()">
                <option value="20" selected>20</option>
                <option value="50">50</option>
                <option value="100">100</option>
                <option value="all">All</option>
            </select>
        </div>

        <div class="status" id="statusBar">Ready</div>

        <div id="schedulerStatus" class="scheduler-status" style="display: none;">
            <div class="scheduler-status-title">Scheduled Tasks</div>
            <div class="scheduler-status-grid">
                <div id="schedulerMoviesStatus" class="scheduler-status-card"></div>
                <div id="schedulerSeriesStatus" class="scheduler-status-card"></div>
                <div id="schedulerCheckFoundStatus" class="scheduler-status-card"></div>
            </div>
        </div>

        <div class="tabs">
            <button class="tab-button active" onclick="switchTab('movies')">Movies (<span id="movieCount">0</span>)</button>
            <button class="tab-button" onclick="switchTab('series')">TV Series (<span id="seriesCount">0</span>)</button>
            <button class="tab-button" onclick="switchTab('processedMovies')">Processed Movies (<span id="processedMovieCount">0</span>)</button>
            <button class="tab-button" onclick="switchTab('processedSeries')">Processed Series (<span id="processedSeriesCount">0</span>)</button>
            <button class="tab-button" onclick="switchTab('schedulerHistory')">Scheduler History</button>
        </div>

        <div class="content">
            <div id="moviesTab" class="tab-content active">
                <table id="moviesTable">
                    <thead>
                        <tr>
                            <th width="30"><input type="checkbox" id="moviesSelectAll" name="moviesSelectAll" onchange="toggleAllMovies(this)"></th>
                            <th>Title</th>
                            <th>Category</th>
                            <th>Release Path</th>
                            <th>Status</th>
                            <th>Found</th>
                        </tr>
                    </thead>
                    <tbody id="moviesTableBody">
                    </tbody>
                </table>
                <div class="pagination-controls">
                    <button id="moviesPrevBtn" onclick="previousMoviesPage()" disabled>Previous</button>
                    <span id="moviesPageInfo">Page 1</span>
                    <button id="moviesNextBtn" onclick="nextMoviesPage()">Next</button>
                </div>
            </div>

            <div id="seriesTab" class="tab-content">
                <div class="group-toolbar">
                    <label><input type="checkbox" id="seriesSelectAll" onchange="toggleAllSeries(this)"> Select Visible</label>
                </div>
                <div id="seriesTree" class="series-tree"></div>
                <div class="pagination-controls">
                    <button id="seriesPrevBtn" onclick="previousSeriesPage()" disabled>Previous</button>
                    <span id="seriesPageInfo">Page 1</span>
                    <button id="seriesNextBtn" onclick="nextSeriesPage()">Next</button>
                </div>
            </div>

            <div id="processedMoviesTab" class="tab-content">
                <table id="processedMoviesTable">
                    <thead>
                        <tr>
                            <th width="30"><input type="checkbox" id="processedMoviesSelectAll" name="processedMoviesSelectAll" onchange="toggleAllProcessedMovies(this)"></th>
                            <th>Title</th>
                            <th>Category</th>
                            <th>Release Path</th>
                            <th>Status</th>
                            <th>Found</th>
                        </tr>
                    </thead>
                    <tbody id="processedMoviesTableBody">
                    </tbody>
                </table>
                <div class="pagination-controls">
                    <button id="processedMoviesPrevBtn" onclick="previousProcessedMoviesPage()" disabled>Previous</button>
                    <span id="processedMoviesPageInfo">Page 1</span>
                    <button id="processedMoviesNextBtn" onclick="nextProcessedMoviesPage()">Next</button>
                </div>
            </div>

            <div id="processedSeriesTab" class="tab-content">
                <div class="group-toolbar">
                    <label><input type="checkbox" id="processedSeriesSelectAll" onchange="toggleAllProcessedSeries(this)"> Select Visible</label>
                </div>
                <div id="processedSeriesTree" class="series-tree"></div>
                <div class="pagination-controls">
                    <button id="processedSeriesPrevBtn" onclick="previousProcessedSeriesPage()" disabled>Previous</button>
                    <span id="processedSeriesPageInfo">Page 1</span>
                    <button id="processedSeriesNextBtn" onclick="nextProcessedSeriesPage()">Next</button>
                </div>
            </div>

            <div id="schedulerHistoryTab" class="tab-content">
                <button onclick="refreshSchedulerHistory()" style="margin-bottom: 10px;">Refresh History</button>
                <table id="schedulerHistoryTable">
                    <thead>
                        <tr>
                            <th>Task Name</th>
                            <th>Started At</th>
                            <th>Completed At</th>
                            <th>Items Processed</th>
                            <th>Status</th>
                            <th>Error Message</th>
                        </tr>
                    </thead>
                    <tbody id="schedulerHistoryTableBody">
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Settings Modal -->
    <div id="settingsModal" class="modal">
        <div class="modal-content">
            <span class="close" onclick="closeSettings()">&times;</span>
            <h2>Settings</h2>
            <form id="settingsForm">
                <h3>Radarr Configuration</h3>
                <label>URL: <input type="text" id="radarr_url" placeholder="http://localhost:7878"></label>
                <label>API Key: <input type="password" id="radarr_api_key"></label>

                <h3>Sonarr Configuration</h3>
                <label>URL: <input type="text" id="sonarr_url" placeholder="http://localhost:8989"></label>
                <label>API Key: <input type="password" id="sonarr_api_key"></label>
                <label style="display: flex; align-items: center; gap: 8px;">
                    <input type="checkbox" id="sonarr_delete_whole_season">
                    <span>Delete & redownload whole season (when unchecked, deletes only selected episodes)</span>
                </label>

                <h3>Processing Settings</h3>
                <label>Batch Size: <input type="number" id="batch_size" min="1" max="50"></label>
                <label>Max Batch Size: <input type="number" id="max_batch_size" min="1" max="100"></label>
                <label>API Delay (seconds): <input type="number" id="api_delay" min="0" max="10" step="0.5"></label>

                <h3>Scheduled Tasks</h3>
                <div style="border: 1px solid #444; padding: 12px; margin: 8px 0; border-radius: 4px;">
                    <h4 style="margin-top: 0;">Random Movies Processing</h4>
                    <label style="display: flex; align-items: center; gap: 8px;">
                        <input type="checkbox" id="schedule_movies_enabled">
                        <span>Enable automatic movie processing</span>
                    </label>
                    <label>Process count: <input type="number" id="schedule_movies_count" min="1" max="50" style="width: 80px;"> random movies</label>
                    <label>Every <input type="number" id="schedule_movies_interval" min="1" max="1440" style="width: 80px;"> minutes</label>
                </div>

                <div style="border: 1px solid #444; padding: 12px; margin: 8px 0; border-radius: 4px;">
                    <h4 style="margin-top: 0;">Random Series Processing</h4>
                    <label style="display: flex; align-items: center; gap: 8px;">
                        <input type="checkbox" id="schedule_series_enabled">
                        <span>Enable automatic series processing</span>
                    </label>
                    <label>Process count: <input type="number" id="schedule_series_count" min="1" max="50" style="width: 80px;"> random series</label>
                    <label>Every <input type="number" id="schedule_series_interval" min="1" max="1440" style="width: 80px;"> minutes</label>
                </div>

                <div style="border: 1px solid #444; padding: 12px; margin: 8px 0; border-radius: 4px;">
                    <h4 style="margin-top: 0;">Check Found Status</h4>
                    <label style="display: flex; align-items: center; gap: 8px;">
                        <input type="checkbox" id="schedule_check_found_enabled">
                        <span>Enable periodic found status checks</span>
                    </label>
                    <label>Every <input type="number" id="schedule_check_found_interval" min="1" max="1440" style="width: 80px;"> minutes</label>
                </div>

                <button type="button" onclick="saveSettings()">Save Settings</button>
                <button type="button" onclick="testConnections()">Test Connections</button>
            </form>
        </div>
    </div>

    <script>
        let allItems = [];
        let filteredItems = [];
        let processedItems = [];
        let currentTab = 'movies';
        let currentPage = 1;
        let currentMoviesPage = 1;
        let currentSeriesPage = 1;
        let currentProcessedMoviesPage = 1;
        let currentProcessedSeriesPage = 1;
        let itemsPerPage = 20; // Default page size
        let pageSize = 20; // Current page size setting

        // Cache for series hierarchies to avoid rebuilding on every page change
        let cachedSeriesHierarchy = null;
        let cachedProcessedSeriesHierarchy = null;
        let lastFilterText = '';

        // Load items on page load
        window.onload = function() {
            refreshItems();
            loadConfig();
            updateSchedulerStatus();
            setInterval(updateStatus, 5000); // Check status every 5 seconds (reduced frequency)
            setInterval(updateSchedulerStatus, 10000); // Check scheduler status every 10 seconds
        };

        function refreshItems() {
            document.getElementById('statusBar').textContent = 'Refreshing items...';
            document.getElementById('statusBar').className = 'status processing';

            fetch('/api/refresh', { method: 'POST' })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        loadItems();
                        document.getElementById('statusBar').textContent = data.success;
                        document.getElementById('statusBar').className = 'status';
                    } else {
                        alert('Error: ' + data.error);
                        document.getElementById('statusBar').textContent = 'Error refreshing items';
                        document.getElementById('statusBar').className = 'status';
                    }
                })
                .catch(err => {
                    console.error('Refresh failed:', err);
                    document.getElementById('statusBar').textContent = 'Refresh failed';
                    document.getElementById('statusBar').className = 'status';
                });
        }

        function refreshSchedulerHistory() {
            Promise.all([
                fetch('/api/scheduler_history').then(r => r.json()),
                fetch('/api/scheduler_status').then(r => r.json())
            ])
            .then(([history, status]) => {
                const tbody = document.getElementById('schedulerHistoryTableBody');
                tbody.innerHTML = '';

                if (history.length === 0) {
                    const row = tbody.insertRow();
                    const cell = row.insertCell();
                    cell.colSpan = 6;
                    cell.textContent = 'No scheduler history found';
                    cell.style.textAlign = 'center';
                    return;
                }

                history.forEach(record => {
                    const row = tbody.insertRow();
                    row.insertCell().textContent = record.task_name;
                    row.insertCell().textContent = new Date(record.started_at).toLocaleString();
                    row.insertCell().textContent = record.completed_at ? new Date(record.completed_at).toLocaleString() : 'Running...';
                    row.insertCell().textContent = record.items_processed;

                    const statusCell = row.insertCell();
                    statusCell.textContent = record.status;
                    statusCell.style.color = record.status === 'completed' ? 'green' : (record.status === 'error' ? 'red' : 'orange');

                    row.insertCell().textContent = record.error_message || '';
                });
            })
            .catch(err => {
                console.error('Failed to load scheduler history:', err);
                alert('Failed to load scheduler history');
            });
        }

        function loadItems() {
            fetch('/api/items')
                .then(response => response.json())
                .then(data => {
                    allItems = data;
                    // Invalidate cache when items are reloaded
                    cachedSeriesHierarchy = null;
                    cachedProcessedSeriesHierarchy = null;
                    applyFilter();
                })
                .catch(err => {
                    console.error('Load items failed:', err);
                });
        }

        function applyFilter() {
            // Show loading indicator for large datasets
            if (allItems.length > 1000) {
                document.getElementById('statusBar').textContent = 'Filtering items...';
                document.getElementById('statusBar').className = 'status processing';
            }

            // Use setTimeout to allow UI to update before heavy processing
            setTimeout(() => {
                const filterText = document.getElementById('filterInput').value.toLowerCase();

                // Filter pending items (not processed)
                filteredItems = allItems.filter(item => {
                    const matchesFilter = !filterText || item.title.toLowerCase().includes(filterText);
                    return matchesFilter && !item.is_processed; // Only show pending items
                });

                // Filter processed items
                processedItems = allItems.filter(item => {
                    const matchesFilter = !filterText || item.title.toLowerCase().includes(filterText);
                    return matchesFilter && item.is_processed; // Only show processed items
                });

                // Invalidate cache if filter changed
                if (filterText !== lastFilterText) {
                    cachedSeriesHierarchy = null;
                    cachedProcessedSeriesHierarchy = null;
                    lastFilterText = filterText;
                }

                updateTables();

                // Clear loading indicator
                if (allItems.length > 1000) {
                    document.getElementById('statusBar').textContent = `Showing ${filteredItems.length} items`;
                    document.getElementById('statusBar').className = 'status';
                }
            }, 10);
        }

        function updateTables() {
            // Handle pending items
            const movies = filteredItems.filter(item => item.media_type === 'movie');
            const series = filteredItems.filter(item => item.media_type === 'series');

            // Handle processed items
            const processedMovies = processedItems.filter(item => item.media_type === 'movie');
            const processedSeries = processedItems.filter(item => item.media_type === 'series');

            // Get current page size
            const currentPageSize = pageSize === 'all' ? Number.MAX_SAFE_INTEGER : parseInt(pageSize);

            // Apply pagination for pending items
            const moviesStartIndex = (currentMoviesPage - 1) * currentPageSize;
            const moviesPage = movies.slice(moviesStartIndex, moviesStartIndex + currentPageSize);

            // Apply pagination for processed items
            const processedMoviesStartIndex = (currentProcessedMoviesPage - 1) * currentPageSize;
            const processedMoviesPage = processedMovies.slice(processedMoviesStartIndex, processedMoviesStartIndex + currentPageSize);

            // Build or use cached grouped views for series
            if (!cachedSeriesHierarchy) {
                cachedSeriesHierarchy = buildSeriesHierarchy(series);
            }
            if (!cachedProcessedSeriesHierarchy) {
                cachedProcessedSeriesHierarchy = buildSeriesHierarchy(processedSeries);
            }

            // Apply pagination to series hierarchies
            const seriesStartIndex = (currentSeriesPage - 1) * currentPageSize;
            const seriesPage = cachedSeriesHierarchy.slice(seriesStartIndex, seriesStartIndex + currentPageSize);

            const processedSeriesStartIndex = (currentProcessedSeriesPage - 1) * currentPageSize;
            const processedSeriesPage = cachedProcessedSeriesHierarchy.slice(processedSeriesStartIndex, processedSeriesStartIndex + currentPageSize);

            // Update tables / trees
            updateTable('moviesTableBody', moviesPage);
            updateTable('processedMoviesTableBody', processedMoviesPage);

            // Render paginated series trees (always render since it's a page-specific slice)
            renderSeriesTree(seriesPage, 'seriesTree');
            renderSeriesTree(processedSeriesPage, 'processedSeriesTree');

            // Update pagination controls
            updatePaginationControls('movies', movies.length);
            updatePaginationControls('processedMovies', processedMovies.length);
            updatePaginationControls('series', cachedSeriesHierarchy.length);
            updatePaginationControls('processedSeries', cachedProcessedSeriesHierarchy.length);

            // Update counts with pagination info
            const movieCountText = pageSize === 'all' || movies.length <= currentPageSize
                ? movies.length
                : `${moviesPage.length}/${movies.length}`;
            const processedMovieCountText = pageSize === 'all' || processedMovies.length <= currentPageSize
                ? processedMovies.length
                : `${processedMoviesPage.length}/${processedMovies.length}`;
            const seriesCountText = pageSize === 'all' || cachedSeriesHierarchy.length <= currentPageSize
                ? `${cachedSeriesHierarchy.length} series / ${series.length} ep`
                : `${seriesPage.length}/${cachedSeriesHierarchy.length} series / ${series.length} ep`;
            const processedSeriesCountText = pageSize === 'all' || cachedProcessedSeriesHierarchy.length <= currentPageSize
                ? `${cachedProcessedSeriesHierarchy.length} series / ${processedSeries.length} ep`
                : `${processedSeriesPage.length}/${cachedProcessedSeriesHierarchy.length} series / ${processedSeries.length} ep`;

            document.getElementById('movieCount').textContent = movieCountText;
            document.getElementById('seriesCount').textContent = seriesCountText;
            document.getElementById('processedMovieCount').textContent = processedMovieCountText;
            document.getElementById('processedSeriesCount').textContent = processedSeriesCountText;

            // Show pagination warning if needed
            if (movies.length > itemsPerPage) {
                document.getElementById('statusBar').textContent =
                    `Showing first ${itemsPerPage} items per category for performance. Use filter to narrow results.`;
                document.getElementById('statusBar').className = 'status';
            }

            updateCheckboxes();
        }

        function updateTable(tableBodyId, items) {
            const tbody = document.getElementById(tableBodyId);

            // Use DocumentFragment for faster DOM manipulation
            const fragment = document.createDocumentFragment();

            // Clear existing content
            tbody.innerHTML = '';

            // Batch DOM operations for better performance
            items.forEach((item, index) => {
                const actualIndex = allItems.indexOf(item);
                const row = document.createElement('tr');
                row.className = item.is_processed ? 'processed' : 'pending';

                // Use template literals for faster HTML creation
                // Extract filename from path, removing /content/movies/ or /content/tv/ prefixes
                let displayPath = item.release_path;
                if (displayPath.startsWith('/content/movies/')) {
                    displayPath = displayPath.substring(16); // Remove '/content/movies/'
                } else if (displayPath.startsWith('/content/tv/')) {
                    displayPath = displayPath.substring(12); // Remove '/content/tv/'
                }

                // If it's still too long, truncate it
                const truncatedPath = displayPath.length > 80
                    ? displayPath.substring(0, 80) + '...'
                    : displayPath;

                const foundStatus = item.found_in_arr === true ? '✓ Found' :
                                   item.found_in_arr === false ? '✗ Not Found' :
                                   item.is_processed ? '? Unknown' : '-';

                row.innerHTML = `
                    <td><input type="checkbox" data-index="${actualIndex}" onchange="updateSelection(this)"></td>
                    <td>${escapeHtml(item.title)}</td>
                    <td>${escapeHtml(item.category)}</td>
                    <td title="${escapeHtml(item.release_path)}">${escapeHtml(truncatedPath)}</td>
                    <td>${item.is_processed ? 'Processed' : 'Pending'}</td>
                    <td>${foundStatus}</td>
                `;
                fragment.appendChild(row);
            });

            // Single DOM update
            tbody.appendChild(fragment);
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function buildSeriesHierarchy(items) {
            const seriesMap = new Map();

            items.forEach(item => {
                const seriesName = item.title || 'Unknown Series';
                const seriesKey = seriesName.toLowerCase();
                if (!seriesMap.has(seriesKey)) {
                    seriesMap.set(seriesKey, {
                        name: seriesName,
                        key: seriesKey,
                        totalEpisodes: 0,
                        seasons: new Map()
                    });
                }

                const seriesEntry = seriesMap.get(seriesKey);
                const seasonInfo = parseSeasonEpisode(item.release_name || item.release_path || '');

                if (!seriesEntry.seasons.has(seasonInfo.seasonKey)) {
                    seriesEntry.seasons.set(seasonInfo.seasonKey, {
                        name: seasonInfo.seasonLabel,
                        key: seasonInfo.seasonKey,
                        episodes: []
                    });
                }

                const seasonEntry = seriesEntry.seasons.get(seasonInfo.seasonKey);
                const itemIndex = allItems.indexOf(item);
                seasonEntry.episodes.push({
                    item,
                    index: itemIndex,
                    episodeLabel: seasonInfo.episodeLabel,
                    releaseName: item.release_name,
                    releasePath: item.release_path
                });

                seriesEntry.totalEpisodes += 1;
            });

            return Array.from(seriesMap.values()).map(seriesEntry => {
                seriesEntry.seasons.forEach(seasonEntry => {
                    seasonEntry.episodes.sort((a, b) => {
                        const labelA = a.episodeLabel || '';
                        const labelB = b.episodeLabel || '';
                        return labelA.localeCompare(labelB, undefined, { numeric: true, sensitivity: 'base' });
                    });
                });

                return {
                    name: seriesEntry.name,
                    key: seriesEntry.key,
                    totalEpisodes: seriesEntry.totalEpisodes,
                    seasons: Array.from(seriesEntry.seasons.values()).sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }))
                };
            }).sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }));
        }

        function parseSeasonEpisode(name) {
            if (!name) {
                return {
                    seasonKey: 'season-00',
                    seasonLabel: 'Season ?',
                    episodeLabel: 'Unknown Episode'
                };
            }

            const normalized = name.replace(/[_\.]+/g, ' ');

            let seasonNumber = null;
            const seasonMatch = normalized.match(/S(\d{1,2})/i);
            const seasonWordMatch = normalized.match(/Season\s*(\d{1,2})/i);
            if (seasonMatch) {
                seasonNumber = parseInt(seasonMatch[1], 10);
            } else if (seasonWordMatch) {
                seasonNumber = parseInt(seasonWordMatch[1], 10);
            } else if (/special/i.test(normalized)) {
                seasonNumber = 0;
            }

            let episodeNumbers = Array.from(normalized.matchAll(/E(\d{1,3})/gi)).map(match => parseInt(match[1], 10));
            if (episodeNumbers.length === 0) {
                const episodeWord = normalized.match(/Episode\s*(\d{1,3})/i);
                if (episodeWord) {
                    episodeNumbers = [parseInt(episodeWord[1], 10)];
                }
            }

            const hasCompleteSeason = /complete/i.test(normalized) && episodeNumbers.length === 0;

            const seasonKey = seasonNumber === null ? 'season-00' : `season-${String(seasonNumber).padStart(2, '0')}`;
            let seasonLabel;
            if (seasonNumber === null) {
                seasonLabel = hasCompleteSeason ? 'Season Pack' : 'Specials';
            } else if (seasonNumber === 0) {
                seasonLabel = 'Specials';
            } else {
                seasonLabel = `Season ${seasonNumber}`;
            }

            let episodeLabel;
            if (episodeNumbers.length > 1) {
                episodeNumbers.sort((a, b) => a - b);
                episodeLabel = `Episodes ${episodeNumbers[0]}–${episodeNumbers[episodeNumbers.length - 1]}`;
            } else if (episodeNumbers.length === 1) {
                episodeLabel = `Episode ${episodeNumbers[0]}`;
            } else if (hasCompleteSeason) {
                episodeLabel = 'Season Pack';
            } else {
                episodeLabel = name;
            }

            return {
                seasonKey,
                seasonLabel,
                episodeLabel
            };
        }

        function renderSeriesTree(hierarchy, containerId) {
            const container = document.getElementById(containerId);
            if (!container) {
                return;
            }

            container.innerHTML = '';

            if (!hierarchy.length) {
                const empty = document.createElement('p');
                empty.className = 'empty-state';
                empty.textContent = 'No items to display.';
                container.appendChild(empty);
                return;
            }

            const fragment = document.createDocumentFragment();

            hierarchy.forEach(series => {
                const seriesWrapper = document.createElement('div');
                seriesWrapper.className = 'series-wrapper';

                const seriesCheckbox = document.createElement('input');
                seriesCheckbox.type = 'checkbox';
                seriesCheckbox.className = 'series-checkbox';
                const seriesIndices = [];

                series.seasons.forEach(season => {
                    season.episodes.forEach(episode => {
                        if (episode.index >= 0) {
                            seriesIndices.push(episode.index);
                        }
                    });
                });

                seriesCheckbox.dataset.indices = seriesIndices.join(',');
                seriesCheckbox.addEventListener('change', handleSeriesCheckboxChange);
                seriesWrapper.appendChild(seriesCheckbox);

                const seriesDetails = document.createElement('details');
                seriesDetails.className = 'series-node';

                const seriesSummary = document.createElement('summary');
                const seriesCaret = document.createElement('span');
                seriesCaret.className = 'caret';
                seriesCaret.textContent = '▶';
                seriesSummary.appendChild(seriesCaret);

                const seriesName = document.createElement('span');
                seriesName.className = 'series-name';
                seriesName.textContent = series.name;
                seriesSummary.appendChild(seriesName);

                const seriesMeta = document.createElement('span');
                seriesMeta.className = 'badge';
                seriesMeta.textContent = `${series.seasons.length} season${series.seasons.length === 1 ? '' : 's'} • ${series.totalEpisodes} episode${series.totalEpisodes === 1 ? '' : 's'}`;
                seriesSummary.appendChild(seriesMeta);

                seriesDetails.appendChild(seriesSummary);

                series.seasons.forEach(season => {
                    const seasonWrapper = document.createElement('div');
                    seasonWrapper.className = 'season-wrapper';

                    const seasonCheckbox = document.createElement('input');
                    seasonCheckbox.type = 'checkbox';
                    seasonCheckbox.className = 'season-checkbox';
                    const seasonIndices = season.episodes.map(episode => episode.index).filter(index => index >= 0);
                    seasonCheckbox.dataset.indices = seasonIndices.join(',');
                    seasonCheckbox.addEventListener('change', handleSeasonCheckboxChange);
                    seasonWrapper.appendChild(seasonCheckbox);

                    const seasonDetails = document.createElement('details');
                    seasonDetails.className = 'season-node';

                    const seasonSummary = document.createElement('summary');
                    const seasonCaret = document.createElement('span');
                    seasonCaret.className = 'caret';
                    seasonCaret.textContent = '▶';
                    seasonSummary.appendChild(seasonCaret);

                    const seasonName = document.createElement('span');
                    seasonName.className = 'season-name';
                    seasonName.textContent = season.name;
                    seasonSummary.appendChild(seasonName);

                    const seasonMeta = document.createElement('span');
                    seasonMeta.className = 'badge';
                    seasonMeta.textContent = `${season.episodes.length} episode${season.episodes.length === 1 ? '' : 's'}`;
                    seasonSummary.appendChild(seasonMeta);

                    seasonDetails.appendChild(seasonSummary);

                    const episodeList = document.createElement('ul');
                    episodeList.className = 'episode-list';

                    season.episodes.forEach(episode => {
                        const li = document.createElement('li');
                        li.className = 'episode-item';
                        li.title = episode.releasePath || episode.releaseName || '';

                        const checkbox = document.createElement('input');
                        checkbox.type = 'checkbox';
                        checkbox.dataset.index = episode.index;
                        checkbox.addEventListener('change', handleEpisodeCheckboxChange);
                        li.appendChild(checkbox);

                        const label = document.createElement('span');
                        label.className = 'episode-label';
                        label.textContent = episode.episodeLabel;
                        li.appendChild(label);

                        const statusBadge = document.createElement('span');
                        statusBadge.className = `badge ${episode.item.is_processed ? 'status-processed' : 'status-pending'}`;
                        statusBadge.textContent = episode.item.is_processed ? 'Processed' : 'Pending';
                        li.appendChild(statusBadge);

                        let foundClass = 'found-unknown';
                        let foundText = '? Unknown';
                        if (episode.item.found_in_arr === true) {
                            foundClass = 'found-yes';
                            foundText = 'Found';
                        } else if (episode.item.found_in_arr === false) {
                            foundClass = 'found-no';
                            foundText = 'Not Found';
                        } else if (!episode.item.is_processed) {
                            foundClass = 'found-unknown';
                            foundText = '-';
                        }

                        const foundBadge = document.createElement('span');
                        foundBadge.className = `badge ${foundClass}`;
                        foundBadge.textContent = foundText;
                        li.appendChild(foundBadge);

                        if (episode.releaseName && episode.releaseName !== episode.episodeLabel) {
                            const releaseInfo = document.createElement('span');
                            releaseInfo.className = 'episode-path';
                            releaseInfo.textContent = episode.releaseName;
                            li.appendChild(releaseInfo);
                        }

                        episodeList.appendChild(li);
                    });

                    seasonDetails.appendChild(episodeList);
                    seasonWrapper.appendChild(seasonDetails);
                    seriesDetails.appendChild(seasonWrapper);
                });

                seriesWrapper.appendChild(seriesDetails);
                fragment.appendChild(seriesWrapper);
            });

            container.appendChild(fragment);
        }

        function getIndicesFromDataset(element) {
            if (!element || !element.dataset || !element.dataset.indices) {
                return [];
            }
            return element.dataset.indices
                .split(',')
                .map(str => parseInt(str, 10))
                .filter(index => !Number.isNaN(index) && index >= 0);
        }

        function handleSeriesCheckboxChange(event) {
            event.stopPropagation();
            const indices = getIndicesFromDataset(event.target);
            indices.forEach(index => {
                if (index < allItems.length) {
                    allItems[index].selected = event.target.checked;
                }
            });
            updateCheckboxes();
        }

        function handleSeasonCheckboxChange(event) {
            event.stopPropagation();
            const indices = getIndicesFromDataset(event.target);
            indices.forEach(index => {
                if (index < allItems.length) {
                    allItems[index].selected = event.target.checked;
                }
            });
            updateCheckboxes();
        }

        function handleEpisodeCheckboxChange(event) {
            event.stopPropagation();
            updateSelection(event.target);
        }

        function updateHeaderToggle(elementId, items) {
            const checkbox = document.getElementById(elementId);
            if (!checkbox) {
                return;
            }

            if (!items.length) {
                checkbox.checked = false;
                checkbox.indeterminate = false;
                return;
            }

            const selectedCount = items.filter(item => item.selected).length;
            if (selectedCount === 0) {
                checkbox.checked = false;
                checkbox.indeterminate = false;
            } else if (selectedCount === items.length) {
                checkbox.checked = true;
                checkbox.indeterminate = false;
            } else {
                checkbox.checked = false;
                checkbox.indeterminate = true;
            }
        }

        function syncAggregateStates() {
            document.querySelectorAll('.season-checkbox').forEach(cb => {
                const indices = getIndicesFromDataset(cb);
                if (!indices.length) {
                    cb.checked = false;
                    cb.indeterminate = false;
                    return;
                }

                const selectedCount = indices.filter(index => allItems[index] && allItems[index].selected).length;
                if (selectedCount === 0) {
                    cb.checked = false;
                    cb.indeterminate = false;
                } else if (selectedCount === indices.length) {
                    cb.checked = true;
                    cb.indeterminate = false;
                } else {
                    cb.checked = false;
                    cb.indeterminate = true;
                }
            });

            document.querySelectorAll('.series-checkbox').forEach(cb => {
                const indices = getIndicesFromDataset(cb);
                if (!indices.length) {
                    cb.checked = false;
                    cb.indeterminate = false;
                    return;
                }

                const selectedCount = indices.filter(index => allItems[index] && allItems[index].selected).length;
                if (selectedCount === 0) {
                    cb.checked = false;
                    cb.indeterminate = false;
                } else if (selectedCount === indices.length) {
                    cb.checked = true;
                    cb.indeterminate = false;
                } else {
                    cb.checked = false;
                    cb.indeterminate = true;
                }
            });

            updateHeaderToggle('seriesSelectAll', filteredItems.filter(item => item.media_type === 'series'));
            updateHeaderToggle('processedSeriesSelectAll', processedItems.filter(item => item.media_type === 'series'));
        }

        function updateSelection(checkbox) {
            const index = parseInt(checkbox.dataset.index);
            allItems[index].selected = checkbox.checked;
            syncAggregateStates();
        }

        function switchTab(tab) {
            currentTab = tab;

            // Update tab buttons
            document.querySelectorAll('.tab-button').forEach(btn => btn.classList.remove('active'));
            document.querySelector(`[onclick="switchTab('${tab}')"]`).classList.add('active');

            // Update tab content
            document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
            document.getElementById(tab + 'Tab').classList.add('active');

            // Load scheduler history when switching to that tab
            if (tab === 'schedulerHistory') {
                refreshSchedulerHistory();
            }
        }

        function selectAll() {
            // Get current visible items on the page only
            let currentPageItems = [];
            const currentPageSize = pageSize === 'all' ? Number.MAX_SAFE_INTEGER : parseInt(pageSize);

            switch(currentTab) {
                case 'movies':
                    const movies = filteredItems.filter(item => item.media_type === 'movie');
                    const moviesStartIndex = (currentMoviesPage - 1) * currentPageSize;
                    currentPageItems = movies.slice(moviesStartIndex, moviesStartIndex + currentPageSize);
                    break;
                case 'series':
                    currentPageItems = filteredItems.filter(item => item.media_type === 'series');
                    break;
                case 'processedMovies':
                    const processedMovies = processedItems.filter(item => item.media_type === 'movie');
                    const processedMoviesStartIndex = (currentProcessedMoviesPage - 1) * currentPageSize;
                    currentPageItems = processedMovies.slice(processedMoviesStartIndex, processedMoviesStartIndex + currentPageSize);
                    break;
                case 'processedSeries':
                    currentPageItems = processedItems.filter(item => item.media_type === 'series');
                    break;
            }

            // Select only items on current page
            currentPageItems.forEach(item => {
                const index = allItems.indexOf(item);
                if (index !== -1) {
                    allItems[index].selected = true;
                }
            });

            updateCheckboxes();
        }

        function selectNone() {
            allItems.forEach(item => item.selected = false);
            updateCheckboxes();
        }

        function updateCheckboxes() {
            document.querySelectorAll('input[type="checkbox"][data-index]').forEach(checkbox => {
                const index = parseInt(checkbox.dataset.index);
                checkbox.checked = allItems[index].selected;
            });

            syncAggregateStates();
        }

        function toggleAllMovies(checkbox) {
            const movies = filteredItems.filter(item => item.media_type === 'movie');
            const currentPageSize = pageSize === 'all' ? Number.MAX_SAFE_INTEGER : parseInt(pageSize);
            const moviesStartIndex = (currentMoviesPage - 1) * currentPageSize;
            const currentPageMovies = movies.slice(moviesStartIndex, moviesStartIndex + currentPageSize);

            currentPageMovies.forEach(item => {
                const index = allItems.indexOf(item);
                if (index !== -1) {
                    allItems[index].selected = checkbox.checked;
                }
            });
            updateCheckboxes();
        }

        function toggleAllSeries(checkbox) {
            const seriesEpisodes = filteredItems.filter(item => item.media_type === 'series');
            seriesEpisodes.forEach(item => {
                const index = allItems.indexOf(item);
                if (index !== -1) {
                    allItems[index].selected = checkbox.checked;
                }
            });
            updateCheckboxes();
        }

        function toggleAllProcessedMovies(checkbox) {
            const processedMovies = processedItems.filter(item => item.media_type === 'movie');
            const currentPageSize = pageSize === 'all' ? Number.MAX_SAFE_INTEGER : parseInt(pageSize);
            const processedMoviesStartIndex = (currentProcessedMoviesPage - 1) * currentPageSize;
            const currentPageProcessedMovies = processedMovies.slice(processedMoviesStartIndex, processedMoviesStartIndex + currentPageSize);

            currentPageProcessedMovies.forEach(item => {
                const index = allItems.indexOf(item);
                if (index !== -1) {
                    allItems[index].selected = checkbox.checked;
                }
            });
            updateCheckboxes();
        }

        function toggleAllProcessedSeries(checkbox) {
            const processedSeriesEpisodes = processedItems.filter(item => item.media_type === 'series');
            processedSeriesEpisodes.forEach(item => {
                const index = allItems.indexOf(item);
                if (index !== -1) {
                    allItems[index].selected = checkbox.checked;
                }
            });
            updateCheckboxes();
        }

        function processSelected() {
            const selectedIndices = allItems
                .map((item, index) => item.selected ? index : -1)
                .filter(index => index !== -1);

            if (selectedIndices.length === 0) {
                alert('Please select items to process.');
                return;
            }

            if (confirm(`Process ${selectedIndices.length} selected item(s)?`)) {
                fetch('/api/process', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ indices: selectedIndices })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        document.getElementById('statusBar').textContent = data.success;
                    } else {
                        alert('Error: ' + data.error);
                    }
                });
            }
        }

        let lastProcessingState = false;

        function updateStatus() {
            fetch('/api/status')
                .then(response => response.json())
                .then(status => {
                    if (status.active) {
                        document.getElementById('statusBar').textContent = status.message;
                        document.getElementById('statusBar').className = 'status processing';
                        lastProcessingState = true;
                    } else {
                        if (status.message) {
                            document.getElementById('statusBar').textContent = status.message;
                        }
                        document.getElementById('statusBar').className = 'status';

                        // Only reload items if processing just finished
                        if (lastProcessingState) {
                            loadItems();
                            lastProcessingState = false;
                        }
                    }
                })
                .catch(err => {
                    console.error('Status update failed:', err);
                });
        }

        function updateSchedulerStatus() {
            fetch('/api/scheduler_status')
                .then(response => response.json())
                .then(status => {
                    const container = document.getElementById('schedulerStatus');
                    const moviesCard = document.getElementById('schedulerMoviesStatus');
                    const seriesCard = document.getElementById('schedulerSeriesStatus');
                    const checkCard = document.getElementById('schedulerCheckFoundStatus');

                    function renderCard(element, label, scheduler) {
                        if (!scheduler) {
                            element.innerHTML = `<div class="status-label">${label}</div><div class="meta">Unavailable</div>`;
                            element.classList.add('disabled');
                            return false;
                        }

                        const enabled = Boolean(scheduler.enabled);
                        const running = Boolean(scheduler.running);
                        const nextRun = scheduler.next_run ? new Date(scheduler.next_run).toLocaleString() : 'N/A';
                        const lastRun = scheduler.last_run ? new Date(scheduler.last_run).toLocaleString() : 'Never';

                        let html = `<div class="status-label">${label}${running ? ' 🔄' : ''}</div>`;
                        html += `<div class="next-run">${enabled ? `Next run: ${nextRun}` : 'Disabled'}</div>`;
                        html += `<div class="meta">Last run: ${lastRun}</div>`;
                        if (running) {
                            html += '<div class="meta">Processing in progress</div>';
                        }

                        element.innerHTML = html;
                        element.classList.toggle('disabled', !enabled);
                        return enabled;
                    }

                    renderCard(moviesCard, 'Movies', status.movies);
                    renderCard(seriesCard, 'Series', status.series);
                    renderCard(checkCard, 'Check Found', status.check_found);

                    const hasAnyStatus = Boolean(status.movies || status.series || status.check_found);
                    container.style.display = hasAnyStatus ? 'block' : 'none';
                })
                .catch(err => console.error('Failed to update scheduler status:', err));
        }

        function openSettings() {
            document.getElementById('settingsModal').style.display = 'block';
        }

        function closeSettings() {
            document.getElementById('settingsModal').style.display = 'none';
        }

        function loadConfig() {
            fetch('/api/config')
                .then(response => response.json())
                .then(config => {
                    Object.keys(config).forEach(key => {
                        const element = document.getElementById(key);
                        if (element) {
                            if (element.type === 'checkbox') {
                                element.checked = config[key];
                            } else {
                                element.value = config[key];
                            }
                        }
                    });
                });
        }

        function saveSettings() {
            const config = {};
            ['radarr_url', 'radarr_api_key', 'sonarr_url', 'sonarr_api_key',
             'batch_size', 'max_batch_size', 'api_delay', 'sonarr_delete_whole_season',
             'schedule_movies_enabled', 'schedule_movies_count', 'schedule_movies_interval',
             'schedule_series_enabled', 'schedule_series_count', 'schedule_series_interval',
             'schedule_check_found_enabled', 'schedule_check_found_interval'].forEach(key => {
                const element = document.getElementById(key);
                if (element) {
                    if (element.type === 'checkbox') {
                        config[key] = element.checked;
                    } else {
                        config[key] = element.type === 'number' ? parseFloat(element.value) : element.value;
                    }
                }
            });

            fetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config)
            })
            .then(response => response.json())
            .then(data => {
                alert(data.success || data.error);
                closeSettings();
            });
        }

        function testConnections() {
            const config = {
                radarr_url: document.getElementById('radarr_url').value,
                radarr_api_key: document.getElementById('radarr_api_key').value,
                sonarr_url: document.getElementById('sonarr_url').value,
                sonarr_api_key: document.getElementById('sonarr_api_key').value
            };

            if (!config.radarr_url && !config.sonarr_url) {
                alert('Please configure at least one service URL first');
                return;
            }

            // Show testing message
            const statusMsg = document.createElement('div');
            statusMsg.textContent = 'Testing connections...';
            statusMsg.style.cssText = 'margin: 10px 0; padding: 10px; background: #e8f4f8; border-radius: 4px;';
            document.querySelector('#settingsModal .modal-content').insertBefore(
                statusMsg,
                document.querySelector('#settingsModal button[type="submit"]')
            );

            fetch('/api/test_connections', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config)
            })
            .then(response => response.json())
            .then(data => {
                statusMsg.remove();
                let message = "Connection Test Results:\\n\\n";

                if (data.radarr) {
                    message += "Radarr: " + (data.radarr.success ? "✓ Connected" : "✗ Failed") + "\\n";
                    if (!data.radarr.success) {
                        message += "  Error: " + data.radarr.error + "\\n";
                    }
                }

                if (data.sonarr) {
                    message += "Sonarr: " + (data.sonarr.success ? "✓ Connected" : "✗ Failed") + "\\n";
                    if (!data.sonarr.success) {
                        message += "  Error: " + data.sonarr.error + "\\n";
                    }
                }

                alert(message);
            })
            .catch(err => {
                statusMsg.remove();
                alert('Connection test failed: ' + err.message);
            });
        }

        // Pagination functions
        function updatePaginationControls(type, totalItems) {
            let currentPageVar;
            const currentPageSize = pageSize === 'all' ? Number.MAX_SAFE_INTEGER : parseInt(pageSize);
            const totalPages = Math.ceil(totalItems / currentPageSize);

            switch(type) {
                case 'movies': currentPageVar = currentMoviesPage; break;
                case 'series': currentPageVar = currentSeriesPage; break;
                case 'processedMovies': currentPageVar = currentProcessedMoviesPage; break;
                case 'processedSeries': currentPageVar = currentProcessedSeriesPage; break;
                default: currentPageVar = 1;
            }

            // Handle button IDs based on type
            const buttonPrefix = type === 'processedMovies' ? 'processedMovies' :
                                 type === 'processedSeries' ? 'processedSeries' : type;
            const pageInfoId = `${buttonPrefix}PageInfo`;
            const prevBtnId = `${buttonPrefix}PrevBtn`;
            const nextBtnId = `${buttonPrefix}NextBtn`;

            const pageInfoElement = document.getElementById(pageInfoId);
            const prevBtnElement = document.getElementById(prevBtnId);
            const nextBtnElement = document.getElementById(nextBtnId);

            if (pageInfoElement) {
                if (pageSize === 'all') {
                    pageInfoElement.textContent = `${totalItems} items`;
                } else {
                    pageInfoElement.textContent = `Page ${currentPageVar} of ${totalPages} (${totalItems} items)`;
                }
            }

            if (prevBtnElement) prevBtnElement.disabled = currentPageVar <= 1 || pageSize === 'all';
            if (nextBtnElement) nextBtnElement.disabled = currentPageVar >= totalPages || pageSize === 'all';
        }

        function updateCurrentPage() {
            // Lightweight page update - only update the visible tab
            const currentPageSize = pageSize === 'all' ? Number.MAX_SAFE_INTEGER : parseInt(pageSize);

            if (currentTab === 'movies') {
                const movies = filteredItems.filter(item => item.media_type === 'movie');
                const moviesStartIndex = (currentMoviesPage - 1) * currentPageSize;
                const moviesPage = movies.slice(moviesStartIndex, moviesStartIndex + currentPageSize);
                updateTable('moviesTableBody', moviesPage);
                updatePaginationControls('movies', movies.length);
            } else if (currentTab === 'processedMovies') {
                const processedMovies = processedItems.filter(item => item.media_type === 'movie');
                const processedMoviesStartIndex = (currentProcessedMoviesPage - 1) * currentPageSize;
                const processedMoviesPage = processedMovies.slice(processedMoviesStartIndex, processedMoviesStartIndex + currentPageSize);
                updateTable('processedMoviesTableBody', processedMoviesPage);
                updatePaginationControls('processedMovies', processedMovies.length);
            } else if (currentTab === 'series') {
                // Series tab - paginate and render
                const seriesStartIndex = (currentSeriesPage - 1) * currentPageSize;
                const seriesPage = cachedSeriesHierarchy.slice(seriesStartIndex, seriesStartIndex + currentPageSize);
                renderSeriesTree(seriesPage, 'seriesTree');
                updatePaginationControls('series', cachedSeriesHierarchy.length);
            } else if (currentTab === 'processedSeries') {
                // Processed series tab - paginate and render
                const processedSeriesStartIndex = (currentProcessedSeriesPage - 1) * currentPageSize;
                const processedSeriesPage = cachedProcessedSeriesHierarchy.slice(processedSeriesStartIndex, processedSeriesStartIndex + currentPageSize);
                renderSeriesTree(processedSeriesPage, 'processedSeriesTree');
                updatePaginationControls('processedSeries', cachedProcessedSeriesHierarchy.length);
            }
        }

        function previousMoviesPage() {
            if (currentMoviesPage > 1) {
                currentMoviesPage--;
                updateCurrentPage();
            }
        }

        function nextMoviesPage() {
            const movies = filteredItems.filter(item => item.media_type === 'movie');
            const currentPageSize = pageSize === 'all' ? Number.MAX_SAFE_INTEGER : parseInt(pageSize);
            const totalPages = Math.ceil(movies.length / currentPageSize);
            if (currentMoviesPage < totalPages) {
                currentMoviesPage++;
                updateCurrentPage();
            }
        }

        function previousSeriesPage() {
            if (currentSeriesPage > 1) {
                currentSeriesPage--;
                updateCurrentPage();
            }
        }

        function nextSeriesPage() {
            const currentPageSize = pageSize === 'all' ? Number.MAX_SAFE_INTEGER : parseInt(pageSize);
            const totalPages = Math.ceil(cachedSeriesHierarchy.length / currentPageSize);
            if (currentSeriesPage < totalPages) {
                currentSeriesPage++;
                updateCurrentPage();
            }
        }

        // New pagination functions for processed items
        function previousProcessedMoviesPage() {
            if (currentProcessedMoviesPage > 1) {
                currentProcessedMoviesPage--;
                updateCurrentPage();
            }
        }

        function nextProcessedMoviesPage() {
            const processedMovies = processedItems.filter(item => item.media_type === 'movie');
            const currentPageSize = pageSize === 'all' ? Number.MAX_SAFE_INTEGER : parseInt(pageSize);
            const totalPages = Math.ceil(processedMovies.length / currentPageSize);
            if (currentProcessedMoviesPage < totalPages) {
                currentProcessedMoviesPage++;
                updateCurrentPage();
            }
        }

        function previousProcessedSeriesPage() {
            if (currentProcessedSeriesPage > 1) {
                currentProcessedSeriesPage--;
                updateCurrentPage();
            }
        }

        function nextProcessedSeriesPage() {
            const currentPageSize = pageSize === 'all' ? Number.MAX_SAFE_INTEGER : parseInt(pageSize);
            const totalPages = Math.ceil(cachedProcessedSeriesHierarchy.length / currentPageSize);
            if (currentProcessedSeriesPage < totalPages) {
                currentProcessedSeriesPage++;
                updateCurrentPage();
            }
        }

        // Page size change function
        function changePageSize() {
            pageSize = document.getElementById('pageSizeSelect').value;
            itemsPerPage = pageSize === 'all' ? Number.MAX_SAFE_INTEGER : parseInt(pageSize);

            // Reset all pages to 1
            currentMoviesPage = 1;
            currentSeriesPage = 1;
            currentProcessedMoviesPage = 1;
            currentProcessedSeriesPage = 1;

            applyFilter();
        }

        // Mark selected items as not processed
        function markSelectedAsNotProcessed() {
            const selectedIndices = allItems
                .map((item, index) => item.selected ? index : -1)
                .filter(index => index !== -1);
            if (selectedIndices.length === 0) {
                alert('Please select items to mark as not processed.');
                return;
            }

            if (confirm(`Mark ${selectedIndices.length} selected item(s) as not processed?`)) {
                fetch('/api/mark_not_processed', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ indices: selectedIndices })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        document.getElementById('statusBar').textContent = data.success;
                        loadItems(); // Refresh to show updated status
                    } else {
                        alert('Error: ' + data.error);
                    }
                })
                .catch(err => {
                    console.error('Mark not processed failed:', err);
                    alert('Error marking items as not processed');
                });
            }
        }

        // Check found status for selected items
        function checkFoundStatus() {
            const selectedIndices = allItems
                .map((item, index) => item.selected ? index : -1)
                .filter(index => index !== -1);
            if (selectedIndices.length === 0) {
                alert('Please select items to check found status.');
                return;
            }

            // Filter to only processed items
            const processedSelected = selectedIndices.filter(index => {
                return allItems[index] && allItems[index].is_processed;
            });

            if (processedSelected.length === 0) {
                alert('Please select processed items to check found status.');
                return;
            }

            if (confirm(`Check found status for ${processedSelected.length} processed item(s)?`)) {
                document.getElementById('statusBar').textContent = 'Checking found status...';
                document.getElementById('statusBar').className = 'status processing';

                fetch('/api/check_found', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ indices: processedSelected })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        document.getElementById('statusBar').textContent = data.success;
                        document.getElementById('statusBar').className = 'status';
                        refreshItems(); // Force reload from database to show updated status
                    } else {
                        alert('Error: ' + data.error);
                        document.getElementById('statusBar').textContent = 'Error checking found status';
                        document.getElementById('statusBar').className = 'status';
                    }
                })
                .catch(err => {
                    console.error('Check found status failed:', err);
                    alert('Error checking found status');
                    document.getElementById('statusBar').textContent = 'Error checking found status';
                    document.getElementById('statusBar').className = 'status';
                });
            }
        }
    </script>
</body>
</html>
        """
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode())

    def serve_css(self):
        """Serve CSS styles"""
        css = """
body {
    font-family: Arial, sans-serif;
    margin: 0;
    padding: 20px;
    background-color: #f5f5f5;
}

.container {
    max-width: 1200px;
    margin: 0 auto;
    background: white;
    border-radius: 8px;
    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
}

header {
    background: #2c3e50;
    color: white;
    padding: 20px;
    border-radius: 8px 8px 0 0;
}

header h1 {
    margin: 0 0 15px 0;
}

.toolbar button {
    background: #3498db;
    color: white;
    border: none;
    padding: 8px 16px;
    margin-right: 10px;
    border-radius: 4px;
    cursor: pointer;
}

.toolbar button:hover {
    background: #2980b9;
}

.filters {
    padding: 15px 20px;
    background: #ecf0f1;
    border-bottom: 1px solid #ddd;
}

.filters input[type="text"] {
    padding: 8px;
    width: 300px;
    margin-right: 20px;
    border: 1px solid #ddd;
    border-radius: 4px;
}

.filters select {
    padding: 8px;
    margin-left: 10px;
    border: 1px solid #ddd;
    border-radius: 4px;
    background: white;
}

.filters label {
    margin-left: 20px;
    font-weight: bold;
}

.status {
    padding: 10px 20px;
    background: #d4edda;
    border-bottom: 1px solid #ddd;
    font-weight: bold;
}

.status.processing {
    background: #fff3cd;
    color: #856404;
}

.scheduler-status {
    background: #1a1a1a;
    color: #f5f5f5;
    margin: 12px 20px;
    padding: 12px 16px;
    border-radius: 6px;
}

.scheduler-status-title {
    font-weight: bold;
    margin-bottom: 8px;
}

.scheduler-status-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
}

.scheduler-status-card {
    flex: 1 1 200px;
    background: #232323;
    border-radius: 6px;
    padding: 10px 12px;
    min-width: 180px;
    display: flex;
    flex-direction: column;
    gap: 4px;
}

.scheduler-status-card.disabled {
    opacity: 0.45;
}

.scheduler-status-card .status-label {
    font-weight: bold;
}

.scheduler-status-card .next-run {
    font-size: 0.9em;
}

.scheduler-status-card .meta {
    font-size: 0.8em;
    color: #c0c0c0;
}

.tabs {
    display: flex;
    background: #f8f9fa;
    border-bottom: 1px solid #ddd;
}

.tab-button {
    background: none;
    border: none;
    padding: 15px 25px;
    cursor: pointer;
    border-bottom: 3px solid transparent;
}

.tab-button.active {
    border-bottom-color: #3498db;
    background: white;
}

.content {
    padding: 20px;
}

.tab-content {
    display: none;
}

.tab-content.active {
    display: block;
}

table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 10px;
}

th, td {
    text-align: left;
    padding: 12px;
    border-bottom: 1px solid #ddd;
}

th {
    background: #f8f9fa;
    font-weight: bold;
}

tr.processed {
    background: #e8f5e8;
}

tr.pending {
    background: #fff8dc;
}

tr:hover {
    background: #f0f0f0;
}

.pagination-controls {
    margin: 15px 0;
    text-align: center;
    padding: 10px;
    background: #f8f9fa;
    border-radius: 5px;
}

.pagination-controls button {
    padding: 8px 16px;
    margin: 0 10px;
    background: #3498db;
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
}

.pagination-controls button:disabled {
    background: #ccc;
    cursor: not-allowed;
}

.pagination-controls span {
    margin: 0 10px;
    font-weight: bold;
}

.group-toolbar {
    margin: 10px 0;
    display: flex;
    justify-content: flex-end;
    font-size: 0.9rem;
}

.group-toolbar label {
    display: flex;
    align-items: center;
    gap: 0.4rem;
}

.series-tree {
    border: 1px solid #e0e0e0;
    border-radius: 6px;
    background: #fdfdfd;
    padding: 10px 14px;
    max-height: 600px;
    overflow-y: auto;
}

.series-tree details {
    margin-bottom: 6px;
}

.series-tree summary {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    list-style: none;
    cursor: pointer;
}

.series-tree summary::-webkit-details-marker {
    display: none;
}

.series-wrapper, .season-wrapper {
    display: flex;
    align-items: flex-start;
    gap: 8px;
}

.series-wrapper > input, .season-wrapper > input {
    margin-top: 6px;
    flex-shrink: 0;
}

.series-wrapper > details, .season-wrapper > details {
    flex: 1;
}

.series-tree .series-node > summary {
    font-weight: 600;
}

.series-tree .season-node {
    margin-left: 1.25rem;
}

.series-tree .season-node > summary {
    font-weight: 500;
}

.series-tree .episode-list {
    list-style: none;
    margin: 0.25rem 0 0.75rem 2.25rem;
    padding: 0;
}

.series-tree .episode-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 2px 0;
}

.series-tree .episode-label {
    flex: 1;
}

.series-tree .episode-path {
    color: #555;
    font-size: 0.75rem;
}

.badge {
    display: inline-block;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.75rem;
    background: #e8f0fe;
    color: #1a73e8;
}

.badge.status-processed {
    background: #e6f4ea;
    color: #0b8043;
}

.badge.status-pending {
    background: #fce8e6;
    color: #d93025;
}

.badge.found-yes {
    background: #e6f4ea;
    color: #0b8043;
}

.badge.found-no {
    background: #fce8e6;
    color: #d93025;
}

.badge.found-unknown {
    background: #fef7e0;
    color: #b06000;
}

.caret {
    display: inline-block;
    transition: transform 0.2s ease;
    font-size: 0.75rem;
    color: #607d8b;
}

details[open] > summary .caret {
    transform: rotate(90deg);
}

.empty-state {
    padding: 12px;
    color: #666;
    font-style: italic;
}

/* Modal styles */
.modal {
    display: none;
    position: fixed;
    z-index: 1;
    left: 0;
    top: 0;
    width: 100%;
    height: 100%;
    background-color: rgba(0,0,0,0.4);
}

.modal-content {
    background-color: white;
    margin: 5% auto;
    padding: 20px;
    border-radius: 8px;
    width: 80%;
    max-width: 600px;
    max-height: 80vh;
    overflow-y: auto;
}

.close {
    color: #aaa;
    float: right;
    font-size: 28px;
    font-weight: bold;
    cursor: pointer;
}

.close:hover {
    color: black;
}

#settingsForm label {
    display: block;
    margin: 10px 0;
}

#settingsForm input {
    width: 100%;
    padding: 8px;
    margin-top: 5px;
    border: 1px solid #ddd;
    border-radius: 4px;
}

#settingsForm button {
    background: #3498db;
    color: white;
    border: none;
    padding: 10px 20px;
    margin: 10px 10px 0 0;
    border-radius: 4px;
    cursor: pointer;
}

#settingsForm button:hover {
    background: #2980b9;
}

#settingsForm h3 {
    color: #2c3e50;
    border-bottom: 1px solid #ddd;
    padding-bottom: 5px;
    margin-top: 20px;
}
        """
        self.send_response(200)
        self.send_header('Content-type', 'text/css')
        self.end_headers()
        self.wfile.write(css.encode())

    def serve_items(self):
        """Serve items data as JSON"""
        # Use cached data to avoid database queries on every request
        self.send_json_response(self.app.items_data)

    def serve_status(self):
        """Serve processing status"""
        self.send_json_response(self.app.processing_status)

    def serve_config(self):
        """Serve configuration data"""
        self.send_json_response(self.app.config.data)

    def serve_scheduler_status(self):
        """Serve scheduler status data"""
        self.send_json_response(self.app.scheduler_status)

    def serve_scheduler_history(self):
        """Serve scheduler history data"""
        history = self.app.status_db.get_scheduler_history(20)
        self.send_json_response(history)

    def send_json_response(self, data):
        """Send JSON response"""
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        """Log requests for debugging"""
        print(f"Request: {format % args}")


def create_handler(app):
    """Create request handler with app instance"""
    class AppHandler(NZBDAVWebHandler):
        def __init__(self, *args, **kwargs):
            self.app = app
            super().__init__(*args, **kwargs)
    return AppHandler


def main():
    """Main entry point"""
    print("Starting NZBDAVMigrator Web Application...", flush=True)

    # Ensure output is flushed immediately
    import sys
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    print(f"Python version: {sys.version}", flush=True)
    print(f"Working directory: {os.getcwd()}", flush=True)
    print(f"Files in current directory: {os.listdir('.')}", flush=True)

    try:
        print("Creating application instance...", flush=True)
        app = NZBDAVMigratorApp()
        print("✓ Application initialized successfully", flush=True)
    except Exception as e:
        print(f"✗ Failed to initialize application: {e}", flush=True)
        import traceback
        traceback.print_exc()
        print("Exiting due to initialization failure", flush=True)
        sys.exit(1)

    # Initial load of items
    try:
        print("Loading initial data...", flush=True)
        app.refresh_items()
        print("✓ Initial data loaded", flush=True)
    except Exception as e:
        print(f"Warning: Could not load initial data: {e}", flush=True)

    # Start web server
    host = app.config.get("host")
    port = app.config.get("port")

    print(f"Creating HTTP server on {host}:{port}", flush=True)

    try:
        handler = create_handler(app)
        httpd = HTTPServer((host, port), handler)
        print("✓ HTTP server created successfully", flush=True)
    except Exception as e:
        print(f"✗ Failed to create HTTP server: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print(f"NZBDAVMigrator Web Interface starting on http://{host}:{port}", flush=True)
    print("Press Ctrl+C to stop the server", flush=True)
    print(f"Server binding to {host}:{port} and ready for connections", flush=True)

    try:
        app.start_background_tasks()
        print("Server starting...", flush=True)
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...", flush=True)
        httpd.shutdown()
    except Exception as e:
        print(f"Error starting server: {e}", flush=True)
        import traceback
        traceback.print_exc()
        httpd.shutdown()
        sys.exit(1)
    finally:
        app.stop_background_tasks()
        httpd.server_close()


if __name__ == "__main__":
    main()
