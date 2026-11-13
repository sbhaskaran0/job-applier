"""Description handling in the ingest (fetch-once providers).

Workday's list endpoint carries no job description, so each one costs a second
HTTP request. Re-fetching all of them every refresh would add ~11 minutes to a
daily run to re-download text that effectively never changes, so the Workday
fetcher fetches each description ONCE and thereafter re-reports the posting
with the text it already stored.

That interacts badly with change detection if left alone: `description` is part
of `_content_hash`, so a posting re-reported with an EMPTY description hashes
differently, the ingest reads it as an edit, and the UPDATE erases the stored
text. These tests pin the guard that makes an empty description non-destructive
-- and the fact that a genuine edit still lands.

Standalone, like the rest of tests/ -- `python tests/test_ingest_descriptions.py`.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, store          # noqa: E402

BOARD = {"name": "Acme", "ats": "workday", "slug": "acme:wd5:External"}
TEXT = "Full job description. " * 40


class _KeepOpen:
    """refresh_from_fetch closes the connection it is handed; the assertions
    need it afterwards, so proxy everything except close()."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


def _row(description, job_id="JR1", title="Product Manager"):
    return {"company": "Acme", "ats": "workday", "slug": "acme:wd5:External",
            "job_id": job_id, "title": title, "location": "Remote - USA",
            "remote": True, "salary_min": None, "salary_max": None,
            "posted": "2026-09-01", "url": "https://x.test/" + job_id,
            "description": description}


def _fresh():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store._SCHEMA)
    store._migrate(conn)
    return conn


def _ingest(conn, postings):
    real_connect, real_watchlist = store.connect, config.load_watchlist
    store.connect = lambda: _KeepOpen(conn)
    config.load_watchlist = lambda: [BOARD]
    try:
        return store.refresh_from_fetch({"postings": postings, "errors": []})
    finally:
        store.connect, config.load_watchlist = real_connect, real_watchlist


def _desc(conn, job_id="JR1"):
    return conn.execute("SELECT description FROM postings WHERE job_id=?",
                        (job_id,)).fetchone()[0]


def test_empty_description_does_not_erase_stored_text():
    # The whole point: a fetch-once provider re-reports a known posting with no
    # text, and the stored description must survive.
    conn = _fresh()
    try:
        _ingest(conn, [_row(TEXT)])
        assert _desc(conn) == TEXT
        _ingest(conn, [_row("")])
        assert _desc(conn) == TEXT, "stored description was erased"
    finally:
        conn.close()


def test_empty_description_is_not_counted_as_a_change():
    # Not just preserved -- the row must not even look edited, or every refresh
    # would rewrite every Workday row for nothing.
    conn = _fresh()
    try:
        _ingest(conn, [_row(TEXT)])
        before = conn.execute(
            "SELECT content_hash, last_seen FROM postings").fetchone()
        _ingest(conn, [_row("")])
        after = conn.execute(
            "SELECT content_hash, last_seen FROM postings").fetchone()
        assert before["content_hash"] == after["content_hash"], "hash flipped"
    finally:
        conn.close()


def test_a_real_description_edit_still_lands():
    # The guard must not freeze descriptions: non-empty new text still wins.
    conn = _fresh()
    try:
        _ingest(conn, [_row(TEXT)])
        _ingest(conn, [_row("Rewritten description.")])
        assert _desc(conn) == "Rewritten description."
    finally:
        conn.close()


def test_first_sight_of_a_posting_with_no_text_is_stored_empty():
    # Nothing to preserve yet: an unhydrated new row is stored blank and is
    # eligible for hydration on a later run.
    conn = _fresh()
    try:
        _ingest(conn, [_row("")])
        assert _desc(conn) == ""
        _ingest(conn, [_row(TEXT)])
        assert _desc(conn) == TEXT, "later hydration did not land"
    finally:
        conn.close()


def test_missing_posted_date_does_not_null_a_known_one():
    # `posted` is NOT in the content hash, and the unchanged-hash branch
    # rewrites it every run -- so it needs its own guard or a fetch-once
    # provider silently nulls every date it stops re-reporting.
    conn = _fresh()
    try:
        _ingest(conn, [_row(TEXT)])
        assert conn.execute("SELECT posted FROM postings").fetchone()[0] == "2026-09-01"
        stale = {**_row(TEXT), "posted": None}
        _ingest(conn, [stale])
        assert conn.execute("SELECT posted FROM postings").fetchone()[0] == "2026-09-01",             "known posted date was nulled"
    finally:
        conn.close()


def test_a_real_posted_date_change_still_lands():
    conn = _fresh()
    try:
        _ingest(conn, [_row(TEXT)])
        _ingest(conn, [{**_row(TEXT), "posted": "2026-09-05"}])
        assert conn.execute("SELECT posted FROM postings").fetchone()[0] == "2026-09-05"
    finally:
        conn.close()


def test_non_string_posted_does_not_crash_the_ingest():
    # Providers disagree on the type of `posted`: Greenhouse/Ashby send ISO
    # strings, LEVER SENDS AN EPOCH INT. A guard that assumes str raises
    # AttributeError and takes down the whole refresh -- which is exactly what
    # happened before _is_blank existed, mid-backfill, after 18 minutes of
    # successful fetching.
    conn = _fresh()
    try:
        lever = {**_row(TEXT), "ats": "lever", "slug": "acme",
                 "posted": 1757462400000}
        _ingest(conn, [lever])
        # SQLite TEXT affinity stores the int as a string; the point of the
        # test is that neither ingest branch raises on a non-str value.
        got = conn.execute("SELECT posted FROM postings").fetchone()[0]
        assert str(got) == "1757462400000", repr(got)
        # again, so the row also goes down the already-exists branch, where the
        # stored value is now a str and the incoming one is still an int
        _ingest(conn, [lever])
        assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1
        got = conn.execute("SELECT posted FROM postings").fetchone()[0]
        assert str(got) == "1757462400000", repr(got)
    finally:
        conn.close()


def test_blank_helper_handles_every_shape():
    assert store._is_blank(None)
    assert store._is_blank("")
    assert store._is_blank("   ")
    assert not store._is_blank(0), "0 is a value, not a blank"
    assert not store._is_blank(1757462400000)
    assert not store._is_blank("2026-09-01")


def test_stored_detail_fields_returns_only_rows_with_text():
    conn = _fresh()
    try:
        _ingest(conn, [_row(TEXT, "JR1"), _row("", "JR2")])
        real_connect = store.connect
        store.connect = lambda: _KeepOpen(conn)
        try:
            got = store.stored_detail_fields("workday", "acme:wd5:External")
        finally:
            store.connect = real_connect
        assert set(got) == {"JR1"}, got
        assert got["JR1"]["description"] == TEXT
        # location and posted ride along: both are lost without carry-forward
        # (location is inside the content hash, posted is rewritten even when
        # the hash matches), and a degraded location fails the baseline gate.
        assert got["JR1"]["location"] == "Remote - USA", got["JR1"]
        assert got["JR1"]["posted"] == "2026-09-01", got["JR1"]
    finally:
        conn.close()


def test_stored_detail_fields_skips_removed_postings():
    # A removed posting's text must not keep a fetcher from re-hydrating it if
    # the role comes back.
    conn = _fresh()
    try:
        _ingest(conn, [_row(TEXT, "JR1")])
        conn.execute("UPDATE postings SET removed_at='2026-09-09T00:00:00+00:00'")
        conn.commit()
        real_connect = store.connect
        store.connect = lambda: _KeepOpen(conn)
        try:
            got = store.stored_detail_fields("workday", "acme:wd5:External")
        finally:
            store.connect = real_connect
        assert got == {}, got
    finally:
        conn.close()


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
