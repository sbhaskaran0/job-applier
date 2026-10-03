"""Run src/browser.py's JavaScript against a REAL DOM.

The CI suites under tests/test_*.py stub Playwright, because CI has no
browser. That leaves an honest gap: no automated test ever executes _SCAN_JS,
_LISTBOX_VALUE_FN or _ERRORS_JS against a live document, so a JS typo or a
wrong selector would pass every gate and only surface in a live application
run -- which costs a real posting.

This harness closes that gap locally. It drives a headless Chromium over
`workday_fixture.html`, a synthetic page reproducing the DOM shapes a Workday
wizard uses (listbox buttons with a "Select One" placeholder, an overlaid
radio, a dependent child field, an sr-only required input, and a
type=submit "Save and Continue"). It is NOT a substitute for one live run on
a real posting -- a real tenant may shape its DOM differently, and the notes
at the bottom say which assertions a live run still has to make -- but it
does prove the JS parses, runs, and means what the Python believes.

It lives under tests/manual/ deliberately: CI globs tests/test_*.py, so this
file is skipped there and needs no Playwright stub.

    python tests/manual/run_dom_harness.py [--headed]

Nothing here touches the user's profile: the browser runs on a throwaway
user_data_dir in the system temp directory, never the persistent profile the
real runs share.
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src import browser as browser_mod          # noqa: E402
from src.browser import BrowserSession          # noqa: E402

FIXTURE = (Path(__file__).resolve().parent / "workday_fixture.html").as_uri()

UPLOAD_FIXTURE = (Path(__file__).resolve().parent
                  / "greenhouse_upload_fixture.html").as_uri()

# authenticate() only ever acts on a Workday host, so the entry/auth fixtures
# are served on a fake tenant host by request interception (no network).
FAKE_TENANT = "https://acme.wd5.myworkdayjobs.com/fixture/"
ENTRY_FIXTURE = FAKE_TENANT + "entry"
AUTH_FIXTURE = FAKE_TENANT + "auth"
_FIXTURE_FILES = {"entry": "workday_entry_fixture.html",
                  "auth": "workday_auth_fixture.html"}
AUTH_FILE_URI = (Path(__file__).resolve().parent
                 / "workday_auth_fixture.html").as_uri()

REVIEW_HTML = """
<!doctype html><meta charset="utf-8"><h1>Review</h1>
<p>Please review your application.</p>
<button id="b1" type="button">Back</button>
<button id="b2" type="submit">Submit application</button>
<script>
  window.__clicks = 0;
  document.querySelectorAll('button').forEach(function (b) {
    b.addEventListener('click', function () { window.__clicks += 1; });
  });
</script>
"""

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("%-4s %s%s" % ("PASS" if ok else "FAIL", name,
                         ("  -- " + str(detail)) if detail else ""))


def idx_by_label(fields, needle):
    """First field index whose label contains `needle` (case-insensitive)."""
    for f in fields:
        if needle.lower() in (f.get("label") or "").lower():
            return f["index"]
    return None


def sig(fields):
    """Comparable identity of a read_form result: what was seen, not where."""
    return sorted((f.get("kind"), f.get("label"), f.get("option_value") or "")
                  for f in fields)


async def run(headless=True):
    tmp = Path(tempfile.mkdtemp(prefix="jobapplier-harness-"))
    # Never the user's persistent profile: a real run shares that directory,
    # and a second Chromium on it would either fail to lock or disturb a
    # half-filled application.
    browser_mod.config.BROWSER_PROFILE_DIR = tmp / "browser"
    s = BrowserSession()
    try:
        await s._ensure(headless=headless)
        await s._open_tab()
        page = s.page
        await page.goto(FIXTURE, wait_until="domcontentloaded")
        await page.wait_for_timeout(300)

        # --- Phase 0: read_form is idempotent ---------------------------
        first = await s.read_form()
        second = await s.read_form()
        check("read_form twice returns an identical field set",
              sig(first) == sig(second),
              "%d fields then %d" % (len(first), len(second)))

        fields = second

        # --- Phase 1: the scanner sees listbox buttons ------------------
        device = idx_by_label(fields, "Phone Device Type")
        country = idx_by_label(fields, "Country")
        school = idx_by_label(fields, "School")
        check("scanner sees button[aria-haspopup=listbox] dropdowns",
              None not in (device, country, school),
              "device=%s country=%s school=%s" % (device, country, school))

        by_index = {f["index"]: f for f in fields}
        check("a listbox is reported kind=combobox + widget=listbox",
              device is not None
              and by_index[device].get("kind") == "combobox"
              and by_index[device].get("widget") == "listbox",
              by_index.get(device))

        # --- the placeholder trap ---------------------------------------
        check('an UNSET listbox reads "", not "Select One"',
              device is not None and by_index[device].get("current_value", "") == "",
              repr(by_index.get(device, {}).get("current_value")))
        check("a SET listbox reads its committed value",
              school is not None
              and by_index[school].get("current_value") ==
              "University of Southern California",
              repr(by_index.get(school, {}).get("current_value")))

        # --- Phase 3: a bogus listbox value must NOT report filled -------
        res = await s.fill_field(device, "Definitely Not An Option")
        check("a bogus listbox value is not reported as filled",
              res.get("status") == "uncommitted" and res.get("verified") is False,
              res)
        check("the failed listbox fill carries observed_value",
              "observed_value" in res, res.get("observed_value"))

        # --- a real listbox value commits and verifies -------------------
        res = await s.fill_field(device, "Mobile")
        check("a real listbox value fills and verifies",
              res.get("status") == "filled" and res.get("verified") is True, res)

        # --- the normalization guard -------------------------------------
        phone = idx_by_label(fields, "Phone Number")
        res = await s.fill_field(phone, "4155551212")
        observed = await s._read_back(phone)
        # Assert the field REALLY reformatted: otherwise this passes trivially
        # (raw == raw) and proves nothing about the loose comparator.
        check("the fixture actually normalized the phone (else the next "
              "assertion is vacuous)",
              observed not in ("", "4155551212"), repr(observed))
        check("a site-normalized phone verifies true (no bogus retry)",
              res.get("verified") is True,
              "wrote 4155551212, field holds %r" % observed)

        # --- password fields: fillable, flagged, never echoed ------------
        pw = idx_by_label(fields, "Create Password")
        check("scanner emits sensitive=true for type=password",
              pw is not None and by_index[pw].get("sensitive") is True,
              by_index.get(pw))
        res = await s.fill_field(pw, "fake-pw-123")
        check("generic fill_field refuses a password field",
              res.get("status") == "use_fill_credential", res)
        res = await s.fill_sensitive(pw, "fake-pw-123", require_password=True)
        check("fill_sensitive fills + verifies on presence, no echo",
              res.get("verified") is True
              and "fake-pw-123" not in repr(res), res)
        after_pw = await s.read_form()
        masked = [f for f in after_pw if f.get("sensitive")]
        check("read_form masks the filled password value",
              masked and masked[0].get("current_value") == "********"
              and "fake-pw-123" not in repr(after_pw), masked)
        check("values_only read_form masks it too",
              "fake-pw-123" not in repr(await s.read_form(values_only=True)))
        check("_read_back masks it",
              await s._read_back(masked[0]["index"]) == "********")

        # show-password toggle: the eye button flips type to text
        await page.evaluate("document.getElementById('pw').type = 'text'")
        flipped = await s.read_form()
        check("after a type=text flip read_form stays masked (sticky)",
              "fake-pw-123" not in repr(flipped)
              and [f for f in flipped if f.get("sensitive")], flipped)
        check("values_only stays masked after the flip",
              "fake-pw-123" not in repr(await s.read_form(values_only=True)))
        res = await s.fill_sensitive(pw, "fake-pw-123", require_password=True)
        check("live type check refuses a password into a type=text box",
              res.get("status") == "not_a_password_field", res)
        res = await s.fill_field(pw, "x")
        check("generic fill still refuses the flipped field",
              res.get("status") == "use_fill_credential", res)
        await page.evaluate("document.getElementById('pw').type = 'password'")

        # --- Defect 3: the overlaid radio --------------------------------
        yes = None
        for f in fields:
            if f.get("kind") == "radio" and f.get("option_value") == "Yes":
                yes = f["index"]
                break
        res = await s.fill_field(yes, "true")
        check("an overlaid radio commits via the label proxy and verifies",
              res.get("status") == "filled" and res.get("verified") is True, res)

        # --- dependent fields: State does not exist until Country is set --
        check("State is absent before Country is set",
              idx_by_label(fields, "State") is None)
        res = await s.fill_field(country, "United States")
        check("Country commits", res.get("verified") is True, res)
        after_country = await s.read_form()
        check("State appears only after a re-read following Country",
              idx_by_label(after_country, "State") is not None)

        # --- the matcher, against a real option list ---------------------
        state = idx_by_label(after_country, "State")
        res = await s.fill_field(state, "CA")
        check("State accepts an abbreviation via the matcher",
              res.get("verified") is True, res)
        res = await s.fill_field(idx_by_label(after_country, "School"), "UCSB")
        check('"UCSB" matches its full school name on a live list',
              res.get("verified") is True, res)

        # --- the safety hazard: submit must not click the wizard Next ----
        before_h1 = await page.inner_text("h1")
        res = await s.submit_application()
        after_h1 = await page.inner_text("h1")
        check("submit_application on a wizard page returns no_submit_button",
              res.get("status") == "no_submit_button", res.get("status"))
        check("submit_application did NOT advance the wizard",
              before_h1 == after_h1, "%r -> %r" % (before_h1, after_h1))
        check("no_submit_button still carries form_snapshot",
              len(res.get("form_snapshot") or []) > 0,
              len(res.get("form_snapshot") or []))

        # --- advance_step: validation errors -----------------------------
        res = await s.advance_step()
        check("a blank required field blocks the advance",
              res.get("status") == "blocked_validation", res.get("status"))
        labels = [e.get("field_label") for e in res.get("errors", [])]
        check("validation errors come back with usable labels",
              any("Legal Name" in (l or "") for l in labels), labels)
        check("an sr-only field's error is reported with index null, not dropped",
              res.get("unmapped_error_count", 0) >= 1,
              "unmapped=%s errors=%s" % (res.get("unmapped_error_count"),
                                         res.get("errors")))
        orphan = [e for e in res.get("errors", [])
                  if "could not be validated" in (e.get("message") or "")]
        check("an orphan error is reported, not dropped", len(orphan) == 1, orphan)
        check("an orphan error is NOT misattributed to an unrelated field",
              bool(orphan) and orphan[0].get("index") is None, orphan)

        mapped = [e for e in res.get("errors", []) if e.get("index") is not None]
        check("an aria-describedby error maps to a real field index",
              len(mapped) >= 1, mapped)

        # --- advance_step: a real advance --------------------------------
        cur = res.get("fields") or await s.read_form()
        await s.fill_field(idx_by_label(cur, "Legal Name"), "Siddharth Bhaskaran")
        await page.evaluate(
            "() => { const e = document.getElementById('consent-code');"
            " e.value = 'OK'; }")
        res = await s.advance_step()
        check("advance_step reports advanced when the step really moves",
              res.get("status") == "advanced", res.get("status"))
        check("advance_step returns a fresh field list",
              isinstance(res.get("fields"), list) and res["fields"],
              len(res.get("fields") or []))
        print("     note: url_changed=%r on this advance (a real Workday run "
              "must confirm whether Workday changes the URL)" %
              res.get("url_changed"))

        # --- the Review page: is_submit_page must click NOTHING ----------
        await page.set_content(REVIEW_HTML)
        await page.wait_for_timeout(200)
        await s.read_form()
        res = await s.advance_step()
        clicks = await page.evaluate("() => window.__clicks")
        check("a Review page reports is_submit_page",
              res.get("status") == "is_submit_page", res.get("status"))
        check("is_submit_page clicked NOTHING", clicks == 0, "clicks=%s" % clicks)

        # --- hidden Greenhouse-style uploads: cover must not hit resume ---
        for fn in ("resume_x.pdf", "cover_letter_x.pdf"):
            (tmp / fn).write_bytes(b"%PDF-1.4 fixture")
        await page.goto(UPLOAD_FIXTURE, wait_until="domcontentloaded")
        await s.read_form()
        res = await s.upload_resume(None, str(tmp / "resume_x.pdf"), "resume")
        check("resume upload lands in the resume slot and verifies",
              res.get("status") == "uploaded" and res.get("verified") is True,
              res)
        res = await s.upload_resume(None, str(tmp / "cover_letter_x.pdf"), "cover")
        check("cover upload verifies with the cover filename",
              res.get("status") == "uploaded" and res.get("verified") is True
              and res.get("observed_filename") == "cover_letter_x.pdf", res)
        names = await page.evaluate(
            "() => ['resume','cover_letter'].map(i =>"
            " (document.getElementById(i).files[0] || {}).name || '')")
        check("resume slot still holds the resume, cover slot the cover",
              names == ["resume_x.pdf", "cover_letter_x.pdf"], names)

        # --- dropzone re-render: the input node is replaced after upload ---
        await page.goto(UPLOAD_FIXTURE + "#rerender", wait_until="domcontentloaded")
        await page.reload(wait_until="domcontentloaded")
        res = await s.upload_resume(None, str(tmp / "cover_letter_x.pdf"), "cover")
        check("re-rendered dropzone: cover upload still verifies via box text",
              res.get("status") == "uploaded" and res.get("verified") is True
              and res.get("observed_filename") == "", res)

        await run_entry_auth(s)
    finally:
        try:
            await s.close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


FAKE_EMAIL = "harness@example.test"
FAKE_PW = "fake-pw-123"


async def _goto(page, url):
    # A hash-only change does not reload the document, so go via about:blank.
    await page.goto("about:blank")
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(300)


async def run_entry_auth(s):
    """Entry (Apply -> chooser -> form) and auth (create / sign-in toggle)
    against the synthetic Workday fixtures. Credentials are fakes injected
    by replacing config.credential; nothing real is read."""
    page = s.page
    s._ledger_path = False           # never write the real profile's ledger
    s._ledger = None

    async def serve(route):
        name = route.request.url.split("/fixture/", 1)[1].split("#")[0].split("?")[0]
        f = Path(__file__).resolve().parent / _FIXTURE_FILES.get(name, "missing")
        if not f.exists():
            await route.fulfill(status=404, body="no fixture")
            return
        await route.fulfill(status=200, content_type="text/html",
                            body=f.read_text(encoding="utf-8"))
    await s.context.route(FAKE_TENANT + "**", serve)

    real_cred = browser_mod.config.credential
    browser_mod.config.credential = (
        lambda service, field: (FAKE_EMAIL if field == "email" else FAKE_PW, None))
    try:
        # --- entry: advance_step clicks nothing, start_application walks ---
        # (a real run reaches here via open_job, which resets the tab's
        # "filled" flag; the phases above filled this same tab)
        s.tabs[s.active_id]["filled"] = False
        await _goto(page, ENTRY_FIXTURE)
        await s.read_form()
        res = await s.advance_step()
        clicked = await page.evaluate("() => window.__clicked")
        check("job page: advance_step reports entry_page",
              res.get("status") == "entry_page", res.get("status"))
        check("job page: advance_step clicked nothing", clicked == [], clicked)
        res = await s.submit_application()
        clicked = await page.evaluate("() => window.__clicked")
        check("job page: submit_application finds no submit (Apply is not one)",
              res.get("status") == "no_submit_button" and clicked == [],
              (res.get("status"), clicked))
        res = await s.start_application()
        clicked = await page.evaluate("() => window.__clicked")
        check("start_application walks job -> chooser -> form",
              res.get("status") == "form"
              and idx_by_label(res.get("fields") or [], "First Name") is not None,
              res.get("status"))
        check("start_application clicked exactly Apply, Apply Manually",
              clicked == ["Apply", "Apply Manually"], clicked)
        res = await s.start_application()
        clicked2 = await page.evaluate("() => window.__clicked")
        check("start_application is idempotent on the form",
              res.get("status") == "form" and clicked2 == clicked, clicked2)

        # --- auth page: classification, no clicks from advance/submit -----
        await _goto(page, AUTH_FIXTURE + "#ok")
        res = await s.start_application()
        auth = res.get("auth") or {}
        check("auth page: start_application reports auth_required",
              res.get("status") == "auth_required", res.get("status"))
        check("auth layout: create_account with email/pw/confirm/consent",
              auth.get("mode") == "create_account"
              and None not in (auth.get("email_index"), auth.get("password_index"),
                               auth.get("confirm_index"))
              and len(auth.get("consent_indexes") or []) == 1, auth)
        check("auth layout: submit_label is the auth button text",
              auth.get("submit_label") == "Create Account", auth)
        check("auth layout: both modes visible (toggle)",
              set(auth.get("available") or []) == {"sign_in", "create_account"},
              auth.get("available"))
        res = await s.advance_step()
        subs = await page.evaluate("() => window.__submits")
        check("auth page: advance_step reports auth_page and submits nothing",
              res.get("status") == "auth_page" and subs == [],
              (res.get("status"), subs))
        res = await s.submit_application()
        subs = await page.evaluate("() => window.__submits")
        check("auth page: submit_application never clicks Create Account",
              res.get("status") == "no_submit_button" and subs == [],
              (res.get("status"), subs))

        # --- authenticate scenarios --------------------------------------
        scenarios = [
            ("ok", "authenticated", ["create"]),
            ("exists", "authenticated", ["create", "sign_in"]),
            ("badpw", "bad_credentials", ["create", "sign_in"]),
            ("verify", "email_verification_required", ["create"]),
            ("signin", "authenticated", ["sign_in", "create"]),
            ("signin,exists", "authenticated", ["sign_in"]),
        ]
        for flags, want, want_subs in scenarios:
            s.clear_auth_ledger()    # each scenario is a fresh tenant history
            await _goto(page, AUTH_FIXTURE + "#" + flags)
            res = await s.authenticate()
            subs = await page.evaluate("() => window.__submits")
            hdr = await page.evaluate("() => window.__hdr")
            check("authenticate #%s -> %s" % (flags, want),
                  res.get("status") == want, res)
            check("authenticate #%s submits %s (<= 2)" % (flags, want_subs),
                  subs == want_subs, subs)
            check("authenticate #%s never clicks the header Sign In" % flags,
                  hdr == 0, hdr)
            check("authenticate #%s result carries no credential" % flags,
                  FAKE_PW not in repr(res) and FAKE_EMAIL not in repr(res), res)
            if flags == "ok":
                agreed = await page.evaluate(
                    "() => document.getElementById('agree').checked")
                check("create-account ticked the privacy consent", agreed)
                promo = await page.evaluate(
                    "() => document.getElementById('promo').checked")
                check("create-account left the marketing opt-in unticked",
                      promo is False, promo)
            if flags == "verify":
                check("verification excerpt is page text, email redacted",
                      "verification email" in res.get("message_excerpt", "")
                      and "<redacted>" in res.get("message_excerpt", ""),
                      res.get("message_excerpt"))

        # --- the ledger: bad_credentials short-circuits the next call ------
        s.clear_auth_ledger()
        await _goto(page, AUTH_FIXTURE + "#badpw")
        first = await s.authenticate()
        check("ledger: first bad-password run ends bad_credentials",
              first.get("status") == "bad_credentials", first)
        await _goto(page, AUTH_FIXTURE + "#badpw")
        res = await s.authenticate()
        subs = await page.evaluate("() => window.__submits")
        check("ledger: bad_credentials on a fresh page submits nothing",
              res.get("status") == "bad_credentials" and subs == [], (res, subs))
        rf = await s.read_form()
        check("after auth, read_form never shows the password",
              FAKE_PW not in repr(rf), [f for f in rf if f.get("sensitive")])
        s.clear_auth_ledger()

        # --- a real overlay: the click_filter intercepts the button --------
        # (covered by every create scenario above; the submit count of 1
        # per attempt proves the overlay and its button were not BOTH hit)

        # --- credentials are Workday-only --------------------------------
        await _goto(page, AUTH_FILE_URI + "#ok")
        res = await s.authenticate()
        subs = await page.evaluate("() => window.__submits")
        val = await page.evaluate("() => document.getElementById('email').value")
        check("non-Workday host: unsupported_auth_flow, nothing typed/clicked",
              res.get("status") == "unsupported_auth_flow" and subs == []
              and val == "", (res, subs, val))

        # --- a filled tab never clicks a bare Apply via start_application -
        await _goto(page, ENTRY_FIXTURE)
        await s.read_form()
        s.tabs[s.active_id]["filled"] = True
        res = await s.start_application()
        clicked = await page.evaluate("() => window.__clicked")
        check("filled tab: start_application refuses the Apply (no click)",
              res.get("status") == "already_in_application" and clicked == [],
              (res.get("status"), clicked))
        res = await s.advance_step()
        clicked = await page.evaluate("() => window.__clicked")
        check("filled tab: a lone Apply reads as is_submit_page, not clicked",
              res.get("status") == "is_submit_page" and clicked == [],
              (res.get("status"), clicked))
        s.tabs[s.active_id]["filled"] = False

        # --- no credentials: the page is not touched ---------------------
        browser_mod.config.credential = (
            lambda service, field: (None, {"status": "no_credentials", "hint": ""}))
        await _goto(page, AUTH_FIXTURE + "#ok")
        res = await s.authenticate()
        subs = await page.evaluate("() => window.__submits")
        val = await page.evaluate("() => document.getElementById('email').value")
        check("no credentials -> no_credentials, nothing typed or clicked",
              res.get("status") == "no_credentials" and subs == [] and val == "",
              (res, subs, val))

        for bad in ("https://evil.example.com/verify",
                    "https://workday.evil.com/x",
                    "https://evilmyworkdayjobs.com/x",
                    "https://myworkdayjobs.com@workday-login.ru/",
                    "https://acme.wd5.myworkdayjobs.com:8443/x"):
            res = await s.open_verification_link(bad)
            check("open_verification_link refuses %s" % bad,
                  res.get("status") == "not_allowed", res)
    finally:
        browser_mod.config.credential = real_cred


def main():
    headless = "--headed" not in sys.argv
    asyncio.run(run(headless=headless))
    failed = [n for n, ok, _ in _results if not ok]
    print("\n%d/%d passed" % (len(_results) - len(failed), len(_results)))
    if failed:
        print("failed: " + ", ".join(failed))
    print(
        "\nStill only a live run on a real posting can confirm:\n"
        "  * whether Workday changes the URL on advance (this harness reports\n"
        "    url_changed above; the fixture never navigates)\n"
        "  * whether a real tenant's error nodes are visible at scan time and\n"
        "    how often unmapped_error_count is > 0\n"
        "  * whether the real Review page exposes only a Submit control\n"
        "  * whether the aria-label-suffix arm of the listbox cascade is ever\n"
        "    reached (the earlier arms win on this fixture)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
