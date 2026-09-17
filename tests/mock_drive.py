"""Mock Google Drive API for integration testing.

Provides an in-memory Drive filesystem that mimics the real DriveAPI class
without making network calls. Supports files, folders, metadata, changes,
and simulated errors.
"""
import hashlib
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class MockDriveFile:
    """An in-memory Drive file/folder."""
    id: str
    name: str
    mime_type: str
    parents: list[str] = field(default_factory=list)
    md5: Optional[str] = None
    content: bytes = b""
    modified_time: str = "2026-01-01T00:00:00.000Z"
    trashed: bool = False
    owned_by_me: bool = True
    size: int = 0


class MockDriveAPI:
    """In-memory mock of DriveAPI for testing without network.

    Simulates:
    - File/folder CRUD
    - Changes API (page token tracking)
    - list_folder_recursive with path building
    - Download/upload
    - Real root ID resolution
    - Shared-with-me filtering
    """

    def __init__(self):
        self._files: dict[str, MockDriveFile] = {}
        self._root_id = "mock-root-id-000"
        self._page_token_counter = 0
        self._changes: list[dict] = []  # Pending changes since last token
        self._real_root_id = self._root_id
        self.shared_drive_id = None

        # Create root folder
        self._files[self._root_id] = MockDriveFile(
            id=self._root_id,
            name="My Drive",
            mime_type="application/vnd.google-apps.folder",
        )

    # ─── Public API (matches DriveAPI interface) ───────────────────────────

    def get_real_root_id(self) -> str:
        return self._real_root_id

    def get_start_page_token(self) -> str:
        return str(self._page_token_counter)

    def get_changes(self, page_token: str) -> tuple[list[dict], str]:
        """Return changes since the given token."""
        token_int = int(page_token)
        changes = self._changes[token_int:]
        new_token = str(len(self._changes))
        return changes, new_token

    def list_folder_recursive(self, folder_id: str = "root",
                              include_shared_with_me: bool = False) -> list[dict]:
        """List all files with their full paths."""
        from syncopath.drive_api import sanitize_filename

        root_id = self._real_root_id
        if folder_id != "root":
            root_id = folder_id

        # Build id->file map
        id_map = {fid: f for fid, f in self._files.items()
                  if not f.trashed}

        result = []
        for fid, f in id_map.items():
            if f.mime_type == "application/vnd.google-apps.folder":
                continue

            # Build path
            parts = [sanitize_filename(f.name)]
            current = f
            is_owned = False

            while True:
                if not current.parents:
                    break
                parent_id = current.parents[0]
                if parent_id == root_id:
                    is_owned = True
                    break
                if parent_id not in id_map:
                    break
                parent = id_map[parent_id]
                parts.insert(0, sanitize_filename(parent.name))
                current = parent

            if not is_owned and not include_shared_with_me:
                continue

            path = "/".join(parts)
            if not is_owned and include_shared_with_me:
                path = f"Shared with me/{path}"

            result.append({
                "id": f.id,
                "name": f.name,
                "mimeType": f.mime_type,
                "md5Checksum": f.md5,
                "modifiedTime": f.modified_time,
                "parents": f.parents,
                "size": f.size,
                "ownedByMe": f.owned_by_me,
                "_path": path,
            })

        return result

    def download_file(self, file_id: str, mime_type: str, dest_path: Path):
        """Download a file to local path."""
        f = self._files.get(file_id)
        if not f:
            raise FileNotFoundError(f"File not found: {file_id}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(f.content)

    def upload_file(self, local_path: Path, parent_id: str,
                    file_id: Optional[str] = None) -> dict:
        """Upload or update a file."""
        content = local_path.read_bytes()
        md5 = hashlib.md5(content).hexdigest()
        now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

        if file_id and file_id in self._files:
            # Update existing
            f = self._files[file_id]
            f.content = content
            f.md5 = md5
            f.modified_time = now
            f.size = len(content)
        else:
            # Create new
            file_id = f"file-{len(self._files):04d}"
            f = MockDriveFile(
                id=file_id,
                name=local_path.name,
                mime_type="application/octet-stream",
                parents=[parent_id],
                md5=md5,
                content=content,
                modified_time=now,
                size=len(content),
            )
            self._files[file_id] = f

        # Record change
        self._changes.append({
            "fileId": file_id,
            "removed": False,
            "file": self._file_to_meta(f),
        })

        return {"id": file_id, "md5Checksum": md5, "modifiedTime": now}

    def create_folder(self, name: str, parent_id: str) -> str:
        """Create a folder, return its ID."""
        folder_id = f"folder-{len(self._files):04d}"
        self._files[folder_id] = MockDriveFile(
            id=folder_id,
            name=name,
            mime_type="application/vnd.google-apps.folder",
            parents=[parent_id],
        )
        return folder_id

    def delete_file(self, file_id: str):
        """Trash a file."""
        if file_id in self._files:
            self._files[file_id].trashed = True
            self._changes.append({
                "fileId": file_id,
                "removed": False,
                "file": {**self._file_to_meta(self._files[file_id]), "trashed": True},
            })

    def get_file_metadata(self, file_id: str) -> dict:
        """Get metadata for a file."""
        f = self._files.get(file_id)
        if not f:
            raise FileNotFoundError(f"File not found: {file_id}")
        return self._file_to_meta(f)

    def list_shared_drives(self) -> list[dict]:
        return []

    def get_shared_with_me_stats(self) -> dict:
        """Get stats for files shared with me (not owned)."""
        total_files = 0
        total_size = 0
        for f in self._files.values():
            if f.trashed:
                continue
            if f.mime_type == "application/vnd.google-apps.folder":
                continue
            if not f.owned_by_me:
                total_files += 1
                total_size += f.size
        return {"file_count": total_files, "total_size": total_size}

    def list_folders(self, extra_kwargs=None) -> list[dict]:
        """List all non-trashed folders."""
        return [
            self._file_to_meta(f) for f in self._files.values()
            if not f.trashed and f.mime_type == "application/vnd.google-apps.folder"
        ]

    def find_folder(self, name: str, parent_id: str):
        """Find a folder by name under a given parent. Returns ID or None."""
        for f in self._files.values():
            if (f.name == name and f.mime_type == "application/vnd.google-apps.folder"
                    and not f.trashed and f.parent_id == parent_id):
                return f.file_id
        return None

    def move_file(self, file_id: str, body=None, add_parents=None, remove_parents=None):
        """Rename and/or reparent a file."""
        f = self._files.get(file_id)
        if not f:
            raise FileNotFoundError(f"File not found: {file_id}")
        if body and "name" in body:
            f.name = body["name"]
        if add_parents:
            f.parent_id = add_parents
        self._changes.append({
            "fileId": file_id,
            "removed": False,
            "file": self._file_to_meta(f),
        })

    @property
    def service(self):
        """Mock service — returns self for chaining."""
        return _MockService(self)

    # ─── Test Helpers (not in real API) ────────────────────────────────────

    def add_file(self, name: str, parent_id: str = None, content: bytes = b"",
                 owned: bool = True, mime_type: str = "application/octet-stream",
                 file_id: str = None) -> str:
        """Helper to add a file for test setup."""
        if parent_id is None:
            if owned:
                parent_id = self._root_id
            else:
                # Shared files have a parent outside our tree
                parent_id = "external-owner-root"
        if file_id is None:
            file_id = f"file-{len(self._files):04d}"

        md5 = hashlib.md5(content).hexdigest() if content else None
        now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

        self._files[file_id] = MockDriveFile(
            id=file_id,
            name=name,
            mime_type=mime_type,
            parents=[parent_id],
            md5=md5,
            content=content,
            modified_time=now,
            owned_by_me=owned,
            size=len(content),
        )
        return file_id

    def add_folder(self, name: str, parent_id: str = None,
                   folder_id: str = None, owned: bool = True) -> str:
        """Helper to add a folder for test setup."""
        if parent_id is None:
            parent_id = self._root_id
        if folder_id is None:
            folder_id = f"folder-{len(self._files):04d}"

        self._files[folder_id] = MockDriveFile(
            id=folder_id,
            name=name,
            mime_type="application/vnd.google-apps.folder",
            parents=[parent_id],
            owned_by_me=owned,
        )
        return folder_id

    def simulate_remote_change(self, file_id: str, new_content: bytes = None,
                                new_name: str = None):
        """Simulate a remote modification for Changes API."""
        f = self._files.get(file_id)
        if not f:
            return
        if new_content is not None:
            f.content = new_content
            f.md5 = hashlib.md5(new_content).hexdigest()
            f.size = len(new_content)
        if new_name is not None:
            f.name = new_name
        f.modified_time = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

        self._changes.append({
            "fileId": file_id,
            "removed": False,
            "file": self._file_to_meta(f),
        })

    def simulate_remote_delete(self, file_id: str):
        """Simulate a remote deletion."""
        self._changes.append({
            "fileId": file_id,
            "removed": True,
            "file": None,
        })
        if file_id in self._files:
            self._files[file_id].trashed = True

    # ─── Internal ──────────────────────────────────────────────────────────

    def _file_to_meta(self, f: MockDriveFile) -> dict:
        return {
            "id": f.id,
            "name": f.name,
            "mimeType": f.mime_type,
            "md5Checksum": f.md5,
            "modifiedTime": f.modified_time,
            "parents": f.parents,
            "trashed": f.trashed,
            "size": f.size,
            "ownedByMe": f.owned_by_me,
        }


class _MockService:
    """Minimal mock for drive.service.files().list/update calls."""

    def __init__(self, mock_drive: MockDriveAPI):
        self._drive = mock_drive

    def files(self):
        return _MockFilesResource(self._drive)


class _MockFilesResource:
    """Mock for service.files() calls."""

    def __init__(self, mock_drive: MockDriveAPI):
        self._drive = mock_drive

    def list(self, **kwargs):
        q = kwargs.get("q", "")
        results = []
        for f in self._drive._files.values():
            if f.trashed:
                continue
            # Simple query parsing for folder lookups
            if "in parents" in q:
                parent_id = q.split("'")[1]
                if parent_id not in f.parents:
                    continue
            if "mimeType = 'application/vnd.google-apps.folder'" in q:
                if f.mime_type != "application/vnd.google-apps.folder":
                    continue
            if "name = " in q:
                name = q.split("name = '")[1].split("'")[0]
                if f.name != name:
                    continue
            results.append(self._drive._file_to_meta(f))
        return _MockRequest({"files": results})

    def get(self, fileId: str, **kwargs):
        return _MockRequest(self._drive._file_to_meta(
            self._drive._files[fileId]))

    def update(self, fileId: str, **kwargs):
        f = self._drive._files.get(fileId)
        if f:
            body = kwargs.get("body", {})
            if "name" in body:
                f.name = body["name"]
            add = kwargs.get("addParents")
            remove = kwargs.get("removeParents")
            if add:
                f.parents = [add]
        return _MockRequest(self._drive._file_to_meta(f) if f else {})


class _MockRequest:
    """Mock for .execute() pattern."""

    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result
