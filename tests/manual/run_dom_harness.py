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
    finally:
        try:
            await s.close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


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
