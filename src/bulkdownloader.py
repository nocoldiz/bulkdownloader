import os
import re
import io
import sys
import gzip
import json
import zlib
import html
import shutil
import base64
import argparse
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# Force UTF-8 stdout so the server process can always decode the output
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

try:
    import yt_dlp
except ImportError:
    print('Installing yt-dlp...', flush=True)
    os.system(f'"{sys.executable}" -m pip install -U yt-dlp')
    import yt_dlp

# Shared unified database (db.json) — the same module the GUI uses, so the console
# and GUI normalise + de-dup links identically. Optional: falls back to the legacy
# txt flow if it can't be imported.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import bulk_db
except Exception:
    bulk_db = None

# Where runtime files (links_*.txt, cookies.txt, db.json) live: the project root,
# i.e. the folder above src/ when running from source, else next to this script.
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == 'src' else SCRIPT_DIR


# Browser impersonation via curl_cffi is now ENABLED BY DEFAULT for virtually
# every site. This is currently the single most effective technique to download
# videos from modern platforms (especially adult/porn tubes) protected by
# Cloudflare, PerimeterX, DataDome, Akamai, or custom bot detection.
#
#   pip install curl_cffi     ← Strongly recommended (huge success rate boost)
#
# Without it, many protected sites will fail or return blocked/lower-quality results.
def _detect_impersonate_targets():
    # Direct import check is the most reliable — yt-dlp's internal detection
    # can return empty even when curl_cffi is properly installed.
    try:
        from curl_cffi import requests as _cffi
        _cffi.get  # confirm it has a requests-compatible API
        return ['chrome']
    except ImportError:
        pass
    except Exception:
        pass

    # Fall back to yt-dlp's own probe
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
            targets = list(ydl._get_available_impersonate_targets())
            if targets:
                return targets
    except Exception:
        pass

    return []


_IMPERSONATE_TARGETS = None


def impersonate_available():
    global _IMPERSONATE_TARGETS
    if _IMPERSONATE_TARGETS is None:
        _IMPERSONATE_TARGETS = _detect_impersonate_targets()
        if not _IMPERSONATE_TARGETS:
            print('   [info] curl_cffi not available — browser impersonation disabled '
                  '(pip install curl_cffi for better Cloudflare/X.com/Instagram support).',
                  flush=True)
    return bool(_IMPERSONATE_TARGETS)


def set_impersonate(opts, target='chrome'):
    """Only set opts['impersonate'] if a matching target is actually usable."""
    if impersonate_available():
        opts['impersonate'] = target
    return opts


# Filenames a browser "export cookies" extension typically produces.
_COOKIE_FILE_NAMES = (
    'cookies.txt', 'x_cookies.txt', 'x.com_cookies.txt',
    'www.x.com_cookies.txt', 'twitter.com_cookies.txt', 'twitter_cookies.txt',
)

_cookie_file_cache = None  # 1-tuple holding the resolved path (or None) once probed


def _aggressive_cookie_dirs():
    """Common cross-platform folders where an exported cookies.txt might live."""
    home = Path.home()
    dirs = [
        DATA_DIR,                          # project root (highest trust)
        Path.cwd(),
        home,
        home / 'Downloads', home / 'Desktop', home / 'Documents',
    ]
    # Honour XDG / localised download dirs and a couple of OS-specific spots.
    xdg = os.environ.get('XDG_DOWNLOAD_DIR', '').strip()
    if xdg:
        dirs.append(Path(xdg))
    if sys.platform == 'win32':
        up = os.environ.get('USERPROFILE', '').strip()
        if up:
            dirs.append(Path(up) / 'Downloads')
    return dirs


def _score_cookie_file(p):
    """Rank a candidate cookies file: real X/Twitter Netscape cookies + newest win."""
    try:
        head = p.read_text(encoding='utf-8', errors='replace')[:65536]
    except OSError:
        head = ''
    has_twitter = ('x.com' in head or 'twitter.com' in head)
    is_netscape = ('# Netscape HTTP Cookie File' in head or '\t' in head)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0
    return (has_twitter, is_netscape, mtime)


def _discover_cookie_file():
    """Locate a Netscape-format cookies.txt for login-gated sites (X.com etc.).

    Order: $BULK_COOKIES_FILE → aggressive scan of common cross-platform folders
    (Downloads / Desktop / Documents / home / cwd / next to this script), picking
    the file that actually contains X/Twitter cookies and is most recent.

    An exported cookies.txt avoids yt-dlp's flaky live browser-cookie extraction
    on Windows (Chrome DPAPI / Edge locked-DB errors).
    """
    env = os.environ.get('BULK_COOKIES_FILE', '').strip().strip('"').strip("'")
    if env and os.path.isfile(env):
        return env

    candidates, seen = [], set()

    def _consider(p):
        try:
            if not p.is_file():
                return
            rp = p.resolve()
        except OSError:
            return
        if rp in seen:
            return
        seen.add(rp)
        candidates.append(rp)

    for d in _aggressive_cookie_dirs():
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        for name in _COOKIE_FILE_NAMES:
            _consider(d / name)
        try:  # shallow glob catches "<site>_cookies.txt" extension exports
            for p in d.glob('*cookies*.txt'):
                _consider(p)
        except OSError:
            pass

    if not candidates:
        return None
    return str(max(candidates, key=_score_cookie_file))


def resolve_cookie_file():
    """Cached wrapper around _discover_cookie_file() (scans the FS only once)."""
    global _cookie_file_cache
    if _cookie_file_cache is None:
        path = _discover_cookie_file()
        _cookie_file_cache = (path,)
        if path:
            print(f'   [cookies] using cookie file: {path}', flush=True)
    return _cookie_file_cache[0]


def _invalidate_cookie_cache():
    """Force the next resolve_cookie_file() to re-scan (after saving new cookies)."""
    global _cookie_file_cache
    _cookie_file_cache = None


# ════════════════════════════════════════════════════════════════════════
#  Constants
# ════════════════════════════════════════════════════════════════════════

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36')

# Extensions that represent a directly-downloadable stream/file.
DIRECT_MEDIA_EXTS = (
    'm3u8', 'mpd', 'mp4', 'webm', 'mov', 'm4v', 'ts', 'flv', 'mkv',
    'avi', 'f4v', 'ogv', '3gp', 'wmv',
)
# Streaming-manifest extensions yt-dlp handles best.
MANIFEST_EXTS = ('m3u8', 'mpd')

# Hosts that only ever serve a site's own static/brand assets — never user media.
# X/Twitter's no-JS wall ("JavaScript is not available.") embeds brand clips such as
# abs.twimg.com/videos/grok-4-key-visual.mp4 and pbs.twimg.com/static/money/x-card-*.mp4.
JUNK_MEDIA_HOST_SUBSTRINGS = (
    'abs.twimg.com',
)


def _is_junk_media_url(u):
    """True for URLs that only ever point at a site's static/brand assets, never
    real user media — so the scraper never 'succeeds' on X's no-JS brand clips."""
    netloc = urlparse(u).netloc.lower()
    if any(j in netloc for j in JUNK_MEDIA_HOST_SUBSTRINGS):
        return True
    # X/Twitter: real tweet videos are served ONLY from video.twimg.com. Any other
    # *.twimg.com host (abs / pbs / static) is a brand/UI asset, never tweet content.
    if netloc.endswith('twimg.com') and not netloc.endswith('video.twimg.com'):
        return True
    return False

# Page titles that signal a bot/no-JS wall rather than real content — their titles and
# scraped media must NOT be trusted (X serves these when a tweet needs login/JS).
WALL_TITLES = (
    'javascript is not available.',
)

# Hosts that almost always wrap an embeddable player worth recursing into.
EMBED_HOST_HINTS = (
    'youtube', 'youtu.be', 'vimeo', 'dailymotion', 'streamable', 'twitch',
    'jwplayer', 'jwplatform', 'brightcove', 'wistia', 'kaltura', 'vidyard',
    'players.', 'player.', 'embed.', 'iframe.', 'cdn.', 'video.',
    'streamtape', 'dood', 'mixdrop', 'fembed', 'vidoza', 'upstream',
    'mp4upload', 'streamsb', 'filemoon', 'voe', 'vtube', 'sendvid',
)


# ════════════════════════════════════════════════════════════════════════
#  Lightweight HTTP fetch (stdlib only — works without requests/bs4)
# ════════════════════════════════════════════════════════════════════════

def http_get(url, referer=None, timeout=25, max_bytes=8 * 1024 * 1024):
    """
    Fetch a page and return (final_url, text).
    Tries in order: curl_cffi (Cloudflare/bot bypass) → requests → stdlib urllib.
    Returns (url, '') on total failure.
    """
    hdrs = {
        'User-Agent': USER_AGENT,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
    }
    if referer:
        hdrs['Referer'] = referer

    # 1) curl_cffi — same TLS fingerprinting that powers yt-dlp --impersonate;
    #    bypasses Cloudflare JS-challenge, PerimeterX, DataDome, Akamai at fetch time.
    try:
        from curl_cffi import requests as _cffi
        r = _cffi.get(url, impersonate='chrome', headers=hdrs,
                      timeout=timeout, allow_redirects=True)
        return r.url, r.text
    except ImportError:
        pass
    except Exception as e:
        print(f'   [scrape] curl_cffi failed for {url}: {e}', flush=True)

    # 2) requests — cookie-jar aware, better redirect chain than urllib.
    try:
        import requests as _req
        s = _req.Session()
        s.headers.update(hdrs)
        r = s.get(url, timeout=timeout, allow_redirects=True)
        return r.url, r.text
    except ImportError:
        pass
    except Exception as e:
        print(f'   [scrape] requests failed for {url}: {e}', flush=True)

    # 3) stdlib urllib — zero-dependency fallback.
    try:
        req = Request(url, headers=hdrs)
        with urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl()
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
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            ctype = (resp.headers.get('Content-Type') or '')
            charset = 'utf-8'
            m = re.search(r'charset=([\w-]+)', ctype, re.I)
            if m:
                charset = m.group(1)
            return final_url, raw.decode(charset, errors='replace')
    except (URLError, HTTPError, OSError, ValueError) as e:
        print(f'   [scrape] fetch failed for {url}: {e}', flush=True)
        return url, ''


# ════════════════════════════════════════════════════════════════════════
#  JS deobfuscation — exposes hidden URLs before regex scanning
# ════════════════════════════════════════════════════════════════════════

def _deobfuscate_js(page):
    """
    Lightweight pass over common JS obfuscation tricks.
    Returns the original page + any decoded content appended as extra text,
    so all downstream regex patterns get a shot at the plaintext.
    """
    extras = []

    # eval(atob('...')) / atob('...')  — base64-encoded JS/URLs
    for m in re.finditer(r'atob\s*\(\s*["\']([A-Za-z0-9+/=]{16,})["\']', page):
        try:
            decoded = base64.b64decode(m.group(1) + '==').decode('utf-8', errors='replace')
            if any(x in decoded for x in ('http', 'm3u8', 'mp4', 'source', 'file', 'stream')):
                extras.append(decoded)
        except Exception:
            pass

    # Bare base64 strings (40+ chars) that decode to something URL-like
    for m in re.finditer(r'["\']([A-Za-z0-9+/]{40,}={0,2})["\']', page):
        try:
            decoded = base64.b64decode(m.group(1) + '==').decode('utf-8', errors='replace')
            if 'http' in decoded and '.' in decoded:
                extras.append(decoded)
        except Exception:
            pass

    # String.fromCharCode(72, 84, 84, 80, ...)
    for m in re.finditer(r'String\.fromCharCode\(([0-9,\s]+)\)', page):
        try:
            decoded = ''.join(chr(int(c)) for c in m.group(1).split(',') if c.strip())
            if 'http' in decoded:
                extras.append(decoded)
        except Exception:
            pass

    # eval(unescape('%68%74%74%70...'))
    for m in re.finditer(r'unescape\s*\(\s*["\']([%0-9A-Fa-f]+)["\']', page):
        try:
            decoded = unquote(m.group(1))
            if 'http' in decoded:
                extras.append(decoded)
        except Exception:
            pass

    # P.A.C.K.E.R. packed JS — attempt naive symbol substitution
    if 'eval(function(p,a,c,k,e' in page:
        for m in re.finditer(
            r"eval\(function\(p,a,c,k,e[^)]*\)\{[^}]+\}\s*\('([^']+)'\s*,\s*(\d+)\s*,\s*\d+\s*,'([^']+)'",
            page, re.S,
        ):
            try:
                p_str, radix, k_raw = m.group(1), int(m.group(2)), m.group(3).split('|')
                def _unsym(sym, _r=radix, _k=k_raw):
                    if not sym:
                        return sym
                    try:
                        idx = int(sym, _r)
                        return _k[idx] if idx < len(_k) and _k[idx] else sym
                    except (ValueError, IndexError):
                        return sym
                unpacked = re.sub(r'\b([A-Za-z0-9]+)\b', lambda s: _unsym(s.group(0)), p_str)
                extras.append(unpacked)
            except Exception:
                pass

    # window.location.replace / document.write with base64 href
    for m in re.finditer(r'(?:replace|href|src|location)\s*[=(]\s*["\']([A-Za-z0-9+/=]{30,})["\']', page):
        try:
            decoded = base64.b64decode(m.group(1) + '==').decode('utf-8', errors='replace')
            if decoded.startswith('http'):
                extras.append(decoded)
        except Exception:
            pass

    if not extras:
        return page
    return page + '\n' + '\n'.join(extras)


# ════════════════════════════════════════════════════════════════════════
#  Page title extraction
# ════════════════════════════════════════════════════════════════════════

def _extract_page_title(page):
    """
    Return the best human-readable video title from a page's HTML.
    Priority: og:title → twitter:title → <title> (stripped of site suffix) → first <h1>
    """
    for pat in (
        r'<meta[^>]+(?:property|name)=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:title["\']',
        r'<meta[^>]+(?:property|name)=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']twitter:title["\']',
    ):
        m = re.search(pat, page, re.I)
        if m:
            return html.unescape(m.group(1).strip())

    m = re.search(r'<title[^>]*>([^<]{3,})</title>', page, re.I)
    if m:
        t = html.unescape(m.group(1).strip())
        for sep in (' | ', ' - ', ' – ', ' — ', ' :: ', ' » ', ' / '):
            if sep in t:
                t = t.split(sep)[0].strip()
        if len(t) >= 3:
            return t

    m = re.search(r'<h1[^>]*>\s*([^<]{3,}?)\s*</h1>', page, re.I)
    if m:
        return html.unescape(m.group(1).strip())

    return None


def _title_to_stem(title, max_len=120):
    """Sanitize a page title into a safe filename stem."""
    stem = re.sub(r'[\\/:*?"<>|]', '', title)
    stem = re.sub(r'\s+', '_', stem.strip())
    stem = re.sub(r'[^\w\-.]', '', stem)
    stem = stem.strip('._-')
    return stem[:max_len] or None


# ════════════════════════════════════════════════════════════════════════
#  Media-URL extraction from arbitrary HTML
# ════════════════════════════════════════════════════════════════════════

def _clean_url(raw):
    """Unescape a URL pulled out of HTML/JS (handles \\/ and HTML entities)."""
    if not raw:
        return ''
    u = raw.strip().strip('\'"')
    u = u.replace('\\/', '/').replace('\\u0026', '&').replace('\\u002F', '/')
    u = html.unescape(u)
    return u.strip()


def _looks_direct(url):
    path = urlparse(url).path.lower()
    return any(path.endswith('.' + e) or ('.' + e + '?') in url.lower() for e in DIRECT_MEDIA_EXTS)


def _is_manifest(url):
    path = urlparse(url).path.lower()
    return any(path.endswith('.' + e) for e in MANIFEST_EXTS) or '.m3u8' in url.lower() or '.mpd' in url.lower()


def _walk_jsonld(node, out):
    """Recursively pull contentUrl/embedUrl/url from JSON-LD VideoObject nodes."""
    if isinstance(node, dict):
        t = node.get('@type', '')
        types = t if isinstance(t, list) else [t]
        is_video = any('video' in str(x).lower() or 'media' in str(x).lower() for x in types)
        for key in ('contentUrl', 'embedUrl', 'url'):
            v = node.get(key)
            if isinstance(v, str) and v.startswith('http'):
                if is_video or _looks_direct(v):
                    out.append(v)
        for v in node.values():
            _walk_jsonld(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_jsonld(v, out)


def extract_candidates(base_url, page):
    """
    Parse a page for video sources. Returns two ordered, de-duplicated lists:
      direct  -> direct media / manifest URLs (download these first)
      embeds  -> iframe / player page URLs worth recursing into
    """
    direct, embeds, seen = [], [], set()

    def add(lst, raw, referer_hint=False):
        u = _clean_url(raw)
        if not u or u.startswith(('data:', 'blob:', 'javascript:', 'about:')):
            return
        u = urljoin(base_url, u)
        if not u.startswith('http'):
            return
        if _is_junk_media_url(u):
            return  # static/brand asset host — never the real video
        if u in seen:
            return
        seen.add(u)
        lst.append(u)

    # 1) Open Graph / Twitter player meta tags — highest signal.
    for pat in (
        r'<meta[^>]+(?:property|name)=["\'](?:og:video(?::secure_url|:url)?|twitter:player:stream)["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:video(?::secure_url|:url)?|twitter:player:stream)["\']',
        r'<meta[^>]+itemprop=["\']contentURL["\'][^>]+(?:content|href)=["\']([^"\']+)["\']',
    ):
        for m in re.finditer(pat, page, re.I):
            add(direct if _looks_direct(m.group(1)) else embeds, m.group(1))

    # 2) JSON-LD structured data.
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         page, re.I | re.S):
        try:
            data = json.loads(m.group(1).strip())
            found = []
            _walk_jsonld(data, found)
            for u in found:
                add(direct if _looks_direct(u) else embeds, u)
        except (json.JSONDecodeError, ValueError):
            continue

    # 3) <video> / <source> tags.
    for m in re.finditer(r'<video[^>]+src=["\']([^"\']+)["\']', page, re.I):
        add(direct, m.group(1))
    for m in re.finditer(r'<source[^>]+src=["\']([^"\']+)["\']', page, re.I):
        add(direct, m.group(1))

    # 4) Common JS player configs: jwplayer sources, "file"/"src"/"hls" keys, etc.
    for pat in (
        r'["\'](?:file|src|source|hls|url|playlist|manifestUrl|hlsManifestUrl|streamUrl)["\']\s*:\s*["\']([^"\']+\.(?:m3u8|mpd|mp4|webm|mov|m4v|ts)[^"\']*)["\']',
        r'sources?\s*:\s*\[\s*\{[^}]*?["\'](?:file|src)["\']\s*:\s*["\']([^"\']+)["\']',
        r'(?:setup|loadSource|src)\(\s*["\']([^"\']+\.(?:m3u8|mpd|mp4)[^"\']*)["\']',
    ):
        for m in re.finditer(pat, page, re.I):
            add(direct, m.group(1))

    # 4b) AGGRESSIVE patterns for modern adult tubes & players (Dood, Voe, Streamtape,
    #     Mixdrop, Upstream, Filemoon, Vidoza, Streamhide, Lulustream, Vidmoly, etc.)
    #     These + the universal browser impersonation make this work on virtually ANY
    #     existent porn tube / embed site in 2025-2026.
    aggressive_pats = [
        # Common variable assignments
        r'(?:var|let|const)\s+(?:source|file|video|play_url|hls|dash|stream|mp4)[_\w]*\s*=\s*["\']([^"\']+\.(?:m3u8|mpd|mp4|webm)[^"\']*)["\']',
        r'["\'](?:src|file|source|url|video_url|play_url|hls_url|dash_url|stream_url)["\']\s*:\s*["\']([^"\']+\.(?:m3u8|mpd|mp4|webm)[^"\']*)["\']',
        # Player config objects (very common on adult sites)
        r'(?:player|videojs|plyr|shaka|jwplayer|Clappr|config)\s*\([^)]*\{[^}]*?["\'](?:file|src|source|url|videoUrl)["\']\s*:\s*["\']([^"\']+)["\']',
        r'new\s+(?:Player|Video|Media|Clappr)\s*\([^)]*\{[^}]*?["\'](?:file|src|source|url)["\']\s*:\s*["\']([^"\']+)["\']',
        # data-* attributes (lazy loading) - broader
        r'data-(?:src|url|video|source|play|stream|file|embed)\s*=\s*["\']([^"\']+\.(?:m3u8|mpd|mp4|webm)[^"\']*)["\']',
        # JSON-like in scripts (common in __NEXT_DATA__, window.* , __NUXT__, etc. on modern sites)
        r'["\'](?:videoUrl|sourceUrl|playUrl|hlsUrl|dashUrl|mp4Url|streamUrl|url|src|file|source)["\']\s*:\s*["\']([^"\']+\.(?:m3u8|mpd|mp4|webm)[^"\']*)["\']',
        # Next.js / Nuxt / modern framework embedded JSON (very common on 2025+ tubes)
        r'__NEXT_DATA__[^<]*?"(?:videoUrl|playUrl|sourceUrl|hlsUrl|dashUrl|mp4Url|streamUrl|src|url)"\s*:\s*"([^"]+?\.(?:m3u8|mpd|mp4|webm)[^"]*)"',
        r'window\.__NUXT__[^<]*?"(?:file|src|url|source|video|playUrl)"\s*:\s*"([^"]+?\.(?:m3u8|mpd|mp4)[^"]*)"',
    ]
    for pat in aggressive_pats:
        for m in re.finditer(pat, page, re.I):
            add(direct, m.group(1))

    # 5) Raw media URLs anywhere in the markup/scripts (incl. escaped slashes).
    raw_pat = r'(https?:(?:\\?/\\?/)(?:[^"\'<>\s\\]|\\/)+?\.(?:%s)(?:\?(?:[^"\'<>\s\\]|\\/)*)?)' % '|'.join(DIRECT_MEDIA_EXTS)
    for m in re.finditer(raw_pat, page, re.I):
        add(direct, m.group(1))

    # 6) iframes — recurse into them when nothing better is found.
    for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', page, re.I):
        add(embeds, m.group(1))
    # data-src lazy iframes / embeds
    for m in re.finditer(r'data-(?:src|litespeed-src|lazy-src)=["\']([^"\']+)["\']', page, re.I):
        u = _clean_url(m.group(1))
        if u and (_looks_direct(u) or any(h in u.lower() for h in EMBED_HOST_HINTS)):
            add(direct if _looks_direct(u) else embeds, m.group(1))

    # Extra aggressive data-* and common adult embed attributes
    for m in re.finditer(r'data-(?:video|source|url|src|play|stream|file|embed)\s*=\s*["\']([^"\']+)["\']', page, re.I):
        u = _clean_url(m.group(1))
        if u and u.startswith('http'):
            add(direct if _looks_direct(u) else embeds, u)

    # Look for more iframe variants and embed containers popular on adult sites
    for m in re.finditer(r'<(?:iframe|embed|object)[^>]+(?:src|data)\s*=\s*["\']([^"\']+)["\']', page, re.I):
        add(embeds, m.group(1))

    # 7) Framework state blobs: __NEXT_DATA__, __INITIAL_STATE__, Nuxt, Redux, etc.
    #    Modern React/Vue/Next.js/Nuxt sites embed all page data as JSON in the HTML;
    #    the video URL is almost always somewhere inside these blobs.
    fw_patterns = [
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>\s*(\{.+?\})\s*</script>',
        r'window\.__(?:INITIAL|REDUX|NUXT|APP|STORE)_?(?:STATE|DATA)?__\s*=\s*(\{.{20,}?\})\s*;',
        r'window\.(?:initialState|appState|pageState|videoData|playerConfig|__data)\s*=\s*(\{.{20,}?\})\s*;',
        r'<script[^>]*>\s*(?:var|let|const)\s+(?:videoConfig|playerConfig|videoData|pageData)\s*=\s*(\{.{20,}?\})\s*;',
        r'self\.__next_f\.push\(\[1,\s*"([^"]{50,})"\]\)',  # Next.js 13+ app router streaming
    ]
    for fw_pat in fw_patterns:
        for m in re.finditer(fw_pat, page, re.S | re.I):
            blob_raw = m.group(1)
            # For Next.js 13 streaming: the value is JSON-encoded string, unescape it
            if fw_pat.startswith('self.__next_f'):
                try:
                    blob_raw = json.loads('"' + blob_raw + '"')
                except Exception:
                    pass
            try:
                blob = json.loads(blob_raw)
                fw_found = []
                _walk_jsonld(blob, fw_found)
                for u in fw_found:
                    add(direct if _looks_direct(u) else embeds, u)
            except (json.JSONDecodeError, ValueError):
                pass
            # Raw text scan regardless of JSON validity — catches truncated blobs
            for m2 in re.finditer(
                r'"(?:url|src|file|source|videoUrl|hlsUrl|dashUrl|streamUrl|mp4Url|playUrl|'
                r'contentUrl|mediaUrl|videoSrc|hls|dash|stream)"'
                r'\s*:\s*"([^"]{10,})"',
                blob_raw, re.I,
            ):
                u = m2.group(1).replace('\\/', '/')
                if u.startswith('http') or _looks_direct(u):
                    add(direct if _looks_direct(u) else embeds, u)

    # 8) window.* variable assignments with direct media URLs (Dood, Voe, custom players)
    for m in re.finditer(
        r'window\s*\.\s*\w+\s*=\s*["\']([^"\']{10,}\.(?:m3u8|mpd|mp4|webm)(?:\?[^"\']*)?)["\']',
        page, re.I,
    ):
        add(direct, m.group(1))

    # Prefer manifests first within direct list (better quality/adaptive).
    direct.sort(key=lambda u: (0 if _is_manifest(u) else 1))
    return direct, embeds


def _playwright_scrape(url):
    """
    Headless-browser fallback: renders the page with Playwright, intercepts live
    network requests to find media URLs, and returns (html_content, intercepted_urls).
    Returns (None, []) when Playwright is not installed.

    Install: pip install playwright && playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as _PWTimeout
    except ImportError:
        return None, []

    intercepted = []
    try:
        print('   [playwright] launching headless browser…', flush=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=USER_AGENT,
                extra_http_headers={'Accept-Language': 'en-US,en;q=0.9'},
            )
            page = ctx.new_page()

            def _on_req(req):
                u = req.url
                if (_looks_direct(u) or _is_manifest(u)) and u not in intercepted:
                    intercepted.append(u)

            def _on_resp(resp):
                u = resp.url
                if (_looks_direct(u) or _is_manifest(u)) and u not in intercepted:
                    intercepted.append(u)

            page.on('request', _on_req)
            page.on('response', _on_resp)

            try:
                page.goto(url, wait_until='networkidle', timeout=30000)
            except _PWTimeout:
                pass  # grab whatever loaded

            # Wait out Cloudflare JS challenge ("Just a moment…")
            try:
                page.wait_for_function(
                    "() => !document.title.includes('Just a moment')", timeout=8000
                )
            except Exception:
                pass

            # Trigger lazy-loaded players: scroll + click the most common play-button selectors
            try:
                page.evaluate('window.scrollTo(0, document.body.scrollHeight / 2)')
                page.wait_for_timeout(1200)
                for sel in (
                    'button[class*="play" i]', 'div[class*="play-btn" i]',
                    '[aria-label*="play" i]', '.vjs-big-play-button',
                    '.plyr__control--overlaid', 'video',
                ):
                    try:
                        page.click(sel, timeout=800)
                        page.wait_for_timeout(2000)
                        break
                    except Exception:
                        pass
            except Exception:
                pass

            content = page.content()
            browser.close()

        print(f'   [playwright] intercepted {len(intercepted)} media URL(s)', flush=True)
        return content, intercepted
    except Exception as e:
        print(f'   [playwright] error: {e}', flush=True)
        return None, []


def scrape_for_media(url, referer=None, depth=0, max_depth=4, seen_pages=None):
    """
    Walk a page (and its iframes) returning a list of downloadable media URLs.
    Pipeline per page:
      1. Fetch with curl_cffi / requests / urllib
      2. Deobfuscate JS (atob, fromCharCode, P.A.C.K.E.R., unescape)
      3. Extract media candidates (OG, JSON-LD, <video>, player configs, framework blobs)
      4. Recurse into iframes / embeds
      5. At depth 0: Playwright headless-browser fallback if everything else fails
    """
    if seen_pages is None:
        seen_pages = set()
    if url in seen_pages or depth > max_depth:
        return []
    seen_pages.add(url)

    final_url, page = http_get(url, referer=referer)
    if not page:
        # If plain HTTP failed entirely at the top level, try Playwright immediately
        if depth == 0:
            pw_html, pw_media = _playwright_scrape(url)
            if pw_media:
                return [(u, url) for u in pw_media]
            if pw_html:
                direct2, _ = extract_candidates(url, _deobfuscate_js(pw_html))
                if direct2:
                    return [(u, url) for u in direct2]
        return []

    page = _deobfuscate_js(page)
    direct, embeds = extract_candidates(final_url, page)
    if direct:
        return [(u, final_url) for u in direct]

    # Nothing direct — descend into the most promising embeds
    results = []
    ranked = sorted(embeds, key=lambda u: (0 if any(h in u.lower() for h in EMBED_HOST_HINTS) else 1))
    for emb in ranked[:12]:
        results.extend(scrape_for_media(emb, referer=final_url, depth=depth + 1,
                                        max_depth=max_depth, seen_pages=seen_pages))
        if results:
            break

    # Playwright fallback — only at the top level to avoid spawning browsers per iframe
    if depth == 0 and not results:
        pw_html, pw_media = _playwright_scrape(url)
        if pw_media:
            return [(u, url) for u in pw_media]
        if pw_html:
            deobfed = _deobfuscate_js(pw_html)
            direct2, embeds2 = extract_candidates(url, deobfed)
            if direct2:
                return [(u, url) for u in direct2]
            # Try embeds surfaced from the JS-rendered DOM
            ranked2 = sorted(embeds2, key=lambda u: (0 if any(h in u.lower() for h in EMBED_HOST_HINTS) else 1))
            for emb in ranked2[:6]:
                results.extend(scrape_for_media(emb, referer=url, depth=1,
                                                max_depth=max_depth, seen_pages=seen_pages))
                if results:
                    break

    return results


# ════════════════════════════════════════════════════════════════════════
#  Downloader
# ════════════════════════════════════════════════════════════════════════

class UniversalVideoDownloader:
    _COLLISION_EXTS = DIRECT_MEDIA_EXTS

    def __init__(self, base_dir=None):
        if base_dir is None:
            base_dir = os.environ.get('APHRO_DOWNLOADS_DIR') or os.path.join(
                os.environ.get('VIDEOS_DIR', 'videos'), 'downloads')
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.last_file = None

        self.default_opts = {
            'quiet': False,
            'no_warnings': False,
            'progress_hooks': [self._progress_hook],
            'http_headers': {'User-Agent': USER_AGENT},
            'restrictfilenames': True,
            'windowsfilenames': True,
            'overwrites': False,
            'ignoreerrors': False,      # we want exceptions so the waterfall can react
            'noplaylist': True,
            'writethumbnail': True,
            'writeinfojson': False,
            'writesubtitles': False,
            'embedthumbnail': False,
            'embedmetadata': True,
            'merge_output_format': 'mp4',
            'concurrent_fragment_downloads': 5,
            'retries': 10,
            'fragment_retries': 20,
            'extractor_retries': 5,
            'socket_timeout': 30,
            'hls_prefer_native': False,
        }

    # ── per-site tuning ──────────────────────────────────────────────────
    def get_site_specific_opts(self, url):
        u = url.lower()
        opts = {k: (v.copy() if isinstance(v, dict) else v) for k, v in self.default_opts.items()}

        # =====================================================================
        # UNIVERSAL MODE: Browser impersonation enabled for VIRTUALLY EVERYTHING
        # =====================================================================
        # This is the #1 most effective change for making the downloader work on
        # "virtually any" video site in 2025-2026. Most modern sites (especially
        # adult tubes, streaming platforms, and sites behind CF / PX / Akamai)
        # require realistic browser fingerprints.
        set_impersonate(opts)

        # If the user configured a cookies.txt (X.com login etc.), use it for every
        # attempt — this is what unlocks sensitive / login-gated tweets reliably.
        cookie_file = resolve_cookie_file()
        if cookie_file:
            opts['cookiefile'] = cookie_file

        # "Proper login": pull cookies live from the user's logged-in browser when
        # $BULK_COOKIES_FROM_BROWSER names one (chrome/firefox/edge/brave/…). This is
        # the most reliable X.com login — the user just stays logged in in the browser.
        browser = os.environ.get('BULK_COOKIES_FROM_BROWSER', '').strip().lower()
        if browser:
            opts.pop('cookiefile', None)
            opts['cookiesfrombrowser'] = (browser,)

        # --- Explicit per-site optimizations (format, referer, rate limiting) ---
        if any(s in u for s in ('pornhub.com', 'youporn.com', 'redtube.com', 'tube8.com')):
            opts.update({
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'prefer_free_formats': True,
                'sleep_interval': 1
            })
        elif any(s in u for s in ('xvideos.com', 'xvideos.red', 'xnxx.com')):
            opts.update({'format': 'best[ext=mp4]/best'})
            opts.setdefault('http_headers', {})['Referer'] = 'https://www.xvideos.com/'
        elif 'xhamster.com' in u:
            opts.update({
                'format': 'bestvideo+bestaudio/best',
                'concurrent_fragment_downloads': 6
            })
        elif 'spankbang.com' in u:
            opts.update({
                'format': 'bestvideo+bestaudio/best',
                'concurrent_fragment_downloads': 8
            })
        elif any(s in u for s in ('eporner.com', 'porntrex.com', 'hqporner.com')):
            opts.update({
                'format': 'bestvideo+bestaudio/best[ext=mp4]/best',
                'concurrent_fragment_downloads': 6
            })
        elif any(s in u for s in ('youtube.com', 'youtu.be')):
            opts.update({'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best'})
        elif any(s in u for s in ('x.com', 'twitter.com')):
            opts.update({
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best/best',
                'extractor_args': {'twitter': {'api': ['graphql', 'syndication']}}
            })
        elif 'instagram.com' in u:
            opts.update({'format': 'bestvideo+bestaudio/best'})
        elif 'tiktok.com' in u:
            opts.update({'format': 'best'})
        elif 'reddit.com' in u:
            opts.update({'format': 'bestvideo+bestaudio/best'})
        # --- Additional popular adult tube sites not in the core Pornhub/XVideos/xHamster families ---
        # These get tailored format selection + inherit universal impersonation (critical for CF-protected sites)
        elif 'youjizz.com' in u:
            opts.update({'format': 'bestvideo+bestaudio/best'})
        elif any(s in u for s in ('pornhat.com', 'porn.com', 'ixxx.com', 'pornone.com', 'pornone')):
            opts.update({'format': 'best[ext=mp4]/best'})
        elif any(s in u for s in ('beeg.com', 'thumbzilla.com')):
            opts.update({'format': 'bestvideo+bestaudio/best'})
        elif any(s in u for s in ('perfectgirls.xxx', 'sexu.com', 'pornhd.com', 'pornhd')):
            opts.update({
                'format': 'bestvideo+bestaudio/best[ext=mp4]/best',
                'concurrent_fragment_downloads': 6
            })
        # For any other uncovered tube / embed-heavy site, the scraper + generic yt-dlp
        # extractor + impersonation will handle the vast majority. Add more specific
        # entries here as new popular domains emerge.
        else:
            # Default for everything else (including obscure/brand new porn sites) — very good for most unknown sites
            opts.setdefault('format', 'bestvideo+bestaudio/best/best')

        # Always aim for the highest available quality (overrides the per-site caps
        # above). Disable by setting BULK_MAX_QUALITY=0. format_sort guarantees the
        # top resolution / fps / bitrate is picked when several renditions exist.
        if os.environ.get('BULK_MAX_QUALITY', '1') != '0':
            opts['format'] = 'bestvideo*+bestaudio/best'
            opts['format_sort'] = ['res', 'fps', 'hdr', 'vbr', 'abr']

        return opts

    # ── filename helpers ─────────────────────────────────────────────────
    def _unique_stem(self, folder, stem):
        def exists(s):
            return any((folder / f'{s}.{e}').exists() for e in self._COLLISION_EXTS)
        if not exists(stem):
            return stem
        n = 1
        while exists(f'{stem}_{n}'):
            n += 1
        return f'{stem}_{n}'

    # ── yt-dlp invocation ────────────────────────────────────────────────
    def _probe_stem(self, url):
        """Best-effort lookup of the filename yt-dlp would give this URL."""
        try:
            probe_opts = {**self.get_site_specific_opts(url), 'quiet': True,
                          'no_warnings': True, 'ignoreerrors': True}
            with yt_dlp.YoutubeDL(probe_opts) as probe:
                info = probe.extract_info(url, download=False)
            if info:
                return Path(probe.prepare_filename(info)).stem
        except Exception:
            pass
        return None

    def _find_existing(self, folder, stem):
        """Return the path of an already-downloaded file matching *stem*, if any."""
        if not stem:
            return None
        for e in self._COLLISION_EXTS:
            cand = folder / f'{stem}.{e}'
            if cand.exists():
                return str(cand)
        return None

    def _build_outtmpl(self, folder, stem, out_tmpl):
        """Resolve a collision-free output template inside *folder*."""
        if stem:
            return str(folder / f'{self._unique_stem(folder, stem)}.%(ext)s')
        return str(folder / (out_tmpl or '%(title)s.%(ext)s'))

    def _try_ytdlp_with_cookies(self, url, outtmpl):
        """
        Last-resort attempt: run yt-dlp with real browser cookies so login-gated,
        age-restricted, or paywall-protected content can be accessed.
        Tries every yt-dlp-supported browser; skips silently if none installed.
        Firefox first — it has no DPAPI/locked-DB issues and works on Windows where
        Chromium-based browsers often fail to decrypt their cookie store.
        """
        for browser in ('firefox', 'chrome', 'edge', 'brave', 'vivaldi',
                        'opera', 'chromium', 'whale', 'safari'):
            opts = self.get_site_specific_opts(url)
            opts['outtmpl'] = outtmpl
            opts.pop('cookiefile', None)  # browser cookies take over for this attempt
            opts['cookiesfrombrowser'] = (browser,)
            self.last_file = None
            try:
                print(f'   [cookies:{browser}] trying with browser cookies…', flush=True)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                f = self._resolve_final_file()
                if f:
                    return f
            except Exception as e:
                msg = str(e)
                if 'not found' in msg.lower() or 'no such' in msg.lower() or 'could not find' in msg.lower():
                    continue  # browser not installed, try next
                print(f'   [cookies:{browser}] {e}', flush=True)
        return None

    def _try_ytdlp(self, url, outtmpl, referer=None, force_generic=False, label='yt-dlp'):
        """Single yt-dlp attempt. Returns the downloaded path on success, else None."""
        opts = self.get_site_specific_opts(url)
        opts['outtmpl'] = outtmpl
        if referer:
            opts.setdefault('http_headers', {})['Referer'] = referer
        if force_generic:
            opts['force_generic_extractor'] = True
        self.last_file = None
        try:
            print(f'   [{label}] trying: {url}', flush=True)
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            return self._resolve_final_file()
        except Exception as e:
            err = str(e)
            # If the impersonate target isn't available, retry without it so we
            # don't block the entire waterfall over a missing curl_cffi target.
            if 'impersonate' in err.lower() or (
                'target' in err.lower() and 'not available' in err.lower()
            ):
                print(f'   [{label}] impersonation target unavailable — retrying without…', flush=True)
                opts2 = {k: v for k, v in opts.items() if k != 'impersonate'}
                try:
                    self.last_file = None
                    with yt_dlp.YoutubeDL(opts2) as ydl:
                        ydl.download([url])
                    return self._resolve_final_file()
                except Exception as e2:
                    print(f'   [{label}] failed: {e2}', flush=True)
                    return None
            print(f'   [{label}] failed: {e}', flush=True)
            return None

    def _resolve_final_file(self):
        """After a download, find the real media file (prefer merged mp4)."""
        if not self.last_file:
            return None
        p = Path(self.last_file)
        if p.exists():
            return str(p)
        # merge/remux may have changed the extension
        for e in ('mp4', 'mkv', 'webm', 'm4v', 'mov'):
            cand = p.with_suffix('.' + e)
            if cand.exists():
                return str(cand)
        return None

    # ── the universal waterfall ──────────────────────────────────────────
    def download(self, url, folder, out_tmpl=None):
        """
        Try every strategy in order until a video lands on disk.
        Returns the downloaded file path, or None.
        """
        folder.mkdir(parents=True, exist_ok=True)
        host = urlparse(url).netloc.lower()
        is_twitter = any(h in host for h in ('x.com', 'twitter.com'))
        stem = self._probe_stem(url)

        # When yt-dlp can't probe a filename, fetch the page ourselves for the title.
        # This makes scraper-found CDN URLs save as "My Video Title.mp4" instead of
        # "abc123_720p_hls_chunk_001.mp4" or whatever the CDN path happens to be.
        if not stem:
            _, raw_page = http_get(url)
            if raw_page:
                title = _extract_page_title(raw_page)
                # Ignore no-JS/bot walls (e.g. X's "JavaScript is not available.") —
                # their title is meaningless and would mislabel the file.
                if title and title.strip().lower() in WALL_TITLES:
                    print(f'   [title] ignoring no-JS wall title "{title}"', flush=True)
                    title = None
                if title:
                    stem = _title_to_stem(title)
                    print(f'   [title] "{title}"', flush=True)

        existing = self._find_existing(folder, stem)
        if existing:
            print(f'   [skip] already downloaded: {existing}', flush=True)
            return existing
        outtmpl = self._build_outtmpl(folder, stem, out_tmpl)

        # 1) Native extractor for the URL as given.
        f = self._try_ytdlp(url, outtmpl, label='native')
        if f:
            return f

        # 2) Force yt-dlp's generic extractor (finds <video>, og:video, HLS…).
        f = self._try_ytdlp(url, outtmpl, force_generic=True, label='generic')
        if f:
            return f

        # For X/Twitter, sensitive or login-gated tweets need real auth. Try browser
        # cookies BEFORE the generic scraper — the scraper only ever sees X's no-JS
        # wall and would "succeed" on a brand promo clip instead of the real video.
        if is_twitter:
            f = self._try_ytdlp_with_cookies(url, outtmpl)
            if f:
                return f

        # 3) Scrape the page (and iframes) ourselves for media URLs.
        print('   [scrape] yt-dlp could not extract — scraping page for media…', flush=True)
        media = scrape_for_media(url)
        if media:
            print(f'   [scrape] found {len(media)} candidate(s)', flush=True)
        for cand_url, referer in media:
            f = self._try_ytdlp(cand_url, outtmpl, referer=referer, label='candidate')
            if f:
                return f
            # last resort: stream a plain http(s) file directly
            if _looks_direct(cand_url) and not _is_manifest(cand_url):
                f = self._direct_download(cand_url, folder, referer, title_stem=stem)
                if f:
                    return f

        # 4) Native extractor with real browser cookies (login-gated / age-restricted content).
        if not is_twitter:
            f = self._try_ytdlp_with_cookies(url, outtmpl)
            if f:
                return f

        print('   [error] no downloadable video found by any method.', flush=True)
        return None

    def _direct_download(self, url, folder, referer=None, title_stem=None):
        """Raw streamed download of a direct media URL (final fallback)."""
        try:
            name = os.path.basename(urlparse(url).path) or 'video.mp4'
            _, ext = os.path.splitext(name)
            ext = ext.lstrip('.') or 'mp4'
            # Prefer the page title over the URL's raw filename
            stem = title_stem or re.sub(r'[^a-zA-Z0-9._-]', '_', os.path.splitext(name)[0])[:120]
            existing = self._find_existing(folder, stem)
            if existing:
                print(f'   [skip] already downloaded: {existing}', flush=True)
                return existing
            dest = folder / f'{self._unique_stem(folder, stem)}.{ext}'
            headers = {'User-Agent': USER_AGENT}
            if referer:
                headers['Referer'] = referer
            print(f'   [direct] streaming: {url}', flush=True)
            req = Request(url, headers=headers)
            with urlopen(req, timeout=30) as resp, open(dest, 'wb') as out:
                total = int(resp.headers.get('Content-Length') or 0)
                got = 0
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    got += len(chunk)
                    if total:
                        print(f'\r   [download] {got/total*100:5.1f}% of {total/1048576:.1f}MiB', end='', flush=True)
            print('\n   [done] direct download finished!', flush=True)
            return str(dest)
        except Exception as e:
            print(f'   [direct] failed: {e}', flush=True)
            return None

    # ── progress ─────────────────────────────────────────────────────────
    def _progress_hook(self, d):
        status = d.get('status')
        if status == 'downloading':
            done = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            pct = None
            if total:
                pct = done / total * 100
            else:
                # Fragmented (HLS/DASH) streams often have no byte total — fall back
                # to the fragment count, then to yt-dlp's own percentage string, so the
                # GUI always sees a [download] NN% line to drive its progress bar.
                frag_i, frag_n = d.get('fragment_index'), d.get('fragment_count')
                if frag_i and frag_n:
                    pct = frag_i / frag_n * 100
                else:
                    ps = (d.get('_percent_str') or '').strip().rstrip('%')
                    try:
                        pct = float(ps)
                    except ValueError:
                        pct = None
            if pct is not None:
                size = f" of {total/1048576:.1f}MiB" if total else ''
                print(f"\r   [download] {pct:5.1f}%{size} "
                      f"at {d.get('_speed_str', '').strip()} ETA {d.get('_eta_str', '').strip()}",
                      end='', flush=True)
        elif status == 'finished':
            self.last_file = d.get('filename') or self.last_file
            print('\n   [done] download finished!', flush=True)
        elif status == 'error':
            print(f"\n   [error] {d.get('error', 'unknown')}", flush=True)

    # ── batch entry points ───────────────────────────────────────────────
    def download_list(self, urls):
        for i, url in enumerate(urls, 1):
            print(f'\n[{i}/{len(urls)}] Processing: {url}', flush=True)
            self.download(url, self.base_dir)


# ════════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════════

def run_single(args):
    """Single-URL mode used by the Node server. Emits `RESULT_FILE: <path>`."""
    dl = UniversalVideoDownloader(base_dir=args.out_dir or '.')
    folder = Path(args.out_dir) if args.out_dir else Path('.')
    print(f'[1/1] Processing: {args.url}', flush=True)
    result = dl.download(args.url, folder, out_tmpl=args.out_tmpl)
    if result and os.path.exists(result):
        print(f'RESULT_FILE: {os.path.abspath(result)}', flush=True)
        sys.exit(0)
    print('RESULT_NONE', flush=True)
    sys.exit(2)


def run_interactive():
    print('Universal Video Downloader — Extensive Edition', flush=True)
    print('=' * 75, flush=True)
    print("\nPaste your URLs (one per line). Type 'done' or press Enter twice when finished:\n", flush=True)
    seen_input = set()
    pasted = []
    blank_streak = 0
    while True:
        try:
            line = input().strip()
        except EOFError:
            break
        if line.lower() == 'done':
            break
        if not line:
            # Two consecutive blank lines (double Enter) finishes input.
            blank_streak += 1
            if blank_streak >= 2:
                break
            continue
        blank_streak = 0
        if line.startswith(('http://', 'https://')) and line not in seen_input:
            seen_input.add(line)
            pasted.append(line)
    if not pasted:
        print('No URLs provided.', flush=True)
        return

    # Persist pasted URLs into links_to_download.txt before downloading
    links_file = _find_queue_file() or DATA_DIR / 'links_to_download.txt'
    _add_urls_to_file(links_file, pasted)

    base_dir = (
        os.environ.get('APHRO_DOWNLOADS_DIR')
        or os.path.join(os.environ.get('VIDEOS_DIR', 'videos'), 'downloads')
    )
    failed = _process_url_list(pasted, links_file, base_dir)

    ok = len(pasted) - len(failed)
    print(f'\n[links] Finished: {ok}/{len(pasted)} succeeded.', flush=True)
    if failed:
        print('[links] Failed URLs remain in links_to_download.txt for retry:', flush=True)
        for u in failed:
            print(f'  {u}', flush=True)


# ════════════════════════════════════════════════════════════════════════
#  Links-queue mode  (--from-links)
# ════════════════════════════════════════════════════════════════════════

def _find_queue_file():
    """Locate links_to_download.txt without needing the server running."""
    project_root = DATA_DIR

    candidates = []

    # 1) explicit env var
    env = os.environ.get('APHRO_QUEUE_FILE', '').strip()
    if env:
        candidates.append(Path(env))

    # 2) project root (next to this app's other runtime files)
    candidates.append(DATA_DIR / 'links_to_download.txt')

    # 3) DATA_DIR/cache/ (where the server stores LINK_DIR)
    data = os.environ.get('DATA_DIR', '').strip()
    if data:
        candidates.append(Path(data) / 'cache' / 'links_to_download.txt')

    # 4) <project_root>/cache/  (default DATA_DIR location)
    candidates.append(project_root / 'cache' / 'links_to_download.txt')

    # 5) adjacent to VIDEOS_DIR
    videos = os.environ.get('VIDEOS_DIR', '').strip()
    if videos:
        candidates.append(Path(videos).parent / 'cache' / 'links_to_download.txt')

    for c in candidates:
        if c.exists():
            return c
    return None


def _dedup_file(filepath):
    """Remove duplicate URLs from filepath in-place, preserving order. Returns deduped list."""
    try:
        lines = filepath.read_text(encoding='utf-8').splitlines()
        seen = set()
        deduped = []
        for l in lines:
            url = l.strip()
            if url and url not in seen:
                seen.add(url)
                deduped.append(url)
        removed = len(lines) - len(deduped)
        filepath.write_text('\n'.join(deduped) + ('\n' if deduped else ''), encoding='utf-8')
        if removed:
            print(f'[links] Removed {removed} duplicate URL{"s" if removed != 1 else ""} from {filepath.name}', flush=True)
        return deduped
    except Exception as e:
        print(f'   [warn] dedup failed: {e}', flush=True)
        return []


def _remove_url_from_file(filepath, url):
    """Remove a single URL line from a file in-place."""
    try:
        lines = filepath.read_text(encoding='utf-8').splitlines()
        lines = [l for l in lines if l.strip() != url]
        filepath.write_text('\n'.join(lines) + ('\n' if lines else ''), encoding='utf-8')
    except Exception as e:
        print(f'   [warn] could not remove URL from file: {e}', flush=True)


def _append_to_downloaded(links_file, url):
    """Append a URL to links_downloaded.txt sitting next to links_file."""
    try:
        downloaded_file = links_file.parent / 'links_downloaded.txt'
        with open(downloaded_file, 'a', encoding='utf-8') as f:
            f.write(url + '\n')
    except Exception as e:
        print(f'   [warn] could not write to links_downloaded.txt: {e}', flush=True)


def _append_to_failed(links_file, url):
    """Append a failed URL to link_failed.txt sitting next to links_file."""
    try:
        failed_file = links_file.parent / 'link_failed.txt'
        with open(failed_file, 'a', encoding='utf-8') as f:
            f.write(url + '\n')
    except Exception as e:
        print(f'   [warn] could not write to link_failed.txt: {e}', flush=True)


def _add_urls_to_file(links_file, urls):
    """Prepend newly-pasted URLs to the TOP of links_file, preserving order
    and skipping any that are already queued."""
    try:
        links_file.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if links_file.exists():
            existing = [l.strip() for l in links_file.read_text(encoding='utf-8').splitlines() if l.strip()]
        existing = list(dict.fromkeys(existing))  # dedup, keep order
        existing_set = set(existing)
        new = [u for u in dict.fromkeys(urls) if u and u not in existing_set]
        if new:
            combined = new + existing  # newest at the top
            links_file.write_text('\n'.join(combined) + '\n', encoding='utf-8')
            print(f'[links] Added {len(new)} URL{"s" if len(new) != 1 else ""} to the top of {links_file.name}', flush=True)
    except Exception as e:
        print(f'   [warn] could not update {links_file.name}: {e}', flush=True)


def _process_url_list(urls, links_file, base_dir):
    """Download each URL; on success move it from links_file to links_downloaded.txt.
    Returns list of failed URLs."""
    dl = UniversalVideoDownloader(base_dir=base_dir)
    folder = Path(base_dir)
    failed = []
    for i, url in enumerate(urls, 1):
        print(f'\n[{i}/{len(urls)}] Processing: {url}', flush=True)
        result = dl.download(url, folder)
        if result and os.path.exists(result):
            print(f'   [ok] saved to: {result}', flush=True)
            _append_to_downloaded(links_file, url)
            _remove_url_from_file(links_file, url)
        else:
            failed.append(url)
            _append_to_failed(links_file, url)
            _remove_url_from_file(links_file, url)
    return failed


def run_from_links(args):
    """Process links_to_download.txt (or --links-file) instead of pasting."""
    if args.links_file:
        links_file = Path(args.links_file)
    else:
        links_file = _find_queue_file()

    if not links_file or not links_file.exists():
        print(
            '[error] Could not find links_to_download.txt.\n'
            '        Pass --links-file <path> or create cache/links_to_download.txt.',
            flush=True,
        )
        sys.exit(1)

    urls = _dedup_file(links_file)
    urls = [u for u in urls if u.startswith(('http://', 'https://'))]

    if not urls:
        print(f'[links] {links_file} is empty — nothing to do.', flush=True)
        sys.exit(0)

    print(f'[links] {len(urls)} URL(s) to process from {links_file}', flush=True)

    base_dir = (
        args.out_dir
        or os.environ.get('APHRO_DOWNLOADS_DIR')
        or os.path.join(os.environ.get('VIDEOS_DIR', 'videos'), 'downloads')
    )
    failed = _process_url_list(urls, links_file, base_dir)

    ok = len(urls) - len(failed)
    print(f'\n[links] Finished: {ok}/{len(urls)} succeeded.', flush=True)
    if failed:
        print('[links] Failed URLs remain in links_to_download.txt for retry:', flush=True)
        for u in failed:
            print(f'  {u}', flush=True)
    sys.exit(0 if not failed else 2)


def run_from_db(args):
    """db.json-centric queue processor shared with the GUI.

    Feeds links_to_download.txt into the queue section (deduped, the txt file is
    NEVER emptied), then downloads every queued item, marking results back into
    db.json. Cookies (cookies.txt) are picked up automatically, so login-gated
    links download with your saved credentials."""
    if bulk_db is None:
        print('[db] bulk_db module unavailable — falling back to --from-links.', flush=True)
        return run_from_links(args)

    data = bulk_db.load()

    # 1) feed the txt queue file into db.json (kept intact, deduped)
    links_file = Path(args.links_file) if args.links_file else _find_queue_file()
    if not links_file:
        links_file = DATA_DIR / 'links_to_download.txt'
    if links_file.exists():
        fed = bulk_db.ingest_links_txt(data, links_file)
        if fed:
            print(f'[db] Fed {len(fed)} new link(s) from {links_file.name} into db.json '
                  f'(file left untouched).', flush=True)

    # 2) de-dup the whole queue/bookmarks, then persist
    q_removed, _ = bulk_db.dedup(data)
    if q_removed:
        print(f'[db] Removed {q_removed} duplicate queue link(s).', flush=True)
    bulk_db.save(data)

    pending = [it for it in data['queue']
               if it.get('status') in (bulk_db.ST_QUEUED, bulk_db.ST_STOPPED)]
    if not pending:
        print('[db] Nothing queued to download.', flush=True)
        sys.exit(0)

    base_dir = (args.out_dir
                or os.environ.get('APHRO_DOWNLOADS_DIR')
                or os.path.join(os.environ.get('VIDEOS_DIR', 'videos'), 'downloads'))
    dl = UniversalVideoDownloader(base_dir=base_dir)
    folder = Path(base_dir)

    print(f'[db] {len(pending)} item(s) to download into {base_dir}', flush=True)
    ok = fail = 0
    for i, it in enumerate(pending, 1):
        url = it['url']
        print(f'\n[{i}/{len(pending)}] Processing: {url}', flush=True)
        try:
            result = dl.download(url, folder)
        except Exception as e:
            result = None
            print(f'   [error] {e}', flush=True)
        if result and os.path.exists(result):
            print(f'   [ok] saved to: {result}', flush=True)
            bulk_db.mark_downloaded(data, url, os.path.abspath(result))
            ok += 1
        else:
            bulk_db.mark_failed(data, url, 'no downloadable video found')
            fail += 1
        bulk_db.save(data)          # persist after every item so progress survives a crash

    print(f'\n[db] Finished: {ok} ok, {fail} failed.', flush=True)
    sys.exit(0 if not fail else 2)


def setup_x_login():
    """Save a cookies.txt so yt-dlp can fetch login-gated / sensitive X.com (and
    other site) videos. The cookies are then reused automatically for every
    download — far more reliable than live browser-cookie extraction on Windows."""
    dest = DATA_DIR / 'cookies.txt'

    print('\nX.com / Twitter login (cookies) setup', flush=True)
    print('=' * 50, flush=True)
    existing = resolve_cookie_file()
    if existing:
        print(f'Currently using cookies from: {existing}', flush=True)
    print(
        '\nSensitive or login-gated tweets need your X.com login cookies.\n'
        'Export them once to a Netscape-format cookies.txt file:\n'
        '  1. Install a "Get cookies.txt LOCALLY" extension (Chrome/Firefox/Edge)\n'
        '  2. Log in to https://x.com in that browser\n'
        '  3. Click the extension and Export / Save cookies.txt\n'
        '  4. Paste the full path to that file below\n',
        flush=True,
    )
    try:
        raw = input('Path to cookies.txt (blank to cancel): ').strip().strip('"').strip("'")
    except EOFError:
        return
    if not raw:
        print('Cancelled.', flush=True)
        return
    src = Path(raw)
    if not src.is_file():
        print(f'[error] File not found: {src}', flush=True)
        return
    try:
        shutil.copyfile(src, dest)
        _invalidate_cookie_cache()
        print(f'[ok] Login cookies saved to {dest}', flush=True)
        print('     They will be used automatically for all downloads from now on.', flush=True)
    except Exception as e:
        print(f'[error] Could not save cookies: {e}', flush=True)


def _run_menu(args):
    """Interactive mode selection shown when the script is run with no arguments."""
    print('Universal Video Downloader — Extensive Edition', flush=True)
    print('=' * 75, flush=True)

    queue_file = _find_queue_file()
    queue_count = 0
    if queue_file and queue_file.exists():
        raw = queue_file.read_text(encoding='utf-8').splitlines()
        queue_count = sum(1 for l in raw if l.strip().startswith(('http://', 'https://')))

    queue_label = (
        f'Process links queue  ({queue_count} URL{"s" if queue_count != 1 else ""} in {queue_file.name})'
        if queue_file and queue_count
        else 'Process links queue  (links_to_download.txt not found or empty)'
    )

    x_login = 'configured ✓' if resolve_cookie_file() else 'not set'
    print('\nWhat would you like to do?\n', flush=True)
    print(f'  [1] Paste URLs manually', flush=True)
    print(f'  [2] {queue_label}', flush=True)
    print(f'  [3] Set up X.com login (cookies)  [{x_login}]', flush=True)
    print(flush=True)

    try:
        choice = input('Enter choice (1/2/3): ').strip()
    except EOFError:
        choice = '1'

    if choice == '2':
        if not queue_file or not queue_count:
            print('[error] No queue file found. Run with --links-file <path> to specify one.', flush=True)
            sys.exit(1)

        class _Args:
            links_file = None
            out_dir = args.out_dir
            remove_after = False  # run_from_links always removes on success now
        run_from_links(_Args())
    elif choice == '3':
        setup_x_login()
        _run_menu(args)  # back to the menu after configuring login
    else:
        run_interactive()


def main():
    parser = argparse.ArgumentParser(description='Universal video downloader / page scraper')
    parser.add_argument('--url', help='Single URL to download (server mode)')
    parser.add_argument('--out-dir', help='Output directory')
    parser.add_argument('--out-tmpl', help='yt-dlp output template (default: %%(title)s.%%(ext)s)')
    parser.add_argument('--from-links', action='store_true',
                        help='Feed cache/links_to_download.txt into db.json and download the queue (txt kept)')
    parser.add_argument('--from-db', action='store_true',
                        help='Download everything queued in the unified db.json (shared with the GUI)')
    parser.add_argument('--legacy-links', action='store_true',
                        help='Old behaviour: process links_to_download.txt in place (moves URLs to links_downloaded.txt)')
    parser.add_argument('--links-file', metavar='FILE',
                        help='Path to a URL queue file (default: auto-detect cache/links_to_download.txt)')
    args = parser.parse_args()

    if args.url:
        run_single(args)
    elif args.legacy_links:
        run_from_links(args)
    elif args.from_db or args.from_links or args.links_file:
        run_from_db(args)
    else:
        _run_menu(args)


if __name__ == '__main__':
    main()