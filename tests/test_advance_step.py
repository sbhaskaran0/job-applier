"""advance_step() moves a wizard forward -- and must never submit.

A live /apply-to-job run parked at step 2 of 7 of a Snap Workday application
with no way to move: there was no tool for "Save and Continue" at all, and the
only control that would have clicked it (submit_application) is destructive and
gated. advance_step is the ungated counterpart.

Three of its properties are the ones that can hurt if they regress, so they are
pinned here:

  * on the Review page the only control reads as Submit -- advance_step reports
    `is_submit_page` and clicks NOTHING. That is enforced server-side rather
    than trusted to a prompt remembering not to use Next as verification, so
    the test asserts a click COUNT of zero, not just the status string.
  * a validation error that cannot be tied to a scanned field is still
    reported, with index null and counted in unmapped_error_count. An sr-only
    input is invisible to _SCAN_JS, so the field most likely to fail
    validation may carry no index at all; dropping the error is how a run
    parks with no explanation.
  * Workday sometimes advances WITHOUT changing the URL, so a real advance is
    detected from a before/after label signature too, not the URL alone.

The browser is faked (CI has no Playwright); the JS error-mapping cascade is
asserted structurally, as in tests/test_scan_idempotence.py.

Standalone, like the rest of tests/ -- `python tests/test_advance_step.py`.
"""

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# src.browser imports playwright at module scope; CI installs only pyyaml and
# httpx, so stub it. The fakes below stand in for a real page.
_pw = types.ModuleType("playwright")
_pw_async = types.ModuleType("playwright.async_api")
_pw_async.async_playwright = lambda: None
sys.modules.setdefault("playwright", _pw)
sys.modules.setdefault("playwright.async_api", _pw_async)

from src import browser                 # noqa: E402


# --------------------------------------------------------------------------
# Fakes: a page is a list of buttons, a list of (label, kind) fields and a list
# of error rows. A button may carry an on_click that mutates the page, which is
# how "the wizard moved" is modeled.
# --------------------------------------------------------------------------
class FakeButton:
    def __init__(self, label, type_attr="", visible=True, on_click=None):
        self.label = label
        self.type = type_attr
        self.visible = visible
        self.on_click = on_click
        self.clicks = 0


def _matches(btn, selector):
    for part in selector.lower().split(","):
        part = part.strip()
        if "has-text(" in part:
            text = part.split("has-text(", 1)[1].rstrip(")").strip("'\"")
            if text in btn.label.lower():
                return True
        elif "[type=submit]" in part:
            if btn.type == "submit":
                return True
        elif part in ("button", "input[type=button]"):
            return True
    return False


class FakeButtonLocator:
    def __init__(self, btn):
        self.btn = btn

    async def is_visible(self):
        return self.btn.visible

    async def evaluate(self, js):
        return self.btn.label

    async def click(self, timeout=None):
        self.btn.clicks += 1
        if self.btn.on_click:
            self.btn.on_click()


class FakeLocatorSet:
    def __init__(self, btns):
        self.btns = btns

    async def count(self):
        return len(self.btns)

    def nth(self, i):
        return FakeButtonLocator(self.btns[i])


class FakeFrame:
    def __init__(self, buttons=(), fields=(), errors=()):
        self.buttons = list(buttons)
        self.fields_now = list(fields)
        self.errors_now = list(errors)

    def locator(self, selector):
        return FakeLocatorSet([b for b in self.buttons if _matches(b, selector)])

    async def evaluate(self, js, arg=None):
        if js is browser._SCAN_JS:
            start = arg or 0
            return [{"index": start + i, "kind": k, "label": lab,
                     "current_value": ""}
                    for i, (lab, k) in enumerate(self.fields_now)]
        if js is browser._ERRORS_JS:
            return list(self.errors_now)
        return None


class FakePage:
    def __init__(self, frame, url):
        self.frames = [frame]
        self.url = url

    async def wait_for_timeout(self, ms):
        return None

    async def inner_text(self, selector):
        return ""


def make_session(buttons, fields=(), errors=(),
                 url="https://wd5.myworkday.com/job/apply/1"):
    frame = FakeFrame(buttons, fields, errors)
    s = browser.BrowserSession()
    s.tabs[0] = {"page": FakePage(frame, url), "fields": {}, "company": "",
                 "title": "", "url": ""}
    s.active_id = 0
    return s, frame


STEP1 = [("First name", "text"), ("Country", "combobox")]
STEP2 = [("School", "combobox"), ("Degree", "combobox")]


# --------------------------------------------------------------------------
# The one that matters most: the Review page must not be clicked
# --------------------------------------------------------------------------
def test_is_submit_page_clicks_nothing():
    btn = FakeButton("Submit application", "submit")
    s, _ = make_session([btn], fields=STEP1)
    res = asyncio.run(s.advance_step())
    assert res["status"] == "is_submit_page", res
    assert btn.clicks == 0, "advance_step must NEVER click a submit control"
    assert res["button_label"] == "submit application", res
    assert res["url_changed"] is False and res["errors"] == [], res
    assert res["fields"], "the review page's fields still come back"


def test_submit_and_continue_also_reads_as_a_submit_page():
    # It contains "continue" but it submits; advance_step must not click it.
    btn = FakeButton("Submit and Continue", "submit")
    s, _ = make_session([btn], fields=STEP1)
    res = asyncio.run(s.advance_step())
    assert res["status"] == "is_submit_page" and btn.clicks == 0, res


# --------------------------------------------------------------------------
# Advancing
# --------------------------------------------------------------------------
def test_advanced_when_labels_change_but_url_does_not():
    # Workday re-renders the next step in place. URL-only classification would
    # call this a "no_change" and the run would stall on a page that DID move.
    frame_box = {}

    def moved():
        frame_box["f"].fields_now = list(STEP2)

    btn = FakeButton("Save and Continue", "submit", on_click=moved)
    s, frame = make_session([btn], fields=STEP1)
    frame_box["f"] = frame
    res = asyncio.run(s.advance_step())
    assert res["status"] == "advanced", res
    assert res["url_changed"] is False, res
    assert btn.clicks == 1, btn.clicks
    assert [f["label"] for f in res["fields"]] == ["School", "Degree"], res
    assert res["button_label"] == "save and continue", res


def test_advanced_when_the_url_changes():
    frame_box, page_box = {}, {}

    def moved():
        page_box["p"].url = "https://wd5.myworkday.com/job/apply/2"

    btn = FakeButton("Next", on_click=moved)
    s, frame = make_session([btn], fields=STEP1)
    frame_box["f"], page_box["p"] = frame, s.page
    res = asyncio.run(s.advance_step())
    assert res["status"] == "advanced" and res["url_changed"] is True, res
    assert res["current_url"].endswith("/2"), res


def test_no_change_when_neither_url_nor_questions_move():
    btn = FakeButton("Next")
    s, _ = make_session([btn], fields=STEP1)
    res = asyncio.run(s.advance_step())
    assert res["status"] == "no_change", res
    assert btn.clicks == 1, btn.clicks
    assert res["url_changed"] is False and res["error_count"] == 0, res


def test_no_advance_button_on_a_single_page_form():
    s, _ = make_session([FakeButton("Save for Later")], fields=STEP1)
    res = asyncio.run(s.advance_step())
    assert res["status"] == "no_advance_button", res
    assert res["button_label"] == "" and res["errors"] == [], res


# --------------------------------------------------------------------------
# Validation errors
# --------------------------------------------------------------------------
def test_blocked_validation_maps_an_error_to_a_field_index():
    errs = [{"index": 1, "field_label": "Country", "message": "Select a value"}]
    s, _ = make_session([FakeButton("Save and Continue", "submit")],
                        fields=STEP1, errors=errs)
    res = asyncio.run(s.advance_step())
    assert res["status"] == "blocked_validation", res
    assert res["error_count"] == 1 and res["unmapped_error_count"] == 0, res
    assert res["errors"][0]["index"] == 1, res
    # The mapped index must address a field in the RETURNED list, since every
    # earlier index died with the re-read.
    assert any(f["index"] == 1 for f in res["fields"]), res["fields"]


def test_unmapped_error_is_reported_not_dropped():
    # An sr-only input is skipped by _SCAN_JS's visibility test, so its error
    # has no index to attach to. It must still come back.
    errs = [{"index": None, "field_label": "Terms and conditions",
             "message": "This field is required"},
            {"index": 0, "field_label": "First name", "message": "Required"}]
    s, _ = make_session([FakeButton("Save and Continue", "submit")],
                        fields=STEP1, errors=errs)
    res = asyncio.run(s.advance_step())
    assert res["status"] == "blocked_validation", res
    assert res["error_count"] == 2, res
    assert res["unmapped_error_count"] == 1, res
    labels = [e["field_label"] for e in res["errors"]]
    assert "Terms and conditions" in labels, labels


def test_validation_wins_over_a_page_that_looks_moved():
    # Belt and braces: if errors are showing, the status is blocked_validation
    # even when the re-render changed the label signature.
    frame_box = {}

    def rerender():
        frame_box["f"].fields_now = list(STEP1) + [("Captcha", "text")]

    errs = [{"index": 0, "field_label": "First name", "message": "Required"}]
    btn = FakeButton("Save and Continue", "submit", on_click=rerender)
    s, frame = make_session([btn], fields=STEP1, errors=errs)
    frame_box["f"] = frame
    res = asyncio.run(s.advance_step())
    assert res["status"] == "blocked_validation", res


# --------------------------------------------------------------------------
# Structural: the generic error mapping and the fixed settle
# --------------------------------------------------------------------------
def test_error_mapping_is_generic_aria_not_per_site():
    js = browser._ERRORS_JS
    assert '[aria-describedby~="' in js, "errors must map via the ARIA link"
    assert "querySelectorAll('[data-jaidx]')" in js, "ancestor fallback missing"
    assert "hops < 4" in js, "the ancestor walk must be bounded"
    # An unmappable error still gets pushed with index null.
    assert "let idx = null;" in js and "index: idx" in js, js


def test_orphan_error_is_never_mapped_to_an_unrelated_field():
    # A real-DOM run (tests/manual/run_dom_harness.py) caught this: the
    # fallback walk used querySelector, which searches an ancestor's WHOLE
    # subtree, so an orphan error reached <body> in two hops and came back as
    # index 0 / "Legal Name" -- the first field on the page, related to
    # nothing. A confidently wrong index is worse than no index: the caller
    # refills the wrong field and unmapped_error_count reads 0, so nothing
    # flags the guess.
    #
    # CI has no browser, so the rule is pinned structurally here and executed
    # for real by the DOM harness. Both halves must hold:
    js = browser._ERRORS_JS
    assert "fs.length === 1" in js, \
        "an ancestor may only be accepted when it holds EXACTLY ONE field"
    assert "fs.length > 1) return null" in js, \
        "an ambiguous ancestor must stop the walk, not climb or guess"
    for tag in ("'BODY'", "'FORM'", "'MAIN'", "'HTML'"):
        assert tag in js, "the walk must never accept a page-level container: " + tag
    # and the discredited "first tagged descendant wins" form must be gone
    assert "querySelector('[data-jaidx]');" not in js, \
        "querySelector returns the first tagged field anywhere in the subtree"


def test_an_ambiguous_ancestor_error_arrives_unmapped_and_counted():
    # The end-to-end consequence of the rule above, through advance_step: the
    # JS hands back index null for an error it cannot associate, and that must
    # show up in unmapped_error_count rather than being silently attached to
    # field 0.
    errs = [{"index": None, "field_label": "",
             "message": "Section could not be validated"},
            {"index": 0, "field_label": "First name", "message": "Required"}]
    s, _ = make_session([FakeButton("Save and Continue", "submit")],
                        fields=STEP1, errors=errs)
    res = asyncio.run(s.advance_step())
    assert res["status"] == "blocked_validation", res
    assert res["unmapped_error_count"] == 1, res
    orphan = [e for e in res["errors"]
              if "could not be validated" in e["message"]]
    assert len(orphan) == 1 and orphan[0]["index"] is None, orphan
    # and it is NOT quietly attributed to the first field
    assert orphan[0]["index"] != 0, orphan


def test_advance_never_waits_for_networkidle():
    # networkidle never fires on Workday (it polls), so the call would hang.
    import inspect
    body = inspect.getsource(browser.BrowserSession.advance_step)
    assert "wait_for_timeout" in body, "the settle must be a fixed timeout"
    # The comment explaining WHY mentions networkidle; an actual call would be
    # awaited, so look for the call, not the word.
    assert "await self.page.wait_for_load_state" not in body, body


def test_advance_and_submit_use_separate_finders():
    # Sharing one finder is how the two exclusion lists drift apart and a Next
    # click ends up submitting.
    assert browser.BrowserSession._find_advance_button is not \
        browser.BrowserSession._find_submit_button


def test_advance_step_result_shape_is_complete():
    s, _ = make_session([FakeButton("Next")], fields=STEP1)
    res = asyncio.run(s.advance_step())
    for key in ("status", "url_changed", "current_url", "errors",
                "error_count", "unmapped_error_count", "fields",
                "button_label"):
        assert key in res, (key, res)


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
