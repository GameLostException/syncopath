# SyncoPath — Technical Documentation

**Version**: 0.1.0
**Last updated**: 2026-07-28

---

## Overview

SyncoPath is a bidirectional Google Drive sync daemon for Linux. It keeps a local directory in sync with a Google Drive account, supporting real-time change detection in both directions.

**Key characteristics:**
- Python 3.11+ daemon with GTK tray icon
- Runs as a systemd user service
- Uses Google Drive Changes API for remote detection (polling)
- Uses inotify (via watchdog) for local detection (real-time)
- SQLite state database for tracking sync state
- Thunar file manager integration via libcloudproviders D-Bus

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  SyncoPath Daemon (__main__.py)                                  │
│                                                                  │
│  ┌─────────────────┐       ┌──────────────────────────┐         │
│  │ Remote Watcher  │       │ Local Watcher            │         │
│  │ (Changes API    │       │ (inotify via watchdog)   │         │
│  │  poll ~30s)     │       │ (debounce 5s)            │         │
│  └────────┬────────┘       └────────────┬─────────────┘         │
│           │                             │                        │
│           ▼                             ▼                        │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │              Sync Worker (sync_worker.py)                   │ │
│  │  - Priority queue (FIFO within priority)                    │ │
│  │  - Single consumer thread (no races)                        │ │
│  │  - Retry with exponential backoff [30s, 1m, 5m, 30m]       │ │
│  └────────────────────────┬────────────────────────────────────┘ │
│                           │                                      │
│                           ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │              Sync Engine (sync_engine.py)                   │ │
│  │  - Downloads / uploads changed files                        │ │
│  │  - Conflict resolution (newer wins + .conflict)             │ │
│  │  - Folder rename/move propagation                           │ │
│  │  - Reconcile (periodic self-healing)                        │ │
│  │  - Startup scan for untracked local files                   │ │
│  │  - Google Docs export (→ .docx/.xlsx/.pptx)                 │ │
│  └────────────────────────┬────────────────────────────────────┘ │
│                           │                                      │
│       ┌───────────────────┼──────────────────┐                   │
│       ▼                   ▼                  ▼                   │
│  ┌──────────────┐ ┌─────────────────┐ ┌───────────────┐         │
│  │ State DB     │ │ Tray Icon       │ │ Thunar        │         │
│  │ (SQLite WAL) │ │ (GTK/AppInd)   │ │ Overlay       │         │
│  └──────────────┘ └─────────────────┘ └───────────────┘         │
└──────────────────────────────────────────────────────────────────┘
```

---

## Module Overview

| Module | Lines | Responsibility |
|--------|-------|----------------|
| `__main__.py` | 376 | Entry point, daemon orchestration, signal handling, startup sequence |
| `sync_engine.py` | 1163 | Core sync logic: upload, download, conflict, reconcile, folder ops |
| `sync_worker.py` | 207 | Event queue, single-threaded worker, retry with backoff |
| `drive_api.py` | 547 | Google Drive API wrapper, OAuth, file listing, path building |
| `state.py` | 191 | SQLite state database CRUD, WAL mode |
| `local_watcher.py` | 163 | inotify watcher with debouncing and suppression |
| `remote_watcher.py` | 106 | Changes API poller |
| `config.py` | 225 | YAML config loading, path validation, sanitization |
| `tray.py` | 428 | GTK tray icon, animations, progress display |
| `thunar_overlay.py` | 402 | File status emblems via libcloudproviders D-Bus |
| `preferences.py` | 1386 | GTK preferences window |

---

## Data Flow

### Startup Sequence

1. Load config from `~/.config/syncopath/config.yaml`
2. PID file check (single instance guard)
3. Initialize GTK tray icon (pre-render SVG→PNG via librsvg)
4. Start Thunar overlay D-Bus export
5. For each account:
   a. Mark existing files with Thunar emblems (sync status)
   b. Index remote folders into state DB (for path resolution)
   c. Scan for untracked local files (upload if genuinely new)
   d. Start sync worker (queue-based single thread)
   e. Start local watcher (inotify → enqueue to worker)
   f. Start remote watcher (poll → enqueue to worker)
   g. Enqueue initial reconcile
   h. Start periodic reconcile timer (every 10 minutes)

### Remote Change Processing

```
Remote Watcher polls Changes API every 30s
  → Classifies changes: created/modified/deleted
  → Enqueues to Sync Worker
    → Worker calls engine.handle_remote_changes()
      → Folders processed first (depth order)
      → For each file change:
        1. Filter: skip shared (ownedByMe check), skip_mime, size limit
        2. Detect rename: compare resolved path vs state DB path
        3. Conflict detection: compare local hash vs state, remote mtime vs local mtime
        4. Download or skip
        5. Update state DB + Thunar emblem
```

### Local Change Processing

```
Local Watcher (inotify) detects change
  → Debounces (5s settle time)
  → Deduplicates (latest event per path wins)
  → Enqueues batch to Sync Worker
    → Worker calls engine.handle_local_changes()
      → For each event:
        1. Filter: skip shared, size limit, conflict files
        2. Skip if hash matches remote (prevents re-upload loops)
        3. Upload to Drive
        4. Update state DB + Thunar emblem
```

### Reconcile (Self-Healing)

Runs on startup + every 10 minutes + on "Sync Now":
1. Finds files with `error`/`unknown` status
2. Finds files marked `synced` but missing locally
3. For error files existing locally: verify hash matches remote, re-download if not
4. For missing files: resolve fresh path from API, re-download (or remove if 404)
5. 404 errors → remove state DB entry (access revoked)

---

## State Database Schema

```sql
CREATE TABLE files (
    path TEXT PRIMARY KEY,        -- Relative path from sync root
    file_id TEXT UNIQUE,          -- Google Drive file ID
    remote_md5 TEXT,              -- MD5 from Drive (NULL for Google Docs)
    local_md5 TEXT,               -- MD5 of local file at last sync
    remote_mtime TEXT,            -- ISO timestamp from Drive
    local_mtime REAL,             -- Local file mtime at last sync
    mime_type TEXT,               -- MIME type
    is_folder INTEGER DEFAULT 0,  -- 1 for folders
    sync_status TEXT DEFAULT 'unknown'  -- synced/error/warning/unknown
);

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- Stores: page_token (Changes API cursor)
```

**WAL mode** enabled for concurrent read safety.
**busy_timeout** = 5000ms.

---

## Key Design Decisions

### Single Worker Thread
All sync operations (upload, download, reconcile) run on a single worker thread via a priority queue. This eliminates:
- Race conditions on state DB
- Concurrent Drive API calls (httplib2 is not thread-safe)
- Dropped events (queue never discards)

### Filename Sanitization
Google Drive allows `/` and `／` (U+FF0F) in filenames. Linux doesn't allow `/`. Solution: replace both with `∕` (U+2215, division slash) which looks identical but is filesystem-safe.

### Shared-with-me Detection
The `_build_path` method checks `ownedByMe` on EVERY node in the parent chain. A shared subfolder inside an owned tree is correctly identified as shared.

### Conflict Resolution
- **Newer wins**: Compare `modifiedTime` (remote) vs `mtime` (local)
- **Loser saved as**: `filename.conflict.ext`
- **Google Docs**: Use mtime comparison since md5 is unavailable
- **Loop prevention**: Skip upload if `local_hash == state.remote_md5`

### Icon Rendering
SVG source icons are pre-rendered to PNG at startup via GdkPixbuf/librsvg, avoiding the unstable glycin SVG renderer that causes SEGV crashes.

---

## Configuration

**Location**: `~/.config/syncopath/config.yaml`

```yaml
log_level: INFO
safe_mode: true          # No deletions on either side
icon_theme: dark         # light, dark, auto
min_free_space_gb: 5     # Pause if below this
default_poll_interval: 30
default_debounce: 5
default_exclude:
  - "*.tmp"
  - ".Trash*"
  - ".syncopath*"

accounts:
  - name: gdrive-st
    local_path: /home/user/gdrive-st
    remote_id: root
    poll_interval: 30
    debounce: 5
    include_shared_with_me: false
    max_file_size_mb: 0   # 0 = no limit
    exclude:
      - "*.tmp"
      - ".Trash*"
```

---

## File Locations

| Path | Purpose |
|------|---------|
| `~/.config/syncopath/config.yaml` | Main configuration |
| `~/.config/syncopath/client_secret.json` | OAuth credentials |
| `~/.config/syncopath/tokens/<account>.json` | Per-account OAuth tokens |
| `~/.config/syncopath/state/<account>.db` | Per-account SQLite state |
| `~/.config/syncopath/syncopath.pid` | PID file (single instance) |
| `~/.config/syncopath/remote_cache_<account>.json` | Cached remote file list (5min TTL) |
| `~/.config/systemd/user/syncopath.service` | Systemd user service |
| `~/.local/share/icons/hicolor/scalable/apps/syncopath*.svg` | Source icons |
| `/tmp/syncopath-icons-*/` | Pre-rendered PNG icons (ephemeral) |

---

## Testing

```bash
cd ~/Lab/syncopath
.venv/bin/pytest tests/ -v          # Run all tests
.venv/bin/pytest tests/ --cov=syncopath  # With coverage
```

**78 tests** covering:
- State DB CRUD operations
- Filename sanitization & MIME detection
- Sync worker queue processing & retry
- Config validation & path overlap
- Full sync cycles (initial, remote change, local change)
- Conflict resolution (remote wins, local wins)
- Folder rename propagation
- Shared-with-me filtering
- Error handling & retry
- Untracked file scanning

Mock infrastructure in `tests/mock_drive.py` provides an in-memory Drive API.

---

## Known Limitations

- No selective sync (all-or-nothing per account)
- No bandwidth throttling
- Google Docs export format is fixed (Docs→docx, Sheets→xlsx, Slides→pptx)
- No partial/chunked file transfers
- Startup scan takes ~100s on first run (API call to list all files)
- `sync_engine.py` is monolithic (1163 lines) — candidate for refactoring
