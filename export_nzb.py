#!/usr/bin/env python3
import os, re, json, sqlite3, html, sys, argparse, urllib.request, urllib.error, urllib.parse, time, random
from datetime import datetime
from collections import defaultdict, Counter
import traceback

# Defaults (can be overridden via CLI)
DB = "db.sqlite"
OUTDIR = "exported_nzbs"
GROUP = "alt.binaries.topless-lap"  # change if you prefer

# Include/exclude knobs
INCLUDE_SAMPLE = True
INCLUDE_NFO = True
INCLUDE_SUBS = True
INCLUDE_PAR2 = True
INCLUDE_SFV = True
INCLUDE_RAR = True
INCLUDE_IMAGES = True
INCLUDE_OTHER = True

# Limit segments per file (None = all)
MAX_SEGS_PER_FILE = None

# Default to a common Usenet article size (~774 KiB) if DB size is unknown
FALLBACK_SEG_BYTES = int(os.getenv("NZB_FALLBACK_SEG_BYTES", 792782))

VIDEO_EXTS = {"mkv","mp4","avi","m2ts","ts","mpg","mov","wmv","vob"}
SUBS_EXTS  = {"srt","sub","ass","idx","sup"}
ARCHIVE_EXTS = {"rar","r00","r01","r02","7z","zip"}
IMAGE_EXTS = {"jpg","jpeg","png","gif","webp","bmp"}
AUX_EXTS = {"nfo","par2","sfv","txt","md"}

# ---------- NEW: name cleaning + classification ----------

YEAR_RE = r'(19\d{2}|20\d{2})'
SERIES_MARKERS = re.compile(r'(?i)(S\d{1,2}E\d{1,3}|S\d{1,2}\b|Season[ ._-]*\d{1,2}\b)')
MOVIE_YEAR = re.compile(rf'\b{YEAR_RE}\b')

NOISE_TOKENS = re.compile(
    r'(?ix)\b('
    r'480p|720p|1080p|2160p|4k|8k|10bit|8bit|x264|x265|h\.?264|h\.?265|hevc|avc|'
    r'webrip|web[- ]?dl|blu[- ]?ray|b[dr]rip|dvdrip|hdr10\+?|hdr|dolby(?:\s+vision)?|dv|remux|'
    r'proper|repack\d*|readnfo|extended|uncut|imax|internal|complete|limited|multi(?:lang)?|'
    r'subs?|dub(?:bed)?|dual[- ]audio|'
    r'dts(?:-?hd)?|truehd|atmos|aac|ac3|eac3|ddp?5\.1|5\.1|7\.1|'
    r'amzn|nf|itunes|hmax|uhd|sd|cam|ts|r5|telesync'
    r')\b'
)

BRACKETS = re.compile(r'[\[\(\{].*?[\]\)\}]')
TITLE_KEY_RE = re.compile(r'[^a-z0-9]+')

def _norm_spaces(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip()

def clean_release_name(name: str) -> str:
    if not name:
        return "unnamed"

    base = name.strip()

    # kill extension-like tail in release folder names
    base = re.sub(r'\.(nzb)$', '', base, flags=re.I)

    # drop group tag at end: "-GROUP" or "[GROUP]"
    base = re.sub(r'[-._ ]\w{2,}$', lambda m: '' if len(m.group(0)) <= 15 else m.group(0), base)

    # Handle parentheses more carefully - remove content but fix orphaned brackets
    # First, remove complete bracket pairs with junk inside
    base = re.sub(r'\[([^[\]]*(?:rip|web|blu|dvd|hdtv|x264|x265|h264|h265)[^[\]]*)\]', ' ', base, flags=re.I)
    base = re.sub(r'\(([^()]*(?:rip|web|blu|dvd|hdtv|x264|x265|h264|h265)[^()]*)\)', ' ', base, flags=re.I)

    # Remove remaining bracket content but be more careful
    base = BRACKETS.sub(' ', base)

    # Clean up any remaining orphaned brackets/parentheses
    base = re.sub(r'[\[\(\{]+[\s]*$', '', base)  # Remove opening brackets at end
    base = re.sub(r'^[\s]*[\]\)\}]+', '', base)  # Remove closing brackets at start
    base = re.sub(r'[\[\(\{][\s]*[\]\)\}]', ' ', base)  # Remove empty bracket pairs
    base = re.sub(r'[\[\(\{][\s]*$', '', base)  # Remove lone opening brackets at end
    base = re.sub(r'^[\s]*[\]\)\}]', '', base)  # Remove lone closing brackets at start

    # turn separators to spaces
    base = re.sub(r'[._]+', ' ', base)

    # remove noise tokens
    base = NOISE_TOKENS.sub(' ', base)

    # trim punctuation noise
    base = re.sub(r'[|]+', ' ', base)

    # Clean up extra spaces and trailing punctuation
    base = _norm_spaces(base)
    base = re.sub(r'[^\w\s]+$', '', base)  # Remove trailing punctuation
    base = re.sub(r'^[^\w\s]+', '', base)  # Remove leading punctuation

    result = base.strip()
    return result if result else "unnamed"

def is_series(release_name: str, category_hint: str) -> bool:
    rn = release_name or ''
    if category_hint and category_hint.lower() in ('tv','series','shows','television'):
        return True
    if SERIES_MARKERS.search(rn.replace('.', ' ').replace('_',' ')):
        return True
    return False

def is_movie(release_name: str, category_hint: str) -> bool:
    rn = release_name or ''
    if category_hint and category_hint.lower() in ('movie','movies','films','film'):
        return True
    if MOVIE_YEAR.search(rn) and not is_series(rn, category_hint):
        return True
    return False

def extract_series_title(release_name: str) -> str:
    if not release_name:
        return "unnamed"

    s = release_name.replace('_',' ').replace('.',' ')

    # Keep everything before Sxx or Season N if present
    m = re.search(r'(?i)^(.*?)[ ._-]*(S\d{1,2}E\d{1,3}|S\d{1,2}\b|Season[ ._-]*\d{1,2}\b)', s)
    if m:
        title_part = m.group(1).strip()
    else:
        # No season info found, use whole string but remove obvious release junk
        release_indicators = r'\b(dvdrip|brrip|webrip|720p|1080p|2160p|4k|x264|x265|hdtv|complete)\b'
        match = re.search(release_indicators, s, flags=re.I)
        if match:
            title_part = s[:match.start()].strip()
        else:
            title_part = s

    # Use gentle cleaning for series too
    cleaned = clean_movie_title_gentle(title_part)
    return cleaned if cleaned else "unnamed"

def extract_movie_title(release_name: str) -> str:
    if not release_name:
        return "unnamed"

    s = release_name.replace('_',' ').replace('.',' ')

    # Find year pattern - prefer the last/latest year found (more likely to be release year)
    year_matches = list(MOVIE_YEAR.finditer(s))
    if year_matches:
        # Take the last year found (most likely to be release year, not part of title)
        year_match = year_matches[-1]
        year = year_match.group(0)
    else:
        year_match = None
        year = None

    if year_match:
        # Extract everything before the year
        title_part = s[:year_match.start()].strip()
    else:
        # No year found, try to extract before common release indicators
        release_indicators = r'\b(dvdrip|brrip|webrip|720p|1080p|2160p|4k|x264|x265|hdtv|bluray|blu-ray)\b'
        match = re.search(release_indicators, s, flags=re.I)
        if match:
            title_part = s[:match.start()].strip()
        else:
            title_part = s

    # Clean the title part more gently - preserve original spacing
    cleaned_title = clean_movie_title_gentle(title_part)

    # If we have a year, include it for better Radarr matching
    if year and cleaned_title and cleaned_title != "unnamed":
        final_title = f"{cleaned_title} ({year})"
    else:
        final_title = cleaned_title

    return final_title if final_title else "unnamed"

def clean_movie_title_gentle(title: str) -> str:
    """Gentle cleaning that preserves title structure better"""
    if not title:
        return "unnamed"

    # Remove only obvious junk, preserve the core title
    cleaned = title.strip()

    # Remove group tags at the end
    cleaned = re.sub(r'[-.]([A-Z]{2,}|[A-Z]+[0-9]+)$', '', cleaned)

    # Remove obvious release info in brackets/parentheses but be selective
    cleaned = re.sub(r'\[([^[\]]*(?:rip|web|dvd|blu|x264|x265|h264|h265|720p|1080p)[^[\]]*)\]', '', cleaned, flags=re.I)
    cleaned = re.sub(r'\(([^()]*(?:rip|web|dvd|blu|x264|x265|h264|h265|720p|1080p)[^()]*)\)', '', cleaned, flags=re.I)

    # Clean up multiple spaces
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    # Remove trailing/leading special characters but preserve apostrophes and hyphens in titles
    cleaned = re.sub(r'^[^\w\s\'-]+|[^\w\s\'-]+$', '', cleaned)

    return cleaned if cleaned else "unnamed"


def _title_key(name: str) -> str:
    if not name:
        return ""
    return TITLE_KEY_RE.sub('', name.lower())


def _build_radarr_title_index(movies):
    index = {}
    for movie in movies or []:
        if not isinstance(movie, dict):
            continue
        names = set()
        names.add(movie.get('title'))
        names.add(movie.get('originalTitle'))
        for alt in movie.get('alternateTitles') or []:
            if isinstance(alt, dict):
                alt_title = alt.get('title')
            else:
                alt_title = str(alt)
            if alt_title:
                names.add(alt_title)
        for candidate in (c for c in names if c):
            key = _title_key(candidate)
            if key:
                index.setdefault(key, []).append(movie)
    return index


def _radarr_prepare_movie_payload(movie):
    if not isinstance(movie, dict):
        return None
    title = movie.get('title')
    quality_profile_id = movie.get('qualityProfileId') or movie.get('profileId')
    tmdb_id = movie.get('tmdbId') or movie.get('tmdbID')
    imdb_id = movie.get('imdbId')
    movie_path = movie.get('path')
    root_folder = movie.get('rootFolderPath')
    if not root_folder and movie_path:
        root_folder = os.path.dirname(movie_path.rstrip('/\\')) or None
    if not title or not quality_profile_id or not (tmdb_id or imdb_id) or not root_folder:
        return None

    payload = {
        'title': title,
        'titleSlug': movie.get('titleSlug') or movie.get('folderName'),
        'qualityProfileId': quality_profile_id,
        'tmdbId': tmdb_id,
        'imdbId': imdb_id,
        'year': movie.get('year'),
        'monitored': movie.get('monitored', True),
        'minimumAvailability': movie.get('minimumAvailability') or 'announced',
        'rootFolderPath': root_folder,
        'path': movie_path or os.path.join(root_folder, movie.get('titleSlug') or title),
        'tags': movie.get('tags', []),
        'addOptions': {'searchForMovie': False}
    }
    language_profile_id = movie.get('languageProfileId')
    if language_profile_id:
        payload['languageProfileId'] = language_profile_id
    return {k: v for k, v in payload.items() if v is not None}


def _api_request(base_url: str, api_key: str, endpoint: str, payload=None, timeout: float = 15.0, method: str = None):
    base = (base_url or "").strip().rstrip('/')
    if not base:
        raise ValueError("Base URL is required")
    url = base + '/' + endpoint.lstrip('/')
    actual_method = method or ('POST' if payload is not None else 'GET')
    data = None
    headers = {
        'X-Api-Key': (api_key or '').strip(),
        'Accept': 'application/json',
    }
    if payload is not None and actual_method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload).encode('utf-8')
    elif payload is not None:
        data = json.dumps(payload).encode('utf-8')

    req = urllib.request.Request(url, data=data, method=actual_method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        if not body:
            return None
        content_type = (resp.headers.get('Content-Type') or '').lower()
        if 'application/json' in content_type:
            return json.loads(body.decode('utf-8'))
        try:
            return json.loads(body.decode('utf-8'))
        except json.JSONDecodeError:
            return body.decode('utf-8', 'ignore')


def trigger_radarr_searches(movie_names, radarr_url, api_key, delay: float = 0.0, timeout: float = 15.0):
    names = [n for n in movie_names if n]
    if not names:
        return []
    base = (radarr_url or '').strip()
    key = (api_key or '').strip()
    if not base or not key:
        print("Radarr configuration missing URL or API key; skipping search triggers.", file=sys.stderr)
        return []

    try:
        library = _api_request(base, key, 'api/v3/movie', timeout=timeout) or []
        title_index = _build_radarr_title_index(library)
        print(f"DEBUG: Loaded {len(library)} movies from Radarr library, {len(title_index)} in title index", flush=True)
    except Exception as e:
        print(f"Radarr: failed to load movie library ({e}). Will fall back to lookup-only mode.", file=sys.stderr)
        library = []
        title_index = {}

    library_ids = {movie.get('id') for movie in library if isinstance(movie.get('id'), int)}
    print(f"Resetting and searching {len(names)} movie(s) in Radarr...")
    command_endpoint = 'api/v3/command'
    successful = []

    for name in names:
        key_name = _title_key(name)
        print(f"DEBUG: Processing movie '{name}' -> key '{key_name}'", flush=True)
        movie_data = None
        movie_id = None
        from_library = False

        if title_index and key_name:
            candidates = title_index.get(key_name, [])
            if candidates:
                movie_data = candidates[0]
                print(f"DEBUG: Found exact match for '{name}': {movie_data.get('title', 'Unknown')}", flush=True)
            else:
                print(f"DEBUG: No exact match for '{key_name}', skipping fuzzy matching to avoid incorrect matches", flush=True)
                # Disabled fuzzy matching to prevent incorrect movie matches
                # Will rely on API lookup instead for better accuracy

        if movie_data:
            movie_id = movie_data.get('id') if isinstance(movie_data.get('id'), int) else None
            from_library = movie_id in library_ids
            print(f"DEBUG: Found movie data for '{name}': ID {movie_id}, from_library: {from_library}, title: '{movie_data.get('title', 'Unknown')}'", flush=True)
        else:
            # Check if the movie might already exist by TMDB ID from the API lookup
            try:
                term = urllib.parse.quote_plus(name)
                print(f"DEBUG: No library match found, checking API lookup for existing movie '{name}'", flush=True)
                lookup = _api_request(base, key, f'api/v3/movie/lookup?term={term}', timeout=timeout) or []
                if lookup:
                    potential_movie = lookup[0]
                    tmdb_id = potential_movie.get('tmdbId')
                    print(f"DEBUG: API lookup found: '{potential_movie.get('title', 'Unknown')}' (TMDB: {tmdb_id})", flush=True)

                    # Check if this TMDB ID already exists in our library
                    for lib_movie in library:
                        if lib_movie.get('tmdbId') == tmdb_id:
                            print(f"DEBUG: Found existing movie in library by TMDB ID: '{lib_movie.get('title', 'Unknown')}' (ID: {lib_movie.get('id')})", flush=True)
                            movie_data = lib_movie
                            movie_id = lib_movie.get('id')
                            from_library = True
                            break

                    if not movie_data:
                        movie_data = potential_movie
                        print(f"DEBUG: Will use API lookup result for adding new movie", flush=True)
            except Exception as e:
                print(f"DEBUG: Error during movie existence check: {e}", flush=True)

        # This block is now handled above in the comprehensive check

        if from_library and movie_id:
            # Movie exists in library - delete only the files, keep the entry
            try:
                print(f"DEBUG: Getting movie files for '{name}' (ID: {movie_id})", flush=True)
                movie_files = _api_request(base, key, f'api/v3/moviefile?movieId={movie_id}', timeout=timeout) or []
                print(f"DEBUG: Found {len(movie_files)} files to delete for '{name}'", flush=True)

                deleted_files = 0
                for file_info in movie_files:
                    file_id = file_info.get('id')
                    if file_id:
                        try:
                            _api_request(base, key, f'api/v3/moviefile/{file_id}', method='DELETE', timeout=timeout)
                            deleted_files += 1
                            print(f"  • Deleted movie file {file_id} for '{name}'")
                        except Exception as e:
                            print(f"Radarr: failed to delete file {file_id} for '{name}': {e}", file=sys.stderr)

                print(f"  • Deleted {deleted_files} existing files for '{name}' (keeping movie entry)")

                # Trigger search for the existing movie entry
                search_payload = {"name": "MoviesSearch", "movieIds": [movie_id]}
                try:
                    _api_request(base, key, command_endpoint, payload=search_payload, timeout=timeout)
                    print(f"  • Triggered search for existing '{name}' entry")
                    successful.append(name)
                except Exception as e:
                    print(f"Radarr: search command failed for '{name}': {e}", file=sys.stderr)

            except Exception as e:
                print(f"Radarr: failed to delete files for '{name}': {e}; skipping.", file=sys.stderr)
                continue
        else:
            # Movie doesn't exist in library - add it normally
            payload = _radarr_prepare_movie_payload(movie_data)
            if not payload:
                print(f"Radarr: insufficient metadata to add '{name}'.", file=sys.stderr)
                continue

            try:
                print(f"DEBUG: Adding new movie '{name}' to Radarr", flush=True)
                added = _api_request(base, key, 'api/v3/movie', payload=payload, timeout=timeout)
                new_id = None
                if isinstance(added, dict):
                    new_id = added.get('id') or added.get('movieId')
                if new_id:
                    search_payload = {"name": "MoviesSearch", "movieIds": [new_id]}
                else:
                    search_payload = {"name": "MoviesSearch", "searchQuery": name}
                try:
                    _api_request(base, key, command_endpoint, payload=search_payload, timeout=timeout)
                except Exception as e:
                    print(f"Radarr: search command failed for '{name}': {e}", file=sys.stderr)
                print(f"  • Added '{name}' to Radarr and triggered search.")
                successful.append(name)
            except urllib.error.HTTPError as e:
                err_body = ''
                try:
                    err_body = e.read().decode('utf-8', 'ignore') if e.fp else ''
                except Exception:
                    err_body = ''
                msg = err_body or getattr(e, 'reason', '') or f'HTTP {e.code}'
                print(f"Radarr: failed to add '{name}': {msg}", file=sys.stderr)
            except urllib.error.URLError as e:
                print(f"Radarr: connection error when adding '{name}': {e}", file=sys.stderr)
            except Exception as e:
                print(f"Radarr: unexpected error when adding '{name}': {e}", file=sys.stderr)

        if delay and delay > 0:
            time.sleep(delay)

    return successful


def _build_sonarr_title_index(series_entries):
    index = {}
    for series in series_entries or []:
        if not isinstance(series, dict):
            continue
        names = set()
        names.add(series.get('title'))
        names.add(series.get('originalTitle'))
        for alt in series.get('alternateTitles') or []:
            if isinstance(alt, dict):
                alt_title = alt.get('title') or alt.get('alternateTitle')
            else:
                alt_title = str(alt)
            if alt_title:
                names.add(alt_title)
        for candidate in (c for c in names if c):
            key = _title_key(candidate)
            if key:
                index.setdefault(key, []).append(series)
    return index


def _sonarr_prepare_series_payload(series):
    if not isinstance(series, dict):
        return None
    title = series.get('title')
    quality_profile_id = series.get('qualityProfileId')
    language_profile_id = series.get('languageProfileId')
    tvdb_id = series.get('tvdbId')
    imdb_id = series.get('imdbId')
    tmdb_id = series.get('tmdbId')
    series_path = series.get('path')
    root_folder = series.get('rootFolderPath')
    if not root_folder and series_path:
        root_folder = os.path.dirname(series_path.rstrip('/\\')) or None
    if not title or not quality_profile_id or not root_folder or not series_path or not (tvdb_id or imdb_id or tmdb_id):
        return None

    payload = {
        'title': title,
        'titleSlug': series.get('titleSlug'),
        'qualityProfileId': quality_profile_id,
        'languageProfileId': language_profile_id,
        'tvdbId': tvdb_id,
        'imdbId': imdb_id,
        'tmdbId': tmdb_id,
        'year': series.get('year'),
        'monitored': series.get('monitored', True),
        'seasonFolder': series.get('seasonFolder', True),
        'seriesType': series.get('seriesType') or 'standard',
        'useSceneNumbering': series.get('useSceneNumbering', False),
        'rootFolderPath': root_folder,
        'path': series_path,
        'tags': series.get('tags', []),
        'seasons': series.get('seasons', []),
        'addOptions': {'monitor': 'existing', 'searchForMissingEpisodes': False}
    }
    return {k: v for k, v in payload.items() if v is not None}


def parse_season_episode_from_release(release_name):
    """
    Parse season and episode numbers from a release name.
    Returns dict with 'season' (int) and 'episodes' (list of ints).
    Returns None if parsing fails.
    """
    import re
    if not release_name:
        return None

    normalized = release_name.replace('_', ' ').replace('.', ' ')

    # Try S##E## pattern
    match = re.search(r'S(\d{1,2})E(\d{1,3})', normalized, re.IGNORECASE)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        # Check for multi-episode (S01E01E02)
        episodes = [episode]
        remaining = normalized[match.end():]
        for extra_match in re.finditer(r'E(\d{1,3})', remaining[:20], re.IGNORECASE):
            episodes.append(int(extra_match.group(1)))
        return {'season': season, 'episodes': episodes}

    # Try Season ## Episode ## pattern
    match = re.search(r'Season\s*(\d{1,2})\s*Episode\s*(\d{1,3})', normalized, re.IGNORECASE)
    if match:
        return {'season': int(match.group(1)), 'episodes': [int(match.group(2))]}

    # Try S## (season pack)
    match = re.search(r'S(\d{1,2})(?:\s|$|[^\dE])', normalized, re.IGNORECASE)
    if match:
        return {'season': int(match.group(1)), 'episodes': []}  # Empty episodes = whole season

    return None


def trigger_sonarr_searches(series_names, sonarr_url, api_key, delay: float = 0.0, timeout: float = 15.0, episode_data=None):
    """
    Trigger Sonarr searches for series.

    Args:
        series_names: List of series names (legacy support)
        episode_data: Optional dict mapping series names to lists of episode info dicts
                     e.g., {"Series Name": [{"season": 1, "episode": 1, "release_path": "..."}]}
    """
    names = [n for n in series_names if n]
    if not names:
        return []
    base = (sonarr_url or '').strip()
    key = (api_key or '').strip()
    if not base or not key:
        print("Sonarr configuration missing URL or API key; skipping search triggers.", file=sys.stderr)
        return []

    try:
        library = _api_request(base, key, 'api/v3/series', timeout=timeout) or []
        title_index = _build_sonarr_title_index(library)
    except Exception as e:
        print(f"Sonarr: failed to load series library ({e}). Will fall back to lookup-only mode.", file=sys.stderr)
        library = []
        title_index = {}

    library_ids = {series.get('id') for series in library if isinstance(series.get('id'), int)}
    print(f"Resetting and searching {len(names)} series in Sonarr...")
    command_endpoint = 'api/v3/command'
    successful = []

    for name in names:
        key_name = _title_key(name)
        series_data = None
        series_id = None
        from_library = False

        if title_index and key_name:
            candidates = title_index.get(key_name, [])
            if candidates:
                series_data = candidates[0]
            else:
                # Disabled fuzzy matching to prevent incorrect series matches
                # Will rely on API lookup instead for better accuracy
                pass

        if series_data:
            series_id = series_data.get('id') if isinstance(series_data.get('id'), int) else None
            from_library = series_id in library_ids

        if not series_data:
            try:
                term = urllib.parse.quote_plus(name)
                lookup = _api_request(base, key, f'api/v3/series/lookup?term={term}', timeout=timeout) or []
                if lookup:
                    series_data = lookup[0]
            except Exception as e:
                print(f"Sonarr: lookup failed for '{name}' ({e}); skipping.", file=sys.stderr)
                series_data = None

        if from_library and series_id:
            # Series exists in library - delete episode files
            try:
                print(f"DEBUG: Getting episode files for '{name}' (ID: {series_id})", flush=True)
                episode_files = _api_request(base, key, f'api/v3/episodefile?seriesId={series_id}', timeout=timeout) or []
                print(f"DEBUG: Found {len(episode_files)} total episode files for '{name}'", flush=True)

                # Get episode-specific data for this series if available
                episodes_to_process = None
                if episode_data and name in episode_data:
                    episodes_to_process = episode_data[name]
                    print(f"DEBUG: Processing specific episodes for '{name}': {len(episodes_to_process)} episodes", flush=True)

                # Get all episodes with their file associations
                episodes_info = _api_request(base, key, f'api/v3/episode?seriesId={series_id}', timeout=timeout) or []
                episode_id_to_file = {}
                for ep in episodes_info:
                    if ep.get('hasFile') and ep.get('episodeFileId'):
                        episode_id_to_file[ep['id']] = {
                            'file_id': ep['episodeFileId'],
                            'season': ep.get('seasonNumber'),
                            'episode': ep.get('episodeNumber')
                        }

                deleted_files = 0
                deleted_file_ids = set()  # Track which files we've already deleted
                deleted_episode_ids = []  # Track episode IDs for search command

                if episodes_to_process:
                    # Selective deletion: delete specified episodes or whole seasons
                    for ep_data in episodes_to_process:
                        season_num = ep_data.get('season')
                        episode_nums = ep_data.get('episodes', [])

                        if season_num is None:
                            print(f"DEBUG: Skipping episode with no season info: {ep_data}", flush=True)
                            continue

                        # Empty episode list means delete entire season
                        delete_entire_season = not episode_nums

                        if delete_entire_season:
                            print(f"DEBUG: Deleting entire season {season_num} for '{name}'", flush=True)

                        # Find matching episodes
                        for ep_id, ep_info in episode_id_to_file.items():
                            if ep_info['season'] == season_num:
                                # If deleting entire season, match all episodes in season
                                # Otherwise, only match specified episode numbers
                                if not delete_entire_season and ep_info['episode'] not in episode_nums:
                                    continue

                                file_id = ep_info['file_id']
                                if file_id and file_id not in deleted_file_ids:
                                    try:
                                        _api_request(base, key, f'api/v3/episodefile/{file_id}', method='DELETE', timeout=timeout)
                                        deleted_files += 1
                                        deleted_file_ids.add(file_id)
                                        deleted_episode_ids.append(ep_id)
                                        print(f"  • Deleted episode file {file_id} for '{name}' S{ep_info['season']:02d}E{ep_info['episode']:02d}")
                                    except Exception as e:
                                        print(f"Sonarr: failed to delete episode file {file_id}: {e}", file=sys.stderr)
                else:
                    # No specific episodes provided - delete ALL files (legacy behavior)
                    for file_info in episode_files:
                        file_id = file_info.get('id')
                        if file_id:
                            try:
                                _api_request(base, key, f'api/v3/episodefile/{file_id}', method='DELETE', timeout=timeout)
                                deleted_files += 1
                                print(f"  • Deleted episode file {file_id} for '{name}'")
                            except Exception as e:
                                print(f"Sonarr: failed to delete episode file {file_id} for '{name}': {e}", file=sys.stderr)

                print(f"  • Deleted {deleted_files} episode file(s) for '{name}' (keeping series entry)")

                # Trigger search for the series or specific episodes
                if deleted_episode_ids:
                    # Search for specific episodes that were deleted
                    search_payload = {"name": "EpisodeSearch", "episodeIds": deleted_episode_ids}
                    print(f"  • Triggering search for {len(deleted_episode_ids)} specific episode(s)")
                else:
                    # Search entire series
                    search_payload = {"name": "SeriesSearch", "seriesId": series_id}
                    print(f"  • Triggering search for entire series")

                try:
                    _api_request(base, key, command_endpoint, payload=search_payload, timeout=timeout)
                    print(f"  • Triggered search for existing '{name}' entry")
                    successful.append(name)
                except Exception as e:
                    print(f"Sonarr: search command failed for '{name}': {e}", file=sys.stderr)

            except Exception as e:
                print(f"Sonarr: failed to delete episode files for '{name}': {e}; skipping.", file=sys.stderr)
                continue
        else:
            # Series doesn't exist in library - add it normally
            payload = _sonarr_prepare_series_payload(series_data)
            if not payload:
                print(f"Sonarr: insufficient metadata to add '{name}'.", file=sys.stderr)
                continue

            try:
                print(f"DEBUG: Adding new series '{name}' to Sonarr", flush=True)
                added = _api_request(base, key, 'api/v3/series', payload=payload, timeout=timeout)
                new_id = None
                if isinstance(added, dict):
                    new_id = added.get('id') or added.get('seriesId')
                if new_id:
                    search_payload = {"name": "SeriesSearch", "seriesIds": [new_id]}
                else:
                    search_payload = {"name": "SeriesSearch", "searchQuery": name}
                try:
                    _api_request(base, key, command_endpoint, payload=search_payload, timeout=timeout)
                except Exception as e:
                    print(f"Sonarr: search command failed for '{name}': {e}", file=sys.stderr)
                print(f"  • Added '{name}' to Sonarr and triggered search.")
                successful.append(name)
            except urllib.error.HTTPError as e:
                err_body = ''
                try:
                    err_body = e.read().decode('utf-8', 'ignore') if e.fp else ''
                except Exception:
                    err_body = ''
                msg = err_body or getattr(e, 'reason', '') or f'HTTP {e.code}'
                print(f"Sonarr: failed to add '{name}': {msg}", file=sys.stderr)
            except urllib.error.URLError as e:
                print(f"Sonarr: connection error when adding '{name}': {e}", file=sys.stderr)
            except Exception as e:
                print(f"Sonarr: unexpected error when adding '{name}': {e}", file=sys.stderr)

        if delay and delay > 0:
            time.sleep(delay)

    return successful

# --------------------------------------------------------

def parse_args():
    ap = argparse.ArgumentParser(description="Export NZBs from nzbdav-like SQLite DB")
    ap.add_argument("-d", "--db", default=os.getenv("NZB_DB", DB), help="Path to SQLite DB file (default: db.sqlite)")
    ap.add_argument("-o", "--outdir", default=os.getenv("NZB_OUTDIR", OUTDIR), help="Output directory for NZBs")
    ap.add_argument("-g", "--group", default=os.getenv("NZB_GROUP", GROUP), help="Usenet group to embed in NZB")
    ap.add_argument("--batch-size", type=int, default=100, help="Number of releases to process in each batch")
    ap.add_argument("--fallback-seg-bytes", type=int, default=FALLBACK_SEG_BYTES, help="Fallback bytes per segment when size unknown (default: 1 MiB)")
    ap.add_argument("--max-segs-per-file", type=int, default=MAX_SEGS_PER_FILE, help="Limit number of segments per file (default: unlimited)")
    ap.add_argument("--radarr", action="store_true", help="Enable Radarr search triggers")
    ap.add_argument("--radarr-url", default=os.getenv("RADARR_URL"), help="Radarr base URL (e.g. http://localhost:7878)")
    ap.add_argument("--radarr-api-key", default=os.getenv("RADARR_API_KEY"), help="Radarr API key")
    ap.add_argument("--radarr-delay", type=float, default=float(os.getenv("RADARR_SEARCH_DELAY", "0")), help="Seconds to pause between Radarr search triggers (default: 0)")
    ap.add_argument("--sonarr", action="store_true", help="Enable Sonarr search triggers")
    ap.add_argument("--sonarr-url", default=os.getenv("SONARR_URL"), help="Sonarr base URL (e.g. http://localhost:8989)")
    ap.add_argument("--sonarr-api-key", default=os.getenv("SONARR_API_KEY"), help="Sonarr API key")
    ap.add_argument("--sonarr-delay", type=float, default=float(os.getenv("SONARR_SEARCH_DELAY", "0")), help="Seconds to pause between Sonarr search triggers (default: 0)")
    ap.add_argument("--names-only", action="store_true", help="Skip NZB export and use an existing movie names file")
    ap.add_argument("--names-file", help="Path to movie names text file (default: <outdir>/movie_names.txt)")
    ap.add_argument("--series-names-file", help="Path to series names text file (default: <outdir>/series_names.txt)")
    ap.add_argument("--limit", type=int, help="Maximum number of movie/series names to send per run (random sample)")
    return ap.parse_args()

def safe_name(s, maxlen=200):
    s = s or "unnamed"
    return re.sub(r'[\/\\:*?"<>|]+', "_", s)[:maxlen]

def parse_release_dir(path):
    if not path or not path.startswith("/"):
        return None, "uncategorized", os.path.basename(path or "unnamed")
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "content":
        rel_dir = "/" + "/".join(parts[:3])
        category = parts[1]
        release_name = parts[2]
        return rel_dir, category, release_name
    if len(parts) >= 2 and parts[-1].lower() in ("_extracted_", "extracted", "repack"):
        parent_dir = os.path.dirname(path.rstrip('/'))
        return parse_release_dir(parent_dir)
    if len(parts) >= 2:
        rel_dir = "/" + "/".join(parts[:-1])
        release_name = parts[-2]
        return rel_dir, "uncategorized", release_name
    if len(parts) == 1:
        return "/" + parts[0], "uncategorized", parts[0]
    return path, "uncategorized", os.path.basename(path.rstrip("/") or "unnamed")

def classify(path, name):
    n = (name or "").lower()
    p = (path or "").lower()
    if not INCLUDE_SAMPLE and ("sample" in n or "/sample" in p):
        return "skip"
    ext = os.path.splitext(n)[1].lstrip(".").lower()
    if ext in VIDEO_EXTS: return "video"
    if ext in SUBS_EXTS: return "subs" if INCLUDE_SUBS else "skip"
    if ext == "nfo": return "nfo" if INCLUDE_NFO else "skip"
    if ext == "par2": return "par2" if INCLUDE_PAR2 else "skip"
    if ext == "sfv": return "sfv" if INCLUDE_SFV else "skip"
    if ext in ARCHIVE_EXTS: return "rar" if INCLUDE_RAR else "skip"
    if ext in IMAGE_EXTS: return "image" if INCLUDE_IMAGES else "skip"
    if ext in AUX_EXTS: return ext
    return "other" if INCLUDE_OTHER else "skip"

def nzb_filename_for_release(category, release):
    base = release or "unnamed"
    if not base.lower().endswith(".nzb"):
        base += ".nzb"
    return safe_name(base)

def parse_iso(dt_str):
    if not dt_str: return datetime.utcnow()
    s = dt_str.replace("Z","")
    if "." in s:
        head, tail = s.split(".", 1)
        frac = re.sub(r"[^0-9].*$", "", tail)
        frac = (frac + "000000")[:6]
        s = f"{head}.{frac}"
    try: return datetime.fromisoformat(s)
    except Exception: return datetime.utcnow()

def build_nzb_xml(file_entries, group):
    L = ['<?xml version="1.0" encoding="utf-8"?>',
         '<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/2003/nzb">',
         '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">']
    for fe in file_entries:
        date_unix = int(fe['date'].timestamp())
        subject_attr = html.escape(fe['subject'] or "unnamed", quote=True)
        L.append(f'  <file poster="nzbdav" date="{date_unix}" subject="{subject_attr}">')
        L.append('    <groups>')
        if group: L.append(f'      <group>{html.escape(group)}</group>')
        L.append('    </groups>')
        L.append('    <segments>')
        for seg in fe['segments']:
            mid = seg['msgid'] or ""
            mid_text = html.escape(mid, quote=False)
            b = int(seg.get('bytes') or FALLBACK_SEG_BYTES)
            if b <= 0: b = FALLBACK_SEG_BYTES
            n = int(seg['number'])
            L.append(f'      <segment bytes="{b}" number="{n}">{mid_text}</segment>')
        L.append('    </segments>')
        L.append('  </file>')
    L.append('</nzb>')
    return "\n".join(L)

SEG_ID_KEYS = ["MessageId", "MessageID", "MsgId", "MsgID", "message_id", "messageId", "Id"]
SEG_SIZE_KEYS = ["Bytes", "Size", "ByteCount", "Length"]

def extract_segments_from_json(raw):
    try: obj = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except Exception: obj = []
    if obj is None: return []
    if isinstance(obj, dict):
        for k in ("SegmentIds", "Segments", "segments"):
            if k in obj: obj = obj[k]; break
    out = []
    if isinstance(obj, list):
        for e in obj:
            if isinstance(e, str): out.append({"msgid": e, "bytes": None})
            elif isinstance(e, dict):
                msgid, size = None, None
                for k in SEG_ID_KEYS:
                    if k in e and isinstance(e[k], str): msgid = e[k]; break
                for k in SEG_SIZE_KEYS:
                    if k in e:
                        try: size = int(e[k]); break
                        except Exception: pass
                if msgid: out.append({"msgid": msgid, "bytes": size})
    return out

def find_columns(cur, table):
    return {c[1] for c in cur.execute(f"PRAGMA table_info('{table}')")}

def load_segment_sizes_for(cur, msgids):
    if not msgids: return {}
    msgids_set = set(msgids)
    all_tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]

    msg_col_names = ["MessageId", "MessageID", "MsgId", "MsgID", "message_id", "messageId"]
    size_col_names = ["Bytes", "Size", "ByteCount", "Length"]

    best_pair = None
    candidate_tables = ["DavSegments", "Segments", "NzbSegments", "Articles", "DavArticles", "Posts", "DavPosts", "UsenetSegments", "NyuuSegments"] + all_tables
    for t in sorted(list(set(candidate_tables))):
        if t not in all_tables: continue
        cols = find_columns(cur, t)
        msg_col = next((c for c in msg_col_names if c in cols), None)
        size_col = next((c for c in size_col_names if c in cols), None)
        if msg_col and size_col:
            best_pair = (t, msg_col, size_col)
            break

    if not best_pair: return {}

    sizes = {}
    t, mid_col, sz_col = best_pair
    msgids_list = list(msgids_set)
    CHUNK = 500
    for i in range(0, len(msgids_list), CHUNK):
        chunk = msgids_list[i:i+CHUNK]
        qmarks = ",".join("?" for _ in chunk)
        sql = f"SELECT {mid_col}, {sz_col} FROM {t} WHERE {mid_col} IN ({qmarks})"
        try:
            for mid, sz in cur.execute(sql, chunk):
                if mid in msgids_set and mid not in sizes:
                    try: sizes[mid] = int(sz)
                    except (ValueError, TypeError): pass
        except sqlite3.Error as e:
            print(f"Warning: SQL error querying segment sizes from {t}: {e}", file=sys.stderr)
            continue
    return sizes

def main():
    global DB, OUTDIR, GROUP, FALLBACK_SEG_BYTES, MAX_SEGS_PER_FILE
    args = parse_args()
    DB, OUTDIR, GROUP = args.db, args.outdir, args.group
    FALLBACK_SEG_BYTES, MAX_SEGS_PER_FILE = args.fallback_seg_bytes, args.max_segs_per_file

    sorted_series = []
    sorted_movies = []
    exported_count = 0
    con = None
    names_path = None

    series_path = None

    if args.names_only:
        names_path = args.names_file or os.path.join(args.outdir or OUTDIR, "movie_names.txt")
        try:
            with open(names_path, "r", encoding="utf-8") as fh:
                sorted_movies = sorted({line.strip() for line in fh if line.strip()})
            print(f"Loaded {len(sorted_movies)} movie names from {names_path}.")
        except FileNotFoundError:
            print(f"FATAL: names-only mode could not find file '{names_path}'.", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"FATAL: Failed reading names file '{names_path}': {e}", file=sys.stderr)
            sys.exit(1)

        series_path = args.series_names_file or os.path.join(args.outdir or OUTDIR, "series_names.txt")
        try:
            with open(series_path, "r", encoding="utf-8") as fh:
                sorted_series = sorted({line.strip() for line in fh if line.strip()})
            print(f"Loaded {len(sorted_series)} series names from {series_path}.")
        except FileNotFoundError:
            sorted_series = []
            if args.sonarr:
                print(f"Warning: series names file '{series_path}' not found; Sonarr queue will be empty.", file=sys.stderr)
        except Exception as e:
            print(f"FATAL: Failed reading series file '{series_path}': {e}", file=sys.stderr)
            sys.exit(1)
    else:
        os.makedirs(OUTDIR, exist_ok=True)
        names_path = os.path.join(OUTDIR, "movie_names.txt")
        series_path = os.path.join(OUTDIR, "series_names.txt")

        # NEW: collectors
        series_names = set()
        movie_names  = set()

        try:
            con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        except sqlite3.OperationalError as e:
            print(f"FATAL: Could not open database file '{DB}'. Error: {e}", file=sys.stderr)
            sys.exit(1)

        con.row_factory = sqlite3.Row
        cur = con.cursor()

        all_file_paths_query = """
            SELECT Path FROM DavItems WHERE Id IN (SELECT Id FROM DavNzbFiles UNION SELECT Id FROM DavRarFiles)
        """
        cur.execute(all_file_paths_query)

        # Group file paths by their calculated release directory
        release_map = defaultdict(list)
        for row in cur.fetchall():
            path = row['Path']
            rel_dir, _, _ = parse_release_dir(path)
            if rel_dir:
                release_map[rel_dir].append(path)

        release_dirs = sorted(release_map.keys())
        total_releases = len(release_dirs)
        print(f"Found {total_releases} unique releases to process.")

        for i in range(0, total_releases, args.batch_size):
            batch_dirs = release_dirs[i:i + args.batch_size]

            by_release = defaultdict(lambda: {"files": [], "all_msgids": set()})
            # prepare batched path lookups
            all_paths_in_batch = []
            for d in batch_dirs:
                all_paths_in_batch.extend(release_map[d])
            if not all_paths_in_batch:
                continue
            path_qmarks = ",".join("?" * len(all_paths_in_batch))

            try:
                cur.execute(f"""
                    SELECT f.Id AS file_id, di.Name AS file_name, di.Path AS file_path,
                           di.CreatedAt AS created_at, f.SegmentIds AS segs_json
                    FROM DavNzbFiles f JOIN DavItems di ON di.Id = f.Id
                    WHERE di.Path IN ({path_qmarks})
                """, all_paths_in_batch)
                for r in cur.fetchall():
                    rel_dir, _, _ = parse_release_dir(r["file_path"])
                    segments = extract_segments_from_json(r["segs_json"])
                    by_release[rel_dir]["files"].append({"kind": "nzb", "row": r, "segments": segments})
                    for s in segments:
                        if s.get("msgid"): by_release[rel_dir]["all_msgids"].add(s["msgid"])

                cur.execute(f"""
                    SELECT r.Id AS file_id, di.Name AS file_name, di.Path AS file_path,
                           di.CreatedAt AS created_at, r.RarParts AS parts_json
                    FROM DavRarFiles r JOIN DavItems di ON di.Id = r.Id
                    WHERE di.Path IN ({path_qmarks})
                """, all_paths_in_batch)
                for r in cur.fetchall():
                    rel_dir, _, _ = parse_release_dir(r["file_path"])
                    parts, msgids = [], set()
                    try: obj = json.loads(r["parts_json"]) if isinstance(r["parts_json"], (str, bytes, bytearray)) else r["parts_json"]
                    except Exception: obj = []
                    if isinstance(obj, list):
                        for part in obj:
                            segs = extract_segments_from_json(part if not isinstance(part, dict) else (part.get("SegmentIds") or part.get("Segments") or part))
                            parts.append(segs)
                            for s in segs:
                                if s.get("msgid"): msgids.add(s["msgid"])
                    by_release[rel_dir]["files"].append({"kind": "rar", "row": r, "parts": parts})
                    by_release[rel_dir]["all_msgids"].update(msgids)
            except sqlite3.Error as e:
                print(f"FATAL: SQL error when fetching batch data: {e}", file=sys.stderr)
                continue

            for rel_dir in batch_dirs:
                payload = by_release[rel_dir]
                current_release_num = release_dirs.index(rel_dir) + 1
                _, category, release_name = parse_release_dir(rel_dir)
                print(f"\n--- Processing release {current_release_num}/{total_releases}: {release_name} ---")

                # NEW: record cleaned names
                try:
                    if is_series(release_name, category):
                        series_names.add(extract_series_title(release_name))
                    elif is_movie(release_name, category):
                        movie_names.add(extract_movie_title(release_name))
                    else:
                        # fallback heuristic: classify by markers first, else by year
                        if SERIES_MARKERS.search(release_name):
                            series_names.add(extract_series_title(release_name))
                        elif MOVIE_YEAR.search(release_name):
                            movie_names.add(extract_movie_title(release_name))
                except Exception:
                    pass  # do not fail NZB export on naming issues

                try:
                    msgid_sizes = load_segment_sizes_for(cur, payload["all_msgids"])
                    file_entries, type_counts = [], Counter()

                    for item in payload["files"]:
                        r, kind = item["row"], item["kind"]
                        fpath, fname = r["file_path"] or "", r["file_name"] or ""
                        ftype = classify(fpath, fname)
                        if ftype == "skip": continue
                        dt = parse_iso(r["created_at"])

                        if kind == "nzb":
                            segs = item.get("segments", [])
                            seg_ids = [{"msgid": s["msgid"], "bytes": s.get("bytes") or msgid_sizes.get(s["msgid"]) or FALLBACK_SEG_BYTES} for s in segs if s.get("msgid")]
                            if not seg_ids: continue
                            if MAX_SEGS_PER_FILE: seg_ids = seg_ids[:MAX_SEGS_PER_FILE]
                            file_entries.append({"subject": fname, "date": dt, "segments": [{"number": i+1, **s} for i,s in enumerate(seg_ids)]})
                            type_counts[ftype] += 1

                        elif kind == "rar" and INCLUDE_RAR:
                            parts = item.get("parts", [])
                            base = os.path.splitext(fname)[0] or "part"
                            for idx_part, segs in enumerate(parts, 1):
                                seg_ids = [{"msgid": s["msgid"], "bytes": s.get("bytes") or msgid_sizes.get(s["msgid"]) or FALLBACK_SEG_BYTES} for s in segs if s.get("msgid")]
                                if not seg_ids: continue
                                if MAX_SEGS_PER_FILE: seg_ids = seg_ids[:MAX_SEGS_PER_FILE]
                                subject = f"{base}.part{idx_part:03d}.rar"
                                file_entries.append({"subject": subject, "date": dt, "segments": [{"number": i+1, **s} for i,s in enumerate(seg_ids)]})
                            if parts: type_counts["rar"] += 1

                    if not file_entries:
                        print(f"Skipping release {release_name}: No valid files with segments found.")
                        continue

                    nzb_name = nzb_filename_for_release(category, release_name)
                    category_dir = os.path.join(OUTDIR, safe_name(category))
                    os.makedirs(category_dir, exist_ok=True)
                    out_path = os.path.join(category_dir, nzb_name)

                    xml = build_nzb_xml(file_entries, group=GROUP)
                    with open(out_path, "w", encoding="utf-8") as fh: fh.write(xml)

                    total_segments = sum(len(fe['segments']) for fe in file_entries)
                    total_bytes = sum(seg["bytes"] for fe in file_entries for seg in fe["segments"])
                    type_summary = ", ".join(f"{k}={v}" for k, v in sorted(type_counts.items()))
                    print(f"Wrote {out_path} [category={category}] with {total_segments} segments across {len(file_entries)} files, total_bytes={total_bytes} [{type_summary}]")
                    exported_count += 1

                except Exception as e:
                    print(f"ERROR: Failed to process release '{release_name}': {e}", file=sys.stderr)
                    traceback.print_exc()

        sorted_series = sorted(n for n in series_names if n)
        sorted_movies = sorted(n for n in movie_names if n)

    limit = args.limit if getattr(args, 'limit', None) else None
    limit = limit if (isinstance(limit, int) and limit > 0) else None

    radarr_enabled = bool(args.radarr)
    sonarr_enabled = bool(args.sonarr)

    movies_for_radarr = []
    processed_existing = set()
    processed_path = None
    if radarr_enabled and sorted_movies:
        processed_dir = os.path.dirname(names_path) if names_path else OUTDIR
        if not processed_dir:
            processed_dir = '.'
        processed_path = os.path.join(processed_dir, "movie_names_processed.txt")

        try:
            with open(processed_path, "r", encoding="utf-8") as fh:
                processed_existing = {line.strip() for line in fh if line.strip()}
        except FileNotFoundError:
            processed_existing = set()
        except Exception as e:
            print(f"Warning: Could not read processed names file '{processed_path}': {e}", file=sys.stderr)

        remaining_movies = [m for m in sorted_movies if m not in processed_existing]
        if not remaining_movies:
            print("Radarr: all movie names appear processed already; nothing to queue.")
            movies_for_radarr = []
        else:
            if limit and limit < len(remaining_movies):
                sample_size = limit
                movies_for_radarr = random.sample(remaining_movies, sample_size)
                print(f"Selected {sample_size} movie name(s) out of {len(remaining_movies)} remaining for Radarr this run.")
            else:
                movies_for_radarr = remaining_movies

    series_for_sonarr = []
    series_processed_existing = set()
    series_processed_path = None
    if sonarr_enabled and sorted_series:
        series_dir = os.path.dirname(series_path) if series_path else OUTDIR
        if not series_dir:
            series_dir = '.'
        series_processed_path = os.path.join(series_dir, "series_names_processed.txt")

        try:
            with open(series_processed_path, "r", encoding="utf-8") as fh:
                series_processed_existing = {line.strip() for line in fh if line.strip()}
        except FileNotFoundError:
            series_processed_existing = set()
        except Exception as e:
            print(f"Warning: Could not read processed series file '{series_processed_path}': {e}", file=sys.stderr)

        remaining_series = [s for s in sorted_series if s not in series_processed_existing]
        if not remaining_series:
            print("Sonarr: all series names appear processed already; nothing to queue.")
            series_for_sonarr = []
        else:
            if limit and limit < len(remaining_series):
                sample_size = limit
                series_for_sonarr = random.sample(remaining_series, sample_size)
                print(f"Selected {sample_size} series name(s) out of {len(remaining_series)} remaining for Sonarr this run.")
            else:
                series_for_sonarr = remaining_series

    # NEW: write out cleaned, deduped names (skip when reusing an existing list)
    if not args.names_only:
        try:
            series_path = os.path.join(OUTDIR, "series_names.txt")
            movies_path = os.path.join(OUTDIR, "movie_names.txt")
            if sorted_series:
                with open(series_path, "w", encoding="utf-8") as fh:
                    for name in sorted_series:
                        fh.write(name + "\n")
                print(f"Wrote {series_path} with {len(sorted_series)} unique series.")
            if sorted_movies:
                with open(movies_path, "w", encoding="utf-8") as fh:
                    for name in sorted_movies:
                        fh.write(name + "\n")
                print(f"Wrote {movies_path} with {len(sorted_movies)} unique movies.")
        except Exception as e:
            print(f"ERROR writing names files: {e}", file=sys.stderr)

    # Trigger Radarr searches if configured
    radarr_url = getattr(args, 'radarr_url', None)
    radarr_api_key = getattr(args, 'radarr_api_key', None)
    radarr_delay = getattr(args, 'radarr_delay', 0.0) or 0.0
    if radarr_delay < 0:
        radarr_delay = 0.0
    timeout_env = os.getenv("RADARR_TIMEOUT")
    try:
        radarr_timeout = float(timeout_env) if timeout_env else 15.0
    except ValueError:
        radarr_timeout = 15.0

    radarr_ready = radarr_enabled and bool(radarr_url and radarr_api_key)
    radarr_success = []
    if radarr_enabled and movies_for_radarr:
        if radarr_ready:
            radarr_success = trigger_radarr_searches(movies_for_radarr, radarr_url, radarr_api_key, delay=radarr_delay, timeout=radarr_timeout)
        else:
            print("Radarr config incomplete (need both URL and API key); skipping search trigger.", file=sys.stderr)

    if radarr_success and processed_path:
        new_processed = [name for name in radarr_success if name not in processed_existing]
        if new_processed:
            try:
                with open(processed_path, "a", encoding="utf-8") as fh:
                    for name in new_processed:
                        fh.write(name + "\n")
                processed_existing.update(new_processed)
                print(f"Appended {len(new_processed)} movie name(s) to {processed_path}.")
            except Exception as e:
                print(f"Warning: Failed to update processed names file '{processed_path}': {e}", file=sys.stderr)

    sonarr_url = getattr(args, 'sonarr_url', None)
    sonarr_api_key = getattr(args, 'sonarr_api_key', None)
    sonarr_delay = getattr(args, 'sonarr_delay', 0.0) or 0.0
    if sonarr_delay < 0:
        sonarr_delay = 0.0
    sonarr_timeout_env = os.getenv("SONARR_TIMEOUT")
    try:
        sonarr_timeout = float(sonarr_timeout_env) if sonarr_timeout_env else 15.0
    except ValueError:
        sonarr_timeout = 15.0

    sonarr_ready = sonarr_enabled and bool(sonarr_url and sonarr_api_key)
    sonarr_success = []
    if sonarr_enabled and series_for_sonarr:
        if sonarr_ready:
            sonarr_success = trigger_sonarr_searches(series_for_sonarr, sonarr_url, sonarr_api_key, delay=sonarr_delay, timeout=sonarr_timeout)
        else:
            print("Sonarr config incomplete (need both URL and API key); skipping search trigger.", file=sys.stderr)

    if sonarr_success and series_processed_path:
        new_series_processed = [name for name in sonarr_success if name not in series_processed_existing]
        if new_series_processed:
            try:
                with open(series_processed_path, "a", encoding="utf-8") as fh:
                    for name in new_series_processed:
                        fh.write(name + "\n")
                series_processed_existing.update(new_series_processed)
                print(f"Appended {len(new_series_processed)} series name(s) to {series_processed_path}.")
            except Exception as e:
                print(f"Warning: Failed to update processed series file '{series_processed_path}': {e}", file=sys.stderr)

    if con:
        con.close()

    radarr_processed = len(radarr_success) if radarr_ready else 0
    sonarr_processed = len(sonarr_success) if sonarr_ready else 0

    summary_parts = []
    if not args.names_only:
        summary_parts.append(f"Exported {exported_count} NZBs to {OUTDIR}")
    if radarr_enabled:
        summary_parts.append(f"Radarr queued {radarr_processed} movie(s)")
    if sonarr_enabled:
        summary_parts.append(f"Sonarr queued {sonarr_processed} series")
    if args.names_only:
        read_parts = []
        read_parts.append(f"{len(sorted_movies)} movie name(s)")
        read_parts.append(f"{len(sorted_series)} series name(s)")
        summary_parts.insert(0, "Read " + " and ".join(read_parts))
    if not summary_parts:
        summary_parts.append("No actions performed")
    print("\nDone. " + "; ".join(summary_parts) + ".")

if __name__ == "__main__":
    main()
