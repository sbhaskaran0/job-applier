"""submit_application must never click a wizard's NEXT button.

`_find_submit_button` used to try `button[type=submit], input[type=submit]`
FIRST -- and Workday's "Save and Continue" is a type=submit button. So on a
multi-step application, submit_application clicked NEXT, advanced the wizard,
and then classified a page it had never seen. A destructive tool silently
doing a different thing than its name is the worst failure mode in this repo.

The fix has two halves, both pinned here: explicit-text candidates now outrank
the generic [type=submit] selector, and `_NOT_SUBMIT_RE` excludes step-control
labels. The exclusion is ANCHORED to the whole label, which is load-bearing:
"Submit and Continue" is a real submit and must survive the word "continue".

The accepted consequence is also pinned: on a wizard page submit_application
returns "no_submit_button" with `form_snapshot` intact and clicks nothing. It
fails honestly instead of advancing the wizard and misreporting.

Standalone, like the rest of tests/ -- `python tests/test_submit_button_guard.py`.
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
# Fakes: a page is a list of buttons plus a list of fields. Selector matching
# mimics only the handful of Playwright selectors this code path uses.
# --------------------------------------------------------------------------
class FakeButton:
    def __init__(self, label, type_attr="", visible=True):
        self.label = label
        self.type = type_attr
        self.visible = visible
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


class FakeLocatorSet:
    def __init__(self, btns):
        self.btns = btns

    async def count(self):
        return len(self.btns)

    def nth(self, i):
        return FakeButtonLocator(self.btns[i])


class FakeFrame:
    def __init__(self, buttons=(), fields=()):
        self.buttons = list(buttons)
        self.fields_now = list(fields)

    def locator(self, selector):
        return FakeLocatorSet([b for b in self.buttons if _matches(b, selector)])

    async def evaluate(self, js, arg=None):
        if js is browser._SCAN_JS:
            start = arg or 0
            return [{"index": start + i, "kind": k, "label": lab,
                     "current_value": ""}
                    for i, (lab, k) in enumerate(self.fields_now)]
        if js is browser._ERRORS_JS:
            return []
        return None


class FakePage:
    def __init__(self, frame, url="https://wd5.myworkday.com/job/apply"):
        self.frames = [frame]
        self.url = url
        self.body = ""

    async def wait_for_timeout(self, ms):
        return None

    async def inner_text(self, selector):
        return self.body


def make_session(buttons, fields=()):
    frame = FakeFrame(buttons, fields)
    s = browser.BrowserSession()
    s.tabs[0] = {"page": FakePage(frame), "fields": {}, "company": "",
                 "title": "", "url": ""}
    s.active_id = 0
    return s, frame


async def _label(session):
    loc = await session._find_submit_button()
    return None if loc is None else await session._label_of(loc)


# --------------------------------------------------------------------------
def test_workday_save_and_continue_is_not_a_submit_button():
    # The live hazard: type=submit, but it is the wizard's Next.
    s, _ = make_session([FakeButton("Save and Continue", "submit")])
    assert asyncio.run(_label(s)) is None


def test_submit_and_continue_is_still_a_submit_button():
    # The anchoring test: excluding on the substring "continue" would kill a
    # real submit control.
    s, _ = make_session([FakeButton("Submit and Continue", "submit")])
    assert asyncio.run(_label(s)) == "submit and continue"


def test_explicit_submit_text_outranks_a_generic_type_submit():
    s, _ = make_session([FakeButton("Save and Continue", "submit"),
                         FakeButton("Submit application", "submit")])
    assert asyncio.run(_label(s)) == "submit application"


def test_quick_apply_is_still_skipped():
    # Pre-existing regression: the DOM-first search once clicked Greenhouse's
    # "Quick Apply with MyGreenhouse".
    s, _ = make_session([FakeButton("Quick Apply with MyGreenhouse", "submit"),
                         FakeButton("Submit application", "submit")])
    assert asyncio.run(_label(s)) == "submit application"
    s2, _ = make_session([FakeButton("Quick Apply with MyGreenhouse", "submit")])
    assert asyncio.run(_label(s2)) is None


def test_other_step_controls_are_excluded_too():
    for label in ("Next", "Continue", "Back", "Previous", "Save",
                  "Save for Later", "Save and Continue"):
        s, _ = make_session([FakeButton(label, "submit")])
        assert asyncio.run(_label(s)) is None, label


def test_plain_submit_button_still_found():
    s, _ = make_session([FakeButton("Submit", "submit")])
    assert asyncio.run(_label(s)) == "submit"


def test_wizard_page_submit_returns_no_submit_button_and_clicks_nothing():
    btn = FakeButton("Save and Continue", "submit")
    s, _ = make_session([btn], fields=[("First name", "text"),
                                       ("Country", "combobox")])
    res = asyncio.run(s.submit_application())
    assert res["status"] == "no_submit_button", res
    assert btn.clicks == 0, "a wizard Next must never be clicked by submit"
    # form_snapshot must survive, or the caller loses what it had filled.
    assert len(res["form_snapshot"]) == 2, res["form_snapshot"]
    assert res["form_snapshot"][0]["label"] == "First name", res["form_snapshot"]
    assert res["current_url"], res


def test_explicit_index_bypasses_the_guard_by_design():
    # Documented escape hatch: passing an index clicks what the caller points
    # at, guard or no guard. Pinned so nobody "fixes" it silently.
    assert "BYPASSES that guard" in browser.BrowserSession.submit_application.__doc__


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
