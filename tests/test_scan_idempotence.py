"""read_form must be idempotent (Phase 0 invariant).

`_SCAN_JS` tags every field it captures with `data-jaidx`, and its
button-group pass *skips* elements that already carry the attribute. Nothing
ever removed those tags, so the SECOND read of a page returned every native
input but **none** of the button-group radios -- an answered Yes/No silently
vanished from the field list while staying visually selected. That is the real
cause of the Ashby "answered button-groups drop out of read_form" gotcha, and
it also corrupts `submit_application`'s `form_snapshot` (taken by a re-read
just before the click) and every verification re-read.

The fix is one sweep at the top of the scan. These tests pin the invariant
structurally: the sweep exists, and it runs *before* both the element query
and the skip-if-already-tagged guard. CI has no browser, so no test here
executes the JS against a real DOM -- that gate is the live check in
`tests/manual/` and the two-reads assertion in the live run.

Standalone, like the rest of tests/ -- `python tests/test_scan_idempotence.py`.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# src.browser imports playwright at module scope; CI installs only pyyaml and
# httpx, so stub it. We only read JS source strings here, never drive a browser.
_pw = types.ModuleType("playwright")
_pw_async = types.ModuleType("playwright.async_api")
_pw_async.async_playwright = lambda: None
sys.modules.setdefault("playwright", _pw)
sys.modules.setdefault("playwright.async_api", _pw_async)

from src import browser                 # noqa: E402

JS = browser._SCAN_JS


def test_scan_clears_stale_tags():
    assert "removeAttribute('data-jaidx')" in JS, \
        "no data-jaidx sweep in _SCAN_JS -- a second read_form drops button groups"


def test_sweep_runs_before_the_element_query():
    sweep = JS.index("removeAttribute('data-jaidx')")
    query = JS.index("const sel = ")
    assert sweep < query, (sweep, query)


def test_sweep_runs_before_the_already_tagged_guard():
    # The button-group pass skips b.hasAttribute('data-jaidx'). If the sweep
    # ran after it, stale tags from the previous read would still suppress the
    # whole group.
    sweep = JS.index("removeAttribute('data-jaidx')")
    guard = JS.index("hasAttribute('data-jaidx')")
    assert sweep < guard, (sweep, guard)


def test_sweep_is_document_wide():
    # Scoping it to the selector list would leave the button-group tags behind,
    # which is exactly the bug.
    line = next(ln for ln in JS.splitlines() if "removeAttribute('data-jaidx')" in ln)
    assert "querySelectorAll('[data-jaidx]')" in line, line


def test_tagging_still_happens_in_both_passes():
    # Guard against a "fix" that removes tagging instead of stale tags: both
    # passes must still set data-jaidx, or fill_field can't locate anything.
    assert JS.count("setAttribute('data-jaidx'") == 2, JS.count("setAttribute('data-jaidx'")


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
