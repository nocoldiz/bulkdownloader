"""Website registry + unified multi-site search/scraper for the bulk downloader GUI.

Websites are stored as a local JSON file (a flat list of
``{name, url, searchURL, scrapeMethod, tags, description, hasSearch}``
dicts) — the same shape AphroArchive exports via
``GET /api/db/websites/export``, so that export can be dropped in directly
via :func:`import_websites_json`.

Searching queries every registered site that has a ``searchURL`` and
returns title/url/thumbnail results. Sites with a known ``scrapeMethod``
(``archive-org``, ``xvideos``) use a dedicated parser; everything else
falls back to :func:`scrape_generic`, a best-effort link/thumbnail scraper.
"""

import html
import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

USER_AGENT = 'Mozilla/5.0 (compatible; AphroArchive/1.0)'
TIMEOUT = 10


def _fetch(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT,
        'Accept': 'text/html,application/xhtml+xml',
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        charset = resp.headers.get_content_charset() or 'utf-8'
        return resp.read().decode(charset, errors='replace')


# ── Website registry ──────────────────────────────────────────────

SCRAPE_METHODS = ('', 'archive-org', 'xvideos')


def load_websites(path):
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return _normalize_sites(data)


def save_websites(path, sites):
    Path(path).write_text(json.dumps(sites, indent=2, ensure_ascii=False), encoding='utf-8')


def _normalize_sites(data):
    """Flatten/clean an arbitrary import payload into a list of site dicts."""
    sites = []

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            if 'url' in node:
                sites.append({
                    'name': node.get('name') or node.get('url', ''),
                    'url': node.get('url', ''),
                    'searchURL': node.get('searchURL', ''),
                    'scrapeMethod': node.get('scrapeMethod', ''),
                    'tags': node.get('tags') if isinstance(node.get('tags'), list) else [],
                    'description': node.get('description', ''),
                    'hasSearch': bool(node.get('hasSearch', bool(node.get('searchURL')))),
                })
            elif 'items' in node:
                visit(node['items'])

    visit(data)
    return sites


def merge_sites(existing, incoming):
    """Merge ``incoming`` sites into ``existing``, de-duping by name+url (incoming wins)."""
    by_key = {}
    order = []
    for s in existing:
        key = (s.get('name', '').strip().lower(), s.get('url', '').strip().lower())
        by_key[key] = dict(s)
        order.append(key)

    added = updated = 0
    for s in incoming:
        key = (s.get('name', '').strip().lower(), s.get('url', '').strip().lower())
        if key in by_key:
            by_key[key].update(s)
            updated += 1
        else:
            by_key[key] = dict(s)
            order.append(key)
            added += 1

    return [by_key[k] for k in order], added, updated


# ── Scrapers ─────────────────────────────────────────────────────

def _abs_url(base, href):
    try:
        return urllib.parse.urljoin(base, href)
    except ValueError:
        return href


def scrape_archive_org(query, limit=24):
    api_url = (
        'https://archive.org/advancedsearch.php?q=' + urllib.parse.quote(query) +
        '+AND+mediatype:(movies)' +
        '&fl[]=identifier&fl[]=title&fl[]=description&fl[]=downloads' +
        '&rows=' + str(limit) + '&output=json&sort[]=downloads+desc'
    )
    data = json.loads(_fetch(api_url))
    docs = (data.get('response') or {}).get('docs') or []
    results = []
    for doc in docs:
        ident = doc.get('identifier')
        if not ident:
            continue
        results.append({
            'title': doc.get('title') or ident,
            'url': 'https://archive.org/details/' + urllib.parse.quote(ident),
            'thumb': 'https://archive.org/services/img/' + urllib.parse.quote(ident),
            'source': 'Archive.org',
        })
    return results


def scrape_xvideos(query, limit=24):
    search_url = f'https://www.xvideos.com/?k={urllib.parse.quote(query)}'
    raw_html = _fetch(search_url)
    results = []
    for block in re.findall(r'<div class="thumb-block[^>]*>[\s\S]*?</div>', raw_html, re.I):
        title_m = re.search(r'title="([^"]+)"', block, re.I)
        title = html.unescape(title_m.group(1).strip()) if title_m else 'Untitled'

        url_m = re.search(r'href="([^"]+)"', block, re.I)
        url = url_m.group(1) if url_m else ''
        if url and not url.startswith('http'):
            url = 'https://www.xvideos.com' + ('' if url.startswith('/') else '/') + url

        thumb = ''
        thumb_m = re.search(r'data-src="([^"]+)"|src="([^"]+)"', block, re.I)
        if thumb_m:
            thumb = thumb_m.group(1) or thumb_m.group(2) or ''
            if thumb and not thumb.startswith('http'):
                thumb = 'https:' + thumb

        if url and title:
            results.append({'title': title, 'url': url, 'thumb': thumb, 'source': 'XVideos'})
        if len(results) >= limit:
            break
    return results


_ANCHOR_RE = re.compile(r'<a\b([^>]*)>(.*?)</a>', re.I | re.S)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_TITLE_ATTR_RE = re.compile(r'title=["\']([^"\']+)["\']', re.I)
_ALT_ATTR_RE = re.compile(r'alt=["\']([^"\']+)["\']', re.I)
_IMG_SRC_RE = re.compile(r'(?:data-src|data-original|src)=["\']([^"\']+)["\']', re.I)
_TAG_RE = re.compile(r'<[^>]+>')


def scrape_generic(site, query, limit=20):
    """Best-effort scraper for sites without a dedicated scrapeMethod.

    Fetches the site's search-results page and extracts links that point
    back to the same host, pairing each with a title (from its title/alt
    attribute or link text) and an optional thumbnail (the first <img>
    inside the link).
    """
    search_url = site.get('searchURL') or ''
    if not search_url:
        return []
    full_url = search_url + urllib.parse.quote(query)
    raw_html = _fetch(full_url)

    try:
        site_host = (urllib.parse.urlparse(site.get('url') or full_url).hostname or '').lower()
    except ValueError:
        site_host = ''
    site_host = site_host[4:] if site_host.startswith('www.') else site_host

    results = []
    seen = set()
    for m in _ANCHOR_RE.finditer(raw_html):
        attrs, inner = m.group(1), m.group(2)
        href_m = _HREF_RE.search(attrs)
        if not href_m:
            continue
        href = href_m.group(1)
        if href.startswith(('#', 'javascript:', 'mailto:')):
            continue

        abs_url = _abs_url(full_url, href)
        try:
            host = (urllib.parse.urlparse(abs_url).hostname or '').lower()
        except ValueError:
            continue
        host = host[4:] if host.startswith('www.') else host
        if site_host and host != site_host:
            continue
        if abs_url in seen:
            continue

        title = ''
        title_m = _TITLE_ATTR_RE.search(attrs)
        if title_m:
            title = title_m.group(1)
        if not title:
            alt_m = _ALT_ATTR_RE.search(inner)
            if alt_m:
                title = alt_m.group(1)
        if not title:
            title = re.sub(r'\s+', ' ', _TAG_RE.sub(' ', inner)).strip()
        title = html.unescape(title).strip()
        if len(title) < 3:
            continue

        thumb = ''
        img_m = _IMG_SRC_RE.search(inner)
        if img_m:
            thumb = _abs_url(full_url, img_m.group(1))

        seen.add(abs_url)
        results.append({
            'title': title[:120],
            'url': abs_url,
            'thumb': thumb,
            'source': site.get('name') or site_host,
        })
        if len(results) >= limit:
            break

    return results


def search_site(site, query, limit=20):
    """Run the appropriate scraper for a single site, returning a result list."""
    method = (site.get('scrapeMethod') or '').strip().lower()
    try:
        if method == 'archive-org':
            return scrape_archive_org(query, limit)
        if method == 'xvideos':
            return scrape_xvideos(query, limit)
        return scrape_generic(site, query, limit)
    except Exception as e:
        return [{'title': f'[error] {e}', 'url': '', 'thumb': '', 'source': site.get('name') or site.get('url', ''), 'error': True}]


def search_all(sites, query, on_result, max_workers=8, limit_per_site=15, should_stop=None):
    """Search every site (that has a searchURL) concurrently.

    ``on_result(site, results)`` is called from the calling thread as each
    site's search completes. If ``should_stop`` is given and returns True,
    remaining completions are skipped (in-flight requests still finish in
    the background but their results are discarded).
    """
    searchable = [s for s in sites if s.get('searchURL')]
    if not searchable:
        return

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(search_site, s, query, limit_per_site): s for s in searchable}
        for fut in as_completed(futures):
            site = futures[fut]
            if should_stop and should_stop():
                continue
            try:
                results = fut.result()
            except Exception as e:
                results = [{'title': f'[error] {e}', 'url': '', 'thumb': '', 'source': site.get('name'), 'error': True}]
            on_result(site, results)
