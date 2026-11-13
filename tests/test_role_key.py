"""Role identity: the join condition behind `already_applied`.

"Have I already applied to this posting?" is answered by matching a posting
against the application log. That match has to agree on what identifies a
role, and for a long time it did not:

  * the applied set was built with `data._normalize` on company and title;
  * posting dedupe (`store._role_key`) lower-cased the title and left the
    company RAW;
  * `company_spread` carried a third, inline copy of the same idea.

Worse, `_normalize` replaces punctuation with a space but never collapses the
result, so "Senior Product Manager, Consumer" normalized to
"senior product manager  consumer" (two spaces) while the same role logged as
"Senior Product Manager - Consumer" normalized to "...manager   consumer"
(three). Those compare unequal. A real Plaid role the user applied to on
2026-07-09 therefore kept resurfacing as never-applied -- against a board that
hard-limits applications per candidate, so re-applying would have burned a
scarce slot. 63 of 93 stored applications had a double space in their key.

`data.role_key` is now the single function both sides use. These tests pin
that, and pin that `_normalize` itself was NOT widened -- it feeds
`_similarity`'s SequenceMatcher and `_upsert_answer`'s question identity, so
changing it would silently change which past answers auto-fill a form.

Standalone, like the rest of tests/ -- `python tests/test_role_key.py`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import data, store          # noqa: E402


# --------------------------------------------------------------------------- #
# the key itself
# --------------------------------------------------------------------------- #
def test_the_exact_live_failure_now_matches():
    # The real pair, from applications.json vs the postings store.
    posting = data.role_key("Plaid", "Senior Product Manager, Consumer")
    logged = data.role_key("Plaid", "Senior Product Manager - Consumer")
    assert posting == logged, (posting, logged)


def test_punctuation_variants_collapse():
    base = data.role_key("Acme", "Product Manager Payments")
    for variant in ("Product Manager, Payments", "Product Manager - Payments",
                    "Product Manager: Payments", "Product Manager & Payments",
                    "Product  Manager   Payments"):
        assert data.role_key("Acme", variant) == base, variant


def test_company_is_normalized_too():
    # _role_key used to leave the company raw on the posting side while the
    # applied set normalized it, so these two could never meet.
    assert data.role_key("Acme, Inc.", "PM") == data.role_key("acme inc", "PM")


def test_genuinely_different_roles_stay_different():
    # The fix must not over-merge: collapsing whitespace is not the same as
    # ignoring words.
    assert data.role_key("Acme", "Product Manager") != \
        data.role_key("Acme", "Product Manager, Growth")
    assert data.role_key("Acme", "Product Manager") != \
        data.role_key("Beta", "Product Manager")
    assert data.role_key("Acme", "Senior Product Manager") != \
        data.role_key("Acme", "Product Manager")


def test_no_key_contains_a_whitespace_run():
    for c, t in (("A  B", "X  Y"), ("A, B", "X - Y"), (" A ", " X ")):
        for part in data.role_key(c, t):
            assert "  " not in part, (c, t, part)


def test_empty_and_missing_inputs_are_safe():
    assert data.role_key("", "") == ("", "")
    assert data.role_key(None, None) == ("", "")


# --------------------------------------------------------------------------- #
# one function, used by every call site
# --------------------------------------------------------------------------- #
def test_store_role_key_delegates_to_data_role_key():
    row = {"company": "Acme, Inc.", "title": "Product Manager, Payments"}
    assert store._role_key(row) == data.role_key(row["company"], row["title"])


def test_store_role_key_tolerates_a_titleless_row():
    # Workday/NVIDIA serve titleless rows; a KeyError here once killed a whole
    # board's fetch.
    assert store._role_key({"company": "Acme"}) == ("acme", "")


def test_application_key_is_built_on_role_key():
    # The log's own dedupe identity and the store's match must not drift.
    c, t = data.role_key("Acme", "Product Manager, Payments")
    assert data._application_key("Acme", "Product Manager, Payments", "") == (c, t, "")


def test_application_key_still_falls_back_to_url_when_title_is_missing():
    key = data._application_key("Acme", "", "https://x.test/job?gh_src=abc#frag")
    assert key[2] == "https://x.test/job", key


def test_both_sides_of_the_join_agree_end_to_end():
    """A posting whose title differs from the logged one only by punctuation
    must read as already applied. This is the regression the whole change is
    for, exercised through the two real call sites rather than by hand."""
    applied = {data.role_key("Plaid", "Senior Product Manager - Consumer")}
    posting = {"company": "Plaid", "title": "Senior Product Manager, Consumer"}
    assert data.role_key(posting["company"], posting["title"]) in applied
    assert store._role_key(posting) in applied


# --------------------------------------------------------------------------- #
# the thing that must NOT have changed
# --------------------------------------------------------------------------- #
def test_normalize_was_not_widened():
    """`_normalize` still leaves whitespace runs intact.

    It feeds `_similarity` (SequenceMatcher + token overlap, thresholded at
    0.7) and `_upsert_answer`'s question identity. Collapsing runs there would
    change similarity scores and therefore which stored answers auto-fill a
    form -- a correctness change wearing a refactor's clothes. role_key exists
    precisely so that join identity could be fixed without touching it.
    """
    assert data._normalize("Product Manager, Payments") == "product manager  payments"
    assert data._normalize("a - b") == "a   b"


def test_history_question_identity_is_unaffected():
    # Two questions differing by INTERNAL punctuation normalize to keys that
    # differ only in a whitespace run, so they must still be DISTINCT history
    # entries. If _normalize were ever widened to collapse runs (the role_key
    # behavior), these two would silently merge and one answer would be
    # demoted to an alternate of the other.
    #
    # Trailing punctuation is a different case and is NOT what this pins:
    # "Why us?" and "Why us" already normalize identically (the "?" becomes a
    # trailing space and is stripped), so those merge today and should.
    history = []
    data._upsert_answer(history, "Why us, really", "A", "evergreen", "", False)
    data._upsert_answer(history, "Why us really", "B", "evergreen", "", False)
    assert len(history) == 2, history
    assert data._normalize("Why us, really") != data._normalize("Why us really")


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
