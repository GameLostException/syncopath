# SyncoPath

SyncoPath keeps a local folder in two-way sync with Google Drive on Linux. It runs as a
systemd user service with a GTK tray icon, detects local changes with inotify and remote
changes with the Drive Changes API, and shows sync status as Thunar emblems. It is built
for an XFCE desktop and one or more personal or Workspace Google accounts.

## Quick Start

```bash
./install.sh
```

```bash
.venv/bin/python -m syncopath --init
```

Place the OAuth client file of your Google Cloud project at
`~/.config/syncopath/client_secret.json`, set your accounts in
`~/.config/syncopath/config.yaml`, then:

```bash
.venv/bin/python -m syncopath --auth gdrive-st
```

```bash
systemctl --user enable --now syncopath
```

## Usage

```
syncopath                       Run the daemon
syncopath --debug               Run with debug logging
syncopath --init                Create the default config file
syncopath --auth ACCOUNT        Authenticate an account (opens the browser)
syncopath --prefs               Open the preferences window only
syncopath --list-shared-drives  List the shared drives of the first account
```

The tray menu offers Sync Now, Pause/Resume, Open Folder, Preferences and Quit. The
[user manual](docs/USER_MANUAL.md) describes every feature and the troubleshooting steps.

## Configuration

Every key, its default and the runtime paths are listed in the
[configuration reference](docs/ARCHITECTURE.md#configuration). The settings that matter most:

- `safe_mode` (default `true`): never delete on either side.
- `accounts[].local_path`: the folder to sync; it must be empty on the first sync.
- `accounts[].include_shared_with_me`: also sync files shared with you.
- `accounts[].max_file_size_mb` and `accounts[].bandwidth_limit_kib`: size and speed limits.

## Requirements

- Linux with a graphical session (GTK 3, AppIndicator), systemd user services
- Python 3.11 or later
- A Google Cloud project with the Drive API enabled and an OAuth desktop client

Python dependencies, pinned in `pyproject.toml`:

| Package | Version | Used for |
|---------|---------|----------|
| google-api-python-client | 2.166.0 | Drive API v3 calls |
| google-auth-oauthlib | 1.2.1 | OAuth consent flow |
| google-auth | 2.40.3 | Credentials and token refresh |
| httplib2 | 0.32.0 | HTTP transport of the API client; its errors are classified as transient |
| watchdog | 6.0.0 | inotify file watching |
| PyGObject | 3.52.3 | GTK tray, preferences, GIO and D-Bus |
| pyyaml | 6.0.2 | Config file |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e . pytest pytest-cov
```

```bash
.venv/bin/pytest
```

```bash
.venv/bin/pytest --cov=syncopath
```

Tests use an in-memory Drive (`tests/mock_drive.py`) and never touch the network.
Architecture and design decisions: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
[docs/decisions/](docs/decisions/).

## Licence

No licence has been chosen yet; all rights reserved by the author.
