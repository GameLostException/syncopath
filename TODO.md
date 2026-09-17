# SyncoPath TODO

## Tray Icon / UX
- [x] Light/dark theme preference for tray icon
- [x] Animate (spin) icon in taskbar when syncing (smooth 12-frame rotation)
- [x] Progress bar in taskbar menu with current file name underneath
- [x] Show file size (no decimal) next to the filename being downloaded in the tray menu
- [x] Auto-refresh opened Thunar windows when a file changes status
- [x] Authenticate button: per-account granularity, greyed out + label "Authenticated" when already authed
- [x] Replace greyed "Authenticated" button with red "Deauthenticate" button. On click: confirmation dialog, then delete account token/cache and revoke credentials
- [x] Fixed-size tray menu: prevent jitter/resize when displaying file names and sizes during sync
- [x] Show initial activity details during startup (indexing folders, scanning local files, etc.)

## Preferences
- [x] Redesign: vertical split layout (accounts 1/3 left, per-account tabs 2/3 right)
- [x] Per-account settings: Shared Drives, Sync, Exclusions tabs
- [x] Global Settings tab with account defaults + app settings + maintenance
- [x] "Include Shared with me" toggle per account (syncs to "Shared with me/" subfolder)
- [x] "Issues (X)" tab in per-account settings showing files with sync issues:
  - 0-byte files (cloud file has content but local is empty — failed download/export)
  - Orphaned files (local file exists but cloud source is gone — 404)
  - Failed exports (Google native files too large to export, or third-party app files like Lucidchart)
  - Tab label shows count, e.g., "Issues (3)"
  - List each warning with file path, issue type, and action button (delete local / retry / ignore)
  - Auto-refresh on prefs open, manual refresh button

## Safety
- [x] Disk space reserve: pause sync with error if target drive free space drops below X GB (configurable in prefs)
- [x] Proper shared-with-me filtering: check full parent chain, not just file's ownedByMe flag

## Backlog (from DESIGN.md)
- [x] Thunar overlay icons (libcloudproviders D-Bus + GIO emblems)
- [x] Selective sync (implemented via path-based exclusions in Exclusions tab)
- [x] Bandwidth throttling (slider: 100 KiB/s to 20 MiB/s, plus Unlimited)
- [x] Show size (or estimated space) of each available shared drive in prefs
- [x] Sync filter by max file size (MB): files above threshold excluded both ways, with a dedicated emblem on local files that are excluded
