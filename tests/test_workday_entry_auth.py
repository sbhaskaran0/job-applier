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
    "Save and Continue": "advance", "Review": "other",
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
    for label, cls in TABLE.items():
        s, _ = make_session([FakeButton(label, "submit")])
        loc = asyncio.run(s._find_submit_button())
        got = None if loc is None else asyncio.run(s._label_of(loc))
        if cls == "submit":
            assert got == label.lower(), (label, got)
        else:
            assert got is None, (label, got)


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
def ctl(i, label, type_="", after=True):
    return {"i": i, "label": label, "type": type_, "after_password": after,
            "cls": browser.classify_control(label), "frame": object()}


class FakeSite:
    def __init__(self, page="auth", mode="create_account", exists=False,
                 pw_ok=True, verify=False, signin_error="combined",
                 echo_email=False):
        self.page, self.mode, self.exists, self.pw_ok = page, mode, exists, pw_ok
        self.verify, self.signin_error = verify, signin_error
        self.echo_email = echo_email
        self.notice = ""
        self.submits, self.clicks, self.filled = [], [], {}
        self.reads = 0

    def state(self):
        self.reads += 1
        url = "https://acme.wd5.myworkdayjobs.com/x/job/1"
        st = {"url": url, "blocked": False, "signals": [], "body": "",
              "notices": [self.notice] if self.notice else []}
        lang = {"index": 9, "kind": "combobox", "widget": "listbox",
                "label": "languageSelectorButton"}    # live header chrome
        if self.page == "job":
            return {**st, "fields": [lang], "headings": ["PM"],
                    "controls": [ctl(0, "Sign In", after=False),
                                 ctl(1, "Apply", after=False)]}
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
        if self.page == "empty":
            return {**st, "fields": [], "headings": [], "controls": []}
        email = {"index": 0, "kind": "text", "label": "Email Address*"}
        pw = {"index": 1, "kind": "text", "label": "Password*",
              "sensitive": True}
        if self.mode == "create_account":
            fields = [email, pw,
                      {"index": 2, "kind": "text", "sensitive": True,
                       "label": "Verify New Password*"},
                      {"index": 3, "kind": "checkbox",
                       "label": "I acknowledge and agree to the Privacy Notice"}]
            controls = [ctl(0, "Sign In", after=False),
                        ctl(1, "Create Account"),            # click_filter
                        ctl(2, "Create Account", "submit"),
                        ctl(3, "Sign In"),                    # in-form toggle
                        ctl(4, "Forgot your password?")]
            heads = ["PM", "Create Account"]
        else:
            fields = [email, pw]
            controls = [ctl(0, "Sign In", after=False),
                        ctl(1, "Sign In", "submit"),
                        ctl(2, "Create Account"),
                        ctl(3, "Forgot your password?")]
            heads = ["PM", "Sign In"]
        return {**st, "url": url + "/apply/applyManually", "fields": fields,
                "controls": controls, "headings": heads}

    def click(self, c):
        label, typ, after = c["label"], c["type"], c["after_password"]
        self.clicks.append(label)
        if self.page == "job" and label == "Apply":
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
                self.page = "form"


class SiteSession(browser.BrowserSession):
    def __init__(self, site):
        super().__init__()
        self.site = site
        self.tabs[0] = {"page": FakePage(FakeFrame()), "fields": {},
                        "company": "", "title": "", "url": ""}
        self.active_id = 0
        self.raise_on_click = False

    async def _page_state(self):
        return self.site.state()

    async def _settle(self, before_sig, timeout=None):
        return self.site.state()

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

    async def fill_sensitive(self, index, value, require_password=False):
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


def run_auth(site, mode="auto", **kw):
    s = SiteSession(site)
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
    res, s = run_auth(FakeSite(mode="sign_in", exists=True))
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
    res, s = run_auth(FakeSite(mode="sign_in", exists=False,
                               signin_error="no_account"))
    assert res["status"] == "authenticated", res
    assert s.site.submits == ["sign_in", "create"], s.site.submits


def test_no_account_in_explicit_sign_in_mode_stops():
    res, s = run_auth(FakeSite(mode="sign_in", exists=False,
                               signin_error="no_account"), mode="sign_in")
    assert res["status"] == "no_account" and s.site.submits == ["sign_in"], res


def test_account_exists_switches_to_sign_in():
    res, s = run_auth(FakeSite(mode="create_account", exists=True))
    assert res["status"] == "authenticated", res
    assert s.site.submits == ["create", "sign_in"], s.site.submits


def test_bad_password_stops_after_one_submit():
    res, s = run_auth(FakeSite(mode="sign_in", exists=True, pw_ok=False))
    assert res["status"] == "bad_credentials", res
    assert s.site.submits == ["sign_in"], s.site.submits
    assert "Incorrect password" in res["message_excerpt"], res


def test_bad_password_after_exists_never_exceeds_two_submits():
    res, s = run_auth(FakeSite(mode="create_account", exists=True, pw_ok=False))
    assert res["status"] == "bad_credentials", res
    assert s.site.submits == ["create", "sign_in"], s.site.submits


def test_ambiguous_signin_error_gets_one_create_attempt():
    # "Wrong email address or password" and no account: create succeeds.
    res, s = run_auth(FakeSite(mode="sign_in", exists=False,
                               signin_error="combined"))
    assert res["status"] == "authenticated", res
    assert s.site.submits == ["sign_in", "create"], s.site.submits
    # Same message but the account exists (so the password was wrong):
    # create says "already exists" -> bad_credentials, 2 submits, no 3rd.
    site = FakeSite(mode="sign_in", exists=True, pw_ok=False)
    site.combined_on_bad_pw = True
    res, s = run_auth(site)
    assert res["status"] == "bad_credentials", res
    assert s.site.submits == ["sign_in", "create"], s.site.submits


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
# 4. open_verification_link allowlist
# --------------------------------------------------------------------------
def test_verification_link_allowlist():
    ok = ["https://acme.wd5.myworkdayjobs.com/en-US/x/activate/abc",
          "https://wd3.myworkday.com/acme/d/verify",
          "https://acme.workday.com/verify?token=1"]
    bad = ["http://acme.wd5.myworkdayjobs.com/activate",
           "https://evil.example.com/myworkdayjobs.com",
           "https://myworkdayjobs.com@evil.example.com/x",
           "javascript:alert(1)", "", "https://bit.ly/abc"]
    for u in ok:
        assert browser.verification_link_allowed(u), u
    for u in bad:
        assert not browser.verification_link_allowed(u), u


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
