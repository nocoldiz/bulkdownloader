# BulkDownloader

A cross-platform bulk video downloader built on [yt-dlp](https://github.com/yt-dlp/yt-dlp).
It ships both a **tabbed desktop GUI** (download manager, bookmark importer, multi-site
search, gallery, and a built-in X.com browser) and a **command-line tool** for headless
queue processing. The GUI and CLI share a single link database, so links normalise and
de-duplicate identically no matter how they were added.

---

## Features

- **Download queue** — paste links or feed `links_to_download.txt`; pause/resume,
  drag-reorder, and run several downloads in parallel. Each URL runs in its own
  subprocess and resumes from its partial `.part` file after a pause or crash.
- **Bookmarks** — pull Firefox / Chromium (Chrome / Edge / Brave) bookmarks whose host
  matches a registered site and push them into the queue.
- **Search** — query every registered site's search URL at once, star favourites, and
  open them in the browser.
- **Gallery** — thumbnail grid of everything already downloaded; double-click to play.
- **X.com** — a built-in Chromium browser you log into once. From there, pull your Likes,
  Bookmarks, the profiles you follow, or any `@handle`'s media straight into the queue;
  the session is exported as cookies so yt-dlp can fetch gated videos.
- **X Links** — a dedicated list of everything scraped from the X.com session (kept
  separate from browser bookmarks). Filter, queue, download all pending with your saved
  login, or remove links you don't want.
- **Bot-detection bypass** — `curl_cffi` browser impersonation is enabled by default for a
  much higher success rate on Cloudflare / DataDome / PerimeterX-protected sites.

---

## Project layout

```
.
├── src/                      # application source
│   ├── bulkdownloader_gui.py # tabbed Tkinter GUI / download manager (entry point)
│   ├── bulkdownloader.py     # CLI downloader + the per-URL worker the GUI spawns
│   ├── bulk_db.py            # shared db.json: queue, downloaded registry, bookmarks
│   ├── site_search.py        # website registry + multi-site search/scraper
│   ├── websites.json         # seed site registry (copied to root on first run)
│   └── categories.json       # seed category → tags map
├── install.sh / install.bat  # one-time dependency installer
├── launch.sh  / launch.bat   # run the GUI from source
├── build.sh   / build.bat    # package a standalone binary with PyInstaller
├── BulkDownloader.command    # double-clickable macOS launcher
├── BulkDownloaderGUI.spec    # PyInstaller spec
└── index.html                # standalone web front-end (optional)
```

Only the Python source lives in `src/`; the launchers, installers and build scripts
sit in the project root and build into `./dist/` (git-ignored).

Runtime files are written to the **project root** (not into `src/`) and are git-ignored:
`db.json`, `links_to_download.txt`, `links_downloaded.txt`, `link_failed.txt`,
`gui_config.json`, `cookies.txt`, `x_browser_profile/`, `downloads/`, and the working
copies of `websites.json` / `categories.json` (seeded from `src/` on first launch).

---

## Requirements

- **Python 3.8+** with Tkinter (the GUI needs it).
  - macOS: use python.org Python, or `brew install python-tk`
  - Linux: `sudo apt-get install python3-tk` (or your distro's equivalent)
- Dependencies installed by the installer: `yt-dlp`, `curl_cffi`, `requests`, and
  (optional) `playwright` + Chromium for the X.com browser and JS-heavy sites.

---

## Quick start

### Windows
```bat
install.bat   :: one-time: install dependencies
launch.bat    :: start the GUI
```

### macOS / Linux
```bash
./install.sh   # one-time: install dependencies
./launch.sh    # start the GUI
```
On macOS you can also just double-click **`BulkDownloader.command`**.

---

## Command-line usage

Run the downloader directly without the GUI (shares the same `db.json`):

```bash
# Download a single URL
python src/bulkdownloader.py --url "https://example.com/video"

# Feed links_to_download.txt into the queue and download it (txt kept intact)
python src/bulkdownloader.py --from-links

# Download everything currently queued in db.json
python src/bulkdownloader.py --from-db
```

Login-gated or sensitive videos are picked up automatically once a `cookies.txt`
is present (export one via the GUI's X.com tab, or `python src/bulkdownloader.py`'s
cookie setup).

---

## Building a standalone binary

PyInstaller **cannot cross-compile** — build on the OS you are targeting.

```bash
./build.sh    # macOS → BulkDownloaderGUI.app ; Linux → ELF binary
build.bat     # Windows → BulkDownloaderGUI.exe
```

Output lands in `./dist/` (git-ignored). Frozen builds keep their runtime data in a
per-OS application-data folder instead of next to the executable.

---

## Notes

- `websites.json` and `categories.json` are mutated at runtime (favourites, edits). The
  pristine seeds live in `src/`; the working copies in the project root are git-ignored.
- Only download content you have the right to. Respect each site's terms of service.
