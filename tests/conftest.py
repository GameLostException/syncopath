"""Shared fixtures: a SyncEngine wired to an in-memory Drive and a temp state DB."""
import shutil
from unittest.mock import patch

import pytest

from syncopath.config import AccountConfig
from syncopath.state import StateDB
from syncopath.sync_engine import SyncEngine
from tests.mock_drive import MockDriveAPI


@pytest.fixture
def make_env(tmp_path):
    """Factory: make_env(safe_mode=True, **account_overrides) -> env dict."""
    patches = [
        patch("syncopath.state.STATE_DIR", tmp_path / "state"),
        patch("syncopath.sync_engine.CONFIG_DIR", tmp_path / "config"),
    ]
    for p in patches:
        p.start()
    (tmp_path / "state").mkdir()
    (tmp_path / "config").mkdir()
    created = []

    def _make(safe_mode: bool = True, **overrides):
        local_root = tmp_path / f"gdrive{len(created)}"
        local_root.mkdir()
        fields = dict(name=f"test{len(created)}", local_path=local_root, remote_id="root",
                      poll_interval=30, debounce=1, include_shared_with_me=False)
        fields.update(overrides)
        config = AccountConfig(**fields)
        state_db = StateDB(config.name)
        drive = MockDriveAPI()
        engine = SyncEngine(config, drive, state_db, safe_mode=safe_mode)
        # Never touch the real desktop trash: "trash" into a temp folder instead
        trash = tmp_path / f"trash{len(created)}"
        trash.mkdir()

        def fake_trash(path):
            target = trash / f"{len(list(trash.iterdir()))}-{path.name}"
            shutil.move(str(path), str(target))

        engine._trash_local = fake_trash
        env = {"engine": engine, "drive": drive, "state_db": state_db,
               "local_root": local_root, "config": config, "trash": trash}
        created.append(env)
        return env

    yield _make
    for env in created:
        env["state_db"].close()
    for p in patches:
        p.stop()


@pytest.fixture
def setup(make_env):
    return make_env()
