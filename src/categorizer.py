#!/usr/bin/env python3
"""
AphroArchive Auto-Categorizer — standalone CLI + shared engine for the GUI panel.

Scans the *downloads* folder and sorts loose video files into per-category
subfolders. The categories (and which ones are "active") come from the shared
``db.json``: every category carries a 0..3 *star* rating, and only starred
categories get a folder created and used as a move target (a higher rating also
wins ties when a filename matches more than one category). Extra match *tags*
per category live in the same db.json entry.

Flow: derive match terms from the starred categories → dry-run the move plan →
print it → physically move the files on confirmation.

Usage:
  python categorizer.py                      # categorize the default downloads folder
  python categorizer.py <downloads_dir>      # categorize a specific folder
  python categorizer.py --seed               # import the bundled category presets
  python categorizer.py --yes                # apply without the confirm prompt

The GUI (bulkdownloader_gui.py) imports this module and reuses
``build_plan`` / ``apply_plan`` directly so both share one matching engine.
"""

import sys
import os
import re
import json
import shutil
import argparse
from pathlib import Path
from typing import Optional

# Shared db.json layer — lives next to this module. Make the import work whether
# we are run as a script from src/ or imported by the GUI/frozen bundle.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import bulk_db  # noqa: E402

VIDEO_EXTS = {
    '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv',
    '.webm', '.m4v', '.ts', '.m2ts', '.mpg', '.mpeg',
}

MIN_SCORE = 50    # minimum term_score to accept a category match
STAR_BONUS = 6    # score added per star, so higher-rated categories win ties

# Folders the auto-categorizer must never treat as a category or move out of.
RESERVED_DIRS = {'downloads', 'download', '.part', 'tmp'}


# ── Matching logic (mirrors the web CategorizerView) ──────────────────────────

def levenshtein(a: str, b: str) -> int:
    m, n = len(a), len(b)
    if not m: return n
    if not n: return m
    row = list(range(n + 1))
    for i in range(1, m + 1):
        diag = row[0]
        row[0] = i
        for j in range(1, n + 1):
            tmp    = row[j]
            cost   = 0 if a[i - 1] == b[j - 1] else 1
            row[j] = min(row[j] + 1, row[j - 1] + 1, diag + cost)
            diag   = tmp
    return row[n]


def normalize(s: str) -> str:
    s = (s or '').lower()
    s = re.sub(r'[._\-/\\]+', ' ', s)
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def term_score(words: list, joined: str, term: str) -> int:
    if not term:
        return 0
    if ' ' in term:
        return 100 if term in joined else 0
    best = 0
    for w in words:
        if w == term:
            return 100
        if len(term) >= 3 and term in w:
            best = max(best, 78)
        elif len(w) >= 4 and w in term:
            best = max(best, 58)
        elif len(term) >= 4 and len(w) >= 4:
            ratio = 1 - levenshtein(w, term) / max(len(w), len(term))
            if ratio >= 0.8:
                best = max(best, round(ratio * 68))
    return best


def best_folder(folder_terms: list, name: str, current_path: str = '') -> Optional[dict]:
    """Return {'path', 'matched', 'score'} for the best target category, or None.

    A different folder wins only if its score strictly exceeds the current
    folder's score, so a file already in a well-matching folder is left alone.
    The star rating is folded into each candidate's total, so among equally good
    text matches the more-starred category wins.
    """
    joined = normalize(re.sub(r'\.[^.]+$', '', name))
    words  = [w for w in joined.split() if w]
    if not words:
        return None

    current_total = 0
    best_path, best_total, best_term = '', 0, ''

    for f in folder_terms:
        f_score, f_term = 0, ''
        for t in f['terms']:
            s = term_score(words, joined, t)
            if s > f_score:
                f_score, f_term = s, t
        if f_score < MIN_SCORE:
            continue
        total = f_score + f.get('depth', 1) * 4 + int(f.get('stars', 0)) * STAR_BONUS
        if f['path'] == current_path:
            current_total = total
            continue  # never pick the current folder as the winner
        if total > best_total:
            best_total, best_path, best_term = total, f['path'], f_term

    if best_path and best_total > current_total:
        return {'path': best_path, 'matched': best_term, 'score': best_total}
    return None


# ── Filesystem scanning ───────────────────────────────────────────────────────

def scan_videos(downloads_dir: Path) -> list:
    """Return [{name, abs_path, cat_path}] for every video file under the folder."""
    videos = []
    for root, dirs, files in os.walk(downloads_dir):
        dirs.sort()
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for fname in sorted(files):
            if Path(fname).suffix.lower() not in VIDEO_EXTS:
                continue
            abs_path = Path(root) / fname
            rel_dir  = Path(root).relative_to(downloads_dir)
            cat_path = rel_dir.as_posix() if rel_dir != Path('.') else ''
            videos.append({'name': fname, 'abs_path': abs_path, 'cat_path': cat_path})
    return videos


def scan_subfolders(downloads_dir: Path) -> list:
    """Existing immediate subfolders (one level) — used as a fallback set of
    categories when nothing is starred in db.json."""
    out = []
    try:
        for p in sorted(downloads_dir.iterdir()):
            if p.is_dir() and not p.name.startswith('.') and p.name.lower() not in RESERVED_DIRS:
                out.append(p.name)
    except OSError:
        pass
    return out


# ── Build folder terms from db.json categories ────────────────────────────────

def folder_terms_from_categories(categories: dict) -> list:
    """Turn a {name: {stars, tags}} map into the scored-matcher shape. The
    category *name* is always a term; its tags are added as extra terms."""
    result = []
    for name, entry in categories.items():
        entry = entry if isinstance(entry, dict) else {}
        tags  = [normalize(t) for t in entry.get('tags', [])]
        terms = [normalize(name)] + tags
        terms = [t for t in terms if t]
        if not terms:
            continue
        result.append({
            'path':  name,
            'depth': 1,
            'stars': int(entry.get('stars', 0)),
            'terms': terms,
        })
    return result


def folder_terms_from_subfolders(downloads_dir: Path, categories: dict) -> list:
    """Fallback matcher set: every existing subfolder is a category, enriched
    with any tags recorded for it in db.json."""
    result = []
    for name in scan_subfolders(downloads_dir):
        entry = categories.get(name) if isinstance(categories.get(name), dict) else {}
        tags  = [normalize(t) for t in (entry or {}).get('tags', [])]
        terms = [normalize(name)] + tags
        terms = [t for t in terms if t]
        if not terms:
            continue
        result.append({'path': name, 'depth': 1,
                       'stars': int((entry or {}).get('stars', 0)), 'terms': terms})
    return result


def build_plan(videos: list, folder_terms: list) -> list:
    """Return the list of proposed moves: each item from scan_videos augmented
    with 'to_path' (destination category) and 'matched'/'score'."""
    moves = []
    for v in videos:
        hit = best_folder(folder_terms, v['name'], current_path=v['cat_path'])
        if not hit:
            continue
        moves.append({**v, 'to_path': hit['path'], 'matched': hit['matched'], 'score': hit['score']})
    return moves


# ── Apply ─────────────────────────────────────────────────────────────────────

def apply_plan(moves: list, downloads_dir: Path, log=print):
    """Physically move each planned file, creating destination folders as needed.
    Returns (done, failed)."""
    done = failed = 0
    for m in moves:
        dest_dir = downloads_dir / m['to_path']
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            failed += 1
            log(f'  x  {m["name"][:60]}  -- {e}')
            continue
        dest = dest_dir / m['name']
        if dest.exists() and dest != m['abs_path']:          # avoid clobbering
            stem, suffix, n = dest.stem, dest.suffix, 1
            while dest.exists():
                dest = dest_dir / f'{stem}_{n}{suffix}'
                n += 1
        try:
            shutil.move(str(m['abs_path']), str(dest))
            done += 1
            log(f'  v  {m["name"][:60]}  ->  {m["to_path"]}')
        except (OSError, shutil.Error) as e:
            failed += 1
            log(f'  x  {m["name"][:60]}  -- {e}')
    return done, failed


# ── Category presets (bundled assets/categories.json) ─────────────────────────

def load_preset_categories() -> dict:
    """The bundled {name: {displayName, tags}} preset map, for --seed."""
    candidates = [
        _HERE.parent / 'assets' / 'categories.json',           # running from src/
        _HERE / 'categories.json',                              # next to this module
        Path(getattr(sys, '_MEIPASS', _HERE)) / 'categories.json',  # frozen bundle
    ]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            continue
    return {}


def default_downloads_dir() -> Path:
    """The folder the categorizer sorts by default — the shared downloads dir,
    matching where bulkdownloader writes finished files."""
    env = os.environ.get('BULK_OUT_DIR', '').strip()
    if env:
        return Path(env)
    return bulk_db.DATA_DIR / 'downloads'


# ── Console print ─────────────────────────────────────────────────────────────

def print_stars(categories: dict):
    starred = bulk_db.starred_categories({'categories': categories})
    if not starred:
        print('No starred categories — nothing will be created. '
              'Star some in the GUI (Categorize tab) or run with --seed and edit db.json.')
        return
    print(f'\nActive categories ({len(starred)}):')
    for name in sorted(starred):
        s = int(starred[name].get('stars', 0))
        print(f'  {"*" * s:<3}  {name}')


def print_plan(moves: list):
    if not moves:
        print('\nNo moves needed — every video is already in its best-matching folder.')
        return

    uncategorized = [m for m in moves if not m['cat_path']]
    recategorized = [m for m in moves if m['cat_path']]

    def _section(section_moves: list, header: str):
        if not section_moves:
            return
        by_dest: dict = {}
        for m in section_moves:
            by_dest.setdefault(m['to_path'], []).append(m)
        print(f'\n  {header} ({len(section_moves)})\n')
        for dest, ms in sorted(by_dest.items(), key=lambda x: x[0]):
            print(f'  -> {dest}')
            for m in ms:
                short = re.sub(r'\.[^.]+$', '', m['name'])
                if len(short) > 58:
                    short = short[:55] + '...'
                from_label = m['cat_path'] or 'root'
                print(f'      {short}')
                print(f'        from: {from_label}   matched: "{m["matched"]}"  score: {m.get("score", "?")}')
            print()

    print(f'\n{"-" * 64}')
    print(f'  {len(moves)} video(s) would be moved')
    _section(uncategorized, 'Uncategorized -> folder')
    _section(recategorized, 'Wrong folder -> better folder')
    print('-' * 64)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Auto-categorize the downloads folder using starred db.json categories.')
    parser.add_argument('downloads_dir', nargs='?',
                        help='Folder to categorize (default: the shared downloads folder)')
    parser.add_argument('--seed', action='store_true',
                        help='Import the bundled category presets into db.json (stars left at 0) and exit')
    parser.add_argument('--yes', '-y', action='store_true',
                        help='Apply the plan without the confirmation prompt')
    args = parser.parse_args()

    data = bulk_db.load()

    if args.seed:
        added = bulk_db.seed_categories(data, load_preset_categories())
        bulk_db.save(data)
        print(f'Seeded {added} new category preset(s) into {bulk_db.db_path()}.')
        print('Star the ones you want (GUI → Categorize tab) so their folders get created.')
        return

    downloads_dir = Path(args.downloads_dir).resolve() if args.downloads_dir else default_downloads_dir()
    if not downloads_dir.is_dir():
        print(f'Error: {downloads_dir} is not a directory. Download something first, or pass a path.')
        sys.exit(1)

    categories = bulk_db.get_categories(data)
    print_stars(categories)

    folder_terms = folder_terms_from_categories(bulk_db.starred_categories(data))
    fallback = False
    if not folder_terms:
        folder_terms = folder_terms_from_subfolders(downloads_dir, categories)
        fallback = bool(folder_terms)
        if fallback:
            print('\n(Using existing subfolders as categories — nothing is starred in db.json.)')

    print(f'\nScanning {downloads_dir} ...')
    videos = scan_videos(downloads_dir)
    print(f'{len(videos)} video(s), {len(folder_terms)} target categor(y/ies).')

    moves = build_plan(videos, folder_terms)
    print_plan(moves)
    if not moves:
        return

    if not args.yes:
        try:
            answer = input('\nApply these moves? [y/N] ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            print('\nCancelled.')
            return
        if answer != 'y':
            print('Cancelled.')
            return

    print()
    done, failed = apply_plan(moves, downloads_dir)
    print(f'\nDone: {done} moved, {failed} failed.')


if __name__ == '__main__':
    main()
