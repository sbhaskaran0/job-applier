"""Removal-pass safety (Workday's offset wall).

The store marks a posting removed when it is absent from a board's fetch.
That inference is only sound when the fetch saw the WHOLE board -- true for
Greenhouse/Lever/Ashby, which answer with the complete list in one request,
and false for a walled Workday board, whose API replays page 0 past offset
2000 so the fetch is a window rather than the board. Absent-from-a-window
means unknown, not gone.

These tests pin both halves of that rule: a complete fetch still removes, an
incomplete one never does, and an incomplete fetch is not logged as a
failure (which would trip the dark-board alarm on a healthy board).

Standalone, like the rest of tests/ -- `python tests/test_refresh_removals.py`.
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, store          # noqa: E402

BOARD = {"name": "Acme", "ats": "workday", "slug": "acme:wd5:External"}
STAMP = "2026-09-01T00:00:00+00:00"


def _row(job_id, title="Product Manager"):
    return {"company": "Acme", "ats": "workday", "slug": "acme:wd5:External",
            "job_id": job_id, "title": title, "location": "Remote - USA",
            "remote": True, "salary_min": None, "salary_max": None,
            "posted": "2026-09-01", "url": "https://x.test/" + job_id,
            "description": ""}


def _fixture(monkey_conn):
    """Seed two live postings on one Workday board."""
    for jid in ("JR1", "JR2"):
        monkey_conn.execute(
            "INSERT INTO postings (ats, slug, job_id, company, title, location,"
            " remote, url, first_seen, last_seen) VALUES"
            " ('workday','acme:wd5:External',?,?,?,?,?,?,?,?)",
            (jid, "Acme", "Product Manager", "Remote - USA", 1,
             "https://x.test/" + jid, STAMP, STAMP))
    monkey_conn.commit()


class _KeepOpen:
    """refresh_from_fetch closes the connection it is handed; the assertions
    need it afterwards, so proxy everything except close()."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


def _run(fetch_result):
    """Run refresh_from_fetch against an in-memory store + a one-board watchlist."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store._SCHEMA)
    # _SCHEMA is the v1 shape; the v2-v4 columns refresh_from_fetch writes are
    # added by connect()'s migration step, so run it here too.
    store._migrate(conn)
    _fixture(conn)
    real_connect, real_watchlist = store.connect, config.load_watchlist
    store.connect = lambda: _KeepOpen(conn)
    config.load_watchlist = lambda: [BOARD]
    try:
        summary = store.refresh_from_fetch(fetch_result)
        still_live = {r["job_id"] for r in conn.execute(
            "SELECT job_id FROM postings WHERE removed_at IS NULL")}
        return summary, still_live
    finally:
        store.connect, config.load_watchlist = real_connect, real_watchlist
        conn.close()


def test_complete_fetch_still_removes_a_vanished_posting():
    # JR2 is genuinely gone and the fetch saw the whole board.
    summary, live = _run({"postings": [_row("JR1")], "errors": []})
    assert summary["removed_count"] == 1, summary["removed_count"]
    assert live == {"JR1"}, live


def test_incomplete_fetch_removes_nothing():
    # Same fetch, but the board hit the wall: JR2's absence proves nothing.
    summary, live = _run({
        "postings": [_row("JR1")], "errors": [],
        "incomplete": [{"company": "Acme", "reason": "hit the offset wall"}]})
    assert summary["removed_count"] == 0, summary["removed_count"]
    assert live == {"JR1", "JR2"}, live


def test_incomplete_is_not_logged_as_a_board_failure():
    # An incomplete fetch is healthy; logging it as failed would trip the
    # dark-board alarm in the digest after three runs.
    summary, _ = _run({
        "postings": [_row("JR1")], "errors": [],
        "incomplete": [{"company": "Acme", "reason": "hit the offset wall"}]})
    assert summary["companies_failed"] == [], summary["companies_failed"]


def test_failed_fetch_still_blocks_removal():
    # The pre-existing guarantee, unchanged by the incomplete branch.
    summary, live = _run({
        "postings": [], "errors": [{"company": "Acme", "reason": "boom"}]})
    assert summary["removed_count"] == 0, summary["removed_count"]
    assert live == {"JR1", "JR2"}, live


def test_missing_incomplete_key_is_tolerated():
    # Every existing caller builds the dict without an `incomplete` key.
    summary, _ = _run({"postings": [_row("JR1"), _row("JR2")], "errors": []})
    assert summary["removed_count"] == 0, summary["removed_count"]
    assert json.loads(json.dumps(summary["companies_failed"])) == []


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
