"""`_match_option` must pick the option that MEANS the value.

Two live failures are pinned here.

1. A Snap Workday school dropdown never matched "UCSB" against "University of
   California, Santa Barbara": the initials of that option are UOCSB, because
   the linking word "of" contributed a letter. The fix is a second,
   stopword-stripped initials spelling -- guarded at 2 characters so a
   single-letter initials string can't match arbitrary noise.

2. The older regression that motivated the whole tiering: "US" once selected
   "AUStralia". Tiers (exact -> prefix -> initials) are what stopped that, so
   when `value` became a LIST of candidate spellings the loop had to stay
   TIER-outer / candidate-inner. Candidate-outer ordering would let an earlier
   candidate's prefix hit beat a later candidate's exact hit -- the tiering
   would be silently destroyed while every single-value test still passed.

Pure Python: `_match_option` is a @staticmethod and needs no browser.
Standalone, like the rest of tests/ -- `python tests/test_field_matching.py`.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# src.browser imports playwright at module scope; CI installs only pyyaml and
# httpx, so stub it. Nothing here drives a browser.
_pw = types.ModuleType("playwright")
_pw_async = types.ModuleType("playwright.async_api")
_pw_async.async_playwright = lambda: None
sys.modules.setdefault("playwright", _pw)
sys.modules.setdefault("playwright.async_api", _pw_async)

from src import browser                 # noqa: E402

match = browser.BrowserSession._match_option


def test_ucsb_matches_university_of_california_santa_barbara():
    # The confirmed live failure: plain initials are "uocsb", so only the
    # stopword-stripped spelling can match.
    opts = ["University of California, Santa Barbara",
            "University of California, Davis", "Stanford University"]
    assert match("UCSB", opts) == "University of California, Santa Barbara"


def test_us_does_not_match_australia():
    # The regression that motivated the tiering: "AUStralia" contains "us".
    assert match("US", ["Australia", "Austria", "Belgium"]) is None


def test_exact_beats_prefix():
    assert match("United", ["United Kingdom", "United"]) == "United"


def test_no_match_returns_none():
    # Never falls back to "first option in the filtered list".
    assert match("Zimbabwe", ["United States", "Canada"]) is None


def test_initials_both_directions():
    # value-initials == option, as well as option-initials == value.
    assert match("United States", ["US", "CA"]) == "US"
    assert match("US", ["United States", "Canada"]) == "United States"


def test_candidate_list_tier_outer_exact_beats_earlier_prefix():
    # "Kingdom" PREFIX-matches the wrong option; "United Kingdom" EXACTLY
    # matches the right one. Tier-outer must return the exact match.
    opts = ["Kingdom of Eswatini", "United Kingdom"]
    assert match(["Kingdom", "United Kingdom"], opts) == "United Kingdom"


def test_candidate_outer_ordering_would_have_failed_that_case():
    # Proof the case above is real and not vacuous: run the first candidate on
    # its own -- i.e. exactly what candidate-outer ordering would do -- and it
    # returns the WRONG option. So the ordering is doing load-bearing work.
    opts = ["Kingdom of Eswatini", "United Kingdom"]
    assert match("Kingdom", opts) == "Kingdom of Eswatini"


def test_candidate_list_falls_through_tiers_to_initials():
    opts = ["University of California, Santa Barbara", "Stanford University"]
    assert match(["UC Santa Barbara Ext.", "UCSB"], opts) == opts[0]


def test_single_letter_initials_are_ignored():
    # "The Guardian" strips to ["Guardian"] -> initials "g". Without the
    # len >= 2 guard, the value "g" would match it; one letter is noise.
    assert match("g", ["The Guardian", "Zebra Corp"]) is None
    # ...while the 2-char plain initials still match.
    assert match("TG", ["The Guardian", "Zebra Corp"]) == "The Guardian"


def test_empty_value_matches_nothing():
    assert match("", ["Yes", "No"]) is None
    assert match([], ["Yes", "No"]) is None
    assert match(["", "   "], ["Yes", "No"]) is None


def test_single_option_substring_fallback_still_works():
    # Pre-existing behavior: with the list already filtered to one option, a
    # substring is good enough.
    assert match("Santa Barbara",
                 ["University of California, Santa Barbara"]) == \
        "University of California, Santa Barbara"


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
