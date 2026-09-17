"""Configuration loading for SyncoPath."""
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


CONFIG_DIR = Path.home() / ".config" / "syncopath"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
STATE_DIR = CONFIG_DIR / "state"
TOKENS_DIR = CONFIG_DIR / "tokens"
SYNC_ROOT = Path.home() / "SyncoPath"


def sanitize_dirname(name: str) -> str:
    """Sanitize a string for use as a directory name.

    Removes/replaces characters that are forbidden or problematic on Linux
    filesystems: / \\ : * ? \" < > | and control chars.
    Strips leading/trailing dots and spaces. Collapses consecutive dashes.
    Returns a safe, non-empty string (falls back to 'unnamed' if empty).
    """
    import re
    # Replace forbidden characters with dash
    safe = re.sub(r'[/\\:*?"<>|\x00-\x1f]', '-', name)
    # Strip leading/trailing dots, spaces, dashes
    safe = safe.strip('. -')
    # Collapse consecutive dashes/underscores
    safe = re.sub(r'-{2,}', '-', safe)
    safe = re.sub(r'_{2,}', '_', safe)
    # Final fallback
    return safe if safe else "unnamed"


def default_account_path(account_name: str) -> Path:
    """Return the default local path for My Drive: ~/SyncoPath/<account>/GoogleDrive."""
    return SYNC_ROOT / sanitize_dirname(account_name) / "GoogleDrive"


def default_shared_drive_path(account_name: str, drive_name: str) -> Path:
    """Return the default local path for a shared drive: ~/SyncoPath/<account>/<drive>."""
    return SYNC_ROOT / sanitize_dirname(account_name) / sanitize_dirname(drive_name)


def validate_sync_dir_empty(path: Path) -> None:
    """Validate that a sync directory is either non-existent or empty.

    Raises ValueError if the directory exists and contains files.
    This prevents accidentally mixing existing files into a new sync root.
    """
    if path.exists():
        if not path.is_dir():
            raise ValueError(
                f"Path exists but is not a directory: {path}"
            )
        # Check if directory has any contents (files or subdirs)
        try:
            contents = list(path.iterdir())
        except PermissionError:
            raise ValueError(f"Cannot read directory: {path}")
        if contents:
            raise ValueError(
                f"Directory is not empty: {path}\n"
                f"Sync directories must be empty on first setup to prevent "
                f"mixing existing files with synced content."
            )


@dataclass
class AccountConfig:
    name: str
    local_path: Path
    remote_id: str = "root"
    poll_interval: int = 30
    debounce: int = 5
    exclude: list[str] = field(default_factory=lambda: ["*.tmp", ".Trash*", ".syncopath*"])
    client_id: str = ""  # Per-account override (falls back to global)
    client_secret: str = ""  # Per-account override (falls back to global)
    shared_drive_id: str = ""  # If set, sync this shared drive instead of My Drive
    include_shared_with_me: bool = False  # Sync "Shared with me" files into a subfolder
    max_file_size_mb: int = 0  # Max file size in MB (0 = no limit). Files above are excluded.
    bandwidth_limit_kib: int = 0  # Bandwidth limit in KiB/s (0 = unlimited)


@dataclass
class AppConfig:
    accounts: list[AccountConfig]
    client_id: str
    client_secret: str
    log_level: str = "INFO"
    safe_mode: bool = True  # No deletions on either side
    icon_theme: str = "dark"  # light, dark, or auto
    min_free_space_gb: int = 5  # Pause if free space below this

    @classmethod
    def load(cls) -> "AppConfig":
        """Load config from ~/.config/syncopath/config.yaml + own OAuth credentials."""
        import json

        # Load OAuth credentials from syncopath's own client_secret.json
        client_secret_path = CONFIG_DIR / "client_secret.json"
        if not client_secret_path.exists():
            raise FileNotFoundError(
                f"OAuth client credentials not found: {client_secret_path}\n"
                f"Download from Google Cloud Console and place at that path."
            )
        oauth_data = json.loads(client_secret_path.read_text())
        oauth_key = list(oauth_data.keys())[0]  # "installed" or "web"
        client_id = oauth_data[oauth_key]["client_id"]
        client_secret = oauth_data[oauth_key]["client_secret"]

        # Load sync config
        if not CONFIG_FILE.exists():
            raise FileNotFoundError(
                f"Config not found: {CONFIG_FILE}\n"
                f"Run 'syncopath --init' to create a default config."
            )

        raw = yaml.safe_load(CONFIG_FILE.read_text())
        accounts = []
        for acc in raw.get("accounts", []):
            # Per-account credentials override global (from config.yaml)
            acc_client_id = acc.get("client_id", "")
            acc_client_secret = acc.get("client_secret", "")

            accounts.append(AccountConfig(
                name=acc["name"],
                local_path=Path(acc["local_path"]).expanduser(),
                remote_id=acc.get("remote_id", "root"),
                poll_interval=acc.get("poll_interval", 30),
                debounce=acc.get("debounce", 5),
                exclude=acc.get("exclude", ["*.tmp", ".Trash*", ".syncopath*"]),
                client_id=acc_client_id or client_id,
                client_secret=acc_client_secret or client_secret,
                shared_drive_id=acc.get("shared_drive_id", ""),
                include_shared_with_me=acc.get("include_shared_with_me", False),
                max_file_size_mb=acc.get("max_file_size_mb", 0),
            ))

        # Validate: no sync dir can be contained by another
        _validate_no_path_overlap([a.local_path for a in accounts])

        return cls(
            accounts=accounts,
            client_id=client_id,
            client_secret=client_secret,
            log_level=raw.get("log_level", "INFO"),
            safe_mode=raw.get("safe_mode", True),
            icon_theme=raw.get("icon_theme", "dark"),
            min_free_space_gb=raw.get("min_free_space_gb", 5),
        )


def _validate_no_path_overlap(paths: list[Path]):
    """Ensure no sync directory is contained within another.

    Raises ValueError if any path is a parent/child of another.
    """
    resolved = [p.resolve() for p in paths]
    for i, p1 in enumerate(resolved):
        for j, p2 in enumerate(resolved):
            if i == j:
                continue
            # Check if p1 is a parent of p2 or vice versa
            try:
                p2.relative_to(p1)
                raise ValueError(
                    f"Sync directory overlap: '{paths[j]}' is inside '{paths[i]}'. "
                    f"Each sync directory must be independent."
                )
            except ValueError as e:
                if "overlap" in str(e):
                    raise
                continue  # Not relative — good


def _get_rclone_credentials(account_name: str) -> Optional[dict]:
    """Extract client_id/secret from rclone config for an account."""
    import subprocess
    try:
        result = subprocess.run(
            ["rclone", "config", "dump"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        import json
        cfg = json.loads(result.stdout)
        data = cfg.get(account_name, {})
        if data.get("client_id"):
            return {
                "client_id": data["client_id"],
                "client_secret": data.get("client_secret", ""),
            }
    except Exception:
        pass
    return None


def init_config():
    """Create default config file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)

    if CONFIG_FILE.exists():
        print(f"Config already exists: {CONFIG_FILE}")
        return

    default = {
        "log_level": "INFO",
        "accounts": [
            {
                "name": "personal",
                "local_path": str(default_account_path("personal")),
                "remote_id": "root",
                "poll_interval": 30,
                "debounce": 5,
                "exclude": ["*.tmp", ".Trash*", ".syncopath*"],
            },
        ],
    }
    CONFIG_FILE.write_text(yaml.dump(default, default_flow_style=False, sort_keys=False))
    print(f"Created config: {CONFIG_FILE}")
    print(f"Default sync path: {default_account_path('personal')}")
    print(f"Edit the account name and path as needed, then run 'syncopath --auth <account>'")
