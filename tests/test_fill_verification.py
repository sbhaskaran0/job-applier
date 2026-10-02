"""`fill_field` must read the value back -- never report a hopeful success.

A live /apply-to-job run parked at step 2 of 7 of a Snap Workday application
because every radio on the page came back `{"status": "filled"}` while none of
them were actually set: Workday paints its own control over the real <input>,
so Playwright's `.check()` raises (or clicks through without firing the React
handler) and the old code returned "filled" unconditionally for
select/radio/checkbox/text without ever looking at the DOM again. The skill
spent its single corrective pass on a lie.

These tests pin the four behaviors that fix depends on:

  * a toggle that does not commit reports `uncommitted` / verified=false /
    observed_value -- NOT "filled";
  * a text field the SITE normalizes ("4155551212" -> "(415) 555-1212") is
    verified=true, so the honest verifier does not invent a retry storm;
  * an unset Workday listbox renders the literal placeholder "Select One", and
    reading that back as a value would both report verified=true against
    nothing and persist "Select One" into the user's answer history -- so the
    shared cascade blacklists it and the fill reports `uncommitted`;
  * a widget with no readable value reports verified=null with a note, because
    "can't tell" is not the same claim as "failed".

The browser is faked (CI has no Playwright and no browser); what is exercised
is the Python decision logic and the shape of what it returns. The JS cascade
itself is asserted structurally, as in tests/test_scan_idempotence.py.

Standalone, like the rest of tests/ -- `python tests/test_fill_verification.py`.
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
# Minimal async fakes. One "element" holds the fake DOM state; the locator
# actions mutate it only when the element is configured to actually commit,
# which is how a no-op widget is modeled.
# --------------------------------------------------------------------------
class FakeElement:
    def __init__(self, *, read_value="", check_raises=False,
                 native_check_commits=True, proxy_exists=True,
                 proxy_commits=True, normalize=None, unreadable=False,
                 aria_scoped=False, options=(), option_click_commits=True,
                 typing_commits=True, expanded=None, never_opens=False):
        self.read_value = read_value
        self.check_raises = check_raises
        self.native_check_commits = native_check_commits
        self.proxy_exists = proxy_exists
        self.proxy_commits = proxy_commits
        self.normalize = normalize or (lambda v: v)
        self.unreadable = unreadable
        self.aria_scoped = aria_scoped
        self.options = list(options)
        self.option_click_commits = option_click_commits
        self.typing_commits = typing_commits
        # aria-expanded: None = the widget doesn't publish it (most fakes),
        # True/False = a real listbox toggle.
        self.expanded = expanded
        self.never_opens = never_opens
        self.checked = False
        self.clicks = 0
        self.proxy_clicks = 0
        self.option_clicks = []

    def value_js(self):
        if self.unreadable:
            return None
        return self.read_value


class FakeLocator:
    def __init__(self, el, proxy=False):
        self.el = el
        self.proxy = proxy

    @property
    def first(self):
        return self

    async def click(self, timeout=None):
        if self.proxy:
            self.el.proxy_clicks += 1
            if self.el.proxy_commits:
                self.el.checked = True
                self.el.read_value = "checked"
            return
        self.el.clicks += 1
        # A listbox button is a toggle: each click flips the popup.
        if self.el.expanded is not None and not self.el.never_opens:
            self.el.expanded = not self.el.expanded

    async def check(self):
        if self.el.check_raises:
            raise RuntimeError("element intercepts pointer events")
        if self.el.native_check_commits:
            self.el.checked = True
            self.el.read_value = "checked"

    async def uncheck(self):
        if self.el.check_raises:
            raise RuntimeError("element intercepts pointer events")
        if self.el.native_check_commits:
            self.el.checked = False
            self.el.read_value = ""

    async def is_checked(self):
        return self.el.checked

    async def fill(self, value):
        self.el.read_value = self.el.normalize(value)

    async def select_option(self, **kwargs):
        self.el.read_value = self.el.normalize(
            kwargs.get("label") or kwargs.get("value") or "")


class FakeOptionLocator:
    def __init__(self, el, name):
        self.el = el
        self.name = name

    @property
    def first(self):
        return self

    async def click(self, timeout=None):
        self.el.option_clicks.append(self.name)
        if not self.el.option_click_commits:
            raise RuntimeError("option not clickable")
        self.el.read_value = self.name
        if self.el.expanded is not None:
            self.el.expanded = False          # picking an option closes it


class FakeFrame:
    def __init__(self, el):
        self.el = el

    async def evaluate(self, js, arg=None):
        el = self.el
        if js in (browser._FIELD_VALUE_JS, browser._COMBO_VALUE_JS,
                  browser._LISTBOX_VALUE_JS):
            return el.value_js()
        if js is browser._PROXY_JS:
            return el.proxy_exists
        if js is browser._COMBO_SCOPED_JS:
            return el.aria_scoped
        if js is browser._EXPANDED_JS:
            return el.expanded
        if js is browser._COMBO_OPTIONS_JS:
            return list(el.options)
        return None                      # the data-japroxy cleanup sweep

    def locator(self, selector):
        return FakeLocator(self.el, proxy="data-japroxy" in selector)

    def get_by_role(self, role, name=None, exact=False):
        return FakeOptionLocator(self.el, name)


class FakeKeyboard:
    def __init__(self, el):
        self.el = el
        self.typed = []
        self.pressed = []

    async def type(self, text, delay=None):
        self.typed.append((text, delay))

    async def press(self, key):
        self.pressed.append(key)
        if key == "Enter" and self.el.typing_commits and self.typed:
            self.el.read_value = self.typed[-1][0]


class FakePage:
    def __init__(self, el):
        self.keyboard = FakeKeyboard(el)
        self.url = "https://example.test/apply"

    async def wait_for_timeout(self, ms):
        return None


def make_session(kind, el, *, native=True, widget=None):
    s = browser.BrowserSession()
    s.tabs[0] = {"page": FakePage(el), "company": "", "title": "", "url": "",
                 "fields": {0: {"frame": FakeFrame(el), "kind": kind,
                                "native": native, "widget": widget}}}
    s.active_id = 0
    return s


# --------------------------------------------------------------------------
# The reported bug
# --------------------------------------------------------------------------
def test_radio_that_does_not_commit_is_uncommitted_not_filled():
    # Workday: .check() raises, the proxy click doesn't take either.
    el = FakeElement(check_raises=True, proxy_exists=True, proxy_commits=False)
    s = make_session("radio", el)
    res = asyncio.run(s.fill_field(0, "Yes"))
    assert res["status"] == "uncommitted", res
    assert res["verified"] is False, res
    assert res["observed_value"] == "", res
    assert el.proxy_clicks == 1, "proxy must be clicked exactly once (a second "\
        "click would un-set a toggle that did work)"


def test_radio_committed_by_the_proxy_click_is_verified():
    # Same intercepted .check(), but the label proxy works -- the actual fix.
    el = FakeElement(check_raises=True, proxy_exists=True, proxy_commits=True)
    s = make_session("radio", el)
    res = asyncio.run(s.fill_field(0, "Yes"))
    assert res["status"] == "filled", res
    assert res["verified"] is True, res
    assert "observed_value" not in res, res


def test_native_radio_still_works_without_a_proxy_click():
    el = FakeElement()
    s = make_session("radio", el)
    res = asyncio.run(s.fill_field(0, "yes"))
    assert res["verified"] is True and res["status"] == "filled", res
    assert el.proxy_clicks == 0, "no proxy click needed when .check() worked"


def test_non_native_button_group_option_is_verified_too():
    # Ashby Yes/No segmented control: bare click(), which used to return
    # "filled" unconditionally.
    el = FakeElement(proxy_exists=False)
    s = make_session("radio", el, native=False)
    res = asyncio.run(s.fill_field(0, "Yes"))
    assert res["status"] == "uncommitted" and res["verified"] is False, res
    assert res["observed_value"] == "", res
    assert el.clicks == 1, el.clicks


def test_uncheck_that_does_not_commit_is_uncommitted():
    el = FakeElement(read_value="checked", native_check_commits=False,
                     proxy_exists=False)
    el.checked = True
    s = make_session("checkbox", el)
    res = asyncio.run(s.fill_field(0, "false"))
    assert res["status"] == "uncommitted" and res["verified"] is False, res
    assert res["observed_value"] == "checked", res


# --------------------------------------------------------------------------
# The false-failure guard: site-normalized text must NOT look like a failure
# --------------------------------------------------------------------------
def test_phone_normalization_is_still_verified():
    el = FakeElement(normalize=lambda v: "(415) 555-1212")
    s = make_session("text", el)
    res = asyncio.run(s.fill_field(0, "4155551212"))
    assert res["status"] == "filled", res
    assert res["verified"] is True, res
    assert "observed_value" not in res, "no retry signal for a normalized value"


def test_values_match_is_loose_but_not_blind():
    m = browser._values_match
    assert m("4155551212", "(415) 555-1212")
    assert m("United States", "united states")
    assert m("Yes", "Yes, I am")                   # containment either way
    assert not m("Yes", "No")
    assert not m("", "")                           # empty is never a match
    assert not m("Yes", "")


def test_text_that_reads_back_different_is_filled_but_unverified():
    # Not a proven failure (the field holds *something*), so status stays
    # "filled" -- but verified=false and observed_value say what is really in
    # there.
    el = FakeElement(normalize=lambda v: "truncated")
    s = make_session("text", el)
    res = asyncio.run(s.fill_field(0, "a much longer answer"))
    assert res["status"] == "filled" and res["verified"] is False, res
    assert res["observed_value"] == "truncated", res


def test_empty_text_readback_is_uncommitted():
    el = FakeElement(normalize=lambda v: "")
    s = make_session("text", el)
    res = asyncio.run(s.fill_field(0, "hello"))
    assert res["status"] == "uncommitted" and res["verified"] is False, res
    assert res["observed_value"] == "", res


def test_select_mismatch_is_uncommitted():
    el = FakeElement(normalize=lambda v: "Select...")
    s = make_session("select", el)
    res = asyncio.run(s.fill_field(0, "United States"))
    assert res["status"] == "uncommitted" and res["verified"] is False, res
    assert res["observed_value"] == "Select...", res


# --------------------------------------------------------------------------
# The "Select One" placeholder trap
# --------------------------------------------------------------------------
def test_listbox_placeholder_blacklist_is_in_the_shared_js():
    js = browser._LISTBOX_VALUE_FN
    for phrase in ("select one", "choose one", "--", "none selected", "search"):
        assert "'%s'" % phrase in js, phrase
    # It must compare case-insensitively against whitespace-collapsed text, or
    # "Select One" / "Select  one" would slip through.
    assert "toLowerCase()" in js, js
    assert "replace(/\\s+/g, ' ')" in js, js
    # ...and an option that merely echoes the field's own label is not a value.
    assert "aria.toLowerCase()" in js, js


def test_read_form_and_the_verifier_share_the_listbox_cascade():
    # One source of truth: if these ever diverge, read_form can report a value
    # the fill verifier calls empty (or the reverse).
    assert browser._LISTBOX_VALUE_FN in browser._SCAN_JS
    assert browser._LISTBOX_VALUE_FN in browser._LISTBOX_VALUE_JS
    assert browser._LISTBOX_VALUE_FN in browser._COMBO_VALUE_JS
    assert "__LISTBOX_VALUE_FN__" not in (
        browser._SCAN_JS + browser._LISTBOX_VALUE_JS + browser._COMBO_VALUE_JS)


def test_scanner_sees_listbox_buttons_as_comboboxes():
    assert 'button[aria-haspopup="listbox"]' in browser._SCAN_JS
    assert "widget: isListbox ? 'listbox' : undefined" in browser._SCAN_JS


def test_listbox_reading_back_the_placeholder_is_uncommitted():
    # The JS filters "Select One" to "", so the fill sees an empty read-back --
    # exactly like an untouched field, which is what it is.
    el = FakeElement(read_value="", aria_scoped=False, typing_commits=False)
    s = make_session("combobox", el, widget="listbox")
    res = asyncio.run(s.fill_field(0, "United States"))
    assert res["status"] == "uncommitted", res
    assert res["verified"] is False, res
    assert res["observed_value"] == "", res
    # Unscoped ARIA -> it must NOT click options scraped from some other
    # widget; it types instead, at the deliberate 100ms delay.
    assert el.option_clicks == [], el.option_clicks
    assert s.page.keyboard.typed == [("United States", 100)], \
        s.page.keyboard.typed
    assert "Escape" in s.page.keyboard.pressed, s.page.keyboard.pressed


def test_listbox_enumerates_first_when_aria_scoping_worked():
    el = FakeElement(aria_scoped=True,
                     options=["United States", "United Kingdom", "Canada"])
    s = make_session("combobox", el, widget="listbox")
    res = asyncio.run(s.fill_field(0, "US"))
    assert el.option_clicks == ["United States"], el.option_clicks
    assert res["status"] == "filled" and res["verified"] is True, res
    assert res["value"] == "United States", res
    assert s.page.keyboard.typed == [], "no typeahead needed after a clean pick"


def test_listbox_falls_back_to_typing_when_nothing_matched():
    el = FakeElement(aria_scoped=True, options=["Alpha", "Beta"])
    s = make_session("combobox", el, widget="listbox")
    res = asyncio.run(s.fill_field(0, "Gamma"))
    assert s.page.keyboard.typed == [("Gamma", 100)], s.page.keyboard.typed
    assert res["verified"] is True and res["value"] == "Gamma", res


def test_an_already_open_listbox_is_not_clicked_shut():
    # A listbox button is a TOGGLE. A real-DOM run caught this: a previous
    # field's failed fill left its popup open (the Escape didn't take), and the
    # next field's perfectly valid fill then failed because the "open" click
    # CLOSED the widget and the enumeration found nothing. A cross-field
    # failure like that is near-impossible to diagnose from a live run.
    el = FakeElement(expanded=True, aria_scoped=True,
                     options=["Mobile", "Home", "Work"])
    s = make_session("combobox", el, widget="listbox")
    res = asyncio.run(s.fill_field(0, "Mobile"))
    assert el.clicks == 0, "an already-open listbox must not be clicked"
    assert el.option_clicks == ["Mobile"], el.option_clicks
    assert res["status"] == "filled" and res["verified"] is True, res


def test_a_closed_listbox_is_opened_before_enumerating():
    el = FakeElement(expanded=False, aria_scoped=True, options=["Mobile"])
    s = make_session("combobox", el, widget="listbox")
    res = asyncio.run(s.fill_field(0, "Mobile"))
    assert el.clicks == 1, el.clicks
    assert res["verified"] is True, res


def test_a_listbox_that_never_opens_says_so_instead_of_blaming_the_value():
    # "didn't open" and "didn't commit" need different corrective actions.
    el = FakeElement(expanded=False, never_opens=True, aria_scoped=True,
                     options=["Mobile"], typing_commits=False)
    s = make_session("combobox", el, widget="listbox")
    res = asyncio.run(s.fill_field(0, "Mobile"))
    assert res["status"] == "uncommitted" and res["verified"] is False, res
    assert "did not OPEN" in res["note"], res["note"]
    # It must not click options out of a popup that never opened...
    assert el.option_clicks == [], el.option_clicks
    # ...and the recovery is ONE bounded retry click, not a loop.
    assert el.clicks == 2, el.clicks


def test_uncommitted_listbox_returns_the_options_it_actually_enumerated():
    el = FakeElement(aria_scoped=True, options=["Alpha", "Beta"],
                     typing_commits=False)
    s = make_session("combobox", el, widget="listbox")
    res = asyncio.run(s.fill_field(0, "Gamma"))
    assert res["status"] == "uncommitted", res
    assert res["options"] == ["Alpha", "Beta"], res


# --------------------------------------------------------------------------
# "Can't tell" is not "failed"
# --------------------------------------------------------------------------
def test_unreadable_widget_reports_verified_null_with_a_note():
    el = FakeElement(unreadable=True)
    s = make_session("text", el)
    res = asyncio.run(s.fill_field(0, "anything"))
    assert res["status"] == "filled", res
    assert res["verified"] is None, res
    assert res.get("note"), res
    assert "observed_value" not in res, res


def test_unreadable_radio_reports_verified_null_too():
    el = FakeElement(unreadable=True)
    s = make_session("radio", el)
    res = asyncio.run(s.fill_field(0, "Yes"))
    assert res["verified"] is None and res["status"] == "filled", res
    assert res.get("note"), res


# --------------------------------------------------------------------------
# The contract the skills read
# --------------------------------------------------------------------------
def test_proven_failures_use_the_uncommitted_status_vocabulary():
    # Returning {"status": "filled", "verified": false} for a proven failure
    # would mean an un-updated skill reads a failure as a success. Both skills
    # already handle "uncommitted" correctly, so proven failures must land
    # there and nowhere else.
    for kind in ("select", "radio", "checkbox", "combobox"):
        res = browser._fill_result(0, kind, "Yes", "No")
        assert res["status"] == "uncommitted", (kind, res)
        assert res["verified"] is False and res["observed_value"] == "No", res


def test_fill_many_still_reports_per_item_errors():
    el = FakeElement()
    s = make_session("text", el)
    out = asyncio.run(s.fill_many([{"index": 0, "value": "hi"},
                                   {"index": 99, "value": "nope"}]))
    assert out[0]["verified"] is True, out[0]
    assert out[1]["status"] == "error", out[1]


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
