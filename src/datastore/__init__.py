"""Storage seam for per-user domain data (answer history + application log).

Phase T1 is the seam only: `FileBackend` is byte-for-byte today's behavior,
and files stay the default. `src/config.py`'s four public loaders
(`load_history`, `save_history`, `load_applications`, `save_applications`)
delegate to `get_backend()` here; their signatures and behavior don't change.

Backend selection is config-driven via the `JOB_APPLIER_DATASTORE` env var:
  - "file" (default, and the fallback for anything unset or unrecognized —
    a typo must not take the MCP server down)
  - "taskman", once a `TaskmanBackend` exists and is registered in
    `_BACKENDS` below (a later wave; no change to src/config.py needed then)

Selection is resolved lazily (never at import time) and memoized per process
via `get_backend()`; `reset_backend()` drops the memo for tests.
"""

import os

from .protocol import Repository
from .file_backend import FileBackend
from .taskman_backend import TaskmanBackend

_BACKENDS = {
    "file": FileBackend,
    "taskman": TaskmanBackend,
}

_backend: Repository | None = None


def get_backend() -> Repository:
    """Return the active Repository, resolved from JOB_APPLIER_DATASTORE and
    memoized for the life of the process (see `reset_backend`)."""
    global _backend
    if _backend is None:
        name = os.environ.get("JOB_APPLIER_DATASTORE", "file").strip().lower()
        _backend = _BACKENDS.get(name, FileBackend)()
    return _backend


def reset_backend() -> None:
    """Drop the memoized backend so the next `get_backend()` call re-resolves
    JOB_APPLIER_DATASTORE. Tests only."""
    global _backend
    _backend = None


__all__ = ["Repository", "FileBackend", "get_backend", "reset_backend"]
