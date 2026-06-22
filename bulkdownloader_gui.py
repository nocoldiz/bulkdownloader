#!/usr/bin/env python3
"""Tabbed GUI download manager for bulkdownloader.py.

Five tabs:

* **Downloads** — a download queue backed by ``links_to_download.txt``.
  The queue auto-loads from the txt files on launch (queued / done / failed),
  preserves the file order, can be paused/resumed and drag-reordered, runs
  several downloads in parallel (configurable), and every change is written
  straight back to the txt files so the order survives a restart. Each URL is
  downloaded by its own ``bulkdownloader.py --url …`` subprocess, so a paused
  item resumes from its partial ``.part`` file on the next run. Rows have
  tick-box selection; Delete removes the ticked/selected rows.
* **Bookmarks** — temporarily reads Firefox + Chromium (Chrome/Edge/Brave)
  bookmarks and shows only the ones whose host matches a site in
  ``websites.json``; filter by typing, tick rows, then push them to the top or
  bottom of the download queue.
* **Search** — open any site's ``searchURL`` for a query in the browser, star
  sites as favourites (persisted to ``websites.json``) and open every
  favourite's search in its own browser tab with one button.
* **Gallery** — a thumbnail grid of every video already in the download
  folder; double-click to play in the system player.
* **X.com** — log in for sensitive / login-gated X.com videos: use your
  browser's live login (recommended), paste ``auth_token`` / ``ct0`` tokens
  (with a step-by-step guide), or import / paste a ``cookies.txt``.

The window size, download folder, parallel count and last tab are remembered
between runs. Files land in the chosen output folder — no categorization here.
"""

import os
import re
import sys
import json
import time
import random
import shutil
import queue
import hashlib
import sqlite3
import tempfile
import threading
import itertools
import subprocess
import webbrowser
import urllib.parse
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

# ── path resolution (works both as a plain script and as a frozen exe) ──

FROZEN = getattr(sys, 'frozen', False)
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, '_MEIPASS', APP_DIR))

if FROZEN:
    # bulkdownloader.py is bundled as data alongside the frozen exe.
    SCRIPT_PATH = BUNDLE_DIR / 'bulkdownloader.py'
else:
    SCRIPT_PATH = APP_DIR / 'bulkdownloader.py'


def _find_project_root():
    for base in (APP_DIR, APP_DIR.parent, APP_DIR.parent.parent):
        if (base / 'server').is_dir() or (base / 'videos').is_dir():
            return base
    return APP_DIR.parent if APP_DIR.parent.exists() else APP_DIR


def _user_data_dir():
    """Per-OS writable folder for user data — used when frozen, so a packaged
    .app/.exe never writes inside its own (possibly read-only / signed) bundle."""
    home = Path.home()
    if sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA', home / 'AppData' / 'Roaming'))
    elif sys.platform == 'darwin':
        base = home / 'Library' / 'Application Support'
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config'))
    return base / 'AphroArchive' / 'bulkdownloader'


PROJECT_ROOT = _find_project_root()
# Downloads land in a plain ./downloads folder next to the app (not videos/downloads).
DEFAULT_OUT_DIR = APP_DIR / 'downloads'
_LEGACY_OUT_DIR = PROJECT_ROOT / 'videos' / 'downloads'

# In dev (running the script) keep everything in the repo folder so it works
# with bulkdownloader.py's own links files. When frozen, use the per-OS dir.
DATA_DIR = APP_DIR if not FROZEN else _user_data_dir()
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    DATA_DIR = APP_DIR

LINKS_TO_DOWNLOAD = DATA_DIR / 'links_to_download.txt'
LINKS_DOWNLOADED = DATA_DIR / 'links_downloaded.txt'
LINKS_FAILED = DATA_DIR / 'link_failed.txt'
CONFIG_FILE = DATA_DIR / 'gui_config.json'

# Website registry — the same shape AphroArchive exports via
# GET /api/db/websites/export. Kept in DATA_DIR so favourites persist.
WEBSITES_JSON = DATA_DIR / 'websites.json'

# Merged category → tags map (joined from every preset) powering the gallery's
# tag sidebar (title-keyword matching).
CATEGORIES_JSON = DATA_DIR / 'categories.json'

# Netscape-format cookies for login-gated sites (X.com sensitive/age-gated tweets).
COOKIES_FILE = DATA_DIR / 'cookies.txt'

# Cache dir for the gallery's ffmpeg-generated thumbnails.
THUMB_CACHE_DIR = Path(tempfile.gettempdir()) / 'aphro_gallery_thumbs'

VIDEO_EXTS = {'.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.flv', '.ts', '.wmv', '.mpg', '.mpeg', '.m2ts'}
GALLERY_MAX = 240          # cap files shown so a huge folder doesn't stall the UI
DONE_LOAD_CAP = 60         # only show the most recent N completed rows on launch
DOWNLOADED_FILE_CAP = 2000  # trim links_downloaded.txt to this many lines
THUMB_W, THUMB_H = 240, 135
CARD_W = 264

# Browsers yt-dlp can read live cookies from (the "proper login" path).
BROWSER_CHOICES = ['chrome', 'firefox', 'edge', 'brave', 'chromium', 'opera', 'vivaldi']
if sys.platform == 'darwin':
    BROWSER_CHOICES.append('safari')

# Per-item subprocess output: "   [download]  45.3% of 120.4MiB at 5.2MiB/s ETA 00:12"
PROGRESS_RE = re.compile(r'\[download\]\s+([\d.]+)%')
SPEED_RE = re.compile(r'\bat\s+([\d.]+\s*[KMG]?i?B/s)', re.I)
ETA_RE = re.compile(r'\bETA\s+([\d:]+)')
TITLE_RE = re.compile(r'\[title\]\s+"(.+)"')

# Query params that are pure tracking noise — stripped only for de-dup keys,
# never from the URL we actually download.
TRACKING_PARAMS = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
                   'fbclid', 'gclid', 'ref', 'ref_', 'igshid', 'si', 'feature'}

# ── palette ──────────────────────────────────────────────────────────
BG = '#f3f4f6'
PANEL_BG = '#ffffff'
ACCENT = '#2563eb'
ACCENT_ACTIVE = '#1d4ed8'
SUCCESS = '#16a34a'
ERROR = '#dc2626'
MUTED = '#6b7280'
BORDER = '#d1d5db'
GOLD = '#d97706'
LOG_BG = '#1e1e1e'
LOG_FG = '#d4d4d4'

# Pick fonts that actually exist on the host OS — Segoe UI/Consolas are
# Windows-only and fall back to ugly defaults on macOS/Linux.
if sys.platform == 'darwin':
    _UI_FONT, _MONO_FONT = 'Helvetica Neue', 'Menlo'
elif sys.platform == 'win32':
    _UI_FONT, _MONO_FONT = 'Segoe UI', 'Consolas'
else:
    _UI_FONT, _MONO_FONT = 'DejaVu Sans', 'DejaVu Sans Mono'

FONT = (_UI_FONT, 10)
FONT_BOLD = (_UI_FONT, 10, 'bold')
FONT_HEADER = (_UI_FONT, 15, 'bold')
FONT_SUB = (_UI_FONT, 9)
FONT_MONO = (_MONO_FONT, 9)

# ── item status labels ───────────────────────────────────────────────
ST_QUEUED = 'queued'
ST_DOWNLOADING = 'downloading'
ST_DONE = 'done'
ST_ERROR = 'error'
ST_STOPPED = 'stopped'

STATUS_LABEL = {
    ST_QUEUED: '⏳ Queued',
    ST_DOWNLOADING: '⬇ Downloading',
    ST_DONE: '✅ Done',
    ST_ERROR: '❌ Error',
    ST_STOPPED: '⏸ Stopped',
}

PENDING_STATUSES = (ST_QUEUED, ST_STOPPED, ST_DOWNLOADING)
RESUMABLE_STATUSES = (ST_QUEUED, ST_STOPPED)

CHK_ON, CHK_OFF = '☑', '☐'


def _python_bin():
    """Interpreter used to run bulkdownloader.py."""
    if not FROZEN:
        return sys.executable
    for name in ('python', 'python3'):
        found = shutil.which(name)
        if found:
            return found
    return 'python'


def _subprocess_flags():
    """Keep child consoles from flashing on Windows when run from a windowed exe."""
    if sys.platform == 'win32':
        return {'creationflags': 0x08000000}  # CREATE_NO_WINDOW
    return {}


def _ensure_link_files():
    for path in (LINKS_TO_DOWNLOAD, LINKS_DOWNLOADED, LINKS_FAILED):
        if not path.exists():
            try:
                path.touch()
            except OSError:
                pass


def _seed_bundled(dest):
    """Copy a bundled data file (websites.json / categories.json) into DATA_DIR
    once — frozen builds and first runs — so it's editable and always present."""
    if dest.exists():
        return
    for src in (BUNDLE_DIR / dest.name, APP_DIR / dest.name):
        try:
            if src.exists() and src.resolve() != dest.resolve():
                shutil.copyfile(src, dest)
                return
        except OSError:
            return


def _read_link_lines(path):
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding='utf-8', errors='replace').splitlines() if line.strip()]


def _write_link_lines(path, lines):
    try:
        path.write_text('\n'.join(lines) + '\n' if lines else '', encoding='utf-8')
    except OSError:
        pass


def _remove_link(path, url):
    lines = _read_link_lines(path)
    if url in lines:
        _write_link_lines(path, [u for u in lines if u != url])


def _append_link(path, url, cap=None):
    lines = _read_link_lines(path)
    if url not in lines:
        lines.append(url)
        if cap and len(lines) > cap:
            lines = lines[-cap:]
        _write_link_lines(path, lines)


def _is_http(url):
    return url.startswith(('http://', 'https://'))


def _norm_key(url):
    """De-dup key: lowercase host, drop tracking params + trailing slash/fragment.
    Used ONLY for duplicate detection — the original URL is what gets downloaded."""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (p.hostname or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    query = urllib.parse.urlencode([
        (k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ])
    return urllib.parse.urlunsplit((p.scheme.lower(), host, p.path.rstrip('/'), query, ''))


def _read_stream(stream):
    """Yield output split on both \\n and \\r so yt-dlp's carriage-return
    progress updates surface immediately instead of only on newline."""
    buf = []
    while True:
        ch = stream.read(1)
        if not ch:
            if buf:
                yield ''.join(buf)
            return
        if ch in ('\r', '\n'):
            if buf:
                yield ''.join(buf)
                buf = []
        else:
            buf.append(ch)


# ── config persistence ────────────────────────────────────────────────

def _load_config():
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


# ── website registry (raw JSON, so favourites + all fields round-trip) ──

def _load_websites_raw():
    for path in (WEBSITES_JSON, APP_DIR / 'websites.json', BUNDLE_DIR / 'websites.json'):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return [s for s in data if isinstance(s, dict)]
        except (OSError, ValueError):
            continue
    return []


def _save_websites_raw(sites):
    try:
        WEBSITES_JSON.write_text(json.dumps(sites, indent=2, ensure_ascii=False), encoding='utf-8')
        return True
    except OSError:
        return False


def _load_categories():
    """The merged {category: {displayName, tags[]}} map for the gallery sidebar."""
    for path in (CATEGORIES_JSON, APP_DIR / 'categories.json', BUNDLE_DIR / 'categories.json'):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            continue
    return {}


def _title_tokens(name):
    """Lowercase alphanumeric word-set of a filename (extension stripped)."""
    base = os.path.splitext(name)[0].lower()
    return set(re.sub(r'[^a-z0-9]+', ' ', base).split())


def _host_of(url):
    try:
        host = (urllib.parse.urlparse(url).hostname or '').lower()
    except ValueError:
        host = ''
    return host[4:] if host.startswith('www.') else host


def _build_site_matchers(sites):
    """For each site, collect candidate hosts + a name token for loose matching."""
    matchers = []
    for s in sites:
        hosts = set()
        for key in ('url', 'searchURL'):
            h = _host_of(s.get(key) or '')
            if h:
                hosts.add(h)
        token = re.sub(r'[^a-z0-9]', '', (s.get('name') or '').lower())
        matchers.append((s.get('name') or (next(iter(hosts), '')), hosts, token))
    return matchers


def _match_host(host, matchers):
    """Return the matching site name for a bookmark host, or None.

    A bookmark matches when its host equals/is a sub-domain of a registered
    host, or when a whole domain label equals the site's name token (also
    catching numbered mirrors like ``xvideos2``). Substring matching is
    deliberately avoided so a site literally named ``porn`` doesn't swallow
    every host that happens to contain the word.
    """
    host = host.lower()
    if host.startswith('www.'):
        host = host[4:]
    labels = host.split('.')
    for name, hosts, token in matchers:
        for h in hosts:
            if h and (host == h or host.endswith('.' + h) or h.endswith('.' + host)):
                return name
        if token and len(token) >= 4:
            for lab in labels:
                if lab == token or (lab.startswith(token) and lab[len(token):].isdigit()):
                    return name
    return None


# ── browser bookmark readers ─────────────────────────────────────────

def _chromium_bookmark_files():
    """List (label, Bookmarks-json-path) for every Chromium profile found."""
    home = Path.home()
    if sys.platform == 'win32':
        local = Path(os.environ.get('LOCALAPPDATA', home / 'AppData' / 'Local'))
        roots = {
            'Chrome': local / 'Google' / 'Chrome' / 'User Data',
            'Edge': local / 'Microsoft' / 'Edge' / 'User Data',
            'Brave': local / 'BraveSoftware' / 'Brave-Browser' / 'User Data',
        }
    elif sys.platform == 'darwin':
        app = home / 'Library' / 'Application Support'
        roots = {
            'Chrome': app / 'Google' / 'Chrome',
            'Edge': app / 'Microsoft Edge',
            'Brave': app / 'BraveSoftware' / 'Brave-Browser',
        }
    else:
        cfg = home / '.config'
        roots = {
            'Chrome': cfg / 'google-chrome',
            'Chromium': cfg / 'chromium',
            'Edge': cfg / 'microsoft-edge',
            'Brave': cfg / 'BraveSoftware' / 'Brave-Browser',
        }
    found = []
    for browser, root in roots.items():
        if not root.is_dir():
            continue
        try:
            profiles = sorted(root.iterdir())
        except OSError:
            continue
        for prof in profiles:
            bm = prof / 'Bookmarks'
            if bm.is_file():
                found.append((f'{browser} · {prof.name}', bm))
    return found


def _read_chromium_bookmarks(path):
    out = []
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError):
        return out

    def walk(node):
        if not isinstance(node, dict):
            return
        if node.get('type') == 'url':
            url = node.get('url') or ''
            if _is_http(url):
                out.append((node.get('name') or url, url))
        for child in node.get('children') or []:
            walk(child)

    for key in ('bookmark_bar', 'other', 'synced'):
        node = (data.get('roots') or {}).get(key)
        if node:
            walk(node)
    return out


def _firefox_places_files():
    home = Path.home()
    if sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA', home / 'AppData' / 'Roaming')) / 'Mozilla' / 'Firefox' / 'Profiles'
    elif sys.platform == 'darwin':
        base = home / 'Library' / 'Application Support' / 'Firefox' / 'Profiles'
    else:
        base = home / '.mozilla' / 'firefox'
    if not base.is_dir():
        return []
    return [(p.name, p / 'places.sqlite') for p in sorted(base.iterdir())
            if p.is_dir() and (p / 'places.sqlite').is_file()]


def _read_firefox_bookmarks(places_path):
    """Read bookmarks from a copy of places.sqlite (the live file is locked while
    Firefox is open). The -wal / -shm sidecars are copied too so the newest
    bookmarks aren't missed."""
    out = []
    tmpdir = Path(tempfile.mkdtemp(prefix='aphro_ff_'))
    try:
        dst = tmpdir / 'places.sqlite'
        shutil.copyfile(places_path, dst)
        for ext in ('-wal', '-shm'):
            side = Path(str(places_path) + ext)
            if side.exists():
                try:
                    shutil.copyfile(side, Path(str(dst) + ext))
                except OSError:
                    pass
        try:
            con = sqlite3.connect(f'file:{dst}?mode=ro', uri=True)
        except sqlite3.Error:
            con = sqlite3.connect(str(dst))
        try:
            cur = con.execute(
                'SELECT b.title, p.url FROM moz_bookmarks b '
                'JOIN moz_places p ON b.fk = p.id '
                "WHERE b.type = 1 AND p.url LIKE 'http%'")
            for title, url in cur.fetchall():
                if url:
                    out.append((title or url, url))
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return out


# ── X.com cookie helpers ─────────────────────────────────────────────

def _cookie_status(config):
    """Human label describing the active X.com login method."""
    browser = (config or {}).get('cookies_from_browser', '')
    if browser:
        return (f'✓ Using your {browser.title()} browser login — yt-dlp reads its live '
                f'cookies. Stay signed in to x.com in {browser.title()}.')
    if not COOKIES_FILE.exists():
        return '○ No X.com login configured — sensitive / login-gated videos may fail.'
    try:
        head = COOKIES_FILE.read_text(encoding='utf-8', errors='replace')[:65536]
    except OSError:
        return '○ cookies.txt present but unreadable.'
    if 'x.com' in head or 'twitter.com' in head:
        return '✓ X.com login cookies saved — used automatically for downloads.'
    return '⚠ cookies.txt present but contains no x.com/twitter cookies.'


def _write_x_cookies_from_tokens(auth_token, ct0):
    """Synthesize a Netscape cookies.txt from the two cookies that matter for X.com."""
    expiry = int(time.time()) + 365 * 24 * 3600
    lines = ['# Netscape HTTP Cookie File',
             '# Generated by AphroArchive Download Manager', '']
    for domain in ('.x.com', '.twitter.com'):
        lines.append(f'{domain}\tTRUE\t/\tTRUE\t{expiry}\tauth_token\t{auth_token}')
        if ct0:
            lines.append(f'{domain}\tTRUE\t/\tTRUE\t{expiry}\tct0\t{ct0}')
    COOKIES_FILE.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def _detect_installed_browsers():
    """yt-dlp browser names whose profile directory exists on this machine
    (checked across Windows / macOS / Linux). Firefox is listed first when
    present — its cookies are the most reliable to read (no DPAPI lock)."""
    home = Path.home()
    found = []

    def check(name, path):
        try:
            if path.is_dir():
                found.append(name)
        except OSError:
            pass

    if sys.platform == 'win32':
        local = Path(os.environ.get('LOCALAPPDATA', home / 'AppData' / 'Local'))
        roam = Path(os.environ.get('APPDATA', home / 'AppData' / 'Roaming'))
        check('firefox', roam / 'Mozilla' / 'Firefox' / 'Profiles')
        check('chrome', local / 'Google' / 'Chrome' / 'User Data')
        check('edge', local / 'Microsoft' / 'Edge' / 'User Data')
        check('brave', local / 'BraveSoftware' / 'Brave-Browser' / 'User Data')
        check('opera', roam / 'Opera Software' / 'Opera Stable')
        check('vivaldi', local / 'Vivaldi' / 'User Data')
    elif sys.platform == 'darwin':
        app = home / 'Library' / 'Application Support'
        check('firefox', app / 'Firefox' / 'Profiles')
        check('chrome', app / 'Google' / 'Chrome')
        check('edge', app / 'Microsoft Edge')
        check('brave', app / 'BraveSoftware' / 'Brave-Browser')
        check('safari', home / 'Library' / 'Safari')
        check('opera', app / 'com.operasoftware.Opera')
        check('vivaldi', app / 'Vivaldi')
    else:
        cfg = home / '.config'
        check('firefox', home / '.mozilla' / 'firefox')
        check('chrome', cfg / 'google-chrome')
        check('chromium', cfg / 'chromium')
        check('edge', cfg / 'microsoft-edge')
        check('brave', cfg / 'BraveSoftware' / 'Brave-Browser')
        check('opera', cfg / 'opera')
        check('vivaldi', cfg / 'vivaldi')
    return found


def _autodetect_cookies():
    """Scan common folders for an exported cookies.txt with x.com cookies."""
    home = Path.home()
    dirs = [DATA_DIR, APP_DIR, Path.cwd(), home, home / 'Downloads', home / 'Desktop', home / 'Documents']
    best = None
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            candidates = list(d.glob('*.txt'))
        except OSError:
            continue
        for p in candidates:
            try:
                if p.resolve() == COOKIES_FILE.resolve():
                    continue
                head = p.read_text(encoding='utf-8', errors='replace')[:65536]
            except OSError:
                continue
            if ('x.com' in head or 'twitter.com' in head) and ('\t' in head or 'Netscape' in head):
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    mtime = 0
                if best is None or mtime > best[0]:
                    best = (mtime, p)
    return best[1] if best else None


# ════════════════════════════════════════════════════════════════════
#  Main window
# ════════════════════════════════════════════════════════════════════

class DownloadManager(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('AphroArchive — Download Manager')
        self.minsize(860, 580)
        self.configure(bg=BG)

        _ensure_link_files()
        _seed_bundled(WEBSITES_JSON)
        _seed_bundled(CATEGORIES_JSON)
        self._config = _load_config()

        self._ids = itertools.count(1)
        self.items = {}                  # iid -> {url, status, pct, file, title, speed, eta, error}
        self.out_queue = queue.Queue()   # worker/threads -> UI messages

        # ── parallel download engine state (all mutated on the main thread) ──
        self.active = {}                 # iid -> Popen (or None until launched)
        self._cancelling = set()         # iids intentionally terminated (pause / cancel)
        self._timeouts = set()           # iids killed by the stall watchdog
        self._activity = {}              # iid -> monotonic ts of last output (watchdog)
        self.is_running = False
        self.paused = False
        self._env = None
        self._out_dir_path = None
        self._drag_iid = None

        self.sites_raw = _load_websites_raw()
        self._all_bookmarks = []

        self.categories_map = _load_categories()
        self._gallery_cat_terms = self._build_cat_terms()
        self._gallery_files = []
        self._gallery_truncated = False
        self._gallery_tag_filter = None
        self._gallery_selecting = False

        self._gallery_gen = 0
        self._gallery_cards = []
        self._gallery_thumb_labels = []
        self._gallery_imgs = []
        self._gallery_cols = 0

        self._dupe_gen = 0
        self._dupe_paths = {}

        _saved_out = self._config.get('out_dir')
        try:   # migrate the old videos/downloads default to the plain downloads folder
            if _saved_out and Path(_saved_out).resolve() == _LEGACY_OUT_DIR.resolve():
                _saved_out = None
        except (OSError, ValueError):
            pass
        self.out_dir = tk.StringVar(value=_saved_out or str(DEFAULT_OUT_DIR))
        self.max_parallel = tk.IntVar(value=int(self._config.get('max_parallel', 2) or 2))
        self.start_timeout = tk.IntVar(value=int(self._config.get('start_timeout', 90) or 0))
        self.autostart_var = tk.BooleanVar(value=bool(self._config.get('autostart', True)))
        self.status_var = tk.StringVar(value='Idle')
        self.overall_var = tk.StringVar(value='')

        self._setup_style()
        self._build_ui()
        self._load_initial_queue()

        # Restore geometry / tab, then start auto-saving on changes.
        geo = self._config.get('geometry')
        try:
            self.geometry(geo if geo else '1060x740')
        except tk.TclError:
            self.geometry('1060x740')
        try:
            self.nb.select(int(self._config.get('last_tab', 0)))
        except (tk.TclError, ValueError):
            pass
        self.out_dir.trace_add('write', lambda *_: self._save_config())
        self.max_parallel.trace_add('write', lambda *_: self._save_config())

        if self._config.get('console_open'):
            self._toggle_console()

        self.after(100, self._poll_queue)
        self.protocol('WM_DELETE_WINDOW', self._on_close)

        # Auto-start the queue shortly after launch (gives the UI time to draw).
        if self.autostart_var.get() and self._next_pending():
            self.after(500, self._autostart)

    # ── styling ───────────────────────────────────────────────────────
    def _setup_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass

        style.configure('.', background=BG, font=FONT)
        style.configure('TFrame', background=BG)
        style.configure('TLabelframe', background=BG, bordercolor=BORDER)
        style.configure('TLabelframe.Label', background=BG, font=FONT_BOLD, foreground='#374151')
        style.configure('TLabel', background=BG, font=FONT)

        style.configure('TButton', font=FONT, padding=(10, 5))
        style.configure('Accent.TButton', font=FONT_BOLD, padding=(12, 6),
                        background=ACCENT, foreground='white')
        style.map('Accent.TButton',
                  background=[('active', ACCENT_ACTIVE), ('disabled', '#93b6f8')],
                  foreground=[('disabled', '#e5e7eb')])

        style.configure('Stop.TButton', font=FONT_BOLD, padding=(12, 6),
                        background=ERROR, foreground='white')
        style.map('Stop.TButton',
                  background=[('active', '#b91c1c'), ('disabled', '#f3a1a1')],
                  foreground=[('disabled', '#fde8e8')])

        style.configure('Header.TLabel', font=FONT_HEADER, background=BG, foreground='#111827')
        style.configure('Sub.TLabel', font=FONT_SUB, background=BG, foreground=MUTED)
        style.configure('Guide.TLabel', font=FONT_SUB, background=PANEL_BG, foreground='#374151')
        style.configure('Status.TLabel', font=FONT, background=BG, foreground='#374151')
        style.configure('Count.TLabel', font=FONT_SUB, background=BG, foreground=MUTED)
        style.configure('Card.TFrame', background=PANEL_BG, relief='solid', borderwidth=1)
        style.configure('CardName.TLabel', background=PANEL_BG, font=FONT_SUB, foreground='#374151')
        style.configure('CardSub.TLabel', background=PANEL_BG, font=(_UI_FONT, 8), foreground=MUTED)

        style.configure('TEntry', padding=4)
        style.configure('TProgressbar', thickness=14, background=ACCENT)

        style.configure('Treeview', font=FONT, rowheight=26, background=PANEL_BG,
                        fieldbackground=PANEL_BG, bordercolor=BORDER)
        style.configure('Treeview.Heading', font=FONT_BOLD, padding=(6, 4))
        style.map('Treeview', background=[('selected', '#dbeafe')], foreground=[('selected', '#111827')])

        style.configure('TNotebook', background=BG, borderwidth=0)
        style.configure('TNotebook.Tab', font=FONT_BOLD, padding=(16, 8))
        style.map('TNotebook.Tab',
                  background=[('selected', PANEL_BG)],
                  foreground=[('selected', ACCENT), ('!selected', MUTED)])

    # ── overall layout ────────────────────────────────────────────────
    def _build_ui(self):
        body = ttk.Frame(self)
        body.pack(fill='both', expand=True, padx=8, pady=(8, 4))

        self._build_console_drawer(body)   # right-hand drawer (created hidden)

        self.nb = ttk.Notebook(body)
        self.nb.pack(side='left', fill='both', expand=True)

        self.tab_downloads = ttk.Frame(self.nb)
        self.tab_errored = ttk.Frame(self.nb)
        self.tab_bookmarks = ttk.Frame(self.nb)
        self.tab_search = ttk.Frame(self.nb)
        self.tab_gallery = ttk.Frame(self.nb)
        self.tab_duplicates = ttk.Frame(self.nb)
        self.tab_xlogin = ttk.Frame(self.nb)

        self.nb.add(self.tab_downloads, text='⬇ Downloads')
        self.nb.add(self.tab_errored, text='❌ Errored')
        self.nb.add(self.tab_bookmarks, text='🔖 Bookmarks')
        self.nb.add(self.tab_search, text='🔍 Search')
        self.nb.add(self.tab_gallery, text='🎬 Gallery')
        self.nb.add(self.tab_duplicates, text='🧬 Duplicates')
        self.nb.add(self.tab_xlogin, text='🔑 X.com')

        self._build_downloads_tab(self.tab_downloads)
        self._build_errored_tab(self.tab_errored)
        self._build_bookmarks_tab(self.tab_bookmarks)
        self._build_search_tab(self.tab_search)
        self._build_gallery_tab(self.tab_gallery)
        self._build_duplicates_tab(self.tab_duplicates)
        self._build_xlogin_tab(self.tab_xlogin)

        self.nb.bind('<<NotebookTabChanged>>', self._on_tab_changed)

        status = ttk.Frame(self)
        status.pack(fill='x', padx=12, pady=(0, 8))
        ttk.Label(status, textvariable=self.status_var, style='Status.TLabel',
                  anchor='w').pack(side='left', fill='x', expand=True)

    # ── reusable tick-box behaviour for any Treeview (column name 'chk') ──
    def _setup_checktree(self, tree):
        tree._checked = set()
        tree.heading('chk', text=CHK_OFF, command=lambda t=tree: self._toggle_all_checks(t))
        tree.bind('<Button-1>', lambda e, t=tree: self._on_chk_click(e, t), add='+')
        tree.bind('<space>', lambda e, t=tree: self._space_toggle(t))

    def _on_chk_click(self, event, tree):
        if tree.identify_region(event.x, event.y) != 'cell':
            return None
        if tree.identify_column(event.x) != '#1':   # the leading tick-box column
            return None
        iid = tree.identify_row(event.y)
        if iid:
            self._set_check(tree, iid, iid not in tree._checked)
            return 'break'
        return None

    def _set_check(self, tree, iid, on):
        if on:
            tree._checked.add(iid)
        else:
            tree._checked.discard(iid)
        try:
            tree.set(iid, 'chk', CHK_ON if on else CHK_OFF)
        except tk.TclError:
            pass

    def _toggle_all_checks(self, tree):
        kids = tree.get_children()
        turn_on = not (kids and all(i in tree._checked for i in kids))
        for i in kids:
            self._set_check(tree, i, turn_on)

    def _space_toggle(self, tree):
        for iid in tree.selection():
            self._set_check(tree, iid, iid not in tree._checked)
        return 'break'

    def _targets(self, tree, fallback_all=False):
        """Ticked rows, else the normal selection, else (optionally) every row."""
        checked = [i for i in tree.get_children() if i in getattr(tree, '_checked', ())]
        if checked:
            return checked
        sel = list(tree.selection())
        if sel:
            return sel
        return list(tree.get_children()) if fallback_all else []

    # ════════════════════════════════════════════════════════════════
    #  Downloads tab
    # ════════════════════════════════════════════════════════════════
    def _build_downloads_tab(self, parent):
        pad = {'padx': 12, 'pady': 6}

        url_panel = ttk.LabelFrame(parent, text='Add URLs (one per line)')
        url_panel.pack(fill='x', **pad)

        text_wrap = ttk.Frame(url_panel)
        text_wrap.pack(fill='x', padx=8, pady=(8, 4))
        self.url_text = tk.Text(text_wrap, height=3, wrap='none', undo=True,
                                font=FONT_MONO, relief='flat', borderwidth=1,
                                highlightthickness=1, highlightbackground=BORDER,
                                highlightcolor=ACCENT)
        url_vscroll = ttk.Scrollbar(text_wrap, command=self.url_text.yview)
        self.url_text.configure(yscrollcommand=url_vscroll.set)
        self.url_text.pack(side='left', fill='both', expand=True)
        url_vscroll.pack(side='right', fill='y')
        self.url_text.bind('<Control-Return>', lambda e: (self._download_now(), 'break')[1])

        url_btns = ttk.Frame(url_panel)
        url_btns.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Button(url_btns, text='⚡ Download now', style='Accent.TButton',
                   command=self._download_now).pack(side='left')
        ttk.Button(url_btns, text='➕ Add to bottom',
                   command=lambda: self._add_box_to_queue(at_top=False)).pack(side='left', padx=6)
        ttk.Button(url_btns, text='⤴ Add to top',
                   command=lambda: self._add_box_to_queue(at_top=True)).pack(side='left')
        ttk.Button(url_btns, text='📋 Paste', command=self._paste_clipboard).pack(side='left', padx=6)
        ttk.Button(url_btns, text='✖ Clear box',
                   command=lambda: self.url_text.delete('1.0', 'end')).pack(side='left')
        ttk.Button(url_btns, text='🔄 Reload from files', command=self._reload_from_files).pack(side='right')

        out_panel = ttk.LabelFrame(parent, text='Destination')
        out_panel.pack(fill='x', **pad)
        out_inner = ttk.Frame(out_panel)
        out_inner.pack(fill='x', padx=8, pady=8)
        ttk.Label(out_inner, text='Save to:').pack(side='left')
        ttk.Entry(out_inner, textvariable=self.out_dir).pack(side='left', fill='x', expand=True, padx=6)
        ttk.Button(out_inner, text='Browse…', command=self._browse).pack(side='left')
        ttk.Button(out_inner, text='Open',
                   command=lambda: self._open_path(Path(self.out_dir.get()))).pack(side='left', padx=(6, 0))

        ctrl = ttk.Frame(parent)
        ctrl.pack(fill='x', **pad)
        self.start_btn = ttk.Button(ctrl, text='▶  Start', style='Accent.TButton', command=self._start)
        self.start_btn.pack(side='left')
        self.pause_btn = ttk.Button(ctrl, text='⏸  Pause', style='Stop.TButton',
                                    command=self._pause, state='disabled')
        self.pause_btn.pack(side='left', padx=6)
        self.console_btn = ttk.Button(ctrl, text='🖥 Console', command=self._toggle_console)
        self.console_btn.pack(side='left', padx=(0, 6))
        ttk.Label(ctrl, text='Parallel:').pack(side='left', padx=(6, 2))
        ttk.Spinbox(ctrl, from_=1, to=10, width=4, textvariable=self.max_parallel,
                    command=self._pump).pack(side='left')
        ttk.Label(ctrl, text='Stall timeout (s):').pack(side='left', padx=(8, 2))
        ttk.Spinbox(ctrl, from_=0, to=600, increment=10, width=5, textvariable=self.start_timeout,
                    command=self._save_config).pack(side='left')
        ttk.Checkbutton(ctrl, text='Auto-start', variable=self.autostart_var,
                        command=self._save_config).pack(side='left', padx=(8, 0))
        ttk.Button(ctrl, text='🔀 Shuffle', command=self._shuffle_queue).pack(side='left', padx=(10, 0))
        ttk.Button(ctrl, text='↻ Retry', command=self._retry_selected).pack(side='left', padx=6)
        ttk.Button(ctrl, text='🗑 Remove', command=self._remove_selected).pack(side='left', padx=6)
        ttk.Button(ctrl, text='🧹 Clear finished', command=self._clear_finished).pack(side='left')
        ttk.Button(ctrl, text='⌫ Clear errored', command=self._clear_errored).pack(side='left', padx=6)
        ttk.Label(ctrl, textvariable=self.overall_var, style='Count.TLabel').pack(side='right')

        list_panel = ttk.LabelFrame(parent, text='Queue  ·  tick rows, drag to reorder, Delete removes')
        list_panel.pack(fill='both', expand=True, **pad)
        list_inner = ttk.Frame(list_panel)
        list_inner.pack(fill='both', expand=True, padx=8, pady=8)

        self.tree = ttk.Treeview(list_inner, columns=('chk', 'status', 'progress', 'speed'),
                                 show='tree headings', selectmode='extended')
        self.tree.heading('#0', text='URL / File')
        self.tree.heading('status', text='Status')
        self.tree.heading('progress', text='%')
        self.tree.heading('speed', text='Speed / ETA')
        self.tree.column('#0', width=480, stretch=True)
        self.tree.column('chk', width=34, anchor='center', stretch=False)
        self.tree.column('status', width=120, anchor='w', stretch=False)
        self.tree.column('progress', width=56, anchor='e', stretch=False)
        self.tree.column('speed', width=150, anchor='w', stretch=False)
        tree_scroll = ttk.Scrollbar(list_inner, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side='left', fill='both', expand=True)
        tree_scroll.pack(side='right', fill='y')
        self._setup_checktree(self.tree)
        self.tree.bind('<Double-1>', self._open_selected_file)
        self.tree.bind('<ButtonPress-1>', self._on_tree_press, add='+')
        self.tree.bind('<B1-Motion>', self._on_tree_motion)
        self.tree.bind('<ButtonRelease-1>', self._on_tree_release)
        self.tree.bind('<Delete>', lambda e: self._remove_selected())
        self.tree.bind('<KP_Delete>', lambda e: self._remove_selected())
        self.tree.bind('<BackSpace>', lambda e: self._remove_selected())
        self.tree.bind('<Button-3>', self._popup_menu)
        self.tree.bind('<Button-2>', self._popup_menu)

        self.tree.tag_configure(ST_DONE, foreground=SUCCESS)
        self.tree.tag_configure(ST_ERROR, foreground=ERROR)
        self.tree.tag_configure(ST_DOWNLOADING, foreground=ACCENT)
        self.tree.tag_configure(ST_STOPPED, foreground=MUTED)

        self.ctx_menu = tk.Menu(self, tearoff=0)
        self.ctx_menu.add_command(label='⚡ Download now', command=self._download_now_rows)
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label='Move to top', command=lambda: self._move_targets(0))
        self.ctx_menu.add_command(label='Move to bottom', command=lambda: self._move_targets('end'))
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label='Retry', command=self._retry_selected)
        self.ctx_menu.add_command(label='Remove', command=self._remove_selected)
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label='Open file / folder', command=self._open_selected_file)

    # ── queue file <-> tree sync ──────────────────────────────────────
    def _load_initial_queue(self):
        """Populate the tree from the txt files on launch, preserving order.
        Done rows are capped so a long history doesn't bloat the queue."""
        seen = set()

        def add_all(path, status, limit=None):
            urls = [u for u in _read_link_lines(path) if _is_http(u)]
            if limit:
                urls = urls[-limit:]
            for url in urls:
                k = _norm_key(url)
                if k not in seen:
                    seen.add(k)
                    self._add_item(url, status=status)
            return len(urls)

        add_all(LINKS_TO_DOWNLOAD, ST_QUEUED)
        add_all(LINKS_FAILED, ST_ERROR)
        total_done = len([u for u in _read_link_lines(LINKS_DOWNLOADED) if _is_http(u)])
        add_all(LINKS_DOWNLOADED, ST_DONE, limit=DONE_LOAD_CAP)

        self._update_overall()
        if self._next_pending():
            extra = f' ({total_done - DONE_LOAD_CAP} older done rows hidden)' if total_done > DONE_LOAD_CAP else ''
            self.status_var.set(f'Queue loaded from files. Press Start to download.{extra}')

    def _rebuild_to_download_file(self):
        urls = []
        for iid in self.tree.get_children():
            it = self.items.get(iid)
            if it and it['status'] in PENDING_STATUSES:
                urls.append(it['url'])
        _write_link_lines(LINKS_TO_DOWNLOAD, list(dict.fromkeys(urls)))

    # ── queue management ──────────────────────────────────────────────
    def _paste_clipboard(self):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        if text.strip():
            self.url_text.insert('end', text.strip() + '\n')

    def _reload_from_files(self):
        if self.is_running:
            messagebox.showinfo('Running', 'Pause downloads before reloading the queue from files.')
            return
        for iid in list(self.tree.get_children()):
            self.tree.delete(iid)
        self.items.clear()
        self.tree._checked.clear()
        self._load_initial_queue()
        self.status_var.set('Queue reloaded from txt files.')

    def _add_box_to_queue(self, at_top=False):
        raw = self.url_text.get('1.0', 'end').splitlines()
        added = self._queue_urls([l.strip() for l in raw], at_top=at_top)
        if added:
            self.url_text.delete('1.0', 'end')
            where = 'top' if at_top else 'bottom'
            self.status_var.set(f'Added {added} URL{"s" if added != 1 else ""} to the {where} of the queue.')
            self._pump()
        else:
            messagebox.showinfo('Nothing added', 'No new http(s) URLs found in the box.')

    def _queue_urls(self, urls, at_top=False):
        existing = {_norm_key(it['url']) for it in self.items.values()}
        new = []
        for u in urls:
            if not _is_http(u):
                continue
            k = _norm_key(u)
            if k in existing:
                continue
            existing.add(k)
            new.append(u)
        for i, u in enumerate(new):
            self._add_item(u, index=(i if at_top else 'end'))
        if new:
            self._rebuild_to_download_file()
            self._update_overall()
            self._maybe_autostart()
        return len(new)

    def _maybe_autostart(self):
        """Keep a running queue fed; if idle and Auto-start is on, start the top now."""
        if self.is_running:
            self._pump()
        elif self.autostart_var.get() and self._next_pending():
            self._autostart()

    def _add_item(self, url, status=ST_QUEUED, index='end'):
        iid = f'item{next(self._ids)}'
        self.items[iid] = {'url': url, 'status': status, 'pct': 100 if status == ST_DONE else 0,
                           'file': None, 'title': None, 'speed': '', 'eta': '', 'error': ''}
        self.tree.insert('', index, iid=iid, text=url, values=(CHK_OFF, '', '', ''))
        self._set_item(iid)
        return iid

    def _set_item(self, iid, **changes):
        item = self.items.get(iid)
        if not item:
            return
        item.update(changes)
        status = item['status']
        label = item.get('title') or item['url']
        if status == ST_DONE and item.get('file'):
            label = os.path.basename(item['file'])
        pct = item.get('pct') or 0
        if status == ST_DOWNLOADING and pct:
            pct_text = f'{pct:.0f}%'
        elif status == ST_DONE:
            pct_text = '100%'
        else:
            pct_text = ''
        speed_text = ''
        if status == ST_DOWNLOADING:
            bits = [b for b in (item.get('speed'), ('ETA ' + item['eta']) if item.get('eta') else '') if b]
            speed_text = '  '.join(bits)
        elif status == ST_ERROR and item.get('error'):
            speed_text = '⚠ double-click for details'
        tag = status if status in (ST_DONE, ST_ERROR, ST_DOWNLOADING, ST_STOPPED) else ''
        self.tree.item(iid, text=label, tags=(tag,) if tag else ())
        self.tree.set(iid, 'status', STATUS_LABEL[status])
        self.tree.set(iid, 'progress', pct_text)
        self.tree.set(iid, 'speed', speed_text)

    def _next_pending(self):
        for iid in self.tree.get_children():
            if iid in self.active:
                continue
            if self.items[iid]['status'] in RESUMABLE_STATUSES:
                return iid
        return None

    def _has_pending(self):
        return any(it['status'] in RESUMABLE_STATUSES for it in self.items.values())

    def _update_overall(self):
        total = len(self.items)
        done = sum(1 for it in self.items.values() if it['status'] == ST_DONE)
        err = sum(1 for it in self.items.values() if it['status'] == ST_ERROR)
        pend = sum(1 for it in self.items.values() if it['status'] in RESUMABLE_STATUSES)
        if not total:
            self.overall_var.set('')
            return
        parts = [f'{done}/{total} done']
        if self.active:
            parts.append(f'{len(self.active)} active')
        if pend:
            parts.append(f'{pend} queued')
        if err:
            parts.append(f'{err} failed')
        self.overall_var.set('  ·  '.join(parts))

    def _remove_selected(self):
        targets = self._targets(self.tree)
        if not targets:
            return
        for iid in targets:
            url = self.items.get(iid, {}).get('url')
            if iid in self.active:                 # remove an in-flight download
                self._cancelling.add(iid)
                proc = self.active.get(iid)
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
            self.items.pop(iid, None)
            self.tree._checked.discard(iid)
            self.tree.delete(iid)
            if url:
                _remove_link(LINKS_TO_DOWNLOAD, url)
        self._rebuild_to_download_file()
        self._update_overall()

    def _retry_selected(self):
        changed = False
        for iid in self._targets(self.tree):
            item = self.items.get(iid)
            if item and item['status'] in (ST_ERROR, ST_DONE, ST_STOPPED):
                self._set_item(iid, status=ST_QUEUED, pct=0, error='', speed='', eta='')
                _remove_link(LINKS_FAILED, item['url'])
                _remove_link(LINKS_DOWNLOADED, item['url'])
                changed = True
        if changed:
            self._rebuild_to_download_file()
            self._update_overall()
            self._maybe_autostart()
            if not self.is_running and not self.autostart_var.get() and self._next_pending():
                self.status_var.set('Items re-queued. Press Start to download.')

    def _clear_finished(self):
        """Clear both completed AND errored rows (errored leave link_failed.txt too)."""
        for iid in list(self.tree.get_children()):
            it = self.items.get(iid)
            if it and it['status'] in (ST_DONE, ST_ERROR):
                if it['status'] == ST_ERROR:
                    _remove_link(LINKS_FAILED, it['url'])
                self.items.pop(iid, None)
                self.tree._checked.discard(iid)
                self.tree.delete(iid)
        self._update_overall()
        self._refresh_errored()

    def _move_targets(self, index):
        for iid in self._targets(self.tree):
            self.tree.move(iid, '', index)
        self._rebuild_to_download_file()

    def _shuffle_queue(self):
        """Randomly reorder ONLY the pending (queued/stopped) rows. Completed, errored
        and active rows keep their exact positions."""
        children = list(self.tree.get_children())
        pending_idx = [i for i, iid in enumerate(children)
                       if iid not in self.active and self.items[iid]['status'] in RESUMABLE_STATUSES]
        if len(pending_idx) < 2:
            return
        shuffled = [children[i] for i in pending_idx]
        random.shuffle(shuffled)
        target = list(children)
        for slot, iid in zip(pending_idx, shuffled):
            target[slot] = iid                 # fixed rows keep their slot
        for i, iid in enumerate(target):       # apply the order from the top down
            self.tree.move(iid, '', i)
        self._rebuild_to_download_file()
        self.status_var.set(f'🔀 Shuffled {len(pending_idx)} queued item(s) (completed left in place).')

    # ── "download now" (top of queue + start immediately) ─────────────
    def _start_or_pump(self):
        if not self.is_running or self.paused:
            self._start()
        else:
            self._pump()

    def _promote_and_start(self, iids):
        """Move the given rows to the top, re-queueing finished/failed ones."""
        promoted = 0
        for iid in iids:
            it = self.items.get(iid)
            if not it:
                continue
            if it['status'] in (ST_ERROR, ST_DONE):
                self._set_item(iid, status=ST_QUEUED, pct=0, error='', speed='', eta='')
                _remove_link(LINKS_FAILED, it['url'])
                _remove_link(LINKS_DOWNLOADED, it['url'])
            self.tree.move(iid, '', promoted)
            promoted += 1
        if promoted:
            self._rebuild_to_download_file()
            self._update_overall()
        return promoted

    def _download_now(self):
        """Put the box URLs (or, if none, the ticked/selected rows) on top and start now."""
        raw = [l.strip() for l in self.url_text.get('1.0', 'end').splitlines()]
        added = self._queue_urls(raw, at_top=True)
        if added:
            self.url_text.delete('1.0', 'end')
        elif not self._promote_and_start(self._targets(self.tree)) and not self._next_pending():
            messagebox.showinfo('Nothing to download', 'Paste a URL or tick a queue row first.')
            return
        self._start_or_pump()
        self.status_var.set('⚡ Downloading now…')

    def _download_now_rows(self):
        """Context-menu action: download the ticked/selected rows immediately."""
        if self._promote_and_start(self._targets(self.tree)):
            self._start_or_pump()
            self.status_var.set('⚡ Downloading now…')

    def _clear_errored(self):
        """Remove every failed row from the queue and from link_failed.txt."""
        n = 0
        for iid in list(self.tree.get_children()):
            it = self.items.get(iid)
            if it and it['status'] == ST_ERROR:
                _remove_link(LINKS_FAILED, it['url'])
                self.items.pop(iid, None)
                self.tree._checked.discard(iid)
                self.tree.delete(iid)
                n += 1
        self._update_overall()
        self._refresh_errored()
        self.status_var.set(f'Cleared {n} errored item(s).')

    # ── stall watchdog: skip downloads that produce no output for N s ──
    def _start_timeout(self):
        try:
            return max(0, int(self.start_timeout.get()))
        except (tk.TclError, ValueError):
            return 0

    def _check_timeouts(self):
        timeout = self._start_timeout()
        if timeout <= 0 or not self.active:
            return
        now = time.monotonic()
        for iid in list(self.active):
            if iid in self._cancelling or iid in self._timeouts:
                continue
            if now - self._activity.get(iid, now) > timeout:
                self._timeouts.add(iid)
                if iid in self.items:
                    self.items[iid]['error'] = f'timed out — no output for {timeout}s'
                    self._console_log(f'⏱ timeout  {self.items[iid]["url"]}  (no output for {timeout}s)')
                proc = self.active.get(iid)
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                    except OSError:
                        pass

    def _popup_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        if self.tree.selection() or self._targets(self.tree):
            try:
                self.ctx_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.ctx_menu.grab_release()

    # ── drag-to-reorder ───────────────────────────────────────────────
    def _on_tree_press(self, event):
        if self.tree.identify_column(event.x) == '#1':   # don't drag from the tick-box
            self._drag_iid = None
        else:
            self._drag_iid = self.tree.identify_row(event.y)

    def _on_tree_motion(self, event):
        if not self._drag_iid:
            return
        target = self.tree.identify_row(event.y)
        if target and target != self._drag_iid:
            self.tree.move(self._drag_iid, '', self.tree.index(target))

    def _on_tree_release(self, event):
        if self._drag_iid:
            self._drag_iid = None
            self._rebuild_to_download_file()

    # ── run control (parallel scheduler) ──────────────────────────────
    def _parallel(self):
        try:
            return max(1, min(10, int(self.max_parallel.get())))
        except (tk.TclError, ValueError):
            return 1

    def _build_env(self):
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['APHRO_DOWNLOADS_DIR'] = str(self._out_dir_path)
        browser = self._config.get('cookies_from_browser', '')
        if browser:
            env['BULK_COOKIES_FROM_BROWSER'] = browser
        elif COOKIES_FILE.exists():
            env['BULK_COOKIES_FILE'] = str(COOKIES_FILE)
        return env

    def _autostart(self):
        if not self.is_running and self._next_pending():
            self.status_var.set('Auto-starting downloads…')
            self._start()

    def _start(self):
        if self.is_running and not self.paused:
            return
        if not SCRIPT_PATH.exists():
            messagebox.showerror('Not found', f'Could not find {SCRIPT_PATH}')
            return
        if not self._next_pending():
            messagebox.showinfo('Empty queue', 'Add some URLs to the queue first.')
            return
        out_dir = Path(self.out_dir.get())
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror('Invalid folder', str(e))
            return

        self._out_dir_path = out_dir
        self._env = self._build_env()
        self.is_running = True
        self.paused = False
        self._update_controls()
        self._pump()

    def _pause(self):
        if not self.is_running or self.paused:
            return
        self.paused = True
        for iid, proc in list(self.active.items()):
            self._cancelling.add(iid)
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        self._update_controls()
        self.status_var.set('Pausing… active downloads stop and resume from their .part files.')

    def _update_controls(self):
        if not self.is_running:
            self.start_btn.configure(state='normal', text='▶  Start')
            self.pause_btn.configure(state='disabled')
        elif self.paused:
            self.start_btn.configure(state='normal', text='▶  Resume')
            self.pause_btn.configure(state='disabled')
        else:
            self.start_btn.configure(state='disabled', text='▶  Start')
            self.pause_btn.configure(state='normal')

    def _pump(self):
        """Main-thread scheduler: keep up to N downloads running, always pulling the
        next pending row from the top of the queue. Safe because all tree/order
        access happens here on the UI thread; workers only download."""
        if not self.is_running or self.paused:
            return
        while len(self.active) < self._parallel():
            iid = self._next_pending()
            if not iid:
                break
            self._launch(iid)
        if not self.active and not self._next_pending():
            self.is_running = False
            self.paused = False
            self._update_controls()
            self.status_var.set('✅ All downloads finished.' if not self._has_pending()
                                else 'Paused — items remain in the queue.')
        self._update_overall()

    def _launch(self, iid):
        self._set_item(iid, status=ST_DOWNLOADING, pct=0, speed='', eta='', error='')
        self.active[iid] = None
        self._activity[iid] = time.monotonic()
        self._rebuild_to_download_file()
        url = self.items[iid]['url']
        self._console_log(f'▶ start   {url}')
        self.status_var.set(f'⬇ Downloading {len(self.active)} item(s)…')
        threading.Thread(target=self._download_worker, args=(iid, url), daemon=True).start()

    def _download_worker(self, iid, url):
        # Must ALWAYS post 'done' — otherwise the slot in self.active leaks and the
        # whole queue stalls. So catch everything and report it back.
        try:
            code, result_file, err = self._run_download(iid, url, self._out_dir_path, self._env)
        except Exception as e:
            code, result_file, err = -1, None, f'downloader crashed: {e}'
        self.out_queue.put(('done', iid, code, (result_file, err)))

    def _run_download(self, iid, url, out_dir, env):
        """Runs entirely on a worker thread. Returns (code, result_file, error_text).
        It must NOT mutate self.items — that's the main thread's job — it only sets
        self.active[iid] (so the row can be terminated) and emits queue messages."""
        cmd = [_python_bin(), '-u', str(SCRIPT_PATH), '--url', url, '--out-dir', str(out_dir)]
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                env=env, cwd=str(PROJECT_ROOT), **_subprocess_flags(),
            )
        except OSError as e:
            return -1, None, f'failed to launch downloader: {e}'
        self.active[iid] = proc
        if iid in self._cancelling:        # paused/removed during the launch window
            try:
                proc.terminate()
            except OSError:
                pass

        host = _host_of(url) or 'download'
        result_file, last = None, ''
        for line in _read_stream(proc.stdout):
            line = line.strip()
            if not line:
                continue
            m = PROGRESS_RE.search(line)
            if m:
                sp = SPEED_RE.search(line)
                eta = ETA_RE.search(line)
                self.out_queue.put(('progress', iid, float(m.group(1)),
                                    (sp.group(1) if sp else '', eta.group(1) if eta else '')))
                continue
            # Everything that isn't a raw progress bar goes to the console drawer.
            self.out_queue.put(('console', iid, f'[{host}] {line}', None))
            mt = TITLE_RE.search(line)
            if mt:
                self.out_queue.put(('title', iid, mt.group(1), None))
            elif line.startswith('RESULT_FILE:'):
                result_file = line.split(':', 1)[1].strip()
            elif not line.startswith('RESULT_'):
                last = line
        code = proc.wait()
        return code, result_file, (last if code != 0 else '')

    # ════════════════════════════════════════════════════════════════
    #  Errored tab
    # ════════════════════════════════════════════════════════════════
    def _build_errored_tab(self, parent):
        pad = {'padx': 12, 'pady': 6}
        head = ttk.Frame(parent)
        head.pack(fill='x', **pad)
        ttk.Label(head, text='Failed downloads', style='Header.TLabel').pack(anchor='w')
        ttk.Label(head, text='Everything that errored or timed out. Re-queue to try again, or clear them out.',
                  style='Sub.TLabel').pack(anchor='w')

        bar = ttk.Frame(parent)
        bar.pack(fill='x', **pad)
        ttk.Button(bar, text='↻ Retry ticked', style='Accent.TButton', command=self._errored_retry).pack(side='left')
        ttk.Button(bar, text='↻ Retry all', command=self._errored_retry_all).pack(side='left', padx=6)
        ttk.Button(bar, text='📋 Copy URLs', command=self._errored_copy).pack(side='left')
        ttk.Button(bar, text='⌫ Clear errored', style='Stop.TButton', command=self._clear_errored).pack(side='left', padx=6)
        self.err_count_var = tk.StringVar(value='')
        ttk.Label(bar, textvariable=self.err_count_var, style='Count.TLabel').pack(side='right')

        list_panel = ttk.LabelFrame(parent, text='Errored items  ·  double-click for the error detail')
        list_panel.pack(fill='both', expand=True, **pad)
        list_inner = ttk.Frame(list_panel)
        list_inner.pack(fill='both', expand=True, padx=8, pady=8)
        self.err_tree = ttk.Treeview(list_inner, columns=('chk', 'reason'),
                                     show='tree headings', selectmode='extended')
        self.err_tree.heading('#0', text='URL')
        self.err_tree.heading('reason', text='Reason')
        self.err_tree.column('#0', width=440, stretch=True)
        self.err_tree.column('chk', width=34, anchor='center', stretch=False)
        self.err_tree.column('reason', width=360, stretch=True)
        esb = ttk.Scrollbar(list_inner, command=self.err_tree.yview)
        self.err_tree.configure(yscrollcommand=esb.set)
        self.err_tree.pack(side='left', fill='both', expand=True)
        esb.pack(side='right', fill='y')
        self._setup_checktree(self.err_tree)
        self.err_tree.bind('<Double-1>', self._errored_show_detail)
        self.err_tree.bind('<Delete>', lambda e: self._clear_errored())

    def _refresh_errored(self):
        if not hasattr(self, 'err_tree'):
            return
        self.err_tree._checked.clear()
        for iid in self.err_tree.get_children():
            self.err_tree.delete(iid)
        n = 0
        for iid in self.tree.get_children():
            it = self.items.get(iid)
            if it and it['status'] == ST_ERROR:
                self.err_tree.insert('', 'end', iid=iid, text=it['url'],
                                     values=(CHK_OFF, it.get('error') or 'unknown error'))
                n += 1
        self.err_count_var.set(f'{n} failed' if n else 'No failed downloads')

    def _requeue_iids(self, iids):
        changed = 0
        for iid in iids:
            it = self.items.get(iid)
            if it and it['status'] == ST_ERROR:
                self._set_item(iid, status=ST_QUEUED, pct=0, error='', speed='', eta='')
                _remove_link(LINKS_FAILED, it['url'])
                changed += 1
        if changed:
            self._rebuild_to_download_file()
            self._update_overall()
            self._refresh_errored()
            self._maybe_autostart()
            self.status_var.set(f'Re-queued {changed} failed item(s).')

    def _errored_retry(self):
        self._requeue_iids(self._targets(self.err_tree))

    def _errored_retry_all(self):
        self._requeue_iids(list(self.err_tree.get_children()))

    def _errored_copy(self):
        rows = self._targets(self.err_tree, fallback_all=True)
        urls = [self.items[i]['url'] for i in rows if i in self.items]
        if urls:
            self.clipboard_clear()
            self.clipboard_append('\n'.join(urls))
            self.status_var.set(f'Copied {len(urls)} URL(s) to clipboard.')

    def _errored_show_detail(self, event=None):
        sel = self.err_tree.selection()
        if not sel:
            return
        it = self.items.get(sel[0])
        if it:
            messagebox.showwarning('Download failed', f"{it['url']}\n\n{it.get('error') or 'unknown error'}")

    # ════════════════════════════════════════════════════════════════
    #  Duplicates tab
    # ════════════════════════════════════════════════════════════════
    def _build_duplicates_tab(self, parent):
        pad = {'padx': 12, 'pady': 6}
        head = ttk.Frame(parent)
        head.pack(fill='x', **pad)
        ttk.Label(head, text='Duplicate finder', style='Header.TLabel').pack(anchor='w')
        ttk.Label(head, text='Finds videos in the download folder with identical size + content hash. '
                             'Tick the copies you want to delete.', style='Sub.TLabel').pack(anchor='w')

        bar = ttk.Frame(parent)
        bar.pack(fill='x', **pad)
        ttk.Button(bar, text='🔍 Scan now', style='Accent.TButton', command=self._scan_duplicates).pack(side='left')
        ttk.Button(bar, text='✓ Tick all but newest', command=self._dupe_tick_extras).pack(side='left', padx=6)
        ttk.Button(bar, text='🗑 Delete ticked', style='Stop.TButton', command=self._delete_duplicates).pack(side='left')
        self.dupe_info = tk.StringVar(value='Press Scan to find duplicate videos.')
        ttk.Label(bar, textvariable=self.dupe_info, style='Count.TLabel').pack(side='right')

        list_panel = ttk.LabelFrame(parent, text='Duplicate groups  ·  double-click to play')
        list_panel.pack(fill='both', expand=True, **pad)
        li = ttk.Frame(list_panel)
        li.pack(fill='both', expand=True, padx=8, pady=8)
        self.dupe_tree = ttk.Treeview(li, columns=('chk', 'size'), show='tree headings', selectmode='extended')
        self.dupe_tree.heading('#0', text='File')
        self.dupe_tree.heading('size', text='Size')
        self.dupe_tree.column('#0', width=520, stretch=True)
        self.dupe_tree.column('chk', width=34, anchor='center', stretch=False)
        self.dupe_tree.column('size', width=100, anchor='e', stretch=False)
        dsb = ttk.Scrollbar(li, command=self.dupe_tree.yview)
        self.dupe_tree.configure(yscrollcommand=dsb.set)
        self.dupe_tree.pack(side='left', fill='both', expand=True)
        dsb.pack(side='right', fill='y')
        self._setup_checktree(self.dupe_tree)
        self.dupe_tree.bind('<Double-1>', self._dupe_open)

    def _scan_duplicates(self):
        folder = Path(self.out_dir.get())
        if not folder.is_dir():
            self.dupe_info.set('Download folder does not exist yet.')
            return
        self.dupe_info.set('Scanning…')
        self._dupe_gen += 1
        gen = self._dupe_gen
        threading.Thread(target=self._scan_duplicates_thread, args=(folder, gen), daemon=True).start()

    def _scan_duplicates_thread(self, folder, gen):
        from collections import defaultdict
        by_size = defaultdict(list)
        try:
            for p in folder.iterdir():
                if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                    try:
                        by_size[p.stat().st_size].append(p)
                    except OSError:
                        pass
        except OSError:
            self.out_queue.put(('duplicates', gen, [], None))
            return
        groups = []
        for size, paths in by_size.items():
            if len(paths) < 2:
                continue
            by_hash = defaultdict(list)
            for p in paths:
                if gen != self._dupe_gen:
                    return
                sig = _dupe_hash(p)
                if sig:
                    by_hash[sig].append(p)
            for ps in by_hash.values():
                if len(ps) > 1:
                    groups.append(sorted(ps, key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True))
        groups.sort(key=lambda g: g[0].stat().st_size if g and g[0].exists() else 0, reverse=True)
        self.out_queue.put(('duplicates', gen, groups, None))

    def _handle_duplicates(self, gen, groups):
        if gen != self._dupe_gen:
            return
        self.dupe_tree._checked.clear()
        for iid in self.dupe_tree.get_children():
            self.dupe_tree.delete(iid)
        self._dupe_paths = {}
        total_files, wasted = 0, 0
        for gi, grp in enumerate(groups):
            try:
                size = grp[0].stat().st_size
            except OSError:
                size = 0
            self.dupe_tree.insert('', 'end', iid=f'g{gi}', open=True,
                                  text=f'Group {gi + 1}  ·  {len(grp)} copies', values=('', _fmt_bytes(size)))
            for fi, p in enumerate(grp):
                iid = f'g{gi}f{fi}'
                self._dupe_paths[iid] = p
                tag = '  (newest — kept)' if fi == 0 else ''
                self.dupe_tree.insert(f'g{gi}', 'end', iid=iid, text=p.name + tag,
                                      values=(CHK_OFF, _human_size(p)))
                total_files += 1
            wasted += size * (len(grp) - 1)
        if groups:
            self.dupe_info.set(f'{len(groups)} group(s) · {total_files} files · ~{_fmt_bytes(wasted)} reclaimable')
        else:
            self.dupe_info.set('No duplicates found.')

    def _dupe_tick_extras(self):
        """Tick every copy except the newest in each group (the kept one)."""
        for iid, p in self._dupe_paths.items():
            self._set_check(self.dupe_tree, iid, not iid.endswith('f0'))

    def _delete_duplicates(self):
        rows = [i for i in self._dupe_paths if i in getattr(self.dupe_tree, '_checked', ())]
        if not rows:
            messagebox.showinfo('Nothing ticked', 'Tick the duplicate copies you want to delete '
                                                   '(or use “Tick all but newest”).')
            return
        if not messagebox.askyesno('Delete files', f'Permanently delete {len(rows)} file(s) from disk?'):
            return
        deleted = 0
        for iid in rows:
            p = self._dupe_paths.get(iid)
            if p and p.exists():
                try:
                    p.unlink()
                    deleted += 1
                except OSError:
                    pass
        self.status_var.set(f'Deleted {deleted} duplicate file(s).')
        self._scan_duplicates()

    def _dupe_open(self, event):
        iid = self.dupe_tree.identify_row(event.y)
        p = self._dupe_paths.get(iid)
        if p and p.exists():
            self._open_file(p)

    # ════════════════════════════════════════════════════════════════
    #  Bookmarks tab
    # ════════════════════════════════════════════════════════════════
    def _build_bookmarks_tab(self, parent):
        pad = {'padx': 12, 'pady': 6}

        head = ttk.Frame(parent)
        head.pack(fill='x', **pad)
        ttk.Label(head, text='Import browser bookmarks', style='Header.TLabel').pack(anchor='w')
        ttk.Label(head, text='Reads Firefox + Chrome/Edge/Brave bookmarks live and keeps only the ones '
                             'matching a site in websites.json.', style='Sub.TLabel').pack(anchor='w')

        src = ttk.Frame(parent)
        src.pack(fill='x', **pad)
        ttk.Button(src, text='🦊 Load Firefox',
                   command=lambda: self._load_bookmarks('firefox')).pack(side='left')
        ttk.Button(src, text='🌐 Load Chrome/Edge/Brave',
                   command=lambda: self._load_bookmarks('chromium')).pack(side='left', padx=6)
        ttk.Button(src, text='📚 Load all', style='Accent.TButton',
                   command=lambda: self._load_bookmarks('all')).pack(side='left')
        self.bm_count_var = tk.StringVar(value='')
        ttk.Label(src, textvariable=self.bm_count_var, style='Count.TLabel').pack(side='right')

        filt = ttk.Frame(parent)
        filt.pack(fill='x', **pad)
        ttk.Label(filt, text='Filter:').pack(side='left')
        self.bm_filter_var = tk.StringVar()
        self.bm_filter_var.trace_add('write', lambda *_: self._refilter_bookmarks())
        ttk.Entry(filt, textvariable=self.bm_filter_var).pack(side='left', fill='x', expand=True, padx=6)
        ttk.Button(filt, text='⤴ Add to top',
                   command=lambda: self._add_bookmarks_to_queue(at_top=True)).pack(side='left')
        ttk.Button(filt, text='⤵ Add to bottom', style='Accent.TButton',
                   command=lambda: self._add_bookmarks_to_queue(at_top=False)).pack(side='left', padx=6)

        list_panel = ttk.LabelFrame(parent, text='Matching bookmarks  ·  tick rows (or add all when none ticked)')
        list_panel.pack(fill='both', expand=True, **pad)
        list_inner = ttk.Frame(list_panel)
        list_inner.pack(fill='both', expand=True, padx=8, pady=8)

        self.bm_tree = ttk.Treeview(list_inner, columns=('chk', 'site', 'url'),
                                    show='tree headings', selectmode='extended')
        self.bm_tree.heading('#0', text='Title')
        self.bm_tree.heading('site', text='Site')
        self.bm_tree.heading('url', text='URL')
        self.bm_tree.column('#0', width=300, stretch=True)
        self.bm_tree.column('chk', width=34, anchor='center', stretch=False)
        self.bm_tree.column('site', width=110, stretch=False)
        self.bm_tree.column('url', width=340, stretch=True)
        bm_scroll = ttk.Scrollbar(list_inner, command=self.bm_tree.yview)
        self.bm_tree.configure(yscrollcommand=bm_scroll.set)
        self.bm_tree.pack(side='left', fill='both', expand=True)
        bm_scroll.pack(side='right', fill='y')
        self._setup_checktree(self.bm_tree)

    def _load_bookmarks(self, source):
        self.bm_count_var.set('Reading bookmarks…')
        self.status_var.set('Reading browser bookmarks…')
        threading.Thread(target=self._read_bookmarks_thread, args=(source,), daemon=True).start()

    def _read_bookmarks_thread(self, source):
        matchers = _build_site_matchers(self.sites_raw)
        raw = []
        if source in ('firefox', 'all'):
            for _label, path in _firefox_places_files():
                raw.extend(_read_firefox_bookmarks(path))
        if source in ('chromium', 'all'):
            for _label, path in _chromium_bookmark_files():
                raw.extend(_read_chromium_bookmarks(path))

        results, seen = [], set()
        for title, url in raw:
            if url in seen:
                continue
            site = _match_host(_host_of(url), matchers)
            if not site:
                continue
            seen.add(url)
            results.append({'site': site, 'title': title or url, 'url': url})
        results.sort(key=lambda r: (r['site'].lower(), r['title'].lower()))
        self.out_queue.put(('bookmarks', None, results, None))

    def _populate_bookmarks(self, results):
        self._all_bookmarks = results
        self._refilter_bookmarks()
        self.status_var.set(f'Found {len(results)} bookmark(s) matching websites.json.')

    def _refilter_bookmarks(self):
        needle = self.bm_filter_var.get().strip().lower()
        self.bm_tree._checked.clear()
        for iid in self.bm_tree.get_children():
            self.bm_tree.delete(iid)
        shown = 0
        for i, bm in enumerate(self._all_bookmarks):
            if needle and needle not in bm['title'].lower() \
                    and needle not in bm['url'].lower() and needle not in bm['site'].lower():
                continue
            self.bm_tree.insert('', 'end', iid=f'bm{i}', text=bm['title'],
                                values=(CHK_OFF, bm['site'], bm['url']))
            shown += 1
        total = len(self._all_bookmarks)
        self.bm_count_var.set(f'{shown} shown / {total} matched' if total else 'No bookmarks loaded')

    def _add_bookmarks_to_queue(self, at_top=False):
        rows = self._targets(self.bm_tree, fallback_all=True)
        urls = [self.bm_tree.set(iid, 'url') for iid in rows]
        urls = [u for u in urls if u]
        if not urls:
            messagebox.showinfo('Nothing to add', 'Load and tick some bookmarks first.')
            return
        added = self._queue_urls(urls, at_top=at_top)
        where = 'top' if at_top else 'bottom'
        if added:
            self.status_var.set(f'Added {added} bookmark(s) to the {where} of the queue.')
            self._pump()
            self.nb.select(self.tab_downloads)
        else:
            messagebox.showinfo('Already queued', 'Those bookmarks are already in the queue.')

    # ════════════════════════════════════════════════════════════════
    #  Search tab
    # ════════════════════════════════════════════════════════════════
    def _build_search_tab(self, parent):
        pad = {'padx': 12, 'pady': 6}

        head = ttk.Frame(parent)
        head.pack(fill='x', **pad)
        ttk.Label(head, text='Search sites in your browser', style='Header.TLabel').pack(anchor='w')
        ttk.Label(head, text='Type a query, then double-click a site to open its search — or open every '
                             '★ favourite at once.', style='Sub.TLabel').pack(anchor='w')

        bar = ttk.Frame(parent)
        bar.pack(fill='x', **pad)
        ttk.Label(bar, text='Query:').pack(side='left')
        self.search_query = tk.StringVar()
        q_entry = ttk.Entry(bar, textvariable=self.search_query)
        q_entry.pack(side='left', fill='x', expand=True, padx=6)
        q_entry.bind('<Return>', lambda e: self._open_all_favourites())
        ttk.Button(bar, text='⭐ Open all favourites', style='Accent.TButton',
                   command=self._open_all_favourites).pack(side='left')
        ttk.Button(bar, text='🎲 Random site', command=self._open_random_search).pack(side='left', padx=6)

        sub = ttk.Frame(parent)
        sub.pack(fill='x', padx=12)
        ttk.Label(sub, text='Click the ★ to favourite a site · double-click a row (or tick + button) to search.',
                  style='Sub.TLabel').pack(side='left')
        ttk.Button(sub, text='↗ Search ticked', command=self._open_ticked_search).pack(side='right')

        list_panel = ttk.LabelFrame(parent, text='Sites with search')
        list_panel.pack(fill='both', expand=True, **pad)
        list_inner = ttk.Frame(list_panel)
        list_inner.pack(fill='both', expand=True, padx=8, pady=8)

        self.search_tree = ttk.Treeview(list_inner, columns=('chk', 'fav', 'url'),
                                        show='tree headings', selectmode='extended')
        self.search_tree.heading('#0', text='Website')
        self.search_tree.heading('fav', text='★')
        self.search_tree.heading('url', text='Search URL')
        self.search_tree.column('#0', width=200, stretch=False)
        self.search_tree.column('chk', width=34, anchor='center', stretch=False)
        self.search_tree.column('fav', width=40, anchor='center', stretch=False)
        self.search_tree.column('url', width=480, stretch=True)
        s_scroll = ttk.Scrollbar(list_inner, command=self.search_tree.yview)
        self.search_tree.configure(yscrollcommand=s_scroll.set)
        self.search_tree.pack(side='left', fill='both', expand=True)
        s_scroll.pack(side='right', fill='y')
        self.search_tree.tag_configure('fav', foreground=GOLD)
        self._setup_checktree(self.search_tree)
        self.search_tree.bind('<Button-1>', self._on_search_click, add='+')
        self.search_tree.bind('<Double-1>', self._on_search_double)

        self._populate_search_sites()

    def _site_label(self, s):
        return s.get('name') or _host_of(s.get('url') or '') or s.get('searchURL') or ''

    def _populate_search_sites(self):
        self.search_tree._checked.clear()
        for iid in self.search_tree.get_children():
            self.search_tree.delete(iid)
        # Favourites first, then alphabetical by name. iids keep the original index.
        searchable = [i for i, s in enumerate(self.sites_raw) if (s.get('searchURL') or '').strip()]
        searchable.sort(key=lambda i: (not bool(self.sites_raw[i].get('favourite')),
                                       self._site_label(self.sites_raw[i]).lower()))
        for idx in searchable:
            s = self.sites_raw[idx]
            fav = bool(s.get('favourite'))
            self.search_tree.insert('', 'end', iid=f'site{idx}',
                                    text=self._site_label(s),
                                    values=(CHK_OFF, '★' if fav else '☆', s.get('searchURL') or ''),
                                    tags=('fav',) if fav else ())

    def _open_random_search(self):
        searchable = [s for s in self.sites_raw if (s.get('searchURL') or '').strip()]
        if not searchable:
            messagebox.showinfo('No sites', 'No searchable sites in websites.json.')
            return
        s = random.choice(searchable)
        q = self.search_query.get().strip()
        full = s['searchURL'].strip() + urllib.parse.quote(q) if q else (s.get('url') or s['searchURL'])
        webbrowser.open(full, new=2)
        self.status_var.set(f'🎲 Opened random site: {self._site_label(s)}')

    def _on_search_click(self, event):
        if self.search_tree.identify_region(event.x, event.y) != 'cell':
            return None
        if self.search_tree.identify_column(event.x) != '#2':   # the ★ column
            return None
        iid = self.search_tree.identify_row(event.y)
        if iid:
            self._toggle_favourite(iid)
            return 'break'
        return None

    def _toggle_favourite(self, iid):
        try:
            idx = int(iid[4:])
        except ValueError:
            return
        s = self.sites_raw[idx]
        s['favourite'] = not bool(s.get('favourite'))
        fav = s['favourite']
        saved = _save_websites_raw(self.sites_raw)
        self._populate_search_sites()   # re-sort so favourites stay on top
        self.search_tree.see(iid)
        if saved:
            n = sum(1 for x in self.sites_raw if x.get('favourite'))
            self.status_var.set(f'{"★ Favourited" if fav else "☆ Unfavourited"} {s.get("name")}  ·  {n} favourite(s).')
        else:
            self.status_var.set('Could not write websites.json (read-only?).')

    def _on_search_double(self, event):
        iid = self.search_tree.identify_row(event.y)
        if iid:
            self._open_site_search(iid)

    def _open_ticked_search(self):
        rows = self._targets(self.search_tree)
        if not rows:
            messagebox.showinfo('No site selected', 'Tick or select one or more sites first.')
            return
        for iid in rows:
            self._open_site_search(iid)

    def _open_site_search(self, iid):
        try:
            idx = int(iid[4:])
        except ValueError:
            return
        s = self.sites_raw[idx]
        search_url = (s.get('searchURL') or '').strip()
        if not search_url:
            return
        q = self.search_query.get().strip()
        full = search_url + urllib.parse.quote(q) if q else (s.get('url') or search_url)
        webbrowser.open(full, new=2)
        self.status_var.set(f'Opened {s.get("name")} search in browser.')

    def _open_all_favourites(self):
        q = self.search_query.get().strip()
        favs = [s for s in self.sites_raw if s.get('favourite') and (s.get('searchURL') or '').strip()]
        if not favs:
            messagebox.showinfo('No favourites', 'Click the ★ next to some sites to favourite them first.')
            return
        if not q:
            messagebox.showinfo('Enter a query', 'Type something to search for.')
            return
        if len(favs) > 8 and not messagebox.askyesno(
                'Open many tabs', f'This will open {len(favs)} browser tabs. Continue?'):
            return
        for s in favs:
            webbrowser.open(s['searchURL'].strip() + urllib.parse.quote(q), new=2)
        self.status_var.set(f'Opened {len(favs)} favourite search tab(s) for “{q}”.')

    # ════════════════════════════════════════════════════════════════
    #  Gallery tab
    # ════════════════════════════════════════════════════════════════
    def _build_cat_terms(self):
        """category label -> list of token-tuples (its name, displayName and tags).
        A video matches the category if ANY of these token-tuples is fully present
        in the filename's word set."""
        out = {}
        for cat, info in (self.categories_map or {}).items():
            if isinstance(info, dict):
                label = info.get('displayName') or cat
                terms = [cat, label] + (info.get('tags') if isinstance(info.get('tags'), list) else [])
            else:
                label, terms = cat, [cat]
            toks, seen = [], set()
            for t in terms:
                if isinstance(t, str) and t.strip():
                    tt = tuple(re.sub(r'[^a-z0-9]+', ' ', t.lower()).split())
                    if tt and tt not in seen:
                        seen.add(tt)
                        toks.append(tt)
            if toks:
                out[label] = toks
        return out

    def _build_gallery_tab(self, parent):
        pad = {'padx': 12, 'pady': 6}

        bar = ttk.Frame(parent)
        bar.pack(fill='x', **pad)
        ttk.Button(bar, text='🔄 Refresh', style='Accent.TButton',
                   command=self._refresh_gallery).pack(side='left')
        ttk.Button(bar, text='📂 Open folder',
                   command=lambda: self._open_path(Path(self.out_dir.get()))).pack(side='left', padx=6)
        self.gallery_info = tk.StringVar(value='Press Refresh to scan the download folder.')
        ttk.Label(bar, textvariable=self.gallery_info, style='Count.TLabel').pack(side='right')

        content = ttk.Frame(parent)
        content.pack(fill='both', expand=True, padx=12, pady=(0, 8))

        # Left: categories ranked by how many videos match (by name or a related tag).
        cats = ttk.LabelFrame(content, text='Categories  ·  by video count')
        cats.pack(side='left', fill='y', padx=(0, 8))
        cats_inner = ttk.Frame(cats)
        cats_inner.pack(fill='both', expand=True, padx=4, pady=4)
        self.gallery_cats = ttk.Treeview(cats_inner, columns=('n',), show='tree headings',
                                         selectmode='browse', height=18)
        self.gallery_cats.heading('#0', text='Category')
        self.gallery_cats.heading('n', text='#')
        self.gallery_cats.column('#0', width=160, stretch=True)
        self.gallery_cats.column('n', width=46, anchor='e', stretch=False)
        cscroll = ttk.Scrollbar(cats_inner, command=self.gallery_cats.yview)
        self.gallery_cats.configure(yscrollcommand=cscroll.set)
        self.gallery_cats.pack(side='left', fill='both', expand=True)
        cscroll.pack(side='right', fill='y')
        self.gallery_cats.bind('<<TreeviewSelect>>', self._on_gallery_cat_select)

        # Right: the thumbnail grid.
        wrap = ttk.Frame(content)
        wrap.pack(side='left', fill='both', expand=True)
        self.gallery_canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        g_scroll = ttk.Scrollbar(wrap, orient='vertical', command=self.gallery_canvas.yview)
        self.gallery_canvas.configure(yscrollcommand=g_scroll.set)
        self.gallery_canvas.pack(side='left', fill='both', expand=True)
        g_scroll.pack(side='right', fill='y')

        self.gallery_inner = ttk.Frame(self.gallery_canvas)
        self._gallery_window = self.gallery_canvas.create_window((0, 0), window=self.gallery_inner, anchor='nw')
        self.gallery_inner.bind('<Configure>',
                                lambda e: self.gallery_canvas.configure(scrollregion=self.gallery_canvas.bbox('all')))
        self.gallery_canvas.bind('<Configure>', self._on_gallery_configure)
        # Scope the mousewheel to when the pointer is actually over the gallery.
        self.gallery_canvas.bind('<Enter>', lambda e: self._gallery_wheel_bind(True))
        self.gallery_canvas.bind('<Leave>', lambda e: self._gallery_wheel_bind(False))

    def _gallery_wheel_bind(self, on):
        events = ('<MouseWheel>', '<Button-4>', '<Button-5>')
        for ev in events:
            if on:
                self.gallery_canvas.bind_all(ev, self._on_gallery_wheel)
            else:
                self.gallery_canvas.unbind_all(ev)

    def _on_gallery_configure(self, event):
        self.gallery_canvas.itemconfigure(self._gallery_window, width=event.width)
        self._gallery_reflow(event.width)

    def _on_gallery_wheel(self, event):
        if getattr(event, 'num', None) == 4:
            delta = 1
        elif getattr(event, 'num', None) == 5:
            delta = -1
        else:
            delta = int(event.delta / 120) if event.delta else 0
        self.gallery_canvas.yview_scroll(-delta, 'units')

    def _gallery_reflow(self, width=None):
        if width is None:
            width = self.gallery_canvas.winfo_width()
        cols = max(1, width // CARD_W)
        if cols == self._gallery_cols and self._gallery_cards:
            return
        self._gallery_cols = cols
        for i, card in enumerate(self._gallery_cards):
            card.grid(row=i // cols, column=i % cols, padx=8, pady=8, sticky='n')

    def _refresh_gallery(self):
        """Re-scan the download folder, rebuild the category list, render the grid."""
        folder = Path(self.out_dir.get())
        if not folder.is_dir():
            self._gallery_files = []
            self._gallery_truncated = False
            self._gallery_tag_filter = None
            self._build_gallery_categories()
            self._render_gallery_grid([])
            self.gallery_info.set('Download folder does not exist yet.')
            return
        try:
            files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
        except OSError as e:
            self.gallery_info.set(f'Cannot read folder: {e}')
            return
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        self._gallery_truncated = len(files) > GALLERY_MAX
        self._gallery_files = files[:GALLERY_MAX]
        self._gallery_tag_filter = None
        self._build_gallery_categories()
        self._render_gallery_grid(self._gallery_files)

    def _build_gallery_categories(self):
        """List categories ordered by how many videos match (name or related tag).
        A video may match — and so be counted under — several categories."""
        if not hasattr(self, 'gallery_cats'):
            return
        for iid in self.gallery_cats.get_children():
            self.gallery_cats.delete(iid)
        files = self._gallery_files
        self._gallery_selecting = True
        self.gallery_cats.insert('', 'end', iid='all', text='All videos', values=(len(files),))
        if files and self._gallery_cat_terms:
            file_tokens = [_title_tokens(p.name) for p in files]
            counts = []
            for label, terms in self._gallery_cat_terms.items():
                c = sum(1 for ft in file_tokens
                        if any(all(w in ft for w in term) for term in terms))
                if c:
                    counts.append((label, c))
            counts.sort(key=lambda kv: (-kv[1], kv[0].lower()))
            for label, c in counts:
                self.gallery_cats.insert('', 'end', iid='cat:' + label, text=label, values=(c,))
        self.gallery_cats.selection_set('all')
        self._gallery_selecting = False

    def _on_gallery_cat_select(self, event=None):
        if getattr(self, '_gallery_selecting', False):
            return
        sel = self.gallery_cats.selection()
        if not sel:
            return
        iid = sel[0]
        if iid == 'all':
            self._gallery_tag_filter = None
            self._render_gallery_grid(self._gallery_files)
        elif iid.startswith('cat:'):
            label = iid[4:]
            terms = self._gallery_cat_terms.get(label, [])
            files = [p for p in self._gallery_files
                     if any(all(w in _title_tokens(p.name) for w in term) for term in terms)]
            self._gallery_tag_filter = label
            self._render_gallery_grid(files)

    def _render_gallery_grid(self, files):
        self._gallery_gen += 1
        gen = self._gallery_gen
        for child in self.gallery_inner.winfo_children():
            child.destroy()
        self._gallery_cards = []
        self._gallery_thumb_labels = []
        self._gallery_imgs = []
        self._gallery_cols = 0

        ffmpeg = _find_ffmpeg()
        for path in files:
            card = ttk.Frame(self.gallery_inner, style='Card.TFrame')
            blank = tk.PhotoImage(width=THUMB_W, height=THUMB_H)
            self._gallery_imgs.append(blank)
            thumb = tk.Label(card, image=blank, bg='#e5e7eb',
                             text=('' if ffmpeg else '🎬'), width=THUMB_W, height=THUMB_H, compound='center')
            thumb.pack()
            name = path.name if len(path.name) <= 40 else path.name[:37] + '…'
            ttk.Label(card, text=name, style='CardName.TLabel', wraplength=THUMB_W).pack(fill='x', padx=4, pady=(4, 0))
            ttk.Label(card, text=_human_size(path), style='CardSub.TLabel').pack(fill='x', padx=4, pady=(0, 4))
            self._bind_open(card, path)
            self._bind_open(thumb, path)
            self._gallery_cards.append(card)
            self._gallery_thumb_labels.append(thumb)

        self._gallery_reflow()
        total = len(self._gallery_files)
        note = f'  ·  first {GALLERY_MAX}' if getattr(self, '_gallery_truncated', False) else ''
        ffnote = '' if ffmpeg else '  ·  ffmpeg not found'
        filt = f'  ·  filtered: {self._gallery_tag_filter}' if self._gallery_tag_filter else ''
        if not total:
            self.gallery_info.set('No videos in the download folder.')
        elif not files:
            self.gallery_info.set(f'No videos match{filt}.  ·  {total} total')
        else:
            self.gallery_info.set(f'{len(files)}/{total} video(s){note}{ffnote}{filt}  ·  double-click to play')

        if ffmpeg and files:
            threading.Thread(target=self._gallery_thumb_thread,
                             args=(ffmpeg, list(files), gen), daemon=True).start()

    def _gallery_thumb_thread(self, ffmpeg, files, gen):
        try:
            THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        for idx, path in enumerate(files):
            if gen != self._gallery_gen:
                return
            png = _thumb_path(path)
            if not png.exists():
                _make_thumb(ffmpeg, path, png)
            if png.exists():
                self.out_queue.put(('gthumb', gen, idx, str(png)))

    def _bind_open(self, widget, path):
        widget.bind('<Double-Button-1>', lambda e, p=path: self._open_file(p))

    # ════════════════════════════════════════════════════════════════
    #  X.com login tab
    # ════════════════════════════════════════════════════════════════
    def _build_xlogin_tab(self, parent):
        pad = {'padx': 12, 'pady': 6}

        head = ttk.Frame(parent)
        head.pack(fill='x', **pad)
        ttk.Label(head, text='X.com login', style='Header.TLabel').pack(anchor='w')
        ttk.Label(head, text='Sensitive / login-gated X.com videos need your login. Pick ONE method below.',
                  style='Sub.TLabel').pack(anchor='w')

        self.cookie_status_var = tk.StringVar(value=_cookie_status(self._config))
        ttk.Label(parent, textvariable=self.cookie_status_var, style='Status.TLabel',
                  wraplength=900).pack(anchor='w', padx=12, pady=(0, 4))

        maint = ttk.Frame(parent)
        maint.pack(fill='x', padx=12, pady=(0, 4))
        ttk.Label(maint, text='X.com downloads break when yt-dlp is outdated — update it if they fail:',
                  style='Sub.TLabel').pack(side='left')
        ttk.Button(maint, text='⬆ Update yt-dlp', command=lambda: self._update_ytdlp(False)).pack(side='left', padx=6)
        ttk.Button(maint, text='⬆ Nightly', command=lambda: self._update_ytdlp(True)).pack(side='left')

        # Method 1 — browser login (recommended)
        m1 = ttk.LabelFrame(parent, text='① Recommended: use your browser login (no copy-paste)')
        m1.pack(fill='x', **pad)
        ttk.Label(m1, text='Stay logged in to x.com in your browser; yt-dlp reads its cookies live. '
                          'Firefox is the most reliable — Chrome/Edge on Windows encrypt their cookie '
                          'store (v127+) and often fail, so close them or prefer Firefox.',
                  style='Sub.TLabel', wraplength=900, justify='left').pack(anchor='w', padx=8, pady=(6, 2))
        m1row = ttk.Frame(m1)
        m1row.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Label(m1row, text='Browser:').pack(side='left')
        self.browser_var = tk.StringVar(value=self._config.get('cookies_from_browser') or 'firefox')
        ttk.Combobox(m1row, textvariable=self.browser_var, values=BROWSER_CHOICES,
                     state='readonly', width=12).pack(side='left', padx=6)
        ttk.Button(m1row, text='✓ Use this browser login', style='Accent.TButton',
                   command=self._use_browser_login).pack(side='left')
        ttk.Button(m1row, text='Stop using browser login',
                   command=self._clear_browser_login).pack(side='left', padx=6)

        # Method 2 — paste tokens, with a step-by-step guide
        m2 = ttk.LabelFrame(parent, text='② Paste tokens')
        m2.pack(fill='x', **pad)
        guide = (
            'Step-by-step:\n'
            '  1.  Open  https://x.com  in your browser and log in.\n'
            '  2.  Press  F12  to open Developer Tools.\n'
            '  3.  Open the “Application” tab (Chrome/Edge) or “Storage” tab (Firefox).\n'
            '  4.  In the left sidebar expand  Cookies  →  click  https://x.com.\n'
            '  5.  Find the row named  auth_token  →  copy its Value into the field below.\n'
            '  6.  Find the row named  ct0  →  copy its Value into the field below.\n'
            '  7.  Click  “Save tokens”.'
        )
        ttk.Label(m2, text=guide, style='Guide.TLabel', justify='left').pack(anchor='w', padx=8, pady=(6, 4))
        tok = ttk.Frame(m2)
        tok.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Label(tok, text='auth_token:').grid(row=0, column=0, sticky='w', pady=3)
        self.auth_token_var = tk.StringVar()
        ttk.Entry(tok, textvariable=self.auth_token_var).grid(row=0, column=1, sticky='we', padx=6, pady=3)
        ttk.Label(tok, text='ct0:').grid(row=1, column=0, sticky='w', pady=3)
        self.ct0_var = tk.StringVar()
        ttk.Entry(tok, textvariable=self.ct0_var).grid(row=1, column=1, sticky='we', padx=6, pady=3)
        tok.columnconfigure(1, weight=1)
        ttk.Button(tok, text='💾 Save tokens', command=self._save_tokens).grid(row=2, column=1, sticky='e', pady=(4, 0))

        # Method 3 — cookies.txt file
        m3 = ttk.LabelFrame(parent, text='③ Import a cookies.txt')
        m3.pack(fill='both', expand=True, **pad)
        row = ttk.Frame(m3)
        row.pack(fill='x', padx=8, pady=(8, 4))
        ttk.Button(row, text='📄 Import cookies.txt…', command=self._import_cookies_file).pack(side='left')
        ttk.Button(row, text='🔎 Auto-detect', command=self._autodetect_cookies_action).pack(side='left', padx=6)
        ttk.Button(row, text='🗑 Clear cookies', style='Stop.TButton', command=self._clear_cookies).pack(side='left')
        raw_inner = ttk.Frame(m3)
        raw_inner.pack(fill='both', expand=True, padx=8, pady=4)
        ttk.Label(raw_inner, text='…or paste a raw Netscape cookies.txt:', style='Sub.TLabel').pack(anchor='w')
        self.raw_cookies_text = tk.Text(raw_inner, height=4, wrap='none', font=FONT_MONO,
                                        relief='flat', borderwidth=1, highlightthickness=1,
                                        highlightbackground=BORDER, highlightcolor=ACCENT)
        self.raw_cookies_text.pack(fill='both', expand=True, pady=(2, 4))
        ttk.Button(m3, text='💾 Save pasted cookies',
                   command=self._save_raw_cookies).pack(anchor='e', padx=8, pady=(0, 8))

    def _refresh_cookie_status(self):
        self.cookie_status_var.set(_cookie_status(self._config))

    def _use_browser_login(self):
        browser = self.browser_var.get().strip().lower()
        if browser not in BROWSER_CHOICES:
            return
        self._config['cookies_from_browser'] = browser
        self._save_config()
        self._refresh_cookie_status()
        self.status_var.set(f'Using {browser.title()} browser login for X.com.')
        messagebox.showinfo('Browser login set',
                            f'X.com downloads will use your {browser.title()} login.\n\n'
                            f'Make sure you are signed in to x.com in {browser.title()}, '
                            f'and that {browser.title()} is closed if it locks its cookie DB '
                            '(mainly Chrome/Edge on Windows).')

    def _clear_browser_login(self):
        self._config['cookies_from_browser'] = ''
        self._save_config()
        self._refresh_cookie_status()
        self.status_var.set('Browser login disabled.')

    def _import_cookies_file(self):
        path = filedialog.askopenfilename(
            title='Select X.com cookies.txt (Netscape format)',
            filetypes=[('Cookies file', '*.txt'), ('All files', '*.*')])
        if not path:
            return
        try:
            shutil.copyfile(path, COOKIES_FILE)
        except OSError as e:
            messagebox.showerror('Could not save cookies', str(e))
            return
        self._clear_browser_login_silent()
        self._refresh_cookie_status()
        self.status_var.set('X.com cookies imported.')

    def _clear_browser_login_silent(self):
        if self._config.get('cookies_from_browser'):
            self._config['cookies_from_browser'] = ''
            self._save_config()

    def _autodetect_cookies_action(self):
        self.status_var.set('Searching for cookies and installed browsers…')
        threading.Thread(target=self._autodetect_thread, daemon=True).start()

    def _autodetect_thread(self):
        # 1) an exported cookies.txt anywhere common → use it directly
        found = _autodetect_cookies()
        if found:
            self.out_queue.put(('autodetect', None, ('file', str(found)), None))
            return
        # 2) otherwise detect an installed browser (all OSes) and use its live login
        browsers = _detect_installed_browsers()
        if browsers:
            pref = 'firefox' if 'firefox' in browsers else browsers[0]
            self.out_queue.put(('autodetect', None, ('browser', pref, browsers), None))
            return
        self.out_queue.put(('autodetect', None, None, None))

    def _clear_cookies(self):
        existed = COOKIES_FILE.exists()
        if existed:
            if not messagebox.askyesno('Clear cookies', 'Delete the saved X.com cookies?'):
                return
            try:
                COOKIES_FILE.unlink()
            except OSError as e:
                messagebox.showerror('Could not delete', str(e))
                return
        self._refresh_cookie_status()
        self.status_var.set('X.com cookies cleared.')

    def _save_tokens(self):
        auth = self.auth_token_var.get().strip()
        ct0 = self.ct0_var.get().strip()
        if not auth:
            messagebox.showinfo('Missing token', 'Paste at least the auth_token value.')
            return
        try:
            _write_x_cookies_from_tokens(auth, ct0)
        except OSError as e:
            messagebox.showerror('Could not save', str(e))
            return
        self._clear_browser_login_silent()
        self._refresh_cookie_status()
        self.status_var.set('X.com cookies built from tokens.')
        messagebox.showinfo('Saved', 'Login cookies built from your tokens — used automatically.')

    def _save_raw_cookies(self):
        text = self.raw_cookies_text.get('1.0', 'end').strip()
        if not text:
            messagebox.showinfo('Empty', 'Paste a Netscape cookies.txt first.')
            return
        if not text.startswith('# Netscape'):
            text = '# Netscape HTTP Cookie File\n' + text
        try:
            COOKIES_FILE.write_text(text + '\n', encoding='utf-8')
        except OSError as e:
            messagebox.showerror('Could not save', str(e))
            return
        self._clear_browser_login_silent()
        self._refresh_cookie_status()
        self.status_var.set('Pasted cookies saved.')

    def _update_ytdlp(self, nightly=False):
        """pip-update yt-dlp (the engine behind every download). Outdated yt-dlp is
        the #1 reason X.com / tube downloads suddenly stop working."""
        if not self._console_open:
            self._toggle_console()
        self.status_var.set('Updating yt-dlp… (see console)')
        threading.Thread(target=self._update_ytdlp_thread, args=(nightly,), daemon=True).start()

    def _update_ytdlp_thread(self, nightly):
        cmd = [_python_bin(), '-m', 'pip', 'install', '-U']
        cmd += ['--pre', 'yt-dlp[default]'] if nightly else ['yt-dlp']
        self.out_queue.put(('console', None, f'[pip] {" ".join(cmd)}', None))
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding='utf-8', errors='replace', **_subprocess_flags())
            for line in _read_stream(proc.stdout):
                line = line.strip()
                if line:
                    self.out_queue.put(('console', None, f'[pip] {line}', None))
            code = proc.wait()
        except OSError as e:
            self.out_queue.put(('console', None, f'[pip] error: {e}', None))
            self.out_queue.put(('status_msg', None, f'yt-dlp update failed: {e}', None))
            return
        self.out_queue.put(('console', None, f'[pip] finished (exit {code})', None))
        self.out_queue.put(('status_msg', None,
                            'yt-dlp updated — try the download again.' if code == 0
                            else 'yt-dlp update failed — see console.', None))

    # ── UI message pump ───────────────────────────────────────────────
    def _poll_queue(self):
        try:
            while True:
                kind, iid, a, b = self.out_queue.get_nowait()
                if kind == 'done':
                    self._handle_done(iid, a, b)
                elif kind == 'progress':
                    if iid in self.active:
                        self._activity[iid] = time.monotonic()
                    if iid in self.items:
                        sp, eta = b
                        self._set_item(iid, status=ST_DOWNLOADING, pct=a, speed=sp, eta=eta)
                elif kind == 'console':
                    if iid in self.active:
                        self._activity[iid] = time.monotonic()
                    self._console_log(a)
                elif kind == 'title':
                    if iid in self.items:
                        self._set_item(iid, title=a)
                elif kind == 'bookmarks':
                    self._populate_bookmarks(a)
                elif kind == 'gthumb':
                    self._apply_gallery_thumb(iid, a, b)
                elif kind == 'duplicates':
                    self._handle_duplicates(iid, a)
                elif kind == 'autodetect':
                    self._handle_autodetect(a)
                elif kind == 'status_msg':
                    self.status_var.set(a)
        except queue.Empty:
            pass
        self._check_timeouts()
        # Safety net: keep the scheduler fed, and auto-start the top of the queue.
        if self.is_running and not self.paused:
            if not self.active or (len(self.active) < self._parallel() and self._next_pending()):
                self._pump()
        elif not self.is_running and self.autostart_var.get() and self._next_pending():
            self._autostart()
        self.after(100, self._poll_queue)

    def _handle_done(self, iid, code, payload):
        result_file, err = payload if isinstance(payload, tuple) else (payload, '')
        timed_out = iid in self._timeouts
        cancelled = iid in self._cancelling and not timed_out
        self._cancelling.discard(iid)
        self._timeouts.discard(iid)
        self.active.pop(iid, None)
        self._activity.pop(iid, None)
        if iid not in self.items:            # row was removed mid-download
            self._pump()
            return
        url = self.items[iid]['url']
        if cancelled:
            self._set_item(iid, status=ST_STOPPED, speed='', eta='')
            self._console_log(f'⏸ stopped {url}')
        elif code == 0 and result_file:
            self._set_item(iid, status=ST_DONE, file=result_file, pct=100, speed='', eta='')
            self._mark_downloaded(url)
            self._console_log(f'✓ done    {os.path.basename(result_file)}')
        else:
            if timed_out:
                reason = self.items[iid].get('error') or 'timed out — no output'
            else:
                reason = err or 'no downloadable video found'
            self._set_item(iid, status=ST_ERROR, error=reason, speed='', eta='')
            _append_link(LINKS_FAILED, url)
            self._console_log(f'✗ {"timeout" if timed_out else "error"}   {url}  — {reason}')
        self._rebuild_to_download_file()
        self._update_overall()
        self._refresh_errored()
        self._pump()

    def _handle_autodetect(self, result):
        if not result:
            messagebox.showinfo('Nothing found',
                                'No cookies.txt and no supported browser profile found.\n\n'
                                'Log in to x.com in Chrome/Firefox/Edge/Brave, then use method ① above.')
            self.status_var.set('Auto-detect found nothing.')
            return
        if result[0] == 'file':
            path = result[1]
            try:
                shutil.copyfile(path, COOKIES_FILE)
            except OSError as e:
                messagebox.showerror('Could not save cookies', str(e))
                return
            self._clear_browser_login_silent()
            self._refresh_cookie_status()
            self.status_var.set(f'Auto-detected cookies from {path}')
            messagebox.showinfo('Cookies found', f'Imported cookies from:\n{path}')
        elif result[0] == 'browser':
            browser = result[1]
            others = result[2] if len(result) > 2 else [browser]
            self.browser_var.set(browser)
            self._config['cookies_from_browser'] = browser
            self._save_config()
            self._refresh_cookie_status()
            self.status_var.set(f'Using {browser.title()} browser login (auto-detected).')
            messagebox.showinfo('Browser login enabled',
                                'No cookies.txt found, but these browsers are installed:\n'
                                f'  {", ".join(b.title() for b in others)}\n\n'
                                f'Now using your {browser.title()} login automatically — make sure '
                                f'you are signed in to x.com in {browser.title()}.')

    def _apply_gallery_thumb(self, gen, idx, png):
        if gen != self._gallery_gen or not (0 <= idx < len(self._gallery_thumb_labels)):
            return
        try:
            img = tk.PhotoImage(file=png)
        except tk.TclError:
            return
        self._gallery_imgs.append(img)
        self._gallery_thumb_labels[idx].configure(image=img, text='')

    def _mark_downloaded(self, url):
        _remove_link(LINKS_TO_DOWNLOAD, url)
        _remove_link(LINKS_FAILED, url)
        _append_link(LINKS_DOWNLOADED, url, cap=DOWNLOADED_FILE_CAP)

    # ── console drawer ────────────────────────────────────────────────
    def _build_console_drawer(self, body):
        self._console_open = False
        self.console_drawer = ttk.Frame(body, width=440)
        hdr = ttk.Frame(self.console_drawer)
        hdr.pack(fill='x', pady=(0, 4))
        ttk.Label(hdr, text='🖥 Console', style='Header.TLabel').pack(side='left')
        ttk.Button(hdr, text='✕', width=3, command=self._toggle_console).pack(side='right')
        ttk.Button(hdr, text='Clear', command=self._clear_console).pack(side='right', padx=4)
        cwrap = ttk.Frame(self.console_drawer)
        cwrap.pack(fill='both', expand=True)
        self.console_text = tk.Text(cwrap, bg=LOG_BG, fg=LOG_FG, insertbackground=LOG_FG,
                                    font=FONT_MONO, wrap='none', relief='flat', borderwidth=0,
                                    state='disabled', width=54)
        csb = ttk.Scrollbar(cwrap, command=self.console_text.yview)
        self.console_text.configure(yscrollcommand=csb.set)
        self.console_text.pack(side='left', fill='both', expand=True)
        csb.pack(side='right', fill='y')

    def _toggle_console(self):
        if self._console_open:
            self.console_drawer.pack_forget()
            self._console_open = False
            self.console_btn.configure(text='🖥 Console ▸')
        else:
            self.console_drawer.pack(side='right', fill='y', padx=(6, 0))
            self.console_drawer.pack_propagate(False)
            self._console_open = True
            self.console_btn.configure(text='🖥 Console ◂')
        self._save_config()

    def _clear_console(self):
        self.console_text.configure(state='normal')
        self.console_text.delete('1.0', 'end')
        self.console_text.configure(state='disabled')

    def _console_log(self, line):
        if not hasattr(self, 'console_text'):
            return
        self.console_text.configure(state='normal')
        self.console_text.insert('end', line + '\n')
        try:
            last = int(self.console_text.index('end-1c').split('.')[0])
            if last > 2500:
                self.console_text.delete('1.0', f'{last - 2000}.0')
        except (ValueError, tk.TclError):
            pass
        self.console_text.see('end')
        self.console_text.configure(state='disabled')

    def _on_tab_changed(self, event=None):
        sel = self.nb.select()
        if sel == str(self.tab_gallery) and not self._gallery_cards:
            self._refresh_gallery()
        elif sel == str(self.tab_errored):
            self._refresh_errored()

    # ── config persistence ────────────────────────────────────────────
    def _save_config(self):
        self._config['out_dir'] = self.out_dir.get()
        self._config['max_parallel'] = self._parallel()
        self._config['start_timeout'] = self._start_timeout()
        if hasattr(self, 'autostart_var'):
            self._config['autostart'] = bool(self.autostart_var.get())
        if hasattr(self, '_console_open'):
            self._config['console_open'] = bool(self._console_open)
        try:
            self._config['last_tab'] = self.nb.index(self.nb.select())
        except (tk.TclError, AttributeError):
            pass
        try:
            CONFIG_FILE.write_text(json.dumps(self._config, indent=2), encoding='utf-8')
        except OSError:
            pass

    # ── misc actions ──────────────────────────────────────────────────
    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.out_dir.get() or str(PROJECT_ROOT))
        if d:
            self.out_dir.set(d)

    def _open_path(self, path):
        path = Path(path)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._open_file(path)

    def _open_file(self, path):
        path = str(path)
        try:
            if sys.platform == 'win32':
                os.startfile(path)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])
        except OSError as e:
            messagebox.showerror('Could not open', str(e))

    def _open_selected_file(self, event=None):
        sel = self.tree.selection() or self._targets(self.tree)
        if not sel:
            return
        item = self.items.get(sel[0])
        if not item:
            return
        if item['status'] == ST_ERROR and item.get('error'):
            messagebox.showwarning('Download failed', item['error'][:2000])
            return
        if item.get('file') and os.path.exists(item['file']):
            self._open_file(item['file'])

    def _on_close(self):
        self.paused = True
        self.is_running = False
        for proc in list(self.active.values()):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        try:
            self._config['geometry'] = self.geometry()
        except tk.TclError:
            pass
        self._save_config()
        self.destroy()


# ── gallery thumbnail helpers ─────────────────────────────────────────

def _find_ffmpeg():
    names = ['ffmpeg.exe', 'ffmpeg'] if sys.platform == 'win32' else ['ffmpeg']
    for base in (PROJECT_ROOT, APP_DIR, PROJECT_ROOT / 'cache'):
        for n in names:
            p = base / n
            if p.is_file():
                return str(p)
    return shutil.which('ffmpeg')


def _thumb_path(video_path):
    try:
        mtime = video_path.stat().st_mtime
    except OSError:
        mtime = 0
    key = hashlib.md5(f'{video_path}|{mtime}'.encode('utf-8')).hexdigest()
    return THUMB_CACHE_DIR / f'{key}.png'


def _make_thumb(ffmpeg, video_path, out_png):
    base = [ffmpeg, '-y']
    tail = ['-frames:v', '1', '-vf', f'scale={THUMB_W}:-2', '-loglevel', 'error', str(out_png)]
    for seek in (['-ss', '3', '-i', str(video_path)], ['-i', str(video_path)]):
        try:
            subprocess.run(base + seek + tail, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30, **_subprocess_flags())
        except (OSError, subprocess.SubprocessError):
            pass
        if out_png.exists():
            return


def _fmt_bytes(num):
    val = float(num)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if val < 1024 or unit == 'TB':
            return f'{val:.0f} {unit}' if unit == 'B' else f'{val:.1f} {unit}'
        val /= 1024
    return ''


def _human_size(path):
    try:
        return _fmt_bytes(path.stat().st_size)
    except OSError:
        return ''


def _dupe_hash(path):
    """Fast content fingerprint: md5 of the first + last 1 MB (size already matched)."""
    try:
        size = path.stat().st_size
        h = hashlib.md5()
        with open(path, 'rb') as f:
            h.update(f.read(1024 * 1024))
            if size > 2 * 1024 * 1024:
                f.seek(-1024 * 1024, 2)
                h.update(f.read(1024 * 1024))
        return h.hexdigest()
    except OSError:
        return None


if __name__ == '__main__':
    DownloadManager().mainloop()
