"""The `Repository` contract — the seam between config.py's four public
loaders and wherever the per-user domain data (answer history, application
log) actually lives.

Phase T1 ships exactly one implementation, `FileBackend`, which is
byte-for-byte today's flat-JSON behavior. A later `TaskmanBackend` slots in
against this same protocol with no change to src/config.py.
"""

from typing import Protocol


class Repository(Protocol):
    """A backend for the answer history and the application log.

    Guarantees every implementation MUST uphold:

    - `load_*` returns `[]` rather than raising, both when the underlying
      store is missing and when it exists but is corrupt/unparseable.
    - `save_*` is atomic-or-nothing: a concurrent reader never observes a
      partial write, and a failure mid-write leaves the previous contents
      intact.
    - No method caches the active profile (or any other resolved location)
      across calls. The active profile can change at runtime (profile
      switching), so every call must re-resolve its target fresh.
    """

    def load_history(self) -> list:
        ...

    def save_history(self, history: list) -> None:
        ...

    def load_applications(self) -> list:
        ...

    def save_applications(self, applications: list) -> None:
        ...
