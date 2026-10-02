"""Country-scope-only locations are INDETERMINATE, not a location failure
(JOB-138).

A board that reports "United States" and no city tells us which country the
role is in and nothing else. The old rule compared that string against the
allowed metros, found no match, and dropped the posting — throwing away roles
that may well be in an allowed city, on the strength of the board having said
nothing. These tests pin the replacement: such a row passes the baseline and
is flagged, while a real non-allowed city and a foreign country keep failing.

Standalone on purpose — the project ships no test runner, so
`python tests/test_location_indeterminate.py` runs the whole file; the plain
`test_*` functions also collect under pytest if one is ever added.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, store              # noqa: E402

# the live profile, trimmed to what these tests exercise: only the location
# branch is under test, so every row carries an acceptable title and no salary.
BASELINE = {
    "acceptable_titles": ["Product Manager"],
    "excluded_seniority": ["Director"],
    "locations_allowed": ["Los Angeles", "Remote"],
    "relocation_targets": [],
    "remote_allowed": True,
    "salary_floor": 130000,
}

# exactly the shapes the live corpus carries on the eight rescued rows —
# including the trailing space that is really in the Airbnb data.
COUNTRY_SCOPE = ["United States", "United States ", "US", "USA"]

# whole-string regions that are NOT an allowed country, so they must keep
# failing. "New York" is the load-bearing one: it is a US *state*, and
# derive_allowed yields countries, so it must not be waved through.
STILL_FAILING = ["New York", "France", "Australia", "Singapore",
                 "Costa Rica", "Brazil"]


def _row(location, remote=0, title="Product Manager"):
    return {"company": "Acme", "title": title, "location": location,
            "remote": remote, "salary_min": None, "salary_max": None,
            "min_years": None}


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


def _listing(conn):
    """list_postings_from_store against a fixture store and fixture criteria."""
    real_connect, real_criteria, real_apps = (
        store.connect, config.load_search_criteria, config.load_applications)
    store.connect = lambda: conn
    config.load_search_criteria = lambda: {"baseline": BASELINE}
    config.load_applications = lambda: []
    try:
        return store.list_postings_from_store()
    finally:
        store.connect = real_connect
        config.load_search_criteria = real_criteria
        config.load_applications = real_apps


# --- AC1: country scope passes, and is tagged ------------------------------

def test_country_scope_passes_baseline():
    for loc in COUNTRY_SCOPE:
        ok, reason = store.passes_baseline(_row(loc), BASELINE)
        assert ok, "%r should pass, got reason %r" % (loc, reason)
        assert reason == "", (loc, reason)


def test_country_scope_is_tagged_indeterminate():
    for loc in COUNTRY_SCOPE:
        assert store.location_indeterminate(_row(loc), BASELINE), loc


# --- AC3: real cities and foreign countries keep failing -------------------

def test_non_allowed_city_still_fails():
    # a genuine city we cannot work in: the board DID say where, and it is the
    # wrong where. Nothing indeterminate about it.
    for loc in ["Seattle, Washington", "Berlin", "Toronto", "Austin, TX"]:
        ok, reason = store.passes_baseline(_row(loc), BASELINE)
        assert not ok, "%r should fail" % loc
        assert reason == "location", (loc, reason)
        assert not store.location_indeterminate(_row(loc), BASELINE), loc


def test_state_and_foreign_regions_still_fail():
    for loc in STILL_FAILING:
        ok, reason = store.passes_baseline(_row(loc), BASELINE)
        assert not ok, "%r should fail" % loc
        assert reason == "location", (loc, reason)
        assert not store.location_indeterminate(_row(loc), BASELINE), loc


def test_allowed_city_passes_and_is_not_tagged():
    # the tag means "we don't know", so a row we DO know must not carry it.
    for loc in ["Los Angeles, CA", "Los Angeles"]:
        ok, reason = store.passes_baseline(_row(loc), BASELINE)
        assert ok, (loc, reason)
        assert not store.location_indeterminate(_row(loc), BASELINE), loc


def test_empty_location_is_not_indeterminate():
    # no string at all is not a country scope — leave that verdict alone.
    assert not store.location_indeterminate(_row(""), BASELINE)
    assert not store.location_indeterminate(_row(None), BASELINE)


def test_remote_rows_are_never_indeterminate():
    # a remote row is judged by foreign_scope; "remote in the US" is a real
    # match, not an unknown, so it must not pick up the tag.
    assert not store.location_indeterminate(_row("United States", remote=1), BASELINE)
    assert not store.location_indeterminate(_row("Remote - USA", remote=1), BASELINE)


def test_foreign_remote_reason_is_untouched():
    # the remote half of the location rule is out of JOB-138's scope and must
    # keep rejecting remote roles scoped outside the allowed countries.
    ok, reason = store.passes_baseline(_row("United Kingdom (Remote)", remote=1), BASELINE)
    assert not ok
    assert reason == "location:foreign_remote", reason


# --- AC4: the tag reaches the list_postings_from_store payload -------------

def test_payload_carries_the_tag():
    conn = _store_with([
        ("Airbnb", "Product Manager", "United States", 0),
        ("Umbrella", "Product Manager", "Los Angeles, CA", 0),
        ("Stark", "Product Manager", "Berlin", 0),          # hidden: location
    ])
    try:
        res = _listing(conn)
    finally:
        conn.close()
    by_company = {p["company"]: p for p in res["postings"]}
    assert set(by_company) == {"Airbnb", "Umbrella"}, sorted(by_company)
    assert by_company["Airbnb"]["location_indeterminate"] is True
    assert by_company["Umbrella"]["location_indeterminate"] is False
    # the rescued row is a match now, and Berlin is still correctly hidden
    assert res["matched"] == 2, res["matched"]
    assert res["hidden_by_reason"]["location"] == 1, res["hidden_by_reason"]


def test_tag_is_and_across_location_variants():
    # same (company, title) collapses into one entry across rows. The tag is a
    # warning that we do not know where the role is — one variant naming a real
    # allowed city answers that, so the merged entry must drop the tag.
    conn = _store_with([
        ("Stripe", "Product Manager", "United States", 0),
        ("Stripe", "Product Manager", "Los Angeles, CA", 0),
    ])
    try:
        res = _listing(conn)
    finally:
        conn.close()
    assert len(res["postings"]) == 1, res["postings"]
    assert res["postings"][0]["location_indeterminate"] is False


def test_tag_survives_when_every_variant_is_country_scope():
    conn = _store_with([
        ("Databricks", "Product Manager", "United States", 0),
        ("Databricks", "Product Manager", "USA", 0),
    ])
    try:
        res = _listing(conn)
    finally:
        conn.close()
    assert len(res["postings"]) == 1, res["postings"]
    assert res["postings"][0]["location_indeterminate"] is True


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
