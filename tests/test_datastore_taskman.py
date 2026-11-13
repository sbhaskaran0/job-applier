"""TaskmanBackend must keep save_* exactly as reliable as FileBackend alone,
even though it also tries to mirror writes to taskman.

Pins: taskman being completely unreachable never breaks a save (the single
most important test here), 401 / 5xx / malformed-JSON / unregistered-type
(422) responses are all swallowed the same way, no TASKMAN_API_KEY degrades
silently to file-only with zero HTTP calls, reads always come from files
(byte-identical to FileBackend, never taskman), a history/application record
maps onto taskman's qa_answer / job_application node shape correctly, only
genuinely new records get mirrored on a given save, and a mirror failure is
logged rather than raised.

Never touches the real profile's data files or the real taskman service --
every test points profiles.active() at a throwaway temp dir and monkeypatches
httpx.post so nothing here makes a network call. This must pass with the
taskman service down and no API key, because that is CI's situation (and
often the user's).

Standalone, like the rest of tests/ -- `python tests/test_datastore_taskman.py`.
"""

import io
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from src import datastore, profiles                        # noqa: E402
from src.datastore import taskman_backend as tb_mod         # noqa: E402
from src.datastore.file_backend import FileBackend          # noqa: E402
from src.datastore.taskman_backend import TaskmanBackend    # noqa: E402


def _fake_profile(root: Path, profile_id: str = "tmp") -> profiles.ActiveProfile:
    root.mkdir(parents=True, exist_ok=True)
    return profiles.ActiveProfile(profile_id=profile_id, root=root)


class _FakeResponse:
    """Stand-in for an httpx.Response: only what _create_node touches."""

    def __init__(self, status_code=201, payload=None, text="", bad_json=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"id": "fake"}
        self.text = text
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("response body is not JSON")
        return self._payload


class _Sandbox:
    """Points profiles.active() at a temp dir for the duration of a test,
    clears/restores TASKMAN_* + JOB_APPLIER_DATASTORE env vars, resets the
    memoized datastore backend and taskman_backend's "warned once" latch, and
    monkeypatches httpx.post (via fake_post) so no test ever makes a real
    network call."""

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="datastore_taskman_test_"))
        self._real_active = profiles.active
        self._env_keys = ("JOB_APPLIER_DATASTORE", "TASKMAN_URL", "TASKMAN_API_KEY")
        self._saved_env = {k: os.environ.get(k) for k in self._env_keys}
        for k in self._env_keys:
            os.environ.pop(k, None)
        self.profile = _fake_profile(self.tmp / "p1")
        profiles.active = lambda: self.profile
        datastore.reset_backend()
        tb_mod._warned_no_key = False
        self._real_post = httpx.post
        return self

    def fake_post(self, fn):
        """Route the module-level httpx.post used inside taskman_backend to
        `fn` for the duration of this sandbox."""
        httpx.post = fn

    def __exit__(self, *exc):
        profiles.active = self._real_active
        httpx.post = self._real_post
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        datastore.reset_backend()
        tb_mod._warned_no_key = False
        shutil.rmtree(self.tmp, ignore_errors=True)


def _capture_warnings():
    """Attach a StringIO handler to taskman_backend's logger; returns
    (stream, handler) -- caller removes the handler when done."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    tb_mod.logger.addHandler(handler)
    tb_mod.logger.setLevel(logging.WARNING)
    return stream, handler


# --------------------------------------------------------------------------- #
# The critical one: offline/unreachable never breaks a save.
# --------------------------------------------------------------------------- #
def test_unreachable_taskman_save_applications_still_succeeds():
    with _Sandbox() as sb:
        os.environ["TASKMAN_API_KEY"] = "test-key"
        sb.fake_post(lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")))
        backend = TaskmanBackend()
        payload = [{"company": "Acme", "job_title": "PM", "url": "https://x.test/1",
                   "date": "2026-09-16", "status": "submitted", "fields": []}]
        backend.save_applications(payload)  # must not raise
        assert backend.load_applications() == payload
        raw = json.loads(sb.profile.applications_json.read_text(encoding="utf-8"))
        assert raw == payload, "the file write must have happened regardless of taskman"


def test_unreachable_taskman_save_history_still_succeeds():
    with _Sandbox() as sb:
        os.environ["TASKMAN_API_KEY"] = "test-key"
        sb.fake_post(lambda *a, **k: (_ for _ in ()).throw(httpx.TimeoutException("slow")))
        backend = TaskmanBackend()
        payload = [{"question": "Why us?", "answer": "Because.", "scope": "evergreen",
                   "company": "", "date": "2026-09-16"}]
        backend.save_history(payload)  # must not raise
        assert backend.load_history() == payload


# --------------------------------------------------------------------------- #
# Every flavor of taskman failure is swallowed the same way.
# --------------------------------------------------------------------------- #
def test_401_response_is_swallowed():
    with _Sandbox() as sb:
        os.environ["TASKMAN_API_KEY"] = "bad-key"
        sb.fake_post(lambda *a, **k: _FakeResponse(status_code=401, text="unauthorized"))
        backend = TaskmanBackend()
        backend.save_history([{"question": "q", "answer": "a"}])
        assert backend.load_history() == [{"question": "q", "answer": "a"}]


def test_5xx_response_is_swallowed():
    with _Sandbox() as sb:
        os.environ["TASKMAN_API_KEY"] = "key"
        sb.fake_post(lambda *a, **k: _FakeResponse(status_code=500, text="boom"))
        backend = TaskmanBackend()
        backend.save_history([{"question": "q", "answer": "a"}])
        assert backend.load_history() == [{"question": "q", "answer": "a"}]


def test_malformed_json_response_is_swallowed():
    with _Sandbox() as sb:
        os.environ["TASKMAN_API_KEY"] = "key"
        sb.fake_post(lambda *a, **k: _FakeResponse(status_code=201, bad_json=True))
        backend = TaskmanBackend()
        backend.save_history([{"question": "q", "answer": "a"}])
        assert backend.load_history() == [{"question": "q", "answer": "a"}]


def test_422_unregistered_type_is_swallowed():
    with _Sandbox() as sb:
        os.environ["TASKMAN_API_KEY"] = "key"
        sb.fake_post(lambda *a, **k: _FakeResponse(status_code=422, text="unknown type"))
        backend = TaskmanBackend()
        payload = [{"company": "Acme", "job_title": "PM"}]
        backend.save_applications(payload)
        assert backend.load_applications() == payload


# --------------------------------------------------------------------------- #
# No API key -> file-only, no HTTP calls, no crash.
# --------------------------------------------------------------------------- #
def test_no_api_key_is_file_only_and_never_crashes():
    with _Sandbox() as sb:
        os.environ.pop("TASKMAN_API_KEY", None)
        calls = []
        sb.fake_post(lambda *a, **k: calls.append((a, k)) or _FakeResponse())
        backend = TaskmanBackend()
        payload = [{"company": "Acme", "job_title": "PM"}]
        backend.save_applications(payload)
        assert calls == [], "no API key should mean zero HTTP calls, not a failed one"
        assert backend.load_applications() == payload


# --------------------------------------------------------------------------- #
# Reads always come from files, byte-identical to FileBackend.
# --------------------------------------------------------------------------- #
def test_reads_match_file_backend_and_never_call_taskman():
    with _Sandbox() as sb:
        os.environ["TASKMAN_API_KEY"] = "key"
        read_calls = []
        sb.fake_post(lambda *a, **k: read_calls.append(1) or _FakeResponse())
        tb = TaskmanBackend()
        fb = FileBackend()
        payload_h = [{"question": "q1", "answer": "a1"}]
        payload_a = [{"company": "Acme", "job_title": "PM"}]
        tb.save_history(payload_h)
        tb.save_applications(payload_a)

        mirror_calls_after_saves = len(read_calls)
        assert tb.load_history() == fb.load_history() == payload_h
        assert tb.load_applications() == fb.load_applications() == payload_a
        # Reading must not have triggered any additional HTTP activity.
        assert len(read_calls) == mirror_calls_after_saves


# --------------------------------------------------------------------------- #
# Record -> taskman node shape.
# --------------------------------------------------------------------------- #
def test_qa_answer_payload_shape():
    with _Sandbox() as sb:
        tb = TaskmanBackend()
        record = {"question": "Why this role?", "answer": "Because X.",
                 "scope": "company", "company": "Acme", "date": "2026-09-16",
                 "alternates": ["Because Y."]}
        payload = tb._qa_answer_payload(record)
        assert payload["type"] == "qa_answer"
        assert payload["title"] == "Why this role?"          # question -> title
        assert payload["description"] == "Because X."        # answer -> description
        assert payload["status"] == "company"                 # scope is the status vocabulary
        assert payload["props"]["scope"] == "company"
        assert payload["props"]["company"] == "Acme"
        assert payload["props"]["owner"] == sb.profile.profile_id
        assert payload["props"]["alternates"] == ["Because Y."]


def test_qa_answer_payload_skips_blank_question():
    with _Sandbox():
        tb = TaskmanBackend()
        assert tb._qa_answer_payload({"question": "   ", "answer": "a"}) is None


def test_job_application_payload_shape():
    with _Sandbox() as sb:
        tb = TaskmanBackend()
        record = {"company": "Acme", "job_title": "PM",
                 "url": "https://x.test/job?gh_src=1", "date": "2026-09-16",
                 "status": "submitted", "outcome": "screen",
                 "outcome_date": "2026-09-20",
                 "fields": [{"question": "q", "answer": "a"}]}
        payload = tb._job_application_payload(record)
        assert payload["type"] == "job_application"
        assert payload["title"] == "Acme — PM"
        assert payload["status"] == "submitted"
        assert payload["props"]["url"] == record["url"]
        assert payload["props"]["company"] == "Acme"
        assert payload["props"]["job_title"] == "PM"
        assert payload["props"]["outcome"] == "screen"
        assert payload["props"]["outcome_date"] == "2026-09-20"
        assert payload["props"]["fields"] == record["fields"]
        assert payload["props"]["owner"] == sb.profile.profile_id
        assert payload["props"]["dedupe_key"]  # non-empty, normalized


# --------------------------------------------------------------------------- #
# Only new-since-last-save records get mirrored.
# --------------------------------------------------------------------------- #
def test_only_new_records_are_mirrored():
    with _Sandbox() as sb:
        os.environ["TASKMAN_API_KEY"] = "key"
        calls = []

        def fake(url, json=None, **k):
            calls.append(json)
            return _FakeResponse()

        sb.fake_post(fake)
        backend = TaskmanBackend()
        backend.save_history([{"question": "q1", "answer": "a1"}])
        assert len(calls) == 1
        backend.save_history([{"question": "q1", "answer": "a1"},
                              {"question": "q2", "answer": "a2"}])
        assert len(calls) == 2, "only the new q2 record should have been mirrored"
        assert calls[1]["title"] == "q2"


# --------------------------------------------------------------------------- #
# Divergence is logged, never raised.
# --------------------------------------------------------------------------- #
def test_mirror_failure_is_logged_not_raised():
    with _Sandbox() as sb:
        os.environ["TASKMAN_API_KEY"] = "key"
        sb.fake_post(lambda *a, **k: _FakeResponse(status_code=422, text="bad type"))
        stream, handler = _capture_warnings()
        try:
            backend = TaskmanBackend()
            backend.save_history([{"question": "q", "answer": "a"}])  # must not raise
        finally:
            tb_mod.logger.removeHandler(handler)
        logged = stream.getvalue()
        assert "422" in logged or "rejected" in logged, logged


def test_unexpected_exception_inside_mirroring_is_swallowed():
    """A bug in the mirror path itself (not just an HTTP failure) must not
    take save_* down with it."""
    with _Sandbox() as sb:
        os.environ["TASKMAN_API_KEY"] = "key"

        tb = TaskmanBackend()

        def _boom(record):
            raise RuntimeError("simulated bug in payload building")

        tb._qa_answer_payload = _boom
        payload = [{"question": "q", "answer": "a"}]
        tb.save_history(payload)  # must not raise
        assert tb.load_history() == payload


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
