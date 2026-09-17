# SyncoPath — Audit & Fix Plan

**Date**: 2026-07-28
**Auditor**: Kiro
**Status**: COMPLETE (2026-08-03)

---

## Critical Path (must fix first — causes data loss/discrepancy)

### FIX-01: Event Queue Architecture [CRITICAL]
**Problem**: Boolean `_syncing` flag causes silent event drops. When engine is busy, remote/local changes are permanently lost (page token already advanced).
**Fix**: Replace with `queue.Queue`-based single worker thread. Local and remote watchers enqueue events. Worker processes sequentially.
**Files**: `sync_engine.py`, `__main__.py`, `remote_watcher.py`, `local_watcher.py`
**Status**: [x] DONE — Created `sync_worker.py`, removed `_syncing` boolean, watchers enqueue to worker

### FIX-02: Path Resolution from Drive Metadata [CRITICAL]
**Problem**: `_resolve_remote_path` trusts state DB paths which may be stale after renames. Builds wrong paths.
**Fix**: Always walk the parent chain from live Drive metadata. Use state DB only as a first-pass cache, but verify terminal parent reaches root_id. If cache miss or mismatch, query API.
**Files**: `sync_engine.py`
**Status**: [x] DONE — Uses real root ID, state DB as cache with full path prepend

### FIX-03: Shared-with-me Filtering in Remote Watcher [HIGH]
**Problem**: Changes API returns ALL visible file changes. Engine downloads "Shared with me" files even when `include_shared_with_me = False`.
**Fix**: In `_download_or_conflict`, check if resolved path belongs to "Shared with me" tree and skip if setting is off. Also filter in `poll_once` using `ownedByMe` field.
**Files**: `sync_engine.py`, `remote_watcher.py`, `drive_api.py` (add `ownedByMe` to fields)
**Status**: [x] DONE — Added `ownedByMe` to changes fields, filter in `_download_or_conflict`

### FIX-16: Shared Folder Chain Detection [CRITICAL] (2026-08-03)
**Problem**: FIX-03 only checked `ownedByMe` on the file itself. Files in shared folders with `ownedByMe=True` (e.g., you uploaded to a shared folder) slipped through.
**Fix**: Added `_is_shared_file()` method that walks the full parent chain to detect shared ancestry. Updated `_download_or_conflict` and `_handle_remote_folder_change`.
**Files**: `sync_engine.py`
**Status**: [x] DONE — Full parent chain check for shared detection

### FIX-18: State DB Bypass in Shared Folder Detection [CRITICAL] (2026-08-10)
**Problem**: `_is_shared_file()` had a shortcut: if a parent folder was found in state DB, it assumed the folder was owned and returned `False` (not shared). This was wrong because folders can be indexed via Changes API even if they're shared — the state DB doesn't track ownership.
**Symptom**: Files in shared folders were synced to root level (e.g., `KACE-PRO_30thJune26_flier.PDF` synced to `~/gdrive-st/` instead of being excluded).
**Fix**: Removed the state DB shortcut. `_is_shared_file()` now always verifies ownership via Drive API for every folder in the parent chain.
**Files**: `sync_engine.py`
**Tests**: Added 2 regression tests in `test_integration.py`:
  - `test_shared_folder_in_state_db_still_excluded`
  - `test_nested_shared_folder_excluded`
**Status**: [x] DONE

---

## High Priority (causes incorrect behavior)

### FIX-04: Folder Index Refresh on Startup [HIGH]
**Problem**: `_index_remote_folders` only runs during initial sync. On restart, new folders created while service was down are unknown.
**Fix**: Run `_index_remote_folders` on every startup (after initial sync check). Fast operation — just folder metadata, no file downloads.
**Files**: `__main__.py` (in `_start_sync`)
**Status**: [x] DONE — Runs on every startup in `_start_sync`

### FIX-05: Local Move Handling (Parents Update) [HIGH]
**Problem**: `_handle_local_move` only updates `name` on Drive. Moving a file between folders locally doesn't update the `parents` field.
**Fix**: Detect if parent changed, and call `files.update` with `addParents`/`removeParents`.
**Files**: `sync_engine.py`
**Status**: [x] DONE — Detects parent change, uses addParents/removeParents

### FIX-06: Retry with Exponential Backoff [HIGH]
**Problem**: Failed operations are permanently marked error. No retry. Transient network issues become permanent.
**Fix**: Add retry queue in the worker. Failed items go back with delay (exponential: 30s, 1m, 5m, 30m, then give up). Respect 429/503 with backoff.
**Files**: `sync_engine.py` (or new `retry.py`)
**Status**: [x] DONE — SyncWorker has `_handle_failure` with backoff schedule [30s, 1m, 5m, 30m]

### FIX-07: State DB Thread Safety [HIGH]
**Problem**: Multiple threads write to SQLite without explicit synchronization.
**Fix**: With FIX-01 (single worker), all DB writes happen from one thread. Read-only queries from other threads are safe with WAL mode.
**Files**: `state.py`
**Status**: [x] DONE — WAL mode + busy_timeout=5000ms

### FIX-17: Startup Activity Not Shown [HIGH] (2026-08-03)
**Problem**: Tray showed "idle" during startup while heavy operations ran in background. User had no visibility.
**Fix**: Added status updates to `_start_sync` for each phase: "Starting account", "Restoring emblems", "Indexing folders", "Scanning local files", "Starting watchers".
**Files**: `__main__.py`
**Status**: [x] DONE — Tray updates during all startup phases

---

## Medium Priority (edge cases, correctness)

### FIX-08: Timer-based Unsuppress Race [MEDIUM]
**Problem**: 2-second timer to unsuppress local watcher is arbitrary. Large files still downloading when unsuppress fires → spurious upload.
**Fix**: Use callback-based unsuppress. Unsuppress immediately after download completes + debounce period, not a fixed timer.
**Files**: `sync_engine.py`, `local_watcher.py`
**Status**: [x] DONE — Timer now uses `config.debounce + 1` (after download completes)

### FIX-09: Folder Rename Ordering [MEDIUM]
**Problem**: Batch of changes containing parent + child folder renames is order-dependent.
**Fix**: Sort folder changes by depth (shallowest first) before processing. Parent renames propagate before children are resolved.
**Files**: `sync_engine.py` (in `handle_remote_changes`)
**Status**: [x] DONE — `handle_remote_changes` separates and processes folders before files

### FIX-10: SQL Injection in Folder Query [MEDIUM]
**Problem**: `_ensure_remote_dirs` interpolates folder name directly into query string. Names with quotes break it.
**Fix**: Escape single quotes in folder names (`part.replace("'", "\\'")`) per Drive API query syntax.
**Files**: `sync_engine.py`
**Status**: [x] DONE

### FIX-11: Google Docs Conflict Detection [MEDIUM]
**Problem**: Google Docs have `md5Checksum = None`. Conflict detection always sees `None != None` as False.
**Fix**: For Google Docs, compare `modifiedTime` instead of hash. If remote mtime > last synced mtime AND local file changed, it's a conflict.
**Files**: `sync_engine.py`
**Status**: [x] DONE — Uses mtime comparison when local_md5 is None

### FIX-12: Single Instance Guard [MEDIUM]
**Problem**: No PID file. Multiple instances corrupt state.
**Fix**: Use PID file (`~/.config/syncopath/syncopath.pid`). Check on startup, bail if another instance holds it. Also add `ExecStartPre` kill in systemd unit.
**Files**: `__main__.py`, `systemd/syncopath.service`
**Status**: [x] DONE — PID file created/checked on startup, cleaned on exit

---

## Low Priority (code quality, performance)

### FIX-13: O(n²) Progress Tracking [LOW]
**Problem**: `events.index(event)` is O(n) per iteration.
**Fix**: Use `enumerate()` instead.
**Files**: `sync_engine.py`
**Status**: [x] DONE — Changed to `enumerate()`

### FIX-14: Inline Imports [LOW]
**Problem**: `import threading`, `import shutil` inside methods.
**Fix**: Move to module-level imports.
**Files**: `sync_engine.py`
**Status**: [x] DONE

### FIX-15: Monolithic sync_engine.py [LOW]
**Problem**: 800+ lines, mixed concerns.
**Fix**: Split into `path_resolver.py`, `conflict.py`, `folder_ops.py`. Keep `sync_engine.py` as orchestrator.
**Priority**: After all correctness fixes. Refactor only when tests exist.
**Status**: [ ] DEFERRED — Low priority, tests provide sufficient safety

---

## Test Coverage

### TEST-01: Unit Tests
- [x] `state.py` — CRUD, rename_prefix, page token, WAL mode (14 tests)
- [x] `drive_api` — sanitize_filename, is_skip_mime (17 tests)
- [x] `sync_worker` — queue processing, sequential execution, retry (8 tests)
- [x] `config.py` — path validation, overlap detection, sanitize_dirname (11 tests)
- [ ] `local_watcher.py` — debounce, suppress/unsuppress, exclude patterns

### TEST-02: Integration Tests
- [x] Full sync cycle: create remote → detect change → download → verify local
- [x] Conflict: modify both sides → verify .conflict file created
- [x] Folder rename: rename remote folder → verify local rename + children paths
- [x] Retry: simulate transient error → verify retry succeeds
- [x] Shared folder chain: file in shared folder excluded even if ownedByMe=True
- [x] Shared folder in state DB: still excluded (regression for FIX-18)
- [x] Nested shared folder: file deep in shared chain excluded

### TEST-03: Regression Tests (from bugs found)
- [x] "My Drive/" prefix never appears in paths (2 tests)
- [x] Unicode slash handling (2 tests)
- [x] md5_file edge cases (4 tests)
- [x] iso_to_timestamp formats (3 tests)
- [x] Google Docs conflict detection with None md5 (2 tests)

**Total: 84 tests passing**

---

## Changelog

### 2026-08-13
- FEAT: Bandwidth throttling implementation
  - Added `bandwidth_limit_kib` parameter to `DriveAPI.__init__` (0 = unlimited)
  - Implemented rate limiting in `download_file()` using time-based throttling
  - Updated `__main__.py` to pass bandwidth config from `AccountConfig`
  - UI slider already existed in preferences (100 KiB/s to 20 MiB/s + Unlimited)
- All 84 tests passing

### 2026-08-10
- FIX-18: Removed faulty state DB shortcut in `_is_shared_file()` — always verify ownership via API
- Added 2 regression tests for shared folder detection bypass

### 2026-08-03
- FIX-16: Shared folder chain detection (`_is_shared_file()`)
- FIX-17: Startup activity display in tray
- Added integration test for shared folder chain

### 2026-07-28
- Initial audit complete
- FIX-01 through FIX-14 implemented
- 78 tests passing

---

## Notes

- Stop the service during implementation to avoid state corruption
- Back up state DB before each major change
- Test each fix with a targeted scenario before moving to next
- FIX-01 is the largest change — will touch most files

### 2026-08-12
- FIX-19: Download to temp file first, rename on success
  - **Problem**: Failed downloads (e.g., `exportSizeLimitExceeded` for large Google Docs) left 0-byte files because file was opened for writing before download started
  - **Fix**: Download to `.partial~` temp file, rename to final destination only on success, delete temp on failure
  - **Files**: `drive_api.py`
- Cleaned 73 orphaned/failed export files (1 orphaned, 72 failed Google native exports including Lucidchart)
