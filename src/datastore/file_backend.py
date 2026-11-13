"""FileBackend — the default `Repository`: today's flat-JSON files, unchanged.

Every path is resolved through `profiles.active()` inside each method call
(never cached on `self` or at import time), because the active profile can
be switched at runtime and this backend must observe that immediately.
"""

import json
import os
import tempfile
from pathlib import Path

from .. import profiles


def _load_json(path: Path) -> list:
    """Return the parsed JSON list at `path`, or `[]` if it's missing or
    unparseable. Both fallbacks are load-bearing — callers rely on getting a
    usable empty list rather than an exception."""
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _atomic_write_json(path: Path, payload) -> None:
    """Whole-file JSON write via temp + os.replace so a concurrent reader never
    sees a truncated file and a crash never loses the previous version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class FileBackend:
    """Repository backed by the active profile's history.json / applications.json.
    Selected by default and whenever JOB_APPLIER_DATASTORE is unset or unrecognized."""

    def load_history(self) -> list:
        return _load_json(profiles.active().history_json)

    def save_history(self, history: list) -> None:
        _atomic_write_json(profiles.active().history_json, history)

    def load_applications(self) -> list:
        return _load_json(profiles.active().applications_json)

    def save_applications(self, applications: list) -> None:
        _atomic_write_json(profiles.active().applications_json, applications)
