"""Dark-board and orphaned-board handling (JOB-137).

Three distinct lifecycles that must NOT be confused with each other:
  * a board that failed ONCE keeps every posting (transient-failure guard),
  * a board dark for _DARK_RUNS runs keeps its rows but stops counting as live
    supply, and shows as STALE rather than silently vanishing,
  * a board that left watchlist.yaml is orphaned and gets retired outright,
    because a config edit is a decision and a failed fetch is not.

Standalone on purpose — the project ships no test runner, so
`python tests/test_dark_boards.py` runs the whole file; the plain `test_*`
functions also collect under pytest if one is ever added. Everything is
in-memory: no real profile, no real DB, no network.
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, refresh, store              # noqa: E402

BASELINE = {
    "acceptable_titles": ["Product Manager"],
    "excluded_seniority": ["Director"],
    "locations_allowed": ["Los Angeles", "Remote"],
    "relocation_targets": [],
    "remote_allowed": True,
    "salary_floor": 130000,
}

STAMP = "2026-09-04T00:00:00+00:00"


def _blank():
    """An empty store at the CURRENT schema. _SCHEMA alone is the v1 shape —
    the refresh_runs yield columns arrive via _migrate, exactly as they do for
    a real connect(), and refresh_from_fetch writes to them."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store._SCHEMA)
    store._migrate(conn)
    return conn


def _store(rows, failed=()):
    """In-memory store: rows are (company, ats, slug, job_id, title, location).
    `failed` is the latest run's companies_failed, as (company, consecutive)."""
    conn = _blank()
    for company, ats, slug, job_id, title, location in rows:
        conn.execute(
            "INSERT INTO postings (ats, slug, job_id, company, title, location,"
            " remote, url, first_seen, last_seen) VALUES (?,?,?,?,?,?,1,?,?,?)",
            (ats, slug, job_id, company, title, location,
             "https://example.test/%s/%s" % (slug, job_id), STAMP, STAMP))
    conn.execute(
        "INSERT INTO refresh_runs (run_at, total_scanned, new_count, "
        "removed_count, relisted_count, companies_ok, companies_failed) "
        "VALUES (?,0,0,0,0,0,?)",
        (STAMP, json.dumps([{"company": c, "reason": "404", "consecutive": n}
                            for c, n in failed])))
    conn.commit()
    return conn


class _KeepOpen:
    """A connection whose close() is a no-op. Store functions each open and
    close their own connection, but a fixture has to survive being closed —
    both so build_digest can reach it twice and so a test can inspect the rows
    afterwards. sqlite3.Connection.close is read-only, hence a proxy."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _with_store(conn, fn, watchlist=None):
    """Run `fn` with store/config pinned to fixtures — no real profile, no real
    DB, no network."""
    real_connect = store.connect
    real_criteria = config.load_search_criteria
    real_apps = config.load_applications
    real_watchlist = config.load_watchlist
    keep = _KeepOpen(conn)
    store.connect = lambda: keep
    config.load_search_criteria = lambda: {"baseline": BASELINE}
    config.load_applications = lambda: []
    if watchlist is not None:
        config.load_watchlist = lambda: list(watchlist)
    try:
        return fn()
    finally:
        store.connect = real_connect
        config.load_search_criteria = real_criteria
        config.load_applications = real_apps
        config.load_watchlist = real_watchlist


def _board(name, ats, slug):
    return {"name": name, "ats": ats, "slug": slug}


def _posting(company, ats, slug, job_id, title="Product Manager",
             location="Los Angeles"):
    """One normalized posting as fetch_all_with_status() would hand it over."""
    return {"ats": ats, "slug": slug, "job_id": job_id, "company": company,
            "title": title, "location": location, "remote": 1,
            "url": "https://example.test/%s/%s" % (slug, job_id),
            "description": "", "posted": None,
            "salary_min": None, "salary_max": None}


# --------------------------------------------------------------------------- #
# dark_boards: the threshold itself
# --------------------------------------------------------------------------- #
def test_dark_boards_is_at_or_over_the_threshold_only():
    conn = _store([], failed=[("AtThreshold", store._DARK_RUNS),
                              ("Over", store._DARK_RUNS + 5),
                              ("Below", store._DARK_RUNS - 1)])
    try:
        assert store.dark_boards(conn=conn) == {"AtThreshold", "Over"}
    finally:
        conn.close()


def test_dark_boards_is_empty_on_a_store_that_never_ran():
    conn = _blank()
    try:
        assert store.dark_boards(conn=conn) == set()
    finally:
        conn.close()


def test_the_threshold_has_one_definition():
    # refresh.py re-exports store's constant rather than keeping its own copy;
    # two copies of a threshold drift.
    assert refresh._DARK_RUNS is store._DARK_RUNS


# --------------------------------------------------------------------------- #
# AC2 — a dark board stops counting as live supply, and shows as STALE
# --------------------------------------------------------------------------- #
DARK_ROWS = [
    ("Live Co", "ashby", "liveco", "1", "Product Manager", "Los Angeles"),
    ("Dark Co", "greenhouse", "darkco", "1", "Product Manager", "Los Angeles"),
    ("Dim Co", "greenhouse", "dimco", "1", "Product Manager", "Los Angeles"),
]


def test_yield_stats_flags_the_dark_board_and_only_it():
    conn = _store(DARK_ROWS, failed=[("Dark Co", store._DARK_RUNS),
                                     ("Dim Co", store._DARK_RUNS - 1)])
    stats = _with_store(conn, store.yield_stats)
    by_company = {s["company"]: s for s in stats}
    assert set(by_company) == {"Live Co", "Dark Co", "Dim Co"}, by_company
    assert by_company["Dark Co"]["stale"] is True, by_company
    assert by_company["Live Co"]["stale"] is False, by_company
    # a board one run BELOW the threshold is not stale yet
    assert by_company["Dim Co"]["stale"] is False, by_company
    # the row is kept, not blanked — the counts are the last thing we saw
    assert by_company["Dark Co"]["qualifying"] == 1, by_company
    # ...and it sorts last, under every live board
    assert stats[-1]["company"] == "Dark Co", stats


def test_dark_rows_are_excluded_from_the_corpus_totals():
    conn = _store(DARK_ROWS, failed=[("Dark Co", store._DARK_RUNS),
                                     ("Dim Co", store._DARK_RUNS - 1)])
    spread = _with_store(conn, store.company_spread)
    assert spread["qualifying"]["total"] == 2, spread["qualifying"]
    assert {t["company"] for t in spread["qualifying"]["top5"]} == \
        {"Live Co", "Dim Co"}, spread["qualifying"]


def test_the_digest_marks_the_stale_row():
    conn = _store(DARK_ROWS, failed=[("Dark Co", store._DARK_RUNS),
                                     ("Dim Co", store._DARK_RUNS - 1)])
    summary = {"run_at": STAMP, "total_scanned": 0, "new_count": 0,
               "removed_count": 0, "relisted_count": 0, "new_rows": [],
               "companies_failed": [
                   {"company": "Dark Co", "reason": "404",
                    "consecutive": store._DARK_RUNS},
                   {"company": "Dim Co", "reason": "timeout",
                    "consecutive": store._DARK_RUNS - 1}]}
    out = _with_store(conn, lambda: refresh.build_digest(summary))
    assert "| Dark Co ⚠️ STALE |" in out, out
    assert "| Live Co |" in out, out
    assert "| Dim Co |" in out, out
    # board health still calls the dark one out, unchanged
    assert "DARK — fix slug or drop from watchlist" in out, out


# --------------------------------------------------------------------------- #
# AC3/AC4 — the two removal passes, and the line between them
# --------------------------------------------------------------------------- #
def _refresh_with(conn, watchlist, postings, errors):
    def go():
        return store.refresh_from_fetch({"postings": postings, "errors": errors})
    return _with_store(conn, go, watchlist=watchlist)


def _active(conn, ats, slug):
    return [r["job_id"] for r in conn.execute(
        "SELECT job_id FROM postings WHERE ats=? AND slug=? "
        "AND removed_at IS NULL", (ats, slug))]


def test_a_board_that_failed_once_keeps_every_posting():
    conn = _store([("Flaky Co", "greenhouse", "flakyco", "1",
                    "Product Manager", "Los Angeles")])
    _refresh_with(conn, [_board("Flaky Co", "greenhouse", "flakyco")],
                  postings=[],
                  errors=[{"company": "Flaky Co", "reason": "timeout"}])
    assert _active(conn, "greenhouse", "flakyco") == ["1"], "transient failure ate rows"
    conn.close()


def test_a_board_absent_from_the_watchlist_is_retired():
    # the JOB-137 regression: repointing Temporal's ATS drops the old board out
    # of the removal loop entirely, stranding its rows as active forever.
    conn = _store([("Temporal Technologies", "greenhouse", "temporaltechnologies",
                    "1", "Product Manager", "Los Angeles"),
                   ("Temporal Technologies", "greenhouse", "temporaltechnologies",
                    "2", "Product Manager", "Remote"),
                   ("Live Co", "ashby", "liveco", "1", "Product Manager",
                    "Los Angeles")])
    summary = _refresh_with(
        conn,
        # repointed: the greenhouse board is gone, the ashby one replaces it
        [_board("Temporal Technologies", "ashby", "temporal"),
         _board("Live Co", "ashby", "liveco")],
        # Live Co still lists its role this run, so the fetch-success removal
        # pass has no reason to touch it — only the orphan sweep should fire.
        postings=[_posting("Live Co", "ashby", "liveco", "1")], errors=[])
    assert _active(conn, "greenhouse", "temporaltechnologies") == [], \
        "orphaned rows survived the repoint"
    assert _active(conn, "ashby", "liveco") == ["1"], "watchlisted board was retired"
    assert summary["removed_count"] == 2, summary["removed_count"]
    conn.close()


def test_a_failed_board_is_not_treated_as_an_orphan():
    # still IN the watchlist, so the config-removal sweep must not see it even
    # though this run's fetch produced no postings for it.
    conn = _store([("Flaky Co", "greenhouse", "flakyco", "1",
                    "Product Manager", "Los Angeles")])
    summary = _refresh_with(conn, [_board("Flaky Co", "greenhouse", "flakyco")],
                            postings=[],
                            errors=[{"company": "Flaky Co", "reason": "500"}])
    assert _active(conn, "greenhouse", "flakyco") == ["1"], "failed board orphaned"
    assert summary["removed_count"] == 0, summary["removed_count"]
    conn.close()


def test_an_empty_watchlist_does_not_retire_the_whole_store():
    # load_watchlist() returns [] for a missing/unreadable watchlist.yaml. That
    # is a config read failure, not a decision to drop every board.
    conn = _store([("Live Co", "ashby", "liveco", "1", "Product Manager",
                    "Los Angeles")])
    _refresh_with(conn, [], postings=[], errors=[])
    assert _active(conn, "ashby", "liveco") == ["1"], "empty watchlist wiped the store"
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
