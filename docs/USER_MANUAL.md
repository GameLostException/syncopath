# SyncoPath — User Manual

**The obsessive Google Drive syncer for Linux that never sleeps 🔪📁**

---

## What is SyncoPath?

SyncoPath keeps a folder on your Linux machine in sync with your Google Drive. Changes you make locally are uploaded; changes made on Drive are downloaded. It runs quietly in the background as a tray icon.

---

## Installation

### Prerequisites
- Python 3.11+
- GTK 3 with AppIndicator support
- A Google Cloud project with Drive API enabled

### Setup

```bash
cd ~/Lab/syncopath
./install.sh
```

This will:
1. Create a Python virtual environment
2. Install dependencies
3. Install the systemd user service
4. Install tray icons

### First-time Authentication

```bash
syncopath --auth <account-name>
```

This opens your browser for Google OAuth consent. Once authorized, the token is stored locally.

---

## Starting & Stopping

### Automatic (recommended)
SyncoPath starts automatically at login via systemd:

```bash
# Start
systemctl --user start syncopath

# Stop
systemctl --user stop syncopath

# Restart
systemctl --user restart syncopath

# View logs
journalctl --user -u syncopath -f

# Enable/disable auto-start
systemctl --user enable syncopath
systemctl --user disable syncopath
```

### Manual
```bash
syncopath              # Run daemon
syncopath --debug      # Run with debug logging
syncopath --prefs      # Open preferences only
```

---

## Tray Icon

The tray icon shows sync status at a glance:

| Icon State | Meaning |
|------------|---------|
| Folder (static) | ✅ All synced, idle |
| Folder (pulsing) | ⏳ Working (indexing, scanning) |
| Folder (spinning) | 🔄 Syncing (uploading/downloading) |
| Warning triangle | ⚠️ Error occurred |
| Pause icon | ⏸ Sync paused |

### Startup Sequence

When SyncoPath starts, the tray shows each phase:
1. "Starting {account}..." — Initializing
2. "Restoring file emblems..." — Marking files in Thunar
3. "Indexing folders..." — Building folder tree from Drive
4. "Scanning local files..." — Finding new local files
5. "Starting watchers..." — Enabling real-time sync

### Tray Menu

Right-click the tray icon for:

- **Status line** — Current state (Synced, Syncing X files, Error details)
- **Progress bar** — Shows during file transfers (X/Y files, percentage)
- **Current file** — Name and size of the file being transferred
- **Sync Now** — Force an immediate sync check + reconcile
- **Open Folder** — Open the sync directory in Thunar
- **Pause/Resume** — Temporarily stop/restart syncing
- **Preferences** — Open settings window
- **Quit** — Graceful shutdown

---

## Preferences

Open via tray menu → Preferences, or `syncopath --prefs`.

### Accounts Tab (left panel)

Shows your sync accounts. Click one to see its settings on the right.

**Add**: Create a new sync account (name + local folder).
**Remove**: Delete an account (local files are NOT deleted).

### Per-Account Settings (right panel)

#### General Tab
- **Account info**: Name, local path
- **Authenticate / Deauthenticate**: Manage OAuth credentials
  - Red "Deauthenticate" button deletes token + cache after confirmation
- **Include Shared with me**: Toggle to sync files shared by others
  - When enabled, shared files sync to `<local_path>/Shared with me/`
  - Shared files are read-only — local changes are never uploaded back

#### Shared Drives Tab
- Enable/disable shared drives to sync
- Each shared drive gets its own local folder
- Click "Refresh from Google" to load available drives with sizes
- Folders must not overlap with each other or My Drive

#### Sync Tab
Settings with override checkboxes (tick to override global default):
- **Remote poll interval** (seconds): How often to check for remote changes (10-300s)
- **Local change debounce** (seconds): Wait time before processing local changes (1-30s)
- **Conflict resolution**: Newer wins / Always keep local / Always keep remote
- **Max file size (MB)**: Files larger than this are excluded from sync (0 = no limit)

#### Exclusions Tab
- Glob patterns for files/folders to exclude (one per line)
- Override checkbox to use account-specific patterns vs global default
- Quick-add buttons: node_modules, .git, *.log, __pycache__, *.iso

### Global Settings Tab

- **Account Defaults**: Default poll interval, debounce, exclusions for new accounts
- **Log level**: DEBUG / INFO / WARNING / ERROR
- **Icon theme**: dark / light / auto
- **Safe mode**: When ON, files are never deleted on either side
- **Minimum free disk space (GB)**: Pauses sync if disk is too full
- **Auto-start at login**: Enable/disable systemd service
- **Reset Sync State**: Force full resync (deletes state DB, not local files)
- **View Logs**: Opens journalctl in a terminal

---

## Features

### Bidirectional Sync
- Local changes are uploaded to Drive
- Remote changes are downloaded locally
- Works with files, folders, renames, moves, and deletions

### Conflict Resolution
When the same file is modified both locally and on Drive between syncs:
- **Winner**: The version with the newer modification time
- **Loser**: Saved as `filename.conflict.ext` alongside the winner
- You can then compare and choose which to keep

### Google Docs Export
Google Docs, Sheets, and Slides are exported as:
- Documents → `.docx`
- Spreadsheets → `.xlsx`
- Presentations → `.pptx`
- Drawings → `.pdf`

These are snapshots — collaborative editing happens on Drive.

### Thunar File Emblems
In Thunar (XFCE file manager), files show sync status overlays:
- ✅ Green checkmark: Synced
- 🔄 Blue arrows: Syncing
- ⚠️ Yellow warning: Unsyncable / size excluded
- ❌ Red X: Error

### Shared with Me
When "Include Shared with me" is enabled:
- Files shared by others are downloaded to `<sync_dir>/Shared with me/`
- These files are **read-only** — local edits are NOT uploaded
- Shared subfolders inside your owned folders are detected and excluded when the setting is OFF

### Safe Mode
When enabled (default), files are **never deleted** on either side. If you delete a file locally, it stays on Drive. If someone trashes it on Drive, it stays locally.

### Disk Space Protection
Sync automatically pauses with an error status if free disk space drops below the configured threshold (default: 5 GB).

### Max File Size Filter
Set a per-account maximum file size. Files above this limit are excluded from sync in both directions and get a warning emblem.

### Self-Healing Reconcile
Every 10 minutes, SyncoPath automatically:
- Verifies all tracked files exist and match their remote versions
- Re-downloads files that were corrupted or have mismatched hashes
- Removes state entries for files whose access was revoked (404)
- Catches up on changes missed during downtime

### Single Instance Guard
Only one instance of SyncoPath can run at a time (PID file lock). This prevents state corruption from duplicate processes.

---

## Troubleshooting

### Sync not working
1. Check service status: `systemctl --user status syncopath`
2. Check logs: `journalctl --user -u syncopath --since "5 minutes ago"`
3. Try "Sync Now" from tray menu
4. Restart: `systemctl --user restart syncopath`

### File has error emblem
The file failed to sync. Click "Sync Now" — the reconcile will attempt to fix it. Check logs for details.

### Files not uploading
- Check if the file matches an exclusion pattern
- Check if it exceeds the max file size limit
- Check if it's in the "Shared with me" folder (read-only)
- Check logs for upload errors

### Tray icon missing
AppIndicator may not be running. Ensure `xfce4-indicator-plugin` is in your panel.

### High CPU usage
The startup scan (indexing + scanning) uses CPU for ~2 minutes. This is normal on first start or after restart. It settles to near-zero once idle.

### "Shared with me" files appearing
If shared files appear in your sync folder unexpectedly, toggle "Include Shared with me" OFF in Preferences → Account → General.

---

## Command-Line Reference

```
syncopath                    Run the daemon (normal mode)
syncopath --init             Create default config file
syncopath --auth ACCOUNT     Authenticate an account
syncopath --prefs            Open preferences window
syncopath --debug            Run with debug logging
syncopath --list-shared-drives  List available shared drives
```

---

## File Exclusion Patterns

Glob patterns (one per line in the Exclusions tab):

| Pattern | Effect |
|---------|--------|
| `*.tmp` | Exclude all .tmp files |
| `.Trash*` | Exclude trash directories |
| `node_modules` | Exclude Node.js dependencies |
| `.git` | Exclude Git repositories |
| `*.iso` | Exclude ISO images |
| `__pycache__` | Exclude Python cache |

Files starting with `.` (hidden) and ending with `~` (backup) are always excluded.
Files containing `.conflict` in their name are always excluded from upload.
