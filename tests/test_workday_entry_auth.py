"""Workday entry + auth: Apply -> chooser -> Create Account / Sign In.

Every Workday attempt used to park at once: the job page's only control is
"Apply", the chooser's are "Autofill with Resume" / "Apply Manually" / "Use My
Last Application", and step 1 is "Create Account/Sign In". No tool could click
Apply, and the submit finder classified "Apply Manually" and the type=submit
"Create Account" as SUBMIT, so submit_application could have clicked them.

Pinned here:
  * the label classification table, and that the submit finder never returns
    an entry / auth / never-click control;
  * advance_step reports entry_page / auth_page and clicks nothing;
  * start_application clicks Apply then Apply Manually, never the autofill or
    last-application buttons, and is idempotent on a form;
  * the authenticate state machine (fake pages): sign-in success, no_account ->
    create, account_exists -> sign-in, a bad password stops, at most 2 submits,
    no credentials -> zero page access, no credential in repr(result), an
    exception reduced to its type name;
  * the open_verification_link allowlist.

Standalone -- `python tests/test_workday_entry_auth.py`.
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

TABLE = {
    "Apply": "entry", "Apply Now": "entry", "Apply Manually": "entry",
    "Autofill with Resume": "never", "Use My Last Application": "never",
    "Sign In": "auth", "Create Account": "auth", "Verify": "auth",
    "Submit": "submit", "Submit Application": "submit", "Next": "advance",
    "Save and Continue": "advance", "Review": "advance",
    "Sign in with email": "auth_reveal", "Sign in with Google": "never",
    "Start Your Application": "entry", "Resend verification email": "auth",
}


# --------------------------------------------------------------------------
# Locator-level fakes (same shape as test_submit_button_guard.py)
# --------------------------------------------------------------------------
class FakeButton:
    def __init__(self, label, type_attr="", visible=True):
        self.label, self.type, self.visible, self.clicks = (label, type_attr,
                                                            visible, 0)


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
        self.fields_now = list(fields)   # (label, kind, sensitive)

    def locator(self, selector):
        return FakeLocatorSet([b for b in self.buttons if _matches(b, selector)])

    async def evaluate(self, js, arg=None):
        if js is browser._SCAN_JS:
            start = arg or 0
            out = []
            for i, (lab, k, sens) in enumerate(self.fields_now):
                d = {"index": start + i, "kind": k, "label": lab,
                     "current_value": ""}
                if sens:
                    d["sensitive"] = True
                out.append(d)
            return out
        if js is browser._CONTROLS_JS:
            return {"controls": [{"i": i, "label": b.label, "tag": "button",
                                  "type": b.type, "role": "",
                                  "after_password": False}
                                 for i, b in enumerate(self.buttons)],
                    "headings": [], "notices": []}
        if js is browser._ERRORS_JS:
            return []
        return None


class FakePage:
    def __init__(self, frame, url="https://acme.wd5.myworkdayjobs.com/x/job/1"):
        self.frames = [frame]
        self.url = url

    async def wait_for_timeout(self, ms):
        return None

    async def inner_text(self, selector):
        return ""


def make_session(buttons, fields=()):
    frame = FakeFrame(buttons, fields)
    s = browser.BrowserSession()
    s.tabs[0] = {"page": FakePage(frame), "fields": {}, "company": "",
                 "title": "", "url": ""}
    s.active_id = 0
    return s, frame


# --------------------------------------------------------------------------
# 1. classification + submit finder
# --------------------------------------------------------------------------
def test_classification_table():
    for label, want in TABLE.items():
        got = browser.classify_control(label)
        assert got == want, (label, got, want)
    assert browser.classify_control("Apply Manually →") == "entry"
    assert browser.classify_control("Submit and Continue") == "submit"


def test_submit_finder_never_returns_entry_auth_or_never_click():
    # unfilled tab: no entry / auth / never-click / advance label is a submit
    for label, cls in TABLE.items():
        s, _ = make_session([FakeButton(label, "submit")])
        loc = asyncio.run(s._find_submit_button())
        got = None if loc is None else asyncio.run(s._label_of(loc))
        if cls == "submit":
            assert got == label.lower(), (label, got)
        else:
            assert got is None, (label, got)


def test_filled_tab_treats_bare_apply_as_the_submit():
    # finding 5/8: on a filled application a final "Apply" IS the submit
    for label, want in (("Apply", "apply"), ("Apply Now", "apply now"),
                        ("Apply Manually", None), ("Sign In", None),
                        ("Autofill with Resume", None)):
        s, _ = make_session([FakeButton(label, "submit")],
                            fields=[("First Name", "text", False)])
        s.tabs[0]["filled"] = True
        loc = asyncio.run(s._find_submit_button())
        got = None if loc is None else asyncio.run(s._label_of(loc))
        assert got == want, (label, got)


def test_filled_lone_apply_is_submit_page_and_not_clicked():
    btn = FakeButton("Apply")
    s, _ = make_session([btn])
    s.tabs[0]["filled"] = True
    res = asyncio.run(s.advance_step())
    assert res["status"] == "is_submit_page" and btn.clicks == 0, res


def test_review_button_advances():
    btn = FakeButton("Review")
    s, _ = make_session([btn], fields=[("First Name", "text", False)])
    asyncio.run(s.advance_step())
    assert btn.clicks == 1, "a non-destructive Review step control advances"


def test_submit_finder_picks_submit_among_entry_and_auth():
    s, _ = make_session([FakeButton("Apply Manually"),
                         FakeButton("Create Account", "submit"),
                         FakeButton("Submit application", "submit")])
    loc = asyncio.run(s._find_submit_button())
    assert asyncio.run(s._label_of(loc)) == "submit application"


def test_submit_application_on_auth_page_clicks_nothing():
    btn = FakeButton("Create Account", "submit")
    s, _ = make_session([btn], fields=[("Email Address*", "text", False),
                                       ("Password*", "text", True)])
    res = asyncio.run(s.submit_application())
    assert res["status"] == "no_submit_button" and btn.clicks == 0, res


# --------------------------------------------------------------------------
# 2. advance_step
# --------------------------------------------------------------------------
def test_advance_step_entry_page():
    btns = [FakeButton("Sign In"), FakeButton("Apply")]
    s, _ = make_session(btns)
    res = asyncio.run(s.advance_step())
    assert res["status"] == "entry_page", res
    assert all(b.clicks == 0 for b in btns)


def test_advance_step_chooser_is_entry_page():
    btns = [FakeButton("Autofill with Resume"), FakeButton("Apply Manually"),
            FakeButton("Use My Last Application")]
    s, _ = make_session(btns)
    res = asyncio.run(s.advance_step())
    assert res["status"] == "entry_page", res
    assert all(b.clicks == 0 for b in btns)


def test_advance_step_auth_page():
    btns = [FakeButton("Sign In"), FakeButton("Create Account", "submit")]
    s, _ = make_session(btns, fields=[("Email Address*", "text", False),
                                      ("Password*", "text", True),
                                      ("Verify New Password*", "text", True)])
    res = asyncio.run(s.advance_step())
    assert res["status"] == "auth_page", res
    assert all(b.clicks == 0 for b in btns)


def test_advance_step_plain_page_still_no_advance_button():
    s, _ = make_session([FakeButton("Sign In")],
                        fields=[("Favourite colour", "text", False)])
    assert asyncio.run(s.advance_step())["status"] == "no_advance_button"


# --------------------------------------------------------------------------
# 3. state-machine fakes: a site object renders states; clicks mutate it.
# --------------------------------------------------------------------------
def ctl(i, label, type_="", after=True, pos=None, chrome=None):
    """`after` = after the password/email field and inside the auth form;
    a control with after=False is page header chrome."""
    return {"i": i, "label": label, "type": type_, "after_password": after,
            "after_email": after, "same_form_email": False,
            "in_chrome": (not after) if chrome is None else chrome,
            "pos": (20 + i) if pos is None else pos,
            "cls": browser.classify_control(label), "frame": object()}


DEFAULT_BOXES = [{"label": "I acknowledge and agree to the Privacy Notice"}]


class FakeSite:
    def __init__(self, page="auth", mode="create_account", exists=False,
                 pw_ok=True, verify=False, signin_error="combined",
                 echo_email=False, boxes=None, host="acme.wd5.myworkdayjobs.com"):
        self.page, self.mode, self.exists, self.pw_ok = page, mode, exists, pw_ok
        self.verify, self.signin_error = verify, signin_error
        self.echo_email = echo_email
        self.boxes = DEFAULT_BOXES if boxes is None else boxes
        self.host = host
        self.toggle_navigates = False
        self.apply_noop = False
        self.body = ""
        self.extra_controls = []
        self.notice = ""
        self.submits, self.clicks, self.filled = [], [], {}
        self.reads = 0
        # live Capital One: a successful create redirects to a plain Sign In
        # page with no message; a successful sign-in shows a blank page for
        # a few reads (the redirect) before the form renders.
        self.create_lands_signin = False
        self.loading_reads = 0

    def state(self):
        self.reads += 1
        if self.page == "loading":
            if self.loading_reads > 0:
                self.loading_reads -= 1
            else:
                self.page = "form"
        url = "https://%s/x/job/1" % self.host
        st = {"url": url, "blocked": False, "signals": [], "body": self.body,
              "has_step": False, "field_pos": {},
              "notices": [self.notice] if self.notice else []}
        lang = {"index": 9, "kind": "combobox", "widget": "listbox",
                "label": "languageSelectorButton"}    # live header chrome
        if self.page == "job":
            return {**st, "fields": [lang], "headings": ["PM"],
                    "controls": [ctl(0, "Sign In", after=False),
                                 ctl(1, "Apply", after=False, chrome=False)]
                    + self.extra_controls}
        if self.page == "chooser":
            return {**st, "url": url + "/apply", "fields": [lang],
                    "headings": ["Start Your Application"],
                    "controls": [ctl(0, "Sign In", after=False),
                                 ctl(1, "Autofill with Resume", after=False),
                                 ctl(2, "Apply Manually", after=False),
                                 ctl(3, "Use My Last Application", after=False)]}
        if self.page == "form":
            return {**st, "url": url + "/apply/applyManually",
                    "fields": [{"index": 0, "kind": "text",
                                "label": "First Name*"}],
                    "headings": ["My Information"],
                    "controls": [ctl(0, "Save and Continue", "submit")]}
        if self.page == "sso":
            return {**st, "fields": [lang], "headings": ["Sign In"],
                    "controls": [ctl(0, "Sign In", after=False),
                                 ctl(1, "Sign in with Google", "button", False),
                                 ctl(2, "Sign in with LinkedIn", "button", False),
                                 ctl(3, "Sign in with email", after=False)]}
        if self.page in ("empty", "loading"):
            return {**st, "fields": [], "headings": [], "controls": []}
        if self.page == "signedin":
            # signed-in chrome only: the header shows the account email
            return {**st, "body": "Search for Jobs  %s  Candidate Home" % FAKE_EMAIL,
                    "chrome": "Search for Jobs  %s" % FAKE_EMAIL,
                    "fields": [lang], "headings": ["PM"],
                    "controls": [ctl(0, "Apply", after=False, chrome=False)]}
        if self.page == "loginlink":
            # an ordinary page: an email field plus a header "Log in" link
            return {**st, "headings": ["Join our talent network"],
                    "fields": [{"index": 0, "kind": "text", "label": "Email"}],
                    "field_pos": {"0": 10},
                    "controls": [ctl(0, "Log in", after=False, pos=1),
                                 ctl(1, "Subscribe", "submit")]}
        email = {"index": 0, "kind": "text", "label": "Email Address*"}
        pw = {"index": 1, "kind": "text", "label": "Password*",
              "sensitive": True}
        pos = {"0": 10, "1": 11, "2": 12}
        if self.mode == "create_account":
            fields = [email, pw,
                      {"index": 2, "kind": "text", "sensitive": True,
                       "label": "Verify New Password*"}]
            for n, box in enumerate(self.boxes):
                idx = 3 + n
                fields.append({"index": idx, "kind": "checkbox", **box})
                pos[str(idx)] = box.get("pos", 13 + n)
            controls = [ctl(0, "Sign In", after=False, pos=1),
                        ctl(1, "Create Account"),            # click_filter
                        ctl(2, "Create Account", "submit"),
                        ctl(3, "Sign In"),                    # in-form toggle
                        ctl(4, "Forgot your password?")]
            heads = ["PM", "Create Account"]
        else:
            fields = [email, pw]
            controls = [ctl(0, "Sign In", after=False, pos=1),
                        ctl(1, "Sign In", "submit"),
                        ctl(2, "Create Account"),
                        ctl(3, "Forgot your password?")]
            heads = ["PM", "Sign In"]
        return {**st, "url": url + "/apply/applyManually", "fields": fields,
                "controls": controls, "headings": heads, "field_pos": pos}

    def click(self, c):
        label, typ, after = c["label"], c["type"], c["after_password"]
        self.clicks.append(label)
        if self.page == "job" and label == "Apply":
            if not self.apply_noop:
                self.page = "chooser"
            return
        if self.page == "chooser" and label == "Apply Manually":
            self.page = "auth"
            return
        if self.page == "sso" and label == "Sign in with email":
            self.page, self.mode = "auth", "sign_in"
            return
        if self.page != "auth":
            return
        if not after:
            return                                   # header nav: no-op
        if typ != "submit" and label in ("Sign In", "Create Account") \
                and not (label == "Create Account" and self.mode == "create_account"):
            self.mode = "sign_in" if label == "Sign In" else "create_account"
            self.notice = ""
            if self.toggle_navigates:
                self.host = "login.example-sso.com"
            return
        if self.mode == "create_account" and label == "Create Account":
            self.submits.append("create")
            if self.exists:
                self.notice = "An account with this email address already exists."
            elif self.verify:
                self.notice = ("We sent a verification email to %s. Verify your "
                               "account to continue." % FAKE_EMAIL
                               if self.echo_email else
                               "Please verify your account: check your email.")
            elif self.create_lands_signin:
                self.mode, self.exists = "sign_in", True
            else:
                self.page = "form"
            return
        if self.mode == "sign_in" and label == "Sign In":
            self.submits.append("sign_in")
            if not self.exists:
                self.notice = ("Wrong email address or password."
                               if self.signin_error == "combined"
                               else "No account found for this email.")
            elif not self.pw_ok:
                self.notice = ("Wrong email address or password."
                               if getattr(self, "combined_on_bad_pw", False)
                               else "Incorrect password. Please try again.")
            else:
                self.page = getattr(self, "after_sign_in", "form")


class SiteSession(browser.BrowserSession):
    def __init__(self, site, ledger_path=False, registry=None):
        super().__init__()
        self.site = site
        self._registry_path = False           # in-memory registry
        self._registry = {h: dict(r) for h, r in (registry or {}).items()}
        self.tabs[0] = {"page": FakePage(FakeFrame(),
                                         url="https://%s/x/job/1" % site.host),
                        "fields": {}, "company": "", "title": "", "url": ""}
        self.active_id = 0
        self.raise_on_click = False
        self._ledger_path = ledger_path       # False = in-memory only

    async def _page_state(self):
        return self.site.state()

    async def _settle(self, before_sig, timeout=None, until=None):
        st = self.site.state()
        self.page.url = st["url"]
        return st

    async def _click_control(self, c, timeout=3000):
        if self.raise_on_click:
            raise RuntimeError('locator.click failed while typing "%s"' % FAKE_PW)
        # the click_filter overlay (non-submit "Create Account" in create
        # mode) is what receives the click on real Workday; model the
        # intercepted type=submit button as failing.
        if (c["label"] == "Create Account" and c["type"] == "submit"):
            return False
        self.site.click(c)
        return True

    async def fill_sensitive(self, index, value, require_password=False,
                             mark=True):
        self.site.filled[index] = value
        return {"index": index, "status": "filled", "sensitive": True}

    async def fill_field(self, index, value):
        self.site.filled[index] = value
        return {"index": index, "status": "filled"}


def with_creds(fn, email=FAKE_EMAIL, password=FAKE_PW, err=None):
    real = config.credential

    def fake(service, field):
        if err:
            return None, {"status": err, "hint": "h"}
        return (email if field == "email" else password), None
    config.credential = fake
    try:
        return fn()
    finally:
        config.credential = real


ACTIVE = {"acme.wd5.myworkdayjobs.com": {
    "email": FAKE_EMAIL, "status": "active", "created_at": None,
    "last_sign_in_at": None, "source": "test"}}


def run_auth(site, mode="auto", registry=None, **kw):
    s = SiteSession(site, registry=registry)
    return with_creds(lambda: asyncio.run(s.authenticate(mode)), **kw), s


def no_secret(res):
    r = repr(res)
    return FAKE_PW not in r and FAKE_EMAIL not in r


# --------------------------------------------------------------------------
def test_start_application_walks_job_chooser_auth():
    site = FakeSite(page="job")
    s = SiteSession(site)
    res = asyncio.run(s.start_application())
    assert res["status"] == "auth_required", res
    assert site.clicks == ["Apply", "Apply Manually"], site.clicks
    auth = res["auth"]
    assert auth["mode"] == "create_account", auth
    assert (auth["email_index"], auth["password_index"],
            auth["confirm_index"], auth["consent_indexes"]) == (0, 1, 2, [3]), auth
    assert auth["submit_label"] == "Create Account", auth
    assert set(auth["available"]) == {"sign_in", "create_account"}, auth


def test_start_application_reveals_email_sign_in_never_sso():
    # Snap's tenant: Google / LinkedIn / email chooser at step 1.
    site = FakeSite(page="sso")
    res = asyncio.run(SiteSession(site).start_application())
    assert res["status"] == "auth_required", res
    assert site.clicks == ["Sign in with email"], site.clicks
    assert res["auth"]["mode"] == "sign_in", res["auth"]


def test_language_picker_is_not_a_form():
    # live Salesforce/Snap job pages expose this header listbox button
    lang = [{"index": 0, "kind": "combobox", "label": "languageSelectorButton"}]
    assert browser.form_fields(lang) == []
    assert browser.form_fields(lang + [{"index": 1, "label": "First Name"}])


def test_start_application_is_idempotent_on_form_and_auth():
    for page in ("form", "auth"):
        site = FakeSite(page=page)
        res = asyncio.run(SiteSession(site).start_application())
        assert res["status"] == ("form" if page == "form" else "auth_required")
        assert site.clicks == [], site.clicks


def test_start_application_no_entry():
    site = FakeSite(page="empty")
    res = asyncio.run(SiteSession(site).start_application())
    assert res["status"] == "no_entry" and site.clicks == [], res


def test_sign_in_success():
    res, s = run_auth(FakeSite(mode="sign_in", exists=True), registry=ACTIVE)
    assert res["status"] == "authenticated", res
    assert s.site.submits == ["sign_in"], s.site.submits
    assert s.site.filled == {0: FAKE_EMAIL, 1: FAKE_PW}, "email/password slots"
    assert no_secret(res), res


def test_create_account_success_ticks_consent_and_confirm():
    res, s = run_auth(FakeSite(mode="create_account"))
    assert res["status"] == "authenticated", res
    assert s.site.submits == ["create"]
    assert s.site.filled == {0: FAKE_EMAIL, 1: FAKE_PW, 2: FAKE_PW,
                             3: "true"}, s.site.filled


def test_no_account_switches_to_create_once():
    # registry says active but the tenant has no such user: create once
    res, s = run_auth(FakeSite(mode="sign_in", exists=False,
                               signin_error="no_account"), registry=ACTIVE)
    assert res["status"] == "authenticated", res
    assert s.site.submits == ["sign_in", "create"], s.site.submits


def test_no_account_in_explicit_sign_in_mode_stops():
    res, s = run_auth(FakeSite(mode="sign_in", exists=False,
                               signin_error="no_account"), mode="sign_in")
    assert res["status"] == "no_account" and s.site.submits == ["sign_in"], res


def test_account_exists_parks_without_a_sign_in():
    # the user's rule: an existing account whose password we never
    # confirmed is parked for a manual sign-in, never tried
    res, s = run_auth(FakeSite(mode="create_account", exists=True))
    assert res["status"] == "account_exists", res
    assert s.site.submits == ["create"], s.site.submits
    row = s._registry["acme.wd5.myworkdayjobs.com"]
    assert row["status"] == "exists_unknown_password", row
    assert row["email"] == FAKE_EMAIL and FAKE_PW not in repr(row), row


def test_bad_password_stops_after_one_submit():
    res, s = run_auth(FakeSite(mode="sign_in", exists=True, pw_ok=False),
                      registry=ACTIVE)
    assert res["status"] == "bad_credentials", res
    assert s.site.submits == ["sign_in"], s.site.submits
    assert "Incorrect password" in res["message_excerpt"], res


def test_existing_account_is_never_signed_into_after_create():
    # even with a wrong password on file: one create submit, then park
    res, s = run_auth(FakeSite(mode="create_account", exists=True, pw_ok=False))
    assert res["status"] == "account_exists", res
    assert s.site.submits == ["create"], s.site.submits


def test_wrong_email_or_password_on_a_known_account_is_bad_credentials():
    # the registry knows the account exists, so "Wrong email address or
    # password" means the password: no create attempt
    site = FakeSite(mode="sign_in", exists=True, pw_ok=False)
    site.combined_on_bad_pw = True
    res, s = run_auth(site, registry=ACTIVE)
    assert res["status"] == "bad_credentials", res
    assert s.site.submits == ["sign_in"], s.site.submits


def test_email_verification_required_and_excerpt_redacted():
    res, s = run_auth(FakeSite(mode="create_account", verify=True,
                               echo_email=True))
    assert res["status"] == "email_verification_required", res
    assert "verification email" in res["message_excerpt"], res
    assert no_secret(res), res
    assert len(res["message_excerpt"]) <= 200


def test_no_credentials_touches_nothing():
    site = FakeSite(mode="sign_in", exists=True)
    res, s = run_auth(site, err="no_credentials")
    assert res["status"] == "no_credentials", res
    assert site.clicks == [] and site.reads == 0 and site.filled == {}, site.clicks
    res, s = run_auth(FakeSite(), err="bad_credentials_file")
    assert res["status"] == "bad_credentials_file", res


def test_exception_reduced_to_type_name():
    site = FakeSite(mode="sign_in", exists=True)
    s = SiteSession(site)
    s.raise_on_click = True
    res = with_creds(lambda: asyncio.run(s.authenticate()))
    assert res["status"] == "error" and res["error"] == "RuntimeError", res
    assert no_secret(res), res


def test_bad_mode_rejected():
    res, s = run_auth(FakeSite(), mode="guess")
    assert res["status"] == "bad_mode" and s.site.clicks == [], res


def test_authenticate_from_job_page_walks_entry_first():
    res, s = run_auth(FakeSite(page="job", mode="create_account"))
    assert res["status"] == "authenticated", res
    assert s.site.clicks[:2] == ["Apply", "Apply Manually"], s.site.clicks
    assert "Autofill with Resume" not in s.site.clicks
    assert "Use My Last Application" not in s.site.clicks


def test_pure_outcome_classifier_ignores_create_page_boilerplate():
    st = {"fields": [{"index": 1, "kind": "text", "sensitive": True,
                      "label": "Verify New Password*"}],
          "notices": [], "body": "Create Account Verify New Password* By "
          "activating your account in Workday, you acknowledge"}
    assert browser._classify_auth_outcome(st) == "still_auth"


# --------------------------------------------------------------------------
# 3b. review findings: host gate, toggles, consent, ledger, bounded clicks
# --------------------------------------------------------------------------
def test_non_workday_host_never_gets_credentials():
    site = FakeSite(mode="sign_in", exists=True, host="acme.icims.com")
    res, s = run_auth(site)
    assert res["status"] == "unsupported_auth_flow", res
    assert site.clicks == [] and site.filled == {}, (site.clicks, site.filled)


def test_host_change_before_fill_or_submit_stops():
    # the page navigates off Workday between classification and the fills
    site = FakeSite(mode="sign_in", exists=True)
    s = SiteSession(site)
    real_layout = s._layout

    def nav_then_layout(state):
        s.page.url = "https://login.example-sso.com/x"
        return real_layout(state)
    s._layout = nav_then_layout
    res = with_creds(lambda: asyncio.run(s.authenticate()))
    assert res["status"] == "unsupported_auth_flow", res
    assert site.filled == {} and site.submits == [], site.filled


def test_toggle_that_navigates_away_aborts():
    # no registry entry -> create; the page is in sign-in mode, and its
    # Create Account toggle navigates off the tenant
    site = FakeSite(mode="sign_in")
    site.toggle_navigates = True
    res, s = run_auth(site)
    assert res["status"] == "toggle_failed", res
    assert site.submits == [], site.submits
    assert site.filled == {}, "nothing typed after the toggle"


def test_header_log_in_plus_email_is_not_an_auth_page():
    site = FakeSite(page="loginlink")
    st = site.state()
    assert not browser._is_auth_page(st["fields"], st["controls"])
    res = asyncio.run(SiteSession(site).start_application())
    assert res["status"] == "form" and site.clicks == [], (res, site.clicks)
    # and even a forced authenticate never toggles via the nav link
    res, s = run_auth(FakeSite(page="loginlink"), mode="create_account")
    assert "Log in" not in s.site.clicks, s.site.clicks


def test_never_toggle_from_unknown_mode():
    # an email + password page with no recognisable heading / submit label
    st = {"fields": [{"index": 0, "kind": "text", "label": "Email"},
                     {"index": 1, "kind": "text", "label": "Password",
                      "sensitive": True}],
          "controls": [ctl(0, "Log in", after=False, pos=1),
                       ctl(1, "Continue", "submit")], "headings": []}
    lay = browser._auth_layout(st["fields"], st["controls"], st["headings"])
    assert lay["mode"] == "unknown", lay
    assert browser._pick_auth_controls(st["controls"], "sign_in", False,
                                       current="unknown") == []


def test_toggle_excludes_the_current_submit_label():
    controls = [ctl(0, "Create Account"), ctl(1, "Create Account", "submit"),
                ctl(2, "Sign In")]
    # toggling TO create from sign-in must never pick the overlay whose label
    # equals the current (sign-in) submit; here: toggling to sign_in from
    # create picks only the in-form "Sign In", never a Create Account.
    t = browser._pick_auth_controls(controls, "sign_in", False,
                                    current="create_account")
    assert [c["i"] for c in t] == [2], t
    controls2 = [ctl(0, "Sign In"), ctl(1, "Sign In", "submit")]
    t2 = browser._pick_auth_controls(controls2, "sign_in", False,
                                     current="create_account")
    assert t2 == [], "a non-submit with the submit's own label is not a toggle"


def test_consent_skips_marketing_and_out_of_span_boxes():
    boxes = [{"label": "I agree to the Terms of Use*", "required": True},
             {"label": "I agree to receive marketing emails and job alerts"},
             {"label": "Join our Talent Community (privacy notice applies)"},
             {"label": "I acknowledge the privacy policy*", "pos": 50}]
    res, s = run_auth(FakeSite(mode="create_account", boxes=boxes))
    assert res["status"] == "authenticated", res
    ticked = {i for i, v in s.site.filled.items() if v == "true"}
    assert ticked == {3}, ticked


def test_single_unmarked_privacy_box_is_ticked():
    # live Salesforce/Zillow: the mandatory acknowledgement has no required
    # marker; a lone non-marketing consent box inside the form is ticked.
    res, s = run_auth(FakeSite(mode="create_account"))
    assert s.site.filled.get(3) == "true", s.site.filled
    two = [{"label": "I agree to the terms"}, {"label": "I accept the privacy notice"}]
    res, s = run_auth(FakeSite(mode="create_account", boxes=two))
    assert "true" not in s.site.filled.values(), "ambiguous: tick none"


def test_ledger_persists_bad_credentials_across_sessions():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "auth_attempts.json"
        site = FakeSite(mode="sign_in", exists=True, pw_ok=False)
        s = SiteSession(site, ledger_path=path, registry=ACTIVE)
        res = with_creds(lambda: asyncio.run(s.authenticate()))
        assert res["status"] == "bad_credentials" and site.submits == ["sign_in"]
        blob = path.read_text(encoding="utf-8")
        assert FAKE_PW not in blob and FAKE_EMAIL not in blob, blob
        # a NEW session (new process) on the same tenant: short-circuit
        site2 = FakeSite(mode="sign_in", exists=True, pw_ok=True)
        s2 = SiteSession(site2, ledger_path=path, registry=ACTIVE)
        res = with_creds(lambda: asyncio.run(s2.authenticate()))
        assert res["status"] == "bad_credentials", res
        assert site2.submits == [] and site2.reads == 0 and site2.filled == {}
        # the user fixed the password and asked: clear, then it signs in
        assert s2.clear_auth_ledger("acme.wd5.myworkdayjobs.com")["tenants"]
        s3 = SiteSession(FakeSite(mode="sign_in", exists=True), ledger_path=path,
                         registry=ACTIVE)
        res = with_creds(lambda: asyncio.run(s3.authenticate()))
        assert res["status"] == "authenticated", res


def test_recent_submit_reclassifies_without_submitting():
    site = FakeSite(mode="create_account", verify=True)
    s = SiteSession(site)
    res = with_creds(lambda: asyncio.run(s.authenticate()))
    assert res["status"] == "email_verification_required" and site.submits == ["create"]
    res = with_creds(lambda: asyncio.run(s.authenticate()))
    assert res["status"] == "email_verification_required", res
    assert site.submits == ["create"], "no second submit within 60s"
    assert "60s" in res.get("note", ""), res


def test_one_failed_sign_in_per_day():
    site = FakeSite(mode="sign_in", exists=True, pw_ok=False)
    site.combined_on_bad_pw = True
    s = SiteSession(site)
    # pretend the first failure was > 60s ago so only the 24h rule applies
    s._ledger = {"acme.wd5.myworkdayjobs.com": [
        {"t": __import__("time").time() - 600, "kind": "sign_in",
         "outcome": "combined"}]}
    res = with_creds(lambda: asyncio.run(s.authenticate(mode="sign_in")))
    assert res["status"] == "bad_credentials" and site.submits == [], res


def test_already_in_application_refuses_entry_click():
    for setup in ("filled", "step", "back"):
        site = FakeSite(page="job")
        s = SiteSession(site)
        if setup == "filled":
            s.tabs[0]["filled"] = True
        elif setup == "step":
            site.body = "current step 6 of 6 Review"
        else:
            site.extra_controls = [ctl(5, "Back", chrome=False)]
        res = asyncio.run(s.start_application())
        assert res["status"] == "already_in_application", (setup, res)
        assert site.clicks == [], (setup, site.clicks)


def test_start_application_stops_when_a_click_changes_nothing():
    site = FakeSite(page="job")
    site.apply_noop = True
    res = asyncio.run(SiteSession(site).start_application())
    assert site.clicks == ["Apply"], site.clicks
    assert res["status"] == "no_entry", res


def test_click_first_stops_when_a_raising_attempt_changed_the_page():
    site = FakeSite(page="job")
    s = SiteSession(site)
    calls = []

    async def flaky(c, timeout=3000):
        calls.append(c["label"])
        site.page = "chooser"                 # the click landed...
        raise RuntimeError("...but Playwright timed out")
    s._click_control = flaky
    a, b = ctl(0, "Apply"), ctl(1, "Apply")
    got = asyncio.run(s._click_first([a, b], s._state_sig(site.state())))
    assert got is a and calls == ["Apply"], calls


def test_search_box_is_page_chrome():
    assert browser.form_fields([{"index": 0, "kind": "text",
                                 "label": "Search for Jobs"}]) == []
    assert browser.form_fields([{"index": 0, "kind": "text",
                                 "label": "Research interests"}])


def test_no_control_characters_in_source():
    root = Path(__file__).resolve().parent.parent
    bad = []
    for base in ("src", "tests"):
        for p in (root / base).rglob("*"):
            if p.suffix not in (".py", ".html", ".js", ".yaml", ".md", ".json"):
                continue
            data = p.read_bytes()
            hits = [i for i, b in enumerate(data) if b < 0x20 and b not in (9, 10, 13)]
            if hits:
                bad.append((str(p.relative_to(root)), hits[:3]))
    assert not bad, bad


# --- snapshot_job walk -------------------------------------------------------
def _load_mcp_server():
    try:
        import mcp.server.mcpserver  # noqa: F401
    except Exception:
        _m = types.ModuleType("mcp")
        _ms = types.ModuleType("mcp.server")
        _mm = types.ModuleType("mcp.server.mcpserver")

        class _FakeMCP:
            def __init__(self, *a, **k):
                pass

            def tool(self, *a, **k):
                return lambda fn: fn
        _mm.MCPServer = _FakeMCP
        sys.modules["mcp"], sys.modules["mcp.server"] = _m, _ms
        sys.modules["mcp.server.mcpserver"] = _mm
    from src import mcp_server
    return mcp_server


class SnapFake:
    def __init__(self, url, entry_status="auth_required", auth_status="authenticated"):
        self.page = FakePage(FakeFrame(), url=url)
        self.entry_status, self.auth_status = entry_status, auth_status
        self.calls = []

    async def open_job(self, url, new_tab=False, company=""):
        self.calls.append("open_job")
        return {"intervention": {}, "title": "PM", "detected_ats": "workday",
                "fields": [{"index": 0, "kind": "combobox",
                            "label": "languageSelectorButton"}]}

    async def get_job_text(self):
        self.calls.append("get_job_text")
        return "the JD"

    async def has_entry_control(self):
        return True

    async def start_application(self):
        self.calls.append("start_application")
        return {"status": self.entry_status, "url": self.page.url,
                "fields": [{"index": 0, "kind": "text", "label": "First Name"}]}

    async def authenticate(self, mode="auto"):
        self.calls.append("authenticate")
        return {"status": self.auth_status, "url": self.page.url,
                "message_excerpt": "Wrong email address or password."}

    async def read_form(self, values_only=False):
        return [{"index": 0, "kind": "text", "label": "First Name"}]


def _snapshot(fake, url):
    import tempfile
    ms = _load_mcp_server()
    real = browser.session
    browser.session = fake
    try:
        with tempfile.TemporaryDirectory() as d:
            return asyncio.run(ms.snapshot_job(url, company="Acme", out_dir=d))
    finally:
        browser.session = real


def test_snapshot_walks_workday_and_authenticates():
    url = "https://acme.wd5.myworkdayjobs.com/x/job/1"
    fake = SnapFake(url)
    res = _snapshot(fake, url)
    assert res["status"] == "snapshotted", res
    assert res["entry_status"] == "auth_required"
    assert res["auth_status"] == "authenticated"
    # the JD is read BEFORE leaving the description page
    assert fake.calls.index("get_job_text") < fake.calls.index("start_application")


def test_snapshot_parks_on_failed_auth_with_reason():
    url = "https://acme.wd5.myworkdayjobs.com/x/job/1"
    res = _snapshot(SnapFake(url, auth_status="bad_credentials"), url)
    assert res["status"] == "parked" and res["park_reason"].startswith(
        "bad_credentials: Wrong email"), res


def test_snapshot_never_authenticates_on_non_workday_host():
    url = "https://careers.acme.com/job/1"
    fake = SnapFake("https://acme.icims.com/jobs/1/login")
    res = _snapshot(fake, url)
    assert "authenticate" not in fake.calls, fake.calls
    assert res["status"] == "parked" and "non-Workday" in res["park_reason"], res
    assert res["entry_status"] == "auth_required" and res["auth_status"] is None


# --------------------------------------------------------------------------
# 4. open_verification_link allowlist
# --------------------------------------------------------------------------
def test_verification_link_allowlist():
    ok = ["https://acme.wd5.myworkdayjobs.com/en-US/x/activate/abc",
          "https://wd3.myworkday.com/acme/d/verify",
          "https://acme.workday.com/verify?token=1",
          "https://myworkdayjobs.com/x"]
    bad = ["http://acme.wd5.myworkdayjobs.com/activate",
           "https://evil.example.com/myworkdayjobs.com",
           "https://myworkdayjobs.com@evil.example.com/x",
           "javascript:alert(1)", "", "https://bit.ly/abc",
           # the review's verified bypasses of the old check:
           "https://workday.evil.com/x",
           "https://evilmyworkdayjobs.com/x",
           "https://notworkdayatall.example/",
           "https://myworkdayjobs.com@workday-login.ru/",
           "https://acme.wd5.myworkdayjоbs.com/x",      # Cyrillic o
           "https://acme.wd5.myworkdayjobs.com:8443/x",
           "https://user:pw@acme.wd5.myworkdayjobs.com/x"]
    for u in ok:
        assert browser.verification_link_allowed(u), u
    for u in bad:
        assert not browser.verification_link_allowed(u), u


def test_verification_link_must_match_the_tab_workday_domain():
    tab = "acme.wd5.myworkdayjobs.com"
    assert browser.verification_link_allowed(
        "https://other.wd1.myworkdayjobs.com/v", tab)
    assert not browser.verification_link_allowed("https://x.myworkday.com/v", tab)
    assert browser.verification_link_allowed("https://x.myworkday.com/v", "")


def test_workday_host_is_dot_boundary():
    for h in ("acme.wd5.myworkdayjobs.com", "myworkday.com", "wd3.myworkday.com",
              "acme.workday.com"):
        assert browser.is_workday_host(h), h
    for h in ("evilmyworkdayjobs.com", "workday.evil.com", "myworkdayjobs.com.evil.io",
              "icims.com", "", "acme.wd5.myworkdayjоbs.com"):
        assert not browser.is_workday_host(h), h


def test_open_verification_link_refuses_without_opening():
    s = browser.BrowserSession()       # no context at all: must not be touched
    res = asyncio.run(s.open_verification_link("https://evil.example.com/v"))
    assert res["status"] == "not_allowed", res


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
