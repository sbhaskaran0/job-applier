"""FileBackend must be byte-for-byte today's config.py history/applications
behavior (Phase T1: the storage seam, no behavior change).

Pins: round-trip fidelity, the missing-file and corrupt-JSON fallbacks to
`[]`, atomic-write durability (no leftover .tmp, a mid-write failure leaves
the previous file intact), unicode + on-disk formatting (`indent=2`,
`ensure_ascii=False`), an unrecognized JOB_APPLIER_DATASTORE value falling
back to the file backend, and that profile switching is observed by the very
next call (the regression test for a cached path).

Never touches the real profile's data files -- every test points
`profiles.active()` at a throwaway temp directory.

Standalone, like the rest of tests/ -- `python tests/test_datastore_file.py`.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import datastore, profiles              # noqa: E402
from src.datastore.file_backend import FileBackend  # noqa: E402


def _fake_profile(root: Path, profile_id: str = "tmp") -> profiles.ActiveProfile:
    root.mkdir(parents=True, exist_ok=True)
    return profiles.ActiveProfile(profile_id=profile_id, root=root)


class _Sandbox:
    """Points profiles.active() at a temp dir for the duration of a test and
    restores the real resolver afterwards. Also resets the memoized backend
    and JOB_APPLIER_DATASTORE so tests don't bleed into each other."""

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="datastore_test_"))
        self._real_active = profiles.active
        self._env_had_datastore = "JOB_APPLIER_DATASTORE" in os.environ
        self._env_datastore = os.environ.get("JOB_APPLIER_DATASTORE")
        self.profile = _fake_profile(self.tmp / "p1")
        profiles.active = lambda: self.profile
        datastore.reset_backend()
        return self

    def switch_profile(self, profile_id: str = "p2"):
        self.profile = _fake_profile(self.tmp / profile_id, profile_id)

    def __exit__(self, *exc):
        profiles.active = self._real_active
        if self._env_had_datastore:
            os.environ["JOB_APPLIER_DATASTORE"] = self._env_datastore
        else:
            os.environ.pop("JOB_APPLIER_DATASTORE", None)
        datastore.reset_backend()
        shutil.rmtree(self.tmp, ignore_errors=True)


def test_round_trip_history():
    with _Sandbox():
        backend = FileBackend()
        payload = [{"question": "q1", "answer": "a1"}, {"question": "q2", "answer": "a2"}]
        backend.save_history(payload)
        assert backend.load_history() == payload


def test_round_trip_applications():
    with _Sandbox():
        backend = FileBackend()
        payload = [{"company": "Acme", "url": "https://x.test/1"}]
        backend.save_applications(payload)
        assert backend.load_applications() == payload


def test_missing_file_returns_empty_list():
    with _Sandbox():
        backend = FileBackend()
        assert backend.load_history() == []
        assert backend.load_applications() == []


def test_corrupt_json_returns_empty_list():
    with _Sandbox() as sb:
        sb.profile.history_json.parent.mkdir(parents=True, exist_ok=True)
        sb.profile.history_json.write_text("{not valid json", encoding="utf-8")
        backend = FileBackend()
        assert backend.load_history() == []


def test_write_leaves_no_tmp_file_behind():
    with _Sandbox() as sb:
        backend = FileBackend()
        backend.save_history([{"a": 1}])
        leftovers = list(sb.profile.data_dir.glob("*.tmp"))
        assert leftovers == [], leftovers


def test_failed_write_preserves_previous_file():
    with _Sandbox() as sb:
        backend = FileBackend()
        backend.save_history([{"a": "original"}])
        before = sb.profile.history_json.read_text(encoding="utf-8")

        class Unserializable:
            pass

        try:
            backend.save_history([{"a": Unserializable()}])
        except TypeError:
            pass
        else:
            raise AssertionError("expected the bad payload to raise")

        after = sb.profile.history_json.read_text(encoding="utf-8")
        assert after == before, "previous file was clobbered by a failed write"
        leftovers = list(sb.profile.data_dir.glob("*.tmp"))
        assert leftovers == [], leftovers


def test_unicode_and_formatting_preserved():
    with _Sandbox() as sb:
        backend = FileBackend()
        payload = [{"answer": "café — résumé 日本語"}]
        backend.save_history(payload)

        raw = sb.profile.history_json.read_text(encoding="utf-8")
        assert "café" in raw and "日本語" in raw, "ensure_ascii escaped unicode"
        assert raw == json.dumps(payload, indent=2, ensure_ascii=False), \
            "on-disk formatting drifted from indent=2/ensure_ascii=False"
        assert backend.load_history() == payload


def test_unrecognized_datastore_value_falls_back_to_file():
    with _Sandbox():
        os.environ["JOB_APPLIER_DATASTORE"] = "totally-not-a-backend"
        datastore.reset_backend()
        backend = datastore.get_backend()
        assert isinstance(backend, FileBackend), type(backend)
        # And it actually works rather than raising.
        backend.save_history([{"ok": True}])
        assert backend.load_history() == [{"ok": True}]


def test_default_datastore_is_file_backend():
    with _Sandbox():
        os.environ.pop("JOB_APPLIER_DATASTORE", None)
        datastore.reset_backend()
        assert isinstance(datastore.get_backend(), FileBackend)


def test_profile_switch_is_observed_on_the_next_call():
    with _Sandbox() as sb:
        backend = datastore.get_backend()
        backend.save_history([{"profile": "p1"}])
        assert backend.load_history() == [{"profile": "p1"}]

        sb.switch_profile("p2")  # profiles.active() now returns a different root

        # No re-resolution of the backend needed -- the SAME backend object
        # must read the new profile's (empty) file, not a cached path.
        assert backend.load_history() == [], \
            "load_history read the OLD profile's file after a profile switch"

        backend.save_history([{"profile": "p2"}])
        assert backend.load_history() == [{"profile": "p2"}]
        # p1's file is untouched.
        p1_history = json.loads((sb.tmp / "p1" / "data" / "history.json").read_text(encoding="utf-8"))
        assert p1_history == [{"profile": "p1"}]


def test_config_delegates_to_the_selected_backend():
    # src/config.py's four public functions must go through datastore, with
    # identical names/signatures and no behavior change.
    from src import config
    with _Sandbox():
        config.save_history([{"via": "config"}])
        assert config.load_history() == [{"via": "config"}]
        config.save_applications([{"via": "config"}])
        assert config.load_applications() == [{"via": "config"}]


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  %s" % name)
        except AssertionError as exc:
            failed += 1
            print("FAIL  %s: %s" % (name, exc))
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
