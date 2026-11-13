"""Removal-pass safety (Workday's offset wall + flaky listings).

The store marks a posting removed when it is absent from a board's fetch.
That inference is only sound when the fetch saw the WHOLE board -- true for
Greenhouse/Lever/Ashby, which answer with the complete list in one request,
and false for a walled Workday board, whose API replays page 0 past offset
2000 so the fetch is a window rather than the board. Absent-from-a-window
means unknown, not gone.

Even a complete Workday fetch is noisy (100-340 false removals per board per
run were observed), so Workday rows need REMOVAL_GRACE consecutive misses
(store schema v5 `missed_runs`) before removed_at is set; other ATSes keep the
one-miss rule.

These tests pin: a complete fetch still removes (after the grace), an
incomplete one never does, an incomplete fetch is not logged as a failure
(which would trip the dark-board alarm on a healthy board), a row missed once
and seen again is not "relisted", and the v4 -> v5 migration.

Standalone, like the rest of tests/ -- `python tests/test_refresh_removals.py`.
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, store          # noqa: E402

BOARD = {"name": "Acme", "ats": "workday", "slug": "acme:wd5:External"}
GH = {"name": "Acme", "ats": "greenhouse", "slug": "acme"}
STAMP = "2026-09-01T00:00:00+00:00"


def _row(job_id, title="Product Manager", ats="workday",
         slug="acme:wd5:External"):
    return {"company": "Acme", "ats": ats, "slug": slug,
            "job_id": job_id, "title": title, "location": "Remote - USA",
            "remote": True, "salary_min": None, "salary_max": None,
            "posted": "2026-09-01", "url": "https://x.test/" + job_id,
            "description": ""}


def _fixture(conn, ats, slug):
    """Seed two live postings on one board."""
    for jid in ("JR1", "JR2"):
        conn.execute(
            "INSERT INTO postings (ats, slug, job_id, company, title, location,"
            " remote, url, first_seen, last_seen) VALUES"
            " (?,?,?,?,?,?,?,?,?,?)",
            (ats, slug, jid, "Acme", "Product Manager", "Remote - USA", 1,
             "https://x.test/" + jid, STAMP, STAMP))
    conn.commit()


class _KeepOpen:
    """refresh_from_fetch closes the connection it is handed; the assertions
    need it afterwards, so proxy everything except close()."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


def _session(fetches, board=BOARD):
    """Run refresh_from_fetch once per fetch result against one in-memory
    store + a one-board watchlist. Returns (summaries, still_live, conn); the
    caller closes conn."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store._SCHEMA)
    store._migrate(conn)
    _fixture(conn, board["ats"], board["slug"])
    real_connect, real_watchlist = store.connect, config.load_watchlist
    real_criteria = config.load_search_criteria
    config.load_search_criteria = lambda: {"baseline": {}}
    store.connect = lambda: _KeepOpen(conn)
    config.load_watchlist = lambda: [board]
    try:
        summaries = [store.refresh_from_fetch(f) for f in fetches]
    finally:
        store.connect, config.load_watchlist = real_connect, real_watchlist
        config.load_search_criteria = real_criteria
    live = {r["job_id"] for r in conn.execute(
        "SELECT job_id FROM postings WHERE removed_at IS NULL")}
    return summaries, live, conn


def _run(fetch_result, board=BOARD):
    summaries, live, conn = _session([fetch_result], board)
    conn.close()
    return summaries[0], live


def _missed(conn, job_id):
    return conn.execute("SELECT missed_runs FROM postings WHERE job_id=?",
                        (job_id,)).fetchone()[0]


def _wd(*ids):
    return {"postings": [_row(i) for i in ids], "errors": []}


def test_workday_row_missed_once_stays_active():
    (summary,), live, conn = _session([_wd("JR1")])
    assert live == {"JR1", "JR2"}, live
    assert summary["removed_count"] == 0, summary["removed_count"]
    assert summary["pending_removal"] == 1, summary["pending_removal"]
    assert _missed(conn, "JR2") == 1
    conn.close()


def test_workday_row_missed_twice_is_removed():
    summaries, live, conn = _session([_wd("JR1"), _wd("JR1")])
    assert live == {"JR1"}, live
    assert summaries[0]["removed_count"] == 0
    assert summaries[1]["removed_count"] == 1, summaries[1]["removed_count"]
    conn.close()


def test_seen_again_resets_count_and_is_not_relisted():
    summaries, live, conn = _session([_wd("JR1"), _wd("JR1", "JR2")])
    assert live == {"JR1", "JR2"}, live
    assert summaries[1]["relisted_count"] == 0, summaries[1]["relisted_count"]
    assert _missed(conn, "JR2") == 0
    conn.close()


def test_genuinely_removed_then_seen_is_relisted():
    summaries, live, conn = _session(
        [_wd("JR1"), _wd("JR1"), _wd("JR1", "JR2")])
    assert summaries[2]["relisted_count"] == 1, summaries[2]["relisted_count"]
    assert live == {"JR1", "JR2"}, live
    conn.close()


def test_greenhouse_row_removed_on_first_miss():
    fetch = {"postings": [_row("JR1", ats="greenhouse", slug="acme")],
             "errors": []}
    summary, live = _run(fetch, GH)
    assert summary["removed_count"] == 1, summary["removed_count"]
    assert live == {"JR1"}, live


def test_incomplete_board_does_not_advance_miss_count():
    (_,), live, conn = _session([{
        "postings": [_row("JR1")], "errors": [],
        "incomplete": [{"company": "Acme", "reason": "wall"}]}])
    assert live == {"JR1", "JR2"}, live
    assert _missed(conn, "JR2") == 0
    conn.close()


def test_migration_v4_to_v5_adds_missed_runs():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store._SCHEMA)
    store._migrate(conn)
    # Rewind to a v4 database: no missed_runs column.
    conn.execute("ALTER TABLE postings DROP COLUMN missed_runs")
    conn.execute("PRAGMA user_version=4")
    conn.execute(
        "INSERT INTO postings (ats, slug, job_id, first_seen, last_seen) "
        "VALUES ('workday','s','J1',?,?)", (STAMP, STAMP))
    conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(postings)")}
    assert "missed_runs" not in cols
    store._migrate(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(postings)")}
    assert "missed_runs" in cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    assert conn.execute("SELECT missed_runs FROM postings").fetchone()[0] == 0
    conn.close()


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
