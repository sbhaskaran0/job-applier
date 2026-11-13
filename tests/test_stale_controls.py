"""Entry/auth clicks never act on a stale control, and slow modal/route
renders are waited for. Both bugs came from the first live Workday run
(Capital One, capitalone.wd12, 2026-10-04):

  1. authenticate filled email + both passwords but never clicked "Create
     Account" (no_auth_submit): the control dicts came from a scan made
     BEFORE the fills, and every later scan renumbered the data-jactl tags
     (Workday adds a password show/hide toggle once a password is typed), so
     the stale overlay index pointed at a different element or none.
  2. start_application returned no_entry twice: "Apply" opens the "Start Your
     Application" chooser as a modal on the SAME URL a few seconds later, and
     "Apply Manually" flips the URL to /apply/applyManually before the sign-in
     form renders; settle returned on the first quiet poll.

Pinned here, with a fake DOM whose scans can renumber every tag (the old,
worst-case behaviour) or keep them stable (the fix):
  * a stale control whose tag now sits on another element is never clicked;
  * _click_first re-picks from a fresh scan and clicks the right element;
  * authenticate clicks the create-account overlay exactly once and never the
    show-password eye inserted by the password fill;
  * start_application waits out a delayed same-URL modal and a delayed
    auth render, prefers the modal's controls, reaches auth_required in one
    call, and stays bounded when nothing ever renders.

Standalone -- `python tests/test_stale_controls.py`.
"""

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_pw = types.ModuleType("playwright")
_pw_async = types.ModuleType("playwright.async_api")
_pw_async.async_playwright = lambda: None
sys.modules.setdefault("playwright", _pw)
sys.modules.setdefault("playwright.async_api", _pw_async)

from src import browser, config          # noqa: E402

FAKE_PW = "fake-pw-123"
FAKE_EMAIL = "me@example.test"
HOST = "acme.wd12.myworkdayjobs.com"


# --------------------------------------------------------------------------
# A fake DOM: elements carry a data-jactl attribute; scans tag them.
# --------------------------------------------------------------------------
class El:
    def __init__(self, name, label, tag="button", type_="", role="",
                 intercepted=False, chrome=False, after=True):
        self.name, self.label, self.tag, self.type, self.role = (
            name, label, tag, type_, role)
        self.intercepted = intercepted   # an overlay sits on top of it
        self.chrome, self.after = chrome, after
        self.jactl = None
        self.clicks = 0


class DomFrame:
    """`renumber=True` reproduces the old _CONTROLS_JS (strip every tag,
    number from 0 on every scan); False reproduces the fix (stable tags)."""

    def __init__(self, els, renumber):
        self.els, self.renumber = list(els), renumber
        self.next = 0
        self.on_click = None

    async def evaluate(self, js, arg=None):
        if js is not browser._CONTROLS_JS:
            return None
        if self.renumber:
            for e in self.els:
                e.jactl = None
            self.next = 0
        out = []
        for pos, e in enumerate(self.els):
            if e.jactl is None:
                e.jactl, self.next = self.next, self.next + 1
            out.append({"i": e.jactl, "label": e.label, "tag": e.tag,
                        "type": e.type, "role": e.role, "pos": 100 + pos,
                        "in_chrome": e.chrome, "same_form_email": False,
                        "after_email": e.after, "after_password": e.after,
                        "in_dialog": False})
        return {"controls": out, "headings": ["Create Account"],
                "notices": [], "field_pos": {"0": 10, "1": 11, "2": 12,
                                             "3": 13},
                "has_step": False, "has_dialog": False}

    def locator(self, selector):
        want = int(selector.split('"')[1])
        return DomLocator(self, [e for e in self.els if e.jactl == want])


class DomLocator:
    def __init__(self, frame, hits):
        self.frame, self.hits = frame, hits

    async def count(self):
        return len(self.hits)

    @property
    def first(self):
        return DomLocator(self.frame, self.hits[:1])

    async def evaluate(self, js, arg=None, timeout=None):
        assert js is browser._CONTROL_LABEL_JS
        if not self.hits:
            raise TimeoutError("no element")
        return self.hits[0].label

    async def click(self, trial=False, timeout=None):
        if not self.hits:
            raise TimeoutError("no element")
        el = self.hits[0]
        if el.intercepted:
            raise TimeoutError("<div class=click_filter> intercepts pointer events")
        if not trial:
            el.clicks += 1
            if self.frame.on_click:
                self.frame.on_click(el)


class DomPage:
    def __init__(self, frame):
        self.frames = [frame]
        self.url = "https://%s/x/job/1/apply/applyManually" % HOST

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0)

    async def inner_text(self, selector):
        return ""


def auth_dom(renumber):
    els = [El("hdr", "Sign In", chrome=True, after=False),
           El("cf", "Create Account", tag="div", role="button"),
           El("submit", "Create Account", type_="submit", intercepted=True),
           El("toggle", "Sign In"),
           El("forgot", "Forgot your password?")]
    return els, DomFrame(els, renumber)


def by_name(els, name):
    return next(e for e in els if e.name == name)


def insert_eye(frame):
    """What typing a password does on Workday: a show/hide toggle appears
    BEFORE the submit, shifting every renumbered tag after it."""
    if not any(e.name == "eye" for e in frame.els):
        idx = next(i for i, e in enumerate(frame.els) if e.name == "cf")
        frame.els.insert(idx, El("eye", "Show Password"))


class DomSession(browser.BrowserSession):
    def __init__(self, frame):
        super().__init__()
        self.frame = frame
        self.tabs[0] = {"page": DomPage(frame), "fields": {}, "company": "",
                        "title": "", "url": ""}
        self.active_id = 0
        self._ledger_path = False
        self.done = False
        self.typed = {}

    async def read_form(self, values_only=False):
        if self.done:
            return [{"index": 0, "kind": "text", "label": "First Name*"}]
        return [{"index": 0, "kind": "text", "label": "Email Address*"},
                {"index": 1, "kind": "text", "label": "Password*",
                 "sensitive": True},
                {"index": 2, "kind": "text", "label": "Verify New Password*",
                 "sensitive": True},
                {"index": 3, "kind": "checkbox",
                 "label": "I acknowledge and agree to the Privacy Notice"}]

    async def detect_blockers(self):
        return {}

    async def fill_sensitive(self, index, value, require_password=False,
                             mark=True):
        self.typed[index] = value
        if require_password:
            insert_eye(self.frame)
            # the fill path re-scans (here: a read of the page), which under
            # the old JS renumbered every tag
            await self._scan_controls()
        return {"index": index, "status": "filled", "sensitive": True}

    async def fill_field(self, index, value):
        self.typed[index] = value
        return {"index": index, "status": "filled"}


def with_creds(fn):
    real = config.credential
    config.credential = lambda service, field: (
        (FAKE_EMAIL if field == "email" else FAKE_PW), None)
    try:
        return fn()
    finally:
        config.credential = real


# --------------------------------------------------------------------------
# Bug 1
# --------------------------------------------------------------------------
def test_stale_control_pointing_elsewhere_is_never_clicked():
    els, frame = auth_dom(renumber=True)
    s = DomSession(frame)
    stale = asyncio.run(s._scan_controls())
    cf = next(c for c in stale if c["label"] == "Create Account"
              and c["tag"] == "div")
    insert_eye(frame)
    asyncio.run(s._scan_controls())          # renumbers: cf's old tag -> eye
    eye = by_name(frame.els, "eye")
    assert eye.jactl == cf["i"], "fixture must put the eye on the stale tag"
    assert asyncio.run(s._click_control(cf)) is False
    assert eye.clicks == 0 and by_name(els, "cf").clicks == 0


def test_stale_control_whose_tag_vanished_is_not_clicked():
    els, frame = auth_dom(renumber=False)
    s = DomSession(frame)
    stale = asyncio.run(s._scan_controls())
    cf = next(c for c in stale if c["tag"] == "div")
    by_name(els, "cf").jactl = None          # React re-rendered the node
    assert asyncio.run(s._click_control(cf)) is False
    assert by_name(els, "cf").clicks == 0


def test_stable_tags_survive_a_rescan_with_an_inserted_control():
    els, frame = auth_dom(renumber=False)
    s = DomSession(frame)
    def key(c):
        return (c["label"], c["tag"], c["in_chrome"])
    first = {key(c): c["i"] for c in asyncio.run(s._scan_controls())}
    assert len(first) == len(els)
    insert_eye(frame)
    second = asyncio.run(s._scan_controls())
    for c in second:
        if c["label"] != "Show Password":
            assert first[key(c)] == c["i"], c
    eye = next(c for c in second if c["label"] == "Show Password")
    assert eye["i"] not in first.values()


def test_click_first_repicks_from_a_fresh_scan():
    for renumber in (True, False):
        els, frame = auth_dom(renumber)
        s = DomSession(frame)
        state = asyncio.run(s._page_state())
        subs = browser._pick_auth_controls(state["controls"], "create_account",
                                           submit=True)
        assert [c["tag"] for c in subs] == ["button", "div"], subs
        insert_eye(frame)
        # tags shift under renumber; the signature is the post-fill one (as
        # authenticate takes it) but `subs` are the stale pre-fill dicts
        fresh = asyncio.run(s._page_state())

        def pick(st):
            return browser._pick_auth_controls(st["controls"],
                                               "create_account", submit=True)
        got = asyncio.run(s._click_first(subs, s._state_sig(fresh), pick=pick))
        eye = by_name(frame.els, "eye")
        cf, sub = by_name(els, "cf"), by_name(els, "submit")
        assert got is not None, renumber
        assert eye.clicks == 0, (renumber, "clicked the show-password eye")
        assert cf.clicks + sub.clicks == 1, (renumber, cf.clicks, sub.clicks)
        assert cf.clicks == 1, (renumber, "the overlay takes the click")


def test_authenticate_clicks_create_account_once_never_the_eye():
    for renumber in (True, False):
        els, frame = auth_dom(renumber)
        s = DomSession(frame)

        def on_click(el, s=s):
            if el.name in ("cf", "submit"):
                s.done = True                 # account created -> the form
        frame.on_click = on_click
        res = with_creds(lambda: asyncio.run(s.authenticate("create_account")))
        eye = by_name(frame.els, "eye")
        cf, sub = by_name(els, "cf"), by_name(els, "submit")
        assert res["status"] == "authenticated", (renumber, res)
        assert cf.clicks + sub.clicks == 1, (renumber, cf.clicks, sub.clicks)
        assert eye.clicks == 0, renumber
        assert by_name(els, "hdr").clicks == 0
        assert by_name(els, "toggle").clicks == 0
        assert s.typed.get(1) == FAKE_PW and s.typed.get(2) == FAKE_PW
        assert FAKE_PW not in repr(res) and FAKE_EMAIL not in repr(res)


# --------------------------------------------------------------------------
# Bug 2: a poll-counted site (each _page_state is one ~500ms poll)
# --------------------------------------------------------------------------
def ctl(i, label, dialog=False, chrome=False):
    return {"i": i, "label": label, "tag": "a", "type": "", "role": "button",
            "after_password": False, "after_email": False,
            "same_form_email": False, "in_chrome": chrome, "pos": i,
            "in_dialog": dialog, "cls": browser.classify_control(label),
            "frame": object()}


class SlowSite:
    """Job page -> (click Apply) dialog shell at once, its buttons after
    `modal_delay` polls, SAME URL -> (click Apply Manually) URL flips at once,
    sign-in form after `auth_delay` polls."""

    def __init__(self, modal_delay=6, auth_delay=6, job_delay=0):
        self.modal_delay, self.auth_delay = modal_delay, auth_delay
        self.job_delay = job_delay
        self.phase, self.since = "job", 0
        self.clicks = []

    def state(self):
        self.since += 1
        base = "https://%s/Capital_One/job/McLean-VA/PM_R1" % HOST
        st = {"url": base, "blocked": False, "signals": [], "body": "",
              "has_step": False, "field_pos": {}, "notices": [],
              "headings": ["Product Manager"], "has_dialog": False,
              "fields": [{"index": 9, "kind": "combobox", "widget": "listbox",
                          "label": "languageSelectorButton"}],
              "controls": [ctl(0, "Sign In", chrome=True), ctl(1, "Apply")]}
        if self.phase == "job" and self.since <= self.job_delay:
            st["controls"] = st["controls"][:1]     # Apply not rendered yet
        if self.phase == "modal":
            st["has_dialog"] = True
            if self.since > self.modal_delay:
                st["headings"] = st["headings"] + ["Start Your Application"]
                st["controls"] = st["controls"] + [
                    ctl(2, "Autofill with Resume", dialog=True),
                    ctl(3, "Apply Manually", dialog=True),
                    ctl(4, "Use My Last Application", dialog=True)]
        elif self.phase == "auth":
            st["url"] = base + "/apply/applyManually"
            st["controls"] = [ctl(0, "Sign In", chrome=True)]
            st["headings"] = []
            if self.since > self.auth_delay:
                st["headings"] = ["Create Account"]
                st["fields"] = [
                    {"index": 0, "kind": "text", "label": "Email Address*"},
                    {"index": 1, "kind": "text", "label": "Password*",
                     "sensitive": True},
                    {"index": 2, "kind": "text", "sensitive": True,
                     "label": "Verify New Password*"}]
                st["field_pos"] = {"0": 10, "1": 11, "2": 12}
                st["controls"] = st["controls"] + [
                    {**ctl(5, "Create Account"), "after_password": True,
                     "after_email": True, "type": "submit", "pos": 20}]
        return st

    def click(self, c):
        self.clicks.append(c["label"])
        if self.phase == "job" and c["label"] == "Apply":
            self.phase, self.since = "modal", 0
        elif self.phase == "modal" and c["label"] == "Apply Manually" \
                and c.get("in_dialog"):
            self.phase, self.since = "auth", 0


class SlowPage:
    def __init__(self):
        self.url = "https://%s/x" % HOST
        self.frames = []

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0.002)

    async def inner_text(self, selector):
        return ""


class SlowSession(browser.BrowserSession):
    def __init__(self, site):
        super().__init__()
        self.site = site
        self.tabs[0] = {"page": SlowPage(), "fields": {}, "company": "",
                        "title": "", "url": ""}
        self.active_id = 0

    async def _page_state(self):
        st = self.site.state()
        self.page.url = st["url"]
        return st

    async def _click_control(self, c, timeout=3000):
        self.site.click(c)
        return True


def test_modal_on_same_url_with_delayed_render_reaches_auth_in_one_call():
    site = SlowSite(modal_delay=6, auth_delay=6)
    res = asyncio.run(SlowSession(site).start_application())
    assert res["status"] == "auth_required", res
    assert site.clicks == ["Apply", "Apply Manually"], site.clicks
    assert res["auth"]["mode"] == "create_account", res["auth"]


def test_late_rendering_apply_is_waited_for():
    # live probe: no Apply at open_job's 2.5s mark, there a moment later
    site = SlowSite(job_delay=3, modal_delay=2, auth_delay=2)
    res = asyncio.run(SlowSession(site).start_application())
    assert res["status"] == "auth_required", res
    assert site.clicks == ["Apply", "Apply Manually"], site.clicks


def test_modal_controls_win_over_the_background_apply():
    st = SlowSite(modal_delay=0)
    st.phase = "modal"
    entries = browser._entry_targets(st.state(), "entry")
    assert [c["label"] for c in entries] == ["Apply Manually"], entries
    # a dialog with no entry control (cookie banner) hides nothing
    st2 = SlowSite()
    s = st2.state()
    s["has_dialog"] = True
    s["controls"].append(ctl(7, "Accept cookies", dialog=True))
    assert [c["label"] for c in browser._entry_targets(s, "entry")] == ["Apply"]


def test_url_change_alone_is_not_progress():
    site = SlowSite()
    before = site.state()
    site.phase, site.since = "auth", 0
    shell = site.state()
    assert not browser._entry_progress(shell, before)
    site.since = 99
    assert browser._entry_progress(site.state(), before)


def test_never_rendering_modal_is_bounded():
    site = SlowSite(modal_delay=10 ** 9)
    s = SlowSession(site)
    s._ENTRY_BUDGET_S = 0.3
    s._WAIT_S = 0.2
    loop = asyncio.new_event_loop()
    try:
        t0 = loop.time()
        res = loop.run_until_complete(s.start_application())
        took = loop.time() - t0
    finally:
        loop.close()
    assert res["status"] == "no_entry", res
    assert took < 2.0, took
    # the background Apply behind the open modal shell was not re-clicked
    # more than the 4-click cap, and never anything else
    assert set(site.clicks) == {"Apply"} and len(site.clicks) <= 4, site.clicks


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
        except Exception as exc:
            failed += 1
            print("ERROR %s: %s: %s" % (name, type(exc).__name__, exc))
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
