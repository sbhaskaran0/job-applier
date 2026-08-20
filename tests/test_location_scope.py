"""Location-scope tests (JOB-123): a remote posting must still be remote from
somewhere the user can actually work.

Standalone on purpose — the project ships no test runner, so
`python tests/test_location_scope.py` runs the whole file; the plain `test_*`
functions also collect under pytest if one is ever added.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store                      # noqa: E402
from src.providers import locations        # noqa: E402

# the live profile, trimmed to what these tests exercise: only the location
# branch is under test, so every row carries an acceptable title and no salary.
BASELINE = {
    "acceptable_titles": ["Product Manager"],
    "locations_allowed": ["Los Angeles", "Remote"],
    "relocation_targets": [],
    "remote_allowed": True,
    "salary_floor": 130000,
}

FOREIGN = [
    "Canada - Remote (ON, AB, BC, or NS Only)",   # scoped to Canadian provinces
    "Remote - India",                             # scope normalize() swallows
    "DE-Berlin-Trion Building",                   # leading DE- is Germany
    "United Kingdom (Remote)",
    "Toronto",
    "Paris",
]
WORKABLE = [
    "Remote",                                     # unscoped: fails open
    "Remote - USA",
    "Remote - Canada; Remote - US",               # bare "US" is an allowed signal
    "San Francisco, CA, New York, NY, Portland, OR, or Remote within Canada "
    "or United States",
    "Los Angeles",
    "Austin, TX",                                 # trailing state abbreviation
    "Wilmington, DE",                             # ", DE" is Delaware, not Germany
    "",
    None,
]


def _row(location, remote=1, title="Product Manager"):
    return {"title": title, "location": location, "remote": remote}


def test_foreign_remote_rows_fail():
    for loc in FOREIGN:
        assert store.passes_baseline(_row(loc), BASELINE) == \
            (False, "location:foreign_remote"), loc


def test_workable_remote_rows_pass():
    for loc in WORKABLE:
        assert store.passes_baseline(_row(loc), BASELINE) == (True, ""), loc


def test_non_remote_rows_keep_the_substring_rule():
    # unchanged behaviour: onsite rows are judged only by locations_allowed
    assert store.passes_baseline(_row("Los Angeles, CA", remote=0),
                                 BASELINE) == (True, "")
    assert store.passes_baseline(_row("Austin, TX", remote=0),
                                 BASELINE) == (False, "location")
    assert store.passes_baseline(_row("Toronto", remote=0),
                                 BASELINE) == (False, "location")


def test_other_baseline_reasons_still_win():
    assert store.passes_baseline(_row("Toronto", title="Data Scientist"),
                                 BASELINE) == (False, "title")
    row = {**_row("Toronto"), "seniority_flag": "Director"}
    assert store.passes_baseline(row, BASELINE) == (False, "seniority:Director")


def test_allowed_countries_knob_widens_the_rule():
    baseline = {**BASELINE, "allowed_countries": ["United States", "Canada"]}
    assert store.passes_baseline(_row("Remote - Canada"), baseline) == (True, "")
    assert store.passes_baseline(_row("Toronto"), baseline) == (True, "")
    assert store.passes_baseline(_row("Remote - India"), baseline) == \
        (False, "location:foreign_remote")


def test_allowed_countries_derive_from_relocation_targets():
    # no profile edit needed: naming a country as a relocation target allows it
    baseline = {**BASELINE, "relocation_targets": ["Canada"]}
    assert store.passes_baseline(_row("Remote - Canada"), baseline) == (True, "")
    assert store.passes_baseline(_row("Paris"), baseline) == \
        (False, "location:foreign_remote")


def test_foreign_scope_helper_fails_open_on_junk():
    for junk in ["", "   ", None, "Somewhere Nice", "Hybrid", "TBD"]:
        assert locations.foreign_scope(junk, None, ["Los Angeles", "Remote"]) \
            is False, junk


def _store_with(rows):
    """In-memory postings store: (company, title, location, remote) tuples."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store._SCHEMA)
    stamp = "2026-08-20T00:00:00+00:00"
    for i, (company, title, location, remote) in enumerate(rows):
        conn.execute(
            "INSERT INTO postings (ats, slug, job_id, company, title, location,"
            " remote, url, first_seen, last_seen) VALUES"
            " ('greenhouse', 'demo', ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(i), company, title, location, remote,
             "https://example.test/%d" % i, stamp, stamp))
    conn.commit()
    return conn


def test_hidden_by_reason_sums_to_hidden_by_criteria():
    conn = _store_with([
        ("Acme", "Product Manager", "Remote - USA", 1),
        ("Acme", "Product Manager", "Toronto", 1),           # same key, foreign
        ("Globex", "Product Manager", "Remote - India", 1),
        ("Initech", "Product Manager", "United Kingdom (Remote)", 1),
        ("Umbrella", "Product Manager", "Austin, TX", 1),
        ("Hooli", "Data Scientist", "Remote - USA", 1),      # title
        ("Stark", "Product Manager", "Berlin", 0),           # onsite: location
    ])
    real_connect = store.connect
    store.connect = lambda: conn
    try:
        res = store.list_postings_from_store()
    finally:
        store.connect = real_connect
        conn.close()
    assert res["matched"] == 2, res["matched"]              # Acme + Umbrella
    assert sum(res["hidden_by_reason"].values()) == res["hidden_by_criteria"]
    assert res["hidden_by_reason"]["location:foreign_remote"] == 3
    assert res["hidden_by_reason"]["title"] == 1
    assert res["hidden_by_reason"]["location"] == 1


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
