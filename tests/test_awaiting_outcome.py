"""Awaiting-outcome digest section (JOB-136): the digest must ASK for the one
input the response-rate goal depends on, and must survive an empty log.

Standalone on purpose — the project ships no test runner, so
`python tests/test_awaiting_outcome.py` runs the whole file; the plain `test_*`
functions also collect under pytest if one is ever added.

These fixtures — not a live refresh — are the binding evidence for the story's
ACs: a lane worktree resolves the PLACEHOLDER `_scratch` profile, whose
application log is empty, so a live digest there can only ever exercise the
empty-log path.
"""

import sqlite3
import sys
from datetime import date, timedelta
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

SUMMARY = {
    "run_at": "2026-09-04T00:00:00+00:00",
    "total_scanned": 0, "new_count": 0, "removed_count": 0,
    "relisted_count": 0, "new_rows": [], "companies_failed": [],
}


def _days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def _app(company, days_ago, status="submitted", outcome=None, title="Product Manager"):
    a = {"company": company, "job_title": title,
         "url": "https://example.test/%s" % company.lower(),
         "date": _days_ago(days_ago), "status": status}
    if outcome is not None:
        a["outcome"] = outcome
    return a


def _empty_store():
    """A fresh empty in-memory postings store. build_digest reaches the store
    more than once (yield_stats, company_spread) and each caller closes the
    connection it was handed, so the fixture hands out a NEW one per call
    rather than one shared connection that the first close would kill."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store._SCHEMA)
    return conn


def _digest(applications):
    """build_digest against an EMPTY in-memory store and a synthetic log, so
    the only thing under test is the awaiting-outcome section."""
    real_connect = store.connect
    real_criteria = config.load_search_criteria
    real_apps = config.load_applications
    store.connect = _empty_store
    config.load_search_criteria = lambda: {"baseline": BASELINE}
    config.load_applications = lambda: [dict(a) if isinstance(a, dict) else a
                                        for a in applications]
    try:
        return refresh.build_digest(dict(SUMMARY))
    finally:
        store.connect = real_connect
        config.load_search_criteria = real_criteria
        config.load_applications = real_apps


# --------------------------------------------------------------------------- #
# AC1 — the section exists, lists eligible records oldest-first, and truncates
# --------------------------------------------------------------------------- #
def test_section_lists_old_unanswered_submissions_oldest_first():
    out = _digest([_app("Acme", 20), _app("Globex", 40),
                   _app("Initech", 30, status="manual_submission")])
    assert "## Awaiting outcome (3)" in out, out
    order = [out.index(c) for c in ("Globex", "Initech", "Acme")]
    assert order == sorted(order), out


def test_section_renders_after_board_health_and_before_yield():
    out = _digest([_app("Acme", 20)])
    assert (out.index("## Board health") < out.index("## Awaiting outcome")
            < out.index("## Yield per company")), out


def test_list_truncates_at_the_cap_with_an_and_n_more_line():
    apps = [_app("Co%02d" % i, 20 + i) for i in range(refresh._AWAITING_LIMIT + 4)]
    out = _digest(apps)
    assert "## Awaiting outcome (%d)" % len(apps) in out, out
    assert "_... and 4 more._" in out, out
    # the cap is on RENDERED rows, not on the headline count
    listed = [a["company"] for a in apps if "**%s —" % a["company"] in out]
    assert len(listed) == refresh._AWAITING_LIMIT, listed


# --------------------------------------------------------------------------- #
# AC2 — the response rate is reported WITH its denominator
# --------------------------------------------------------------------------- #
def test_response_rate_shows_its_denominator():
    out = _digest([_app("Acme", 20), _app("Globex", 20, outcome="rejected"),
                   _app("Hooli", 20, outcome="interview"),
                   _app("Umbrella", 20, status="parked")])
    # 2 replies of 3 submitted (the parked record is not a submit)
    assert "**66.7%**" in out, out
    assert "2 replied of 3 submitted" in out, out
    assert "2 record(s) carry no outcome" in out, out  # Acme + the parked one


def test_zero_response_rate_renders_as_an_honest_zero():
    out = _digest([_app("Acme", 20), _app("Globex", 20)])
    assert "**0.0%**" in out, out
    assert "0 replied of 2 submitted" in out, out


# --------------------------------------------------------------------------- #
# AC3 — anything already answered, too recent, or never submitted stays out
# --------------------------------------------------------------------------- #
def test_records_with_an_outcome_are_absent():
    for outcome in ("rejected", "screen", "interview", "offer", "ghosted"):
        out = _digest([_app("Acme", 40, outcome=outcome)])
        assert "## Awaiting outcome (0)" in out, (outcome, out)
        assert "Nothing waiting" in out, (outcome, out)


def test_the_none_sentinel_still_counts_as_awaiting():
    # "none" is APPLICATION_OUTCOMES' no-reply sentinel, not a recorded reply;
    # "ghosted" is the vocabulary's way to close a silent application out.
    out = _digest([_app("Acme", 40, outcome="none")])
    assert "## Awaiting outcome (1)" in out, out


def test_recent_and_unsubmitted_records_are_absent():
    out = _digest([_app("Acme", 3),                       # inside the grace window
                   _app("Globex", 40, status="attempted"),
                   _app("Initech", 40, status="parked")])
    assert "## Awaiting outcome (0)" in out, out


def test_the_grace_window_boundary_is_inclusive():
    assert "## Awaiting outcome (1)" in _digest(
        [_app("Acme", refresh._AWAITING_DAYS)])
    assert "## Awaiting outcome (0)" in _digest(
        [_app("Acme", refresh._AWAITING_DAYS - 1)])


# --------------------------------------------------------------------------- #
# AC4 — an empty / missing / junk log still renders
# --------------------------------------------------------------------------- #
def test_empty_log_renders_without_dividing_by_zero():
    out = _digest([])
    assert "## Awaiting outcome (0)" in out, out
    assert "**0.0%**" in out, out
    assert "0 replied of 0 submitted" in out, out


def test_junk_records_are_skipped_not_raised_on():
    # Driven at the helper rather than through build_digest on purpose: the
    # digest's other sections (store.company_spread) predate this story and are
    # not non-dict-tolerant, and widening them is not this change's business.
    picked = refresh._awaiting_outcome(
        ["not a dict", None, {}, {"status": "submitted"},
         _app("Acme", 40), {"status": "submitted", "date": "soon"}])
    # only the well-formed old record survives; the undated and the
    # unparseable-date submits are skipped rather than guessed at
    assert [a["company"] for a in picked] == ["Acme"], picked


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
