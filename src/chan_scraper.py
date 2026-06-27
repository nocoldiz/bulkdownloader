#!/usr/bin/env python3
"""Imageboard ("chan") thread scraper — shared engine for the GUI's Chan tab.

Given an imageboard thread/board URL it returns every image + video media URL on
the page and can download each file to a folder. It is engine-agnostic and aims
to work with *any* imageboard:

  1. 4chan / 4channel — its official JSON API (most reliable).
  2. Any thread URL of the shape /{board}/res/{id} or /{board}/thread/{id} — a
     generic JSON-API probe that understands the two engines almost every board
     runs on: vichan / Tinyboard (``tim`` + ``ext``, files under /{board}/src/)
     and Lynxchan (a ``files`` array of ``path`` entries, e.g. 8kun / endchan).
  3. Anything else, or a board that hides its JSON — a generic HTML media-link
     scan, the universal fallback that works on any page.

The GUI (bulkdownloader_gui.py) imports this module: it scrapes in a background
thread, saves the media URLs to the bookmark DB tagged with the source site, and
downloads the files into the download folder.

All network access degrades gracefully: curl_cffi (TLS-impersonation, beats
Cloudflare) → requests → stdlib urllib, so it works even in a bare environment.
"""

import os
import io
import re
import gzip
import zlib
import json
import shutil
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.avif'}
VIDEO_EXTS = {'.webm', '.mp4', '.mov', '.mkv', '.m4v', '.avi', '.gifv', '.ogv'}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

# Hosts handled through the 4chan JSON API rather than HTML scraping.
_FOURCHAN_HOST = re.compile(r'(?:^|\.)4chan(?:nel)?\.org$', re.I)
# Thread URL shape shared by virtually every imageboard engine:
#   /{board}/thread/{id}   (4chan, modern Lynxchan)
#   /{board}/res/{id}.html (vichan / Tinyboard, classic Lynxchan)
_THREAD_RE = re.compile(r'/([^/?#]+)/(?:thread|res)/(\d+)', re.I)
# href/src/data-src/poster on any tag — the broadest net for media links.
_ATTR_RE = re.compile(r'(?:href|src|data-src|data-original|poster|content)\s*=\s*["\']([^"\']+)["\']', re.I)
_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.I | re.S)
# A bare URL pasted into post text — used to scrape links posted in comments.
_LINK_RE = re.compile(r'https?://[^\s"\'<>()\[\]{}]+', re.I)


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _headers(referer=None):
    h = {'User-Agent': USER_AGENT,
         'Accept': '*/*',
         'Accept-Language': 'en-US,en;q=0.9',
         'Accept-Encoding': 'gzip, deflate'}
    if referer:
        h['Referer'] = referer
    return h


def fetch_text(url, timeout=25, max_bytes=12 * 1024 * 1024):
    """Return (final_url, text) for a page, '' on failure. Tries curl_cffi →
    requests → urllib so Cloudflare-fronted boards still load."""
    hdrs = _headers()
    try:
        from curl_cffi import requests as _cffi
        r = _cffi.get(url, impersonate='chrome', headers=hdrs, timeout=timeout, allow_redirects=True)
        return str(r.url), r.text
    except ImportError:
        pass
    except Exception:
        pass
    try:
        import requests as _req
        r = _req.get(url, headers=hdrs, timeout=timeout, allow_redirects=True)
        return r.url, r.text
    except ImportError:
        pass
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final = resp.geturl()
            raw = resp.read(max_bytes)
            enc = (resp.headers.get('Content-Encoding') or '').lower()
            if 'gzip' in enc:
                try:
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                except OSError:
                    pass
            elif 'deflate' in enc:
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    try:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                    except zlib.error:
                        pass
            ctype = resp.headers.get('Content-Type') or ''
            m = re.search(r'charset=([\w-]+)', ctype, re.I)
            return final, raw.decode(m.group(1) if m else 'utf-8', errors='replace')
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return url, ''


def _safe_filename(url):
    """Derive a filesystem-safe filename from a media URL."""
    path = urllib.parse.urlsplit(url).path
    name = urllib.parse.unquote(os.path.basename(path)) or 'file'
    name = re.sub(r'[^\w.\-]+', '_', name).strip('_') or 'file'
    if not os.path.splitext(name)[1]:
        name += '.bin'
    return name[:120]


def _unique_path(dest):
    if not dest.exists():
        return dest
    stem, suffix, n = dest.stem, dest.suffix, 1
    while dest.exists():
        dest = dest.with_name(f'{stem}_{n}{suffix}')
        n += 1
    return dest


def _rm(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def download_file(url, dest_dir, referer=None, timeout=90):
    """Download *url* into *dest_dir*. Returns the saved Path, or None on failure.
    Skips re-downloading when an identically named file already exists."""
    dest_dir = Path(dest_dir)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    dest = _unique_path(dest_dir / _safe_filename(url))
    hdrs = _headers(referer)

    try:
        from curl_cffi import requests as _cffi
        with _cffi.get(url, impersonate='chrome', headers=hdrs, timeout=timeout,
                       stream=True, allow_redirects=True) as r:
            if r.status_code >= 400:
                return None
            with open(dest, 'wb') as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk)
        return dest if dest.exists() and dest.stat().st_size > 0 else (_rm(dest) or None)
    except ImportError:
        pass
    except Exception:
        _rm(dest)

    try:
        import requests as _req
        with _req.get(url, headers=hdrs, timeout=timeout, stream=True, allow_redirects=True) as r:
            if r.status_code >= 400:
                return None
            with open(dest, 'wb') as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk)
        return dest if dest.exists() and dest.stat().st_size > 0 else (_rm(dest) or None)
    except ImportError:
        pass
    except Exception:
        _rm(dest)

    try:
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, 'wb') as f:
            shutil.copyfileobj(resp, f)
        return dest if dest.exists() and dest.stat().st_size > 0 else (_rm(dest) or None)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        _rm(dest)
        return None


# ── Scraping ───────────────────────────────────────────────────────────────────

def is_imageboard_url(url):
    return isinstance(url, str) and url.strip().startswith(('http://', 'https://'))


def _dedup(urls):
    seen, out = set(), []
    for u in urls:
        k = u.lower()
        if k not in seen:
            seen.add(k)
            out.append(u)
    return out


def _media_ext(url):
    path = urllib.parse.urlsplit(url).path.lower()
    return os.path.splitext(path)[1]


def _media_links_in_text(text):
    """Yield direct-media URLs pasted as plain text in a post (e.g. an external
    file-host link dropped in a comment). Trailing punctuation and stray HTML
    entities left over from comment markup are trimmed before the ext check."""
    if not isinstance(text, str) or 'http' not in text:
        return
    for m in _LINK_RE.finditer(text):
        u = m.group(0)
        for ent in ('&quot;', '&gt;', '&lt;', '&amp;', '&#'):  # cut leftover HTML entities
            u = u.split(ent)[0]
        u = u.rstrip('.,;:!?)\'">')                            # then trailing prose punctuation
        if _media_ext(u) in MEDIA_EXTS:
            yield u


def scrape_media(url, want_images=True, want_videos=True):
    """Return (title, [media_url, ...]) for the imageboard page at *url*, limited
    to the requested media kinds.

    Strategy, most reliable first: 4chan's JSON API → a generic JSON-API probe
    (vichan / Lynxchan) → a generic HTML media scan. The first source that finds
    media wins, so engine-native APIs are preferred over scraping but every board
    still degrades to the universal HTML net."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or '').lower()
    m = _THREAD_RE.search(parts.path)
    title, urls = '', []

    if m and _FOURCHAN_HOST.search(host):
        title, urls = _scrape_4chan(m.group(1), m.group(2))
    elif m:
        title, urls = _scrape_json_api(parts, m.group(1), m.group(2))

    if not urls:                                          # JSON unavailable → scrape HTML
        title, urls = _scrape_generic(url)

    wanted = set()
    if want_images:
        wanted |= IMAGE_EXTS
    if want_videos:
        wanted |= VIDEO_EXTS
    urls = [u for u in urls if _media_ext(u) in wanted]
    return title, _dedup(urls)


def _scrape_4chan(board, thread):
    api = f'https://a.4cdn.org/{board}/thread/{thread}.json'
    _, text = fetch_text(api)
    title = f'/{board}/ thread {thread}'
    urls = []
    try:
        data = json.loads(text)
    except ValueError:
        return title, []
    posts = data.get('posts') or []
    if posts and posts[0].get('sub'):
        title = re.sub(r'<[^>]+>', '', posts[0]['sub']).strip() or title
    for p in posts:
        if 'tim' in p and 'ext' in p:
            urls.append(f'https://i.4cdn.org/{board}/{p["tim"]}{p["ext"]}')
        for extra in (p.get('extra_files') or []):       # multi-file posts
            if 'tim' in extra and 'ext' in extra:
                urls.append(f'https://i.4cdn.org/{board}/{extra["tim"]}{extra["ext"]}')
    return title, _dedup(urls)


def _scrape_json_api(parts, board, thread):
    """Generic imageboard JSON-API probe. Tries the two endpoint shapes used by
    nearly every engine and walks whatever JSON comes back, recognising both
    vichan/Tinyboard (``tim`` + ``ext``) and Lynxchan (``files[].path``) posts.
    Returns ('', []) when no JSON endpoint responds with media."""
    root = f'{parts.scheme or "https"}://{parts.netloc}'
    for api in (f'{root}/{board}/res/{thread}.json',
                f'{root}/{board}/thread/{thread}.json'):
        _, text = fetch_text(api)
        if not text:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        urls = _dedup(list(_walk_json_media(data, board, root)))
        if urls:
            return _json_title(data, board, thread), urls
    return '', []


def _walk_json_media(node, board, root):
    """Yield every full-resolution media URL found anywhere in a thread JSON tree.
    Thumbnails (vichan derives them, Lynxchan keys them under ``thumb``) are never
    emitted because only the ``path`` and ``tim``/``ext`` keys are followed."""
    if isinstance(node, dict):
        tim, ext = node.get('tim'), node.get('ext')
        if tim and isinstance(ext, str) and ext:                  # vichan / 4chan
            yield f'{root}/{board}/src/{tim}{ext}'
        path = node.get('path')
        if isinstance(path, str) and _media_ext(path) in MEDIA_EXTS:  # Lynxchan
            yield urllib.parse.urljoin(root + '/', path)
        for v in node.values():
            yield from _walk_json_media(v, board, root)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_json_media(item, board, root)
    elif isinstance(node, str):                                   # links posted in comments
        yield from _media_links_in_text(node)


def _json_title(data, board, thread):
    """Best-effort thread subject from a JSON tree, across engine key names."""
    candidates = []
    if isinstance(data, dict):
        candidates.append(data)                                   # Lynxchan: OP at top level
        posts = data.get('posts')
        if isinstance(posts, list) and posts and isinstance(posts[0], dict):
            candidates.append(posts[0])                           # vichan / 4chan: OP first post
    for c in candidates:
        for key in ('subject', 'sub', 'title'):
            v = c.get(key)
            if isinstance(v, str):
                clean = re.sub(r'<[^>]+>', '', v).strip()
                if clean:
                    return clean[:120]
    return f'/{board}/ thread {thread}'


def _scrape_generic(url):
    final, html = fetch_text(url)
    base = final or url
    title = ''
    mt = _TITLE_RE.search(html)
    if mt:
        title = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', mt.group(1))).strip()[:120]
    urls = []
    for m in _ATTR_RE.finditer(html):
        raw = m.group(1).strip()
        if not raw or raw.startswith(('data:', 'javascript:', '#', 'mailto:')):
            continue
        absu = urllib.parse.urljoin(base, raw)
        path = urllib.parse.urlsplit(absu).path.lower()
        if os.path.splitext(path)[1] not in MEDIA_EXTS:
            continue
        name = os.path.basename(path)
        if ('/thumb' in path or '/thumbs/' in path        # skip low-res thumbnails:
                or '/.media/t_' in path                   #   Lynxchan thumb path
                or name.startswith(('t_', 'thumb'))):     #   common thumb prefixes
            continue
        urls.append(absu)
    urls.extend(_media_links_in_text(html))               # media links posted as text
    return title, _dedup(urls)


# ── Standalone CLI (handy for testing without the GUI) ─────────────────────────

def main():
    import sys
    if len(sys.argv) < 2:
        print('Usage: python chan_scraper.py <imageboard_url> [dest_dir]')
        sys.exit(1)
    url = sys.argv[1]
    dest = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd() / 'chan_media'
    title, media = scrape_media(url)
    print(f'Thread: {title or "(untitled)"}')
    print(f'Found {len(media)} media file(s).')
    if not media:
        return
    ok = 0
    for i, m in enumerate(media, 1):
        p = download_file(m, dest, referer=url)
        ok += 1 if p else 0
        print(f'  [{i}/{len(media)}] {"saved " + p.name if p else "FAILED " + m}')
    print(f'\nDone: {ok}/{len(media)} downloaded into {dest}')


if __name__ == '__main__':
    main()
