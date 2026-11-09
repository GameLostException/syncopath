"""SyncoPath — The obsessive Google Drive syncer for Linux."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("syncopath")  # single source: pyproject.toml
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
