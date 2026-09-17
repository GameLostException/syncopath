"""Google Drive API wrapper for SyncoPath."""
import json
import logging
import mimetypes
import threading
from io import BytesIO
from pathlib import Path
from typing import Optional

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from .config import TOKENS_DIR

log = logging.getLogger(__name__)

# Characters that are unsafe in Linux filenames (only / and null are truly forbidden)
# We also handle fullwidth variants that LOOK like separators
_FILENAME_SANITIZE_MAP = str.maketrans({
    "/": "\u2215",     # U+002F (slash) → U+2215 (division slash)
    "\uff0f": "\u2215",  # U+FF0F (fullwidth solidus) → U+2215 (division slash)
    "\x00": "",        # Null → remove
})


def sanitize_filename(name: str) -> str:
    """Sanitize a Google Drive filename for safe use on Linux filesystem.

    Replaces characters that would be interpreted as path separators
    with visually similar safe unicode alternatives.
    """
    return name.translate(_FILENAME_SANITIZE_MAP)

# Google Docs export MIME mappings
GOOGLE_EXPORT_MAP = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.form": ("application/pdf", ".pdf"),
}

# MIME types that are Google-native (can't be downloaded directly)
GOOGLE_NATIVE_MIMES = set(GOOGLE_EXPORT_MAP.keys())

# Google types that can't be exported at all — skip these
GOOGLE_SKIP_MIMES = {
    "application/vnd.google-apps.script",
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.folder",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.site",
    "application/vnd.google-apps.jam",
}


def is_skip_mime(mime_type: str) -> bool:
    """Check if a MIME type should be skipped (not downloadable/exportable).

    Includes Google-native types and third-party Drive SDK types (Lucidchart, etc.)
    """
    if mime_type in GOOGLE_SKIP_MIMES:
        return True
    # Third-party apps registered via Drive SDK (e.g., Lucidchart, Figma)
    if mime_type.startswith("application/vnd.google-apps.drive-sdk"):
        return True
    return False


class DriveAPI:
    """Authenticated Google Drive API client."""

    def __init__(self, account_name: str, client_id: str, client_secret: str,
                 shared_drive_id: str = None, bandwidth_limit_kib: int = 0):
        self.account_name = account_name
        self.client_id = client_id
        self.client_secret = client_secret
        self.shared_drive_id = shared_drive_id
        self.bandwidth_limit_kib = bandwidth_limit_kib  # 0 = unlimited
        self._token_path = TOKENS_DIR / f"{account_name}.json"
        self._service = None
        self._creds = None
        self._real_root_id = None  # Cached real root folder ID
        # httplib2 (used internally by googleapiclient) is NOT thread-safe.
        # The RemoteWatcher polls on its own thread while the SyncWorker downloads/uploads
        # on another. A single lock serialises all Drive API calls to prevent SSL corruption.
        self._api_lock = threading.Lock()

    def _load_credentials(self) -> Credentials:
        """Load or refresh OAuth2 credentials."""
        if self._token_path.exists():
            data = json.loads(self._token_path.read_text())
            self._creds = Credentials(
                token=data.get("access_token"),
                refresh_token=data.get("refresh_token"),
                token_uri="https://oauth2.googleapis.com/token",
                client_id=self.client_id,
                client_secret=self.client_secret,
            )

        if self._creds and self._creds.expired and self._creds.refresh_token:
            log.debug("Refreshing token for %s", self.account_name)
            self._creds.refresh(Request())
            self._save_credentials()
        elif not self._creds or not self._creds.valid:
            raise RuntimeError(
                f"No valid credentials for '{self.account_name}'. "
                f"Run 'syncopath --auth {self.account_name}' to authenticate."
            )

        return self._creds

    def _save_credentials(self):
        """Persist credentials to disk."""
        TOKENS_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "access_token": self._creds.token,
            "refresh_token": self._creds.refresh_token,
            "token_type": "Bearer",
        }
        self._token_path.write_text(json.dumps(data))

    @property
    def service(self):
        """Lazy-init the Drive API service."""
        if self._service is None:
            creds = self._load_credentials()
            self._service = build("drive", "v3", credentials=creds)
        return self._service

    def get_real_root_id(self) -> str:
        """Resolve the 'root' alias to the actual folder ID.

        The Drive API accepts 'root' as an alias, but file metadata always
        contains the real folder ID in the parents field. We need the real ID
        to properly terminate path resolution.
        """
        if self._real_root_id is None:
            with self._api_lock:
                # Double-check after acquiring lock
                if self._real_root_id is None:
                    resp = self.service.files().get(fileId="root", fields="id").execute()
                    self._real_root_id = resp["id"]
        return self._real_root_id

    def authenticate_interactive(self):
        """Run interactive OAuth2 flow for initial auth."""
        import http.server
        import urllib.parse
        import webbrowser
        from google.oauth2.credentials import Credentials as OAuth2Credentials
        from .config import CONFIG_DIR

        client_secret_path = CONFIG_DIR / "client_secret.json"
        oauth_data = json.loads(client_secret_path.read_text())
        installed = oauth_data["installed"]
        client_id = installed["client_id"]
        client_secret = installed["client_secret"]

        # Start local server on a fixed port
        port = 8085
        redirect_uri = f"http://localhost:{port}"

        # Build auth URL
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "https://www.googleapis.com/auth/drive",
            "access_type": "offline",
            "prompt": "consent",
        }
        auth_url = f"https://accounts.google.com/o/oauth2/auth?{urllib.parse.urlencode(params)}"

        # Capture the authorization code via local HTTP server
        auth_code = None

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                nonlocal auth_code
                query = urllib.parse.urlparse(self.path).query
                qs = urllib.parse.parse_qs(query)
                auth_code = qs.get("code", [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>SyncoPath authenticated!</h1><p>You can close this tab.</p>")

            def log_message(self, *args):
                pass  # Suppress server logs

        server = http.server.HTTPServer(("localhost", port), Handler)
        webbrowser.open(auth_url)
        log.info("Waiting for authorization at %s ...", redirect_uri)
        server.handle_request()  # Handle one request (the callback)
        server.server_close()

        if not auth_code:
            raise RuntimeError("No authorization code received.")

        # Exchange code for tokens
        token_resp = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": auth_code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        })

        if token_resp.status_code != 200:
            raise RuntimeError(f"Token exchange failed: {token_resp.text}")

        token_data = token_resp.json()
        self._creds = Credentials(
            token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
        )
        self._save_credentials()
        log.info("Authenticated account: %s", self.account_name)

    def get_start_page_token(self) -> str:
        """Get the initial page token for changes tracking."""
        with self._api_lock:
            kwargs = {"supportsAllDrives": True}
            if self.shared_drive_id:
                kwargs["driveId"] = self.shared_drive_id
            resp = self.service.changes().getStartPageToken(**kwargs).execute()
            return resp["startPageToken"]

    def get_changes(self, page_token: str) -> tuple[list[dict], str]:
        """
        Get changes since the given page token.
        Returns (list_of_changes, new_page_token).
        """
        changes = []
        kwargs = {
            "pageToken": page_token,
            "spaces": "drive",
            "includeRemoved": True,
            "fields": "nextPageToken,newStartPageToken,changes("
                      "fileId,removed,file(id,name,mimeType,md5Checksum,"
                      "modifiedTime,parents,trashed,size,ownedByMe))",
            "pageSize": 1000,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if self.shared_drive_id:
            kwargs["driveId"] = self.shared_drive_id
            kwargs["includeCorpusRemovals"] = True

        with self._api_lock:
            while True:
                resp = self.service.changes().list(**kwargs).execute()
                changes.extend(resp.get("changes", []))
                if "nextPageToken" in resp:
                    kwargs["pageToken"] = resp["nextPageToken"]
                else:
                    page_token = resp.get("newStartPageToken", page_token)
                    break
        return changes, page_token

    def list_all_files(self, folder_id: str = "root") -> list[dict]:
        """List all files recursively from a folder. Used for initial sync."""
        all_files = []
        page_token = None

        # Get all files in Drive (we'll build tree ourselves)
        query = "trashed = false"
        if folder_id != "root":
            # For specific folder, we need recursive listing
            query = f"'{folder_id}' in parents and trashed = false"

        with self._api_lock:
            while True:
                resp = (
                    self.service.files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields="nextPageToken,files(id,name,mimeType,md5Checksum,"
                        "modifiedTime,parents,size)",
                        pageSize=1000,
                        pageToken=page_token,
                    )
                    .execute()
                )
                all_files.extend(resp.get("files", []))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        return all_files

    def list_folder_recursive(self, folder_id: str = "root",
                              include_shared_with_me: bool = False) -> list[dict]:
        """Recursively list all files with their full paths.

        Args:
            folder_id: Root folder ID to list from.
            include_shared_with_me: If True, also include files shared with the user.
                These files will have their _path prefixed with "Shared with me/".

        Returns:
            List of file metadata dicts with added "_path" key.
        """
        # Get ALL files (flat list)
        all_files = []
        page_token = None

        # Shared drive queries need different parameters
        kwargs = {
            "q": "trashed = false",
            "spaces": "drive",
            "fields": "nextPageToken,files(id,name,mimeType,md5Checksum,"
                      "modifiedTime,parents,size,ownedByMe)",
            "pageSize": 1000,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if self.shared_drive_id:
            kwargs["corpora"] = "drive"
            kwargs["driveId"] = self.shared_drive_id

        with self._api_lock:
            while True:
                if page_token:
                    kwargs["pageToken"] = page_token
                resp = self.service.files().list(**kwargs).execute()
                all_files.extend(resp.get("files", []))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        # Build path tree
        root_id = self.shared_drive_id if self.shared_drive_id else self.get_real_root_id()
        if folder_id != "root" and not self.shared_drive_id:
            root_id = folder_id  # Specific folder override
        id_to_file = {f["id"]: f for f in all_files}
        result = []

        for f in all_files:
            if f["mimeType"] == "application/vnd.google-apps.folder":
                continue

            path, is_owned = self._build_path(f, id_to_file, root_id)

            if path is not None and is_owned:
                # File belongs to My Drive tree
                f["_path"] = path
                result.append(f)
            elif path is not None and not is_owned and include_shared_with_me:
                # File is "Shared with me" — prefix with dedicated folder
                f["_path"] = f"Shared with me/{path}"
                result.append(f)

        return result

    def _build_path(self, file: dict, id_map: dict, root_id: str) -> tuple[Optional[str], bool]:
        """Build the full path of a file from root.

        Returns:
            (path, is_owned): path string and whether the file traces back to root_id.
            If path is None, the file is an orphan.
            is_owned=True means the file AND all parent folders in the chain are owned.
            is_owned=False means the file or any parent is shared (not owned by us).
        """
        parts = [sanitize_filename(file["name"])]
        current = file
        is_owned = False

        # Check if the file itself is not owned
        if file.get("ownedByMe") is not None and not file.get("ownedByMe"):
            # File explicitly not owned — mark as shared
            pass  # Continue to build path, but is_owned stays False

        while True:
            parents = current.get("parents", [])
            if not parents:
                # Orphan file (no parents at all) — skip
                return None, False
            parent_id = parents[0]
            if parent_id == root_id:
                # Traced back to our root — owned (unless file/folder was marked shared)
                is_owned = True
                break
            if parent_id not in id_map:
                # Parent not in our file list — file is shared with us
                is_owned = False
                break
            parent = id_map[parent_id]
            # If any folder in the chain is not owned, the whole subtree is shared
            if parent.get("ownedByMe") is not None and not parent.get("ownedByMe"):
                is_owned = False
                # Still build the path (for Shared with me prefix)
                parts.insert(0, sanitize_filename(parent["name"]))
                # Walk up to find remaining path to root for display
                current = parent
                continue
            parts.insert(0, sanitize_filename(parent["name"]))
            current = parent

        # Override: if the file itself is explicitly not owned, it's shared
        if file.get("ownedByMe") is not None and not file.get("ownedByMe"):
            is_owned = False

        return "/".join(parts), is_owned

    def download_file(self, file_id: str, mime_type: str, dest_path: Path):
        """Download a file from Drive."""
        if mime_type in GOOGLE_SKIP_MIMES:
            log.debug("Skipping non-exportable type %s: %s", mime_type, dest_path)
            return

        # Skip any google-apps type not in the export map (third-party apps, etc.)
        if mime_type.startswith("application/vnd.google-apps.") and mime_type not in GOOGLE_NATIVE_MIMES:
            log.debug("Skipping unsupported Google type %s: %s", mime_type, dest_path)
            return

        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # Build the request object under the lock (service/credentials are shared state),
        # then stream the actual bytes outside the lock so a large download doesn't block
        # the remote-watcher poll thread for minutes.
        with self._api_lock:
            if mime_type in GOOGLE_NATIVE_MIMES:
                export_mime, _ = GOOGLE_EXPORT_MAP[mime_type]
                request = self.service.files().export_media(
                    fileId=file_id, mimeType=export_mime
                )
            else:
                request = self.service.files().get_media(fileId=file_id)

        # Download to a temp file first, then rename on success
        # This prevents leaving 0-byte files when download fails
        temp_path = dest_path.with_suffix(dest_path.suffix + ".partial~")
        try:
            with open(temp_path, "wb") as f:
                downloader = MediaIoBaseDownload(f, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                    # Apply bandwidth throttling
                    if self.bandwidth_limit_kib > 0 and status:
                        # Estimate sleep time based on bytes downloaded
                        bytes_downloaded = status.resumable_progress
                        target_time = bytes_downloaded / (self.bandwidth_limit_kib * 1024)
                        # Sleep to throttle (rough approximation)
                        import time
                        elapsed = time.time() - getattr(self, '_download_start', time.time())
                        if elapsed < target_time:
                            time.sleep(min(target_time - elapsed, 1.0))
            # Success — rename to final destination
            temp_path.rename(dest_path)
        except Exception:
            # Clean up partial file on failure
            temp_path.unlink(missing_ok=True)
            raise

        log.debug("Downloaded: %s", dest_path)

    def upload_file(self, local_path: Path, parent_id: str, file_id: Optional[str] = None) -> dict:
        """Upload or update a file on Drive."""
        mime_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
        media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)

        metadata = {"name": local_path.name}

        with self._api_lock:
            if file_id:
                # Update existing file
                result = (
                    self.service.files()
                    .update(fileId=file_id, media_body=media,
                            fields="id,md5Checksum,modifiedTime",
                            supportsAllDrives=True)
                    .execute()
                )
            else:
                # Create new file
                metadata["parents"] = [parent_id]
                result = (
                    self.service.files()
                    .create(body=metadata, media_body=media,
                            fields="id,md5Checksum,modifiedTime",
                            supportsAllDrives=True)
                    .execute()
                )

        log.debug("Uploaded: %s -> %s", local_path.name, result["id"])
        return result

    def create_folder(self, name: str, parent_id: str) -> str:
        """Create a folder on Drive, return its ID."""
        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        with self._api_lock:
            result = self.service.files().create(
                body=metadata, fields="id", supportsAllDrives=True
            ).execute()
        return result["id"]

    def delete_file(self, file_id: str):
        """Trash a file on Drive."""
        with self._api_lock:
            self.service.files().update(
                fileId=file_id, body={"trashed": True}, supportsAllDrives=True
            ).execute()
        log.debug("Trashed remote file: %s", file_id)

    def get_file_metadata(self, file_id: str) -> dict:
        """Get metadata for a single file."""
        with self._api_lock:
            return (
                self.service.files()
                .get(fileId=file_id, fields="id,name,mimeType,md5Checksum,modifiedTime,parents,trashed,size")
                .execute()
            )

    def list_shared_drives(self) -> list[dict]:
        """List all shared drives accessible by this account."""
        drives = []
        page_token = None
        with self._api_lock:
            while True:
                resp = self.service.drives().list(
                    pageSize=100, pageToken=page_token
                ).execute()
                drives.extend(resp.get("drives", []))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        return drives

    def get_my_drive_storage(self) -> dict:
        """Get storage quota for My Drive.

        Returns dict with keys: usage, limit (in bytes). limit=0 means unlimited.
        """
        with self._api_lock:
            resp = self.service.about().get(fields="storageQuota").execute()
        quota = resp.get("storageQuota", {})
        return {
            "usage": int(quota.get("usage", 0)),
            "limit": int(quota.get("limit", 0)),
        }

    def get_shared_drive_usage(self, drive_id: str) -> int:
        """Get total storage used by a shared drive (sum of all file sizes).

        Returns usage in bytes.
        """
        total = 0
        page_token = None
        with self._api_lock:
            while True:
                kwargs = {
                    "q": "trashed = false",
                    "corpora": "drive",
                    "driveId": drive_id,
                    "includeItemsFromAllDrives": True,
                    "supportsAllDrives": True,
                    "fields": "nextPageToken,files(size)",
                    "pageSize": 1000,
                }
                if page_token:
                    kwargs["pageToken"] = page_token
                resp = self.service.files().list(**kwargs).execute()
                for f in resp.get("files", []):
                    total += int(f.get("size", 0) or 0)
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        return total

    def get_shared_with_me_stats(self) -> dict:
        """Get statistics for files shared with me.

        Returns dict with keys:
        - file_count: number of files shared with me
        - total_size: total size in bytes (excludes Google Docs which have size=0)
        """
        total_files = 0
        total_size = 0
        page_token = None

        with self._api_lock:
            while True:
                resp = self.service.files().list(
                    q="sharedWithMe=true and mimeType!='application/vnd.google-apps.folder' and trashed=false",
                    fields="nextPageToken,files(id,size)",
                    pageSize=1000,
                    pageToken=page_token,
                ).execute()

                for f in resp.get("files", []):
                    total_files += 1
                    total_size += int(f.get("size", 0) or 0)

                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        return {
            "file_count": total_files,
            "total_size": total_size,
        }

    def move_file(self, file_id: str, body: Optional[dict] = None,
                  add_parents: Optional[str] = None,
                  remove_parents: Optional[str] = None):
        """Rename and/or reparent a file on Drive."""
        kwargs = {"fileId": file_id, "supportsAllDrives": True}
        if body:
            kwargs["body"] = body
        if add_parents:
            kwargs["addParents"] = add_parents
        if remove_parents:
            kwargs["removeParents"] = remove_parents
        with self._api_lock:
            self.service.files().update(**kwargs).execute()

    def list_folders(self, extra_kwargs: Optional[dict] = None) -> list[dict]:
        """List all non-trashed folders in the drive. Used for folder indexing."""
        all_folders = []
        page_token = None
        kwargs = {
            "q": "mimeType = 'application/vnd.google-apps.folder' and trashed = false",
            "spaces": "drive",
            "fields": "nextPageToken,files(id,name,parents)",
            "pageSize": 1000,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if extra_kwargs:
            kwargs.update(extra_kwargs)
        with self._api_lock:
            while True:
                if page_token:
                    kwargs["pageToken"] = page_token
                resp = self.service.files().list(**kwargs).execute()
                all_folders.extend(resp.get("files", []))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        return all_folders

    def find_folder(self, name: str, parent_id: str) -> Optional[str]:
        """Find a folder by name under a given parent. Returns file ID or None."""
        escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
        query = (
            f"name = '{escaped_name}' and '{parent_id}' in parents "
            f"and mimeType = 'application/vnd.google-apps.folder' "
            f"and trashed = false"
        )
        with self._api_lock:
            resp = self.service.files().list(
                q=query, fields="files(id)", pageSize=1,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
        files = resp.get("files", [])
        return files[0]["id"] if files else None
