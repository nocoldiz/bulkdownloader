#!/usr/bin/env python3
"""Unified JSON database shared by the console (bulkdownloader.py) and the GUI
(bulkdownloader_gui.py).

A single ``db.json`` holds every link the app knows about, split into sections:

* ``queue``      — ordered list of items to download (``{url, status, title, file,
                   error, source, added_at}``). status ∈ queued/downloading/done/
                   error/stopped.
* ``downloaded`` — registry of finished URLs keyed by their normalised key
                   (``{normkey: {url, file, ts}}``) so a link is never re-queued
                   while its file is still on disk.
* ``bookmarks``  — saved video links (e.g. scraped from a logged-in X.com session)
                   ``{url, title, site, source, added_at}``. These can later be fed
                   into the queue and downloaded (with the saved credentials).

Design rules (per project requirements):
* The ``links_to_download.txt`` file is an *input only*: its links are fed into the
  queue section and the txt file is **never emptied** by the import.
* Every mutation de-duplicates by normalised key, so the same link can be fed in
  repeatedly (txt import, paste, scrape) without ever duplicating.
"""

import os
import json
import time
import urllib.parse
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

# Runtime files live in the project root — the folder above src/ when running
# from source, otherwise next to this module (frozen builds set $BULK_DB_FILE).
DATA_DIR = APP_DIR.parent if APP_DIR.name == 'src' else APP_DIR
# App-managed state (the db) is kept in a config/ subfolder; the GUI overrides
# this with $BULK_DB_FILE so both processes always agree on the exact file.
CONFIG_DIR = DATA_DIR / 'config'

# Query params that are pure tracking noise — stripped only for the de-dup key,
# never from the URL we actually download.
TRACKING_PARAMS = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
                   'fbclid', 'gclid', 'ref', 'ref_', 'igshid', 'si', 'feature'}

DOWNLOADED_CAP = 5000        # max entries kept in the downloaded registry
BOOKMARKS_CAP = 20000        # max saved bookmarks

# Status labels (kept in sync with the GUI).
ST_QUEUED = 'queued'
ST_DOWNLOADING = 'downloading'
ST_DONE = 'done'
ST_ERROR = 'error'
ST_STOPPED = 'stopped'


def db_path():
    """Location of the shared db.json — override with $BULK_DB_FILE."""
    env = os.environ.get('BULK_DB_FILE', '').strip()
    return Path(env) if env else (CONFIG_DIR / 'db.json')


def is_http(url):
    return isinstance(url, str) and url.startswith(('http://', 'https://'))


def norm_key(url):
    """De-dup key: lowercase host, drop tracking params + trailing slash/fragment.
    Used ONLY for duplicate detection — the original URL is what gets downloaded.
    MUST stay identical between console and GUI so dedup is consistent."""
    try:
        p = urllib.parse.urlsplit((url or '').strip())
    except ValueError:
        return (url or '').strip().lower()
    host = (p.hostname or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    query = urllib.parse.urlencode([
        (k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ])
    return urllib.parse.urlunsplit((p.scheme.lower(), host, p.path.rstrip('/'), query, ''))


def blank():
    return {'version': 2, 'queue': [], 'downloaded': {}, 'bookmarks': []}


def load(path=None):
    p = Path(path) if path else db_path()
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            data.setdefault('version', 2)
            if not isinstance(data.get('queue'), list):
                data['queue'] = []
            if not isinstance(data.get('downloaded'), dict):
                data['downloaded'] = {}
            if not isinstance(data.get('bookmarks'), list):
                data['bookmarks'] = []
            return data
    except (OSError, ValueError):
        pass
    return blank()


def save(data, path=None):
    """Atomically write the db (tmp file + replace)."""
    p = Path(path) if path else db_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + '.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(p)
        return True
    except OSError:
        return False


# ── queue helpers ─────────────────────────────────────────────────────

def queue_keys(data):
    return {norm_key(it.get('url', '')) for it in data['queue']}


def is_downloaded(data, url):
    """True when *url* is already downloaded AND its recorded file still exists
    (a removed file allows a re-download)."""
    rec = data['downloaded'].get(norm_key(url))
    if not rec:
        return False
    f = rec.get('file')
    if f and not os.path.exists(f):
        return False
    return True


def add_to_queue(data, urls, source=None, at_top=False, status=ST_QUEUED):
    """Add new http(s) URLs to the queue section, skipping anything already queued
    or already downloaded. Returns the list of URLs actually added (deduped)."""
    existing = queue_keys(data)
    new_items, added = [], []
    for u in urls:
        if not is_http(u):
            continue
        k = norm_key(u)
        if k in existing:
            continue
        if is_downloaded(data, u):
            existing.add(k)
            continue
        existing.add(k)
        new_items.append({'url': u, 'status': status, 'title': None, 'file': None,
                          'error': '', 'source': source, 'added_at': int(time.time())})
        added.append(u)
    if at_top:
        data['queue'][:0] = new_items
    else:
        data['queue'].extend(new_items)
    return added


def mark_downloaded(data, url, file=None):
    """Record a finished download and drop the matching queue row's pending state."""
    data['downloaded'][norm_key(url)] = {'url': url, 'file': file, 'ts': int(time.time())}
    if len(data['downloaded']) > DOWNLOADED_CAP:
        stale = sorted(data['downloaded'], key=lambda k: data['downloaded'][k].get('ts', 0))
        for k in stale[:len(data['downloaded']) - DOWNLOADED_CAP]:
            data['downloaded'].pop(k, None)
    # reflect on the matching queue item if present
    nk = norm_key(url)
    for it in data['queue']:
        if norm_key(it.get('url', '')) == nk:
            it['status'] = ST_DONE
            it['file'] = file
            it['error'] = ''
    # mirror onto any matching bookmark
    for bm in data['bookmarks']:
        if norm_key(bm.get('url', '')) == nk:
            bm['downloaded'] = True


def mark_failed(data, url, reason=''):
    nk = norm_key(url)
    for it in data['queue']:
        if norm_key(it.get('url', '')) == nk:
            it['status'] = ST_ERROR
            it['error'] = reason or 'download failed'


# ── bookmark helpers ──────────────────────────────────────────────────

def bookmark_keys(data):
    return {norm_key(b.get('url', '')) for b in data['bookmarks']}


def add_bookmarks(data, items, source=None):
    """Save scraped/imported links into the bookmarks section (deduped). Each item
    may be a URL string or a dict ``{url, title?, site?}``. Returns URLs added."""
    existing = bookmark_keys(data)
    added = []
    for it in items:
        url = it if isinstance(it, str) else (it.get('url') if isinstance(it, dict) else None)
        if not is_http(url):
            continue
        k = norm_key(url)
        if k in existing:
            continue
        existing.add(k)
        data['bookmarks'].append({
            'url': url,
            'title': (it.get('title') if isinstance(it, dict) else None),
            'site': (it.get('site') if isinstance(it, dict) else None),
            'source': source,
            'added_at': int(time.time()),
            'downloaded': is_downloaded(data, url),
        })
        added.append(url)
    if len(data['bookmarks']) > BOOKMARKS_CAP:
        data['bookmarks'] = data['bookmarks'][-BOOKMARKS_CAP:]
    return added


def pending_bookmark_urls(data):
    """Bookmark URLs that are not yet downloaded and not already queued — i.e. the
    ones a 'download bookmarks' action should fetch."""
    queued = queue_keys(data)
    out = []
    for bm in data['bookmarks']:
        url = bm.get('url', '')
        if not is_http(url):
            continue
        if is_downloaded(data, url):
            continue
        if norm_key(url) in queued:
            continue
        out.append(url)
    return out


# ── txt ingest + dedup ────────────────────────────────────────────────

def read_links_txt(txt_path):
    p = Path(txt_path)
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []
    return [l.strip() for l in lines if is_http(l.strip())]


def ingest_links_txt(data, txt_path, source='links.txt'):
    """Feed links_to_download.txt into the queue section (deduped). The txt file is
    deliberately left untouched — it is an input, not the database."""
    return add_to_queue(data, read_links_txt(txt_path), source=source)


def dedup(data):
    """Collapse duplicate queue rows + bookmarks by normalised key (keep first).
    Returns (queue_removed, bookmarks_removed)."""
    seen = set()
    new_q = []
    for it in data['queue']:
        k = norm_key(it.get('url', ''))
        if k in seen:
            continue
        seen.add(k)
        new_q.append(it)
    q_removed = len(data['queue']) - len(new_q)
    data['queue'] = new_q

    seen = set()
    new_b = []
    for bm in data['bookmarks']:
        k = norm_key(bm.get('url', ''))
        if k in seen:
            continue
        seen.add(k)
        new_b.append(bm)
    b_removed = len(data['bookmarks']) - len(new_b)
    data['bookmarks'] = new_b
    return q_removed, b_removed
