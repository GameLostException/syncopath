# SyncoPath 🔪📁

> The obsessive Google Drive syncer for Linux that never sleeps.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  syncopath (Python daemon)                               │
│                                                          │
│  ┌─────────────────┐       ┌──────────────────────────┐  │
│  │ Remote Watcher  │       │ Local Watcher            │  │
│  │ (Changes API    │       │ (inotify via watchdog)   │  │
│  │  poll ~30s)     │       │                          │  │
│  └────────┬────────┘       └────────────┬─────────────┘  │
│           │                             │                │
│           ▼                             ▼                │
│  ┌─────────────────────────────────────────────────────┐ │
│  │              Sync Engine                            │ │
│  │  - Compares remote vs local state                  │ │
│  │  - Resolves conflicts (newer wins + .conflict)     │ │
│  │  - Downloads / uploads changed files               │ │
│  │  - Handles Google Docs export (→ .docx/.xlsx)      │ │
│  └────────────────────────┬────────────────────────────┘ │
│                           │                              │
│           ┌───────────────┼──────────────┐               │
│           ▼               ▼              ▼               │
│  ┌──────────────┐ ┌─────────────┐ ┌───────────────┐     │
│  │ State DB     │ │ Tray Icon   │ │ Journal Log   │     │
│  │ (SQLite)     │ │ (GTK/AppInd)│ │ (systemd)     │     │
│  └──────────────┘ └─────────────┘ └───────────────┘     │
└──────────────────────────────────────────────────────────┘
```

## Components

### 1. Remote Watcher
- Uses Google Drive **Changes API** (`changes.list` with stored `startPageToken`)
- Polls every **30 seconds** (lightweight — just asks "what changed since last token?")
- Detects: new files, modified files, deleted files, renames, moves

### 2. Local Watcher
- Uses `watchdog` library (wraps inotify)
- Detects local filesystem changes in real-time
- Debounces rapid changes (5s settle time)
- Ignores: `.syncopath/`, `.partial~`, temp files

### 3. Sync Engine
- **State DB** (SQLite): tracks `fileId`, `remoteMd5`, `localMd5`, `mtime`, `path`, `revision`
- On remote change → download if local hasn't changed, else conflict
- On local change → upload if remote hasn't changed, else conflict
- **Conflict resolution**: newer modification time wins; loser saved as `filename.conflict.ext`
- **Deletions**: if file deleted on one side and unmodified on other → delete on other side
- **Google Docs**: exported as Office formats (configurable)

### 4. Tray Icon
- GTK StatusIcon / AppIndicator (XFCE-compatible)
- States: ✅ synced, 🔄 syncing, ⚠️ error, ⏸ paused
- Right-click menu: Pause/Resume, Sync Now, Open folder, Quit
- Tooltip: last sync time + files pending

### 5. Systemd Integration
- `~/.config/systemd/user/syncopath.service` — auto-start at login
- Logs to journald
- Restart on failure

## Configuration

```yaml
# ~/.config/syncopath/config.yaml
accounts:
  - name: gdrive-st
    remote_id: "root"              # or specific folder ID
    local_path: /home/boris/gdrive-st
    poll_interval: 30              # seconds
    debounce: 5                    # seconds for local changes
    exclude:
      - "*.tmp"
      - ".Trash*"

  - name: gdrive-boris
    remote_id: "root"
    local_path: /home/boris/gdrive-boris
    poll_interval: 30
    debounce: 5
    exclude:
      - "*.tmp"
      - ".Trash*"
```

## Dependencies

- Python 3.11+
- `google-api-python-client` — Drive API
- `google-auth-oauthlib` — OAuth2 flow
- `watchdog` — filesystem monitoring (inotify)
- `PyGObject` (gi) — GTK tray icon
- `pyyaml` — config file
- Standard lib: `sqlite3`, `hashlib`, `pathlib`, `asyncio`

## File Structure

```
~/Lab/syncopath/
├── DESIGN.md
├── pyproject.toml
├── syncopath/
│   ├── __init__.py
│   ├── __main__.py          # Entry point
│   ├── config.py            # Config loading
│   ├── state.py             # SQLite state DB
│   ├── remote_watcher.py    # Google Drive Changes API poller
│   ├── local_watcher.py     # inotify/watchdog watcher
│   ├── sync_engine.py       # Core sync logic
│   ├── drive_api.py         # Google Drive API wrapper
│   ├── tray.py              # GTK tray icon
│   └── conflicts.py         # Conflict resolution
├── systemd/
│   └── syncopath.service
└── install.sh               # Sets up venv, credentials, systemd
```

## Auth Strategy

Reuse existing OAuth credentials from `~/.kiro/mcp-gdrive/` (same Google API project).
Store per-account tokens in `~/.config/syncopath/tokens/`.

## MVP Scope (v0.1)

1. ✅ Single account sync (gdrive-st)
2. ✅ Remote polling (Changes API, 30s)
3. ✅ Local watching (inotify)
4. ✅ Bidirectional sync with conflict resolution
5. ✅ SQLite state tracking
6. ✅ Tray icon (synced/syncing/error)
7. ✅ Systemd service
8. ❌ Selective sync (later)
9. ❌ Thunar overlay icons (later)
10. ❌ Bandwidth throttling (later)

## Non-goals

- No Google Docs collaborative editing (files are snapshots)
- No partial/chunked file sync (full file transfers)
- No encryption at rest (use LUKS if needed)
