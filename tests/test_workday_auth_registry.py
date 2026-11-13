"""Workday sign-in outcome classification + the tenant-account registry.

Live failure (Capital One, 2026-11-13), pinned here:
  * authenticate(auto) on Create Account returned "unconfirmed": the account
    HAD been created, and the site redirected to a plain Sign In page with
    no message;
  * authenticate(sign_in) then returned "unconfirmed" while the page was
    still redirecting to the form (start_application a moment later saw the
    form, header showing the signed-in email);
  * both were recorded as outcome "unknown", and the lockout rule then
    counted the sign-in as FAILED, refusing every new session for 24h.

So: wait for the form / signed-in chrome after a submit; a create that lands
on a sign-in page is followed by the sign-in in the same call; only a
positive wrong-password signal counts against the lockout budget.

The registry (profiles/<id>/data/workday_accounts.json) decides the mode in
auto: active -> sign in; no entry -> create; create says "exists" -> park
(exists_unknown_password), no sign-in; exists_unknown_password / sso_only ->
touch nothing. It never holds a password.

Reuses the fake site from test_workday_entry_auth. Standalone --
`python tests/test_workday_auth_registry.py`.
"""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import test_workday_entry_auth as T      # noqa: E402  (stubs playwright)
from src import accounts, browser, config, profiles   # noqa: E402

HOST = "acme.wd5.myworkdayjobs.com"


class PollingSession(T.SiteSession):
    """The REAL _settle (polling until the predicate holds); the fake page's
    waits are instant."""
    _settle = browser.BrowserSession._settle


def auth(site, mode="auto", registry=None, cls=PollingSession, **kw):
    s = cls(site, registry=registry, **kw)
    return T.with_creds(lambda: asyncio.run(s.authenticate(mode))), s


# --------------------------------------------------------------------------
# A1: wait for, and recognise, the signed-in state
# --------------------------------------------------------------------------
def test_sign_in_waits_out_the_redirect_to_the_form():
    site = T.FakeSite(mode="sign_in", exists=True)
    site.after_sign_in, site.loading_reads = "loading", 3
    res, s = auth(site, registry=T.ACTIVE)
    assert res["status"] == "authenticated", res
    assert site.submits == ["sign_in"], site.submits
    led = s._ledger_entries(HOST)
    assert led[-1]["outcome"] == "authenticated", led


def test_signed_in_chrome_counts_as_authenticated():
    # no form fields yet, but the header shows the account's email
    site = T.FakeSite(mode="sign_in", exists=True)
    site.after_sign_in = "signedin"
    res, s = auth(site, registry=T.ACTIVE)
    assert res["status"] == "authenticated", res
    assert T.no_secret(res), res


def test_pure_classifier_signed_in_and_loading():
    loading = {"fields": [], "notices": [], "body": "", "controls": [],
               "headings": []}
    assert browser._classify_auth_outcome(loading) == "unknown"
    assert not browser._auth_settled(loading, [], "sign_in", T.FAKE_EMAIL)
    hdr = dict(loading, body="Candidate Home  %s" % T.FAKE_EMAIL)
    assert browser._classify_auth_outcome(hdr, "sign_in", T.FAKE_EMAIL) == \
        "authenticated"
    assert browser._auth_settled(hdr, [], "sign_in", T.FAKE_EMAIL)


# --------------------------------------------------------------------------
# A2: create -> plain Sign In page -> sign in, same call
# --------------------------------------------------------------------------
def test_create_that_lands_on_sign_in_is_followed_by_the_sign_in():
    site = T.FakeSite(mode="create_account")
    site.create_lands_signin = True
    res, s = auth(site)
    assert res["status"] == "authenticated", res
    assert site.submits == ["create", "sign_in"], site.submits
    assert res.get("account_created") is True, res
    row = s._registry[HOST]
    assert row["status"] == "active" and row["created_at"] \
        and row["last_sign_in_at"], row
    outcomes = [e["outcome"] for e in s._ledger_entries(HOST)]
    assert outcomes == ["created_signin_required", "authenticated"], outcomes


def test_pure_classifier_create_landing_on_sign_in():
    site = T.FakeSite(mode="sign_in")
    st = site.state()
    assert browser._classify_auth_outcome(st, "create_account") == \
        "created_signin_required"
    assert browser._classify_auth_outcome(st, "sign_in") == "still_auth"
    assert browser._auth_settled(st, [], "create_account")
    assert not browser._auth_settled(st, [], "sign_in")


def test_created_but_no_submit_left_says_so():
    # registry active, sign-in says no such user -> create (2nd submit)
    # lands on Sign In: the account exists, but the budget is spent
    site = T.FakeSite(mode="sign_in", exists=False, signin_error="no_account")
    site.create_lands_signin = True
    res, s = auth(site, registry=T.ACTIVE)
    assert res["status"] == "account_created_signin_required", res
    assert site.submits == ["sign_in", "create"], site.submits
    assert s._registry[HOST]["status"] == "active"


# --------------------------------------------------------------------------
# A3: only a positive bad-credentials signal counts toward lockout
# --------------------------------------------------------------------------
def _seeded(outcome, ago):
    return {HOST: [{"t": time.time() - ago, "kind": "sign_in",
                    "outcome": outcome}]}


def test_unknown_sign_in_does_not_lock_the_tenant():
    for outcome in ("unknown", "still_auth", "no_account"):
        site = T.FakeSite(mode="sign_in", exists=True)
        s = PollingSession(site, registry=T.ACTIVE)
        s._ledger = _seeded(outcome, 600)
        assert not s._ledger_failed_sign_in(HOST), outcome
        res = T.with_creds(lambda: asyncio.run(s.authenticate("sign_in")))
        assert res["status"] == "authenticated", (outcome, res)
        assert site.submits == ["sign_in"], (outcome, site.submits)


def test_positive_bad_password_still_locks():
    for outcome in ("bad_password", "combined"):
        site = T.FakeSite(mode="sign_in", exists=True)
        s = PollingSession(site, registry=T.ACTIVE)
        s._ledger = _seeded(outcome, 600)
        res = T.with_creds(lambda: asyncio.run(s.authenticate("sign_in")))
        assert res["status"] == "bad_credentials" and site.submits == [], res


def test_sixty_second_guard_still_applies_to_unknown():
    site = T.FakeSite(mode="sign_in", exists=True)
    s = PollingSession(site, registry=T.ACTIVE)
    s._ledger = _seeded("unknown", 10)
    res = T.with_creds(lambda: asyncio.run(s.authenticate("sign_in")))
    assert res["status"] == "recently_submitted" and site.submits == [], res


# --------------------------------------------------------------------------
# B: the account registry drives auto mode
# --------------------------------------------------------------------------
def test_registry_active_signs_in():
    site = T.FakeSite(mode="create_account", exists=True)  # page shows create
    res, s = auth(site, registry=T.ACTIVE)
    assert res["status"] == "authenticated", res
    assert site.submits == ["sign_in"], "toggled to sign-in, never created"
    row = s._registry[HOST]
    assert row["last_sign_in_at"] and row["created_at"] is None, row


def test_no_registry_entry_creates_even_on_a_sign_in_page():
    site = T.FakeSite(mode="sign_in")
    res, s = auth(site)
    assert res["status"] == "authenticated", res
    assert site.submits == ["create"], site.submits
    row = s._registry[HOST]
    assert row["status"] == "active" and row["created_at"], row
    assert res.get("account_created") is True


def test_exists_unknown_password_and_sso_only_touch_nothing():
    for status, want in (("exists_unknown_password", "account_exists"),
                         ("sso_only", "unsupported_auth_flow")):
        reg = {HOST: dict(T.ACTIVE[HOST], status=status)}
        site = T.FakeSite(mode="sign_in", exists=True)
        res, s = auth(site, registry=reg)
        assert res["status"] == want and res["registry_status"] == status, res
        assert site.submits == [] and site.filled == {}, (site.submits,
                                                          site.filled)


def test_explicit_mode_still_overrides_the_registry():
    reg = {HOST: dict(T.ACTIVE[HOST], status="exists_unknown_password")}
    site = T.FakeSite(mode="sign_in", exists=True)
    res, s = auth(site, mode="sign_in", registry=reg)
    assert res["status"] == "authenticated", res
    assert s._registry[HOST]["status"] == "active", "confirmed success upgrades"


def test_registry_file_in_a_temp_profile_never_holds_a_password():
    real = profiles._active
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "tmpprofile"
        profiles._active = profiles.ActiveProfile(profile_id="tmp", root=root)
        try:
            path = Path(config.WORKDAY_ACCOUNTS_PATH)
            assert path == root / "data" / "workday_accounts.json", path
            site = T.FakeSite(mode="create_account")
            s = PollingSession(site)
            s._registry_path, s._registry = None, None   # the real store
            res = T.with_creds(lambda: asyncio.run(s.authenticate()))
            assert res["status"] == "authenticated", res
            blob = path.read_text(encoding="utf-8")
            assert T.FAKE_PW not in blob and "password\"" not in blob, blob
            row = json.loads(blob)[HOST]
            assert set(row) == set(accounts.FIELDS) and row["status"] == "active"
            # a new session (new process) reads it and signs in
            site2 = T.FakeSite(mode="create_account", exists=True)
            s2 = PollingSession(site2)
            s2._registry_path, s2._registry = None, None
            res = T.with_creds(lambda: asyncio.run(s2.authenticate()))
            assert res["status"] == "authenticated" and site2.submits == ["sign_in"]
            # the read-only tool
            ms = T._load_mcp_server()
            saved = browser.session
            browser.session = s2
            try:
                rows = ms.list_workday_accounts()
            finally:
                browser.session = saved
            assert rows and rows[0]["tenant"] == HOST, rows
            assert T.FAKE_PW not in repr(rows)
        finally:
            profiles._active = real


def test_load_drops_unknown_keys_and_bad_statuses():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "w.json"
        p.write_text(json.dumps({
            "A.wd1.myworkdayjobs.com": {"email": "e", "status": "active",
                                        "password": "hunter2"},
            "b.wd1.myworkdayjobs.com": {"email": "e", "status": "weird"}}),
            encoding="utf-8")
        reg = accounts.load(p)
        assert list(reg) == ["a.wd1.myworkdayjobs.com"], reg
        assert "password" not in reg["a.wd1.myworkdayjobs.com"]
        accounts.save(p, reg)
        assert "hunter2" not in p.read_text(encoding="utf-8")
    try:
        accounts.record({}, HOST, "maybe")
        assert False, "bad status accepted"
    except ValueError:
        pass


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
