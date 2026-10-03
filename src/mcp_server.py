"""job-applier MCP server.

Exposes the browser, data, knowledge, and discovery components as tools that
Claude Code calls. Claude Code is the agentic loop; these tools are the hands
and the memory. Run via:  python -m src.mcp_server   (stdio transport).
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer

from . import browser, config, context, data, store, tailor
from .providers import watchlist as wl

mcp = MCPServer("job-applier")


# --------------------------------------------------------------------------- #
# Browser tools (ATS-agnostic; act on the one live session)
# --------------------------------------------------------------------------- #
@mcp.tool()
async def open_job(url: str, new_tab: bool = False, company: str = "") -> dict:
    """Open a job posting / application page in the live browser (non-headless,
    so the user can watch). One-shot: returns {title, detected_ats, tab_id,
    resume_synced, intervention:{blocked,signals,message}, fields:[...]} — i.e.
    it ALSO runs the CAPTCHA/intervention check and reads the form, so you don't
    need separate check_for_intervention / read_form calls right after opening.

    Tabs: pass new_tab=True to open this job in its OWN tab, preserving any
    already-filled forms in other tabs — batch mode uses this so a job you can't
    submit can be left FILLED for a one-click manual submit instead of being
    re-filled from scratch. Default (new_tab=False) reuses the active tab —
    right for single-job apply, retries, and re-opening the same URL. Pass
    `company` to label the tab in list_tabs. Manage tabs with list_tabs /
    switch_tab / close_tab.

    If intervention.blocked is true, stop and ask the user to clear it. Re-call
    read_form only if the page later navigates/changes."""
    return await browser.session.open_job(url, new_tab=new_tab, company=company)


@mcp.tool()
async def list_tabs() -> list:
    """List the open browser tabs. In batch mode each open tab is one job's
    filled/loaded application, so at hand-off time this IS the manual-submit
    queue. Returns [{tab_id, company, title, url, active, closed}]."""
    return await browser.session.list_tabs()


@mcp.tool()
async def switch_tab(tab_id: int) -> dict:
    """Make `tab_id` the active tab (all field/submit tools act on the active
    tab), bring it to the front, and re-read its form — field indexes are
    per-tab and only valid after a read on that page. Returns
    {tab_id, url, title, company, fields}."""
    return await browser.session.switch_tab(tab_id)


@mcp.tool()
async def close_tab(tab_id: int) -> dict:
    """Close a browser tab — call after a job is verified submitted to keep the
    window to just the unfinished jobs. Returns {status, tab_id, active_id}."""
    return await browser.session.close_tab(tab_id)


@mcp.tool()
async def read_form(values_only: bool = False) -> list:
    """Read the live page and all iframes; return every fillable field as
    {index, kind, label, options, required, current_value, group}. Works on any
    ATS with no per-site rules. Re-call after navigation. Use the returned
    index with fill_field / upload_resume / submit_application.

    Pass values_only=True for a lean re-read — only {index, kind, label,
    current_value} per field — when you just need to VERIFY that fills took
    (not discover fields). Use it for post-fill verification passes to avoid
    re-sending option lists and metadata you already have. Either way, long
    native <select> lists are summarized (options_count / options_sample /
    options_note); call get_field_options(index) for the full list."""
    return await browser.session.read_form(values_only=values_only)


@mcp.tool()
async def fill_field(index: int, value: str) -> dict:
    """Fill the field at `index`. Handles text/textarea (types), select (chooses
    the option by label or value), and radio/checkbox (pass 'yes'/'no'). To fill
    many fields at once, prefer fill_many.

    Comboboxes are verified: status "filled" means the widget committed the
    (possibly normalized) `value` shown in the result. Status "unmatched" means
    no option matched — the result carries the widget's real `options`; call
    again with the correct option text verbatim (e.g. "United States" → "US")."""
    return await browser.session.fill_field(index, value)


@mcp.tool()
async def fill_many(fields: list[dict]) -> list:
    """Fill MANY fields in one call — the fast path. `fields` is
    [{index, value}, ...], applied in order; a per-field failure is captured in
    the result (not fatal). Use this after resolve_fields to fill everything you
    resolved/approved in a single round-trip instead of one fill_field per
    field. Check each result: a combobox row with status "unmatched" includes
    its real `options` — refill those indexes with the matching option text."""
    return await browser.session.fill_many(fields)


@mcp.tool()
async def fill_repeating_section(section: str) -> dict:
    """Fill a repeating "My Experience" section from the PROFILE's structured
    entries — `section` is "experience" (Work Experience) or "education".
    You pass no entry data: entries come from profile.yaml server-side, so
    nothing can be invented. The section is found by its heading; every value
    goes through fill_field, so dropdowns and verification behave as usual.
    NOTHING already on the page is overwritten (fill-blanks only).

    Where each entry goes, decided before anything is clicked: a group that
    already holds it (company + loose title or same start; school) gets its
    empty fields completed; an "orphan" group (values but no company/title/
    school, e.g. a half-finished earlier run) consistent with it is
    completed; a blank group is reused; only then is "Add" / "Add Another"
    clicked (max 8 entries, 2 clicks each). "I currently work here" is
    ticked before To and To is skipped for a current role. Degrees match
    only the profile's own degree (BS <-> Bachelor of Science (BS)) or, on a
    generic list, its level. GPA follows the profile's gpa_policy (always |
    if_required | never). Re-running is safe.

    Returns {section, status, added, existing_groups, filled: [{entry, result
    (added | filled | completed_blanks | completed_orphan), fields: [{key,
    label, index, status, verified, value, ...}]}], skipped: [{entry,
    reason}], errors, gpa_policy_used, leftover_blank_groups,
    orphan_groups?, required_empty?, warnings?}. `entry` indexes the
    profile's list. Long values are truncated to 60 chars.
    status:
      - "ok" — every attempted field verified (or unverifiable) and no
        required field of a touched group is empty.
      - "already_present" — every entry was already complete on the page.
      - "partial" — a field is uncommitted / unmatched (`options` listed;
        refill that index with fill_field once) / unsupported_date_widget /
        error; or a company/title/school/start/current-role control was
        not_found; or `required_empty` lists empty required fields; or
        `orphan_groups` lists groups no entry fits (Add was not clicked).
      - "section_not_found" — no heading names the section on this step.
      - "no_add_button" / "ambiguous_add" / "add_failed" /
        "new_group_not_blank" — no usable Add control, more than one, or
        clicking it produced no new blank group.
      - "no_entries" — the profile has no entries for the section.
      - "bad_section" — section is not experience / education.
    Per-field statuses beyond fill_field's: already_set, skipped (reason
    no_value / current_role / gpa_policy / incomplete_date /
    to_already_filled), not_found; `day_assumed: true` marks a native date
    input that got day 01; `committed_as: "pill"` marks a typeahead whose
    choice shows as a chip."""
    return await browser.session.fill_repeating_section(section)


@mcp.tool()
async def fill_credential(index: int, field: str = "password",
                          service: str = "workday") -> dict:
    """Fill the account email or password for an ATS login/signup field at
    `index`, from the user's local credentials.yaml. The value is loaded and
    typed SERVER-SIDE and never returned. NEVER invent, generate, or type a
    password yourself, and never pass one through fill_field/fill_many (they
    refuse password fields with status "use_fill_credential").

    `field` is "password" or "email". A password is only typed into a real
    type=password input; otherwise status is "not_a_password_field".
    Returns {index, status, verified, sensitive: true}, or
    {status: "no_credentials", hint} when the user has not created
    profiles/<id>/secrets/credentials.yaml: then PARK and ask the user to
    create it; do not make one up."""
    if field not in ("password", "email"):
        return {"index": index, "status": "bad_field",
                "error": 'field must be "password" or "email"'}
    value, err = config.credential(service, field)
    if err:
        return err
    try:
        res = await browser.session.fill_sensitive(
            index, value, require_password=(field == "password"))
    except Exception as e:  # never str(e): it may embed the value
        res = {"index": index, "status": "error", "error": type(e).__name__}
    res = {k: res[k] for k in ("index", "status", "verified", "error") if k in res}
    res["sensitive"] = True
    return res


@mcp.tool()
async def upload_resume(index: int = -1, path: str = "",
                        target: str = "resume") -> dict:
    """Attach the resume, or a cover-letter file. Pass `index` >= 0 to use a
    specific file field from read_form; omit it (or -1) to auto-locate a file
    input anywhere on the page, including hidden ones behind an "Attach"/
    dropzone widget (Greenhouse/Ashby). target="resume" (default) prefers a
    resume/CV input and uses the project resume file unless `path` is given.
    For a cover letter pass target="cover" AND path=cover_letter_path: only an
    input labelled "cover" is eligible and the resume slot is never used; with
    none the result is {status: "no_cover_slot"} (fall back to pasting
    cover_letter_text). Always check `verified` (+ `observed_filename`) in the
    result; status "error" means a cover upload landed in the resume slot."""
    return await browser.session.upload_resume(index if index >= 0 else None,
                                               path or None, target)


@mcp.tool()
async def get_field_options(index: int) -> dict:
    """Return the real selectable options for a dropdown field at `index` (native
    select or react-select combobox). Call this on a custom combobox (e.g. "How
    did you hear about us?") to see the exact choices, then pass the matching one
    to fill_field."""
    return await browser.session.get_field_options(index)


@mcp.tool()
async def screenshot(path: str = "") -> dict:
    """Take a full-page screenshot so you can visually verify the filled form
    or inspect an unusual widget."""
    return await browser.session.screenshot(path or None)


@mcp.tool()
async def check_for_intervention() -> dict:
    """Check whether the page is showing a CAPTCHA / 'verify you are human' /
    login or challenge wall that only the human user can clear. Returns
    {blocked, signals, warnings, message}.

    Only a VISIBLE, interactable challenge (a reCAPTCHA v2 checkbox, an open
    image challenge, a Turnstile/hCaptcha widget) sets blocked=true — STOP and
    ask the user to clear it. Background anti-bot that needs no interaction
    (reCAPTCHA v3, invisible v2, the "protected by reCAPTCHA" badge) comes back
    blocked=false with a note in `warnings`; proceed normally and do NOT ask the
    user to solve anything. An email/OTP code gate is NOT reported here — use
    detect_verification_gate for that."""
    return await browser.session.detect_blockers()


@mcp.tool()
async def detect_verification_gate() -> dict:
    """Detect an email/one-time-code verification gate (e.g. Greenhouse's 8-box
    code challenge that can appear after clicking submit). Returns {present,
    count, mode, text_hint, message}. When present, this is recoverable WITHOUT
    the user: fetch the latest verification code from the applicant's inbox (via
    the Gmail tools), call fill_verification_code(code), then submit_application
    again and verify the confirmation."""
    return await browser.session.detect_verification_gate()


@mcp.tool()
async def fill_verification_code(code: str) -> dict:
    """Fill a detected email/OTP verification-code gate with `code` (the code you
    fetched from the applicant's inbox). Handles both a segmented N-box OTP and a
    single code field. Does NOT submit — call submit_application afterward to
    complete and verify the submission."""
    return await browser.session.fill_verification_code(code)


@mcp.tool()
async def advance_step() -> dict:
    """Move a MULTI-STEP application (Workday's "Save and Continue", any
    wizard's "Next"/"Continue") to the next step. Advancing is NOT submitting
    and is NOT gated — you do not need the user's confirmation to call this.
    It never clicks a submit control.

    Returns {status, url_changed, current_url, errors, error_count,
    unmapped_error_count, fields, button_label, note}:
      - "advanced" — the step moved. **Every field index from before this call
        is dead**; use the `fields` in this result (a fresh read_form) for all
        subsequent fill_field / fill_many / upload_resume calls.
      - "blocked_validation" — the page refused to advance. `errors` is
        [{index, field_label, message}]; fix those fields and call again. An
        error whose `index` is null could not be tied to a scanned field
        (usually a visually-hidden input) — match it by `field_label`;
        `unmapped_error_count` says how many are like that. Errors are never
        silently dropped.
      - "no_change" — clicked, but nothing moved (swallowed click, or a modal
        is open). Screenshot before retrying.
      - "is_submit_page" — the only control here is Submit, so NOTHING was
        clicked. Review, confirm with the user, then call submit_application.
        Never use advance_step as a way to "check" the review page.
      - "entry_page" — a job page / "Start Your Application" chooser (only
        Apply-like controls). Nothing clicked; call start_application.
      - "auth_page" — a sign-in / create-account page. Nothing clicked; call
        authenticate.
      - "no_advance_button" — single-page form; just submit it.

    `fields` is always a fresh read of the page as it now stands, so you never
    need a separate read_form after this call."""
    return await browser.session.advance_step()


@mcp.tool()
async def start_application() -> dict:
    """Get from a job page to the application form (or its sign-in gate) on
    the ACTIVE tab. Clicks only entry controls: "Apply" on the job page, then
    "Apply Manually" on Workday's "Start Your Application" chooser, and an
    "Sign in with email" reveal behind an SSO chooser. It NEVER clicks
    "Autofill with Resume", "Use My Last Application", SSO buttons, or any
    submit. Idempotent: on a form or auth page it only classifies. Not gated.

    Returns {status, url, fields, auth}:
      - "form" — the application form is showing; `fields` is a fresh
        read_form (passwords masked).
      - "auth_required" — a sign-in / create-account page. `auth` is
        {mode: "sign_in"|"create_account"|"unknown", available, email_index,
        password_index, confirm_index, consent_indexes, submit_label}. Call
        authenticate() next; never type a password yourself.
      - "blocked" — a visible CAPTCHA (`signals`); hand to the user.
      - "no_entry" — nothing to click (dead posting or unknown layout).
      - "already_in_application" — the tab was already filled, or shows a
        Back control / step indicator; an "Apply" there may be the FINAL
        submit, so nothing was clicked. Use advance_step / submit_application."""
    return await browser.session.start_application()


@mcp.tool()
async def authenticate(mode: str = "auto") -> dict:
    """Sign in to, or create, the ATS tenant account on the active tab using
    the user's local credentials.yaml (workday: email/password). The values
    are filled server-side and NEVER returned; you never see or invent a
    password. mode: "auto" (default; follows the page, falls back once
    between sign-in and create-account), "sign_in" or "create_account".
    Creating a tenant account is allowed in batch Stage A. At most 2 auth
    submits per call; a wrong password is never retried.

    Returns {status, url, message_excerpt} (message_excerpt is visible page
    notice text, <= 200 chars, credentials redacted). Statuses:
      - "authenticated" — the application form is visible; read_form next.
      - "email_verification_required" — fetch the email from the inbox, then
        open_verification_link(url) for a link, or detect_verification_gate /
        fill_verification_code for a code; then authenticate() again.
      - "account_exists" — create-account said the account exists and the
        automatic switch to sign-in was not possible.
      - "no_account" — sign-in said no such user (mode "sign_in" only, else it
        switches to create-account once automatically).
      - "bad_credentials" — wrong password. STOP and park; do not retry.
      - "no_credentials" / "bad_credentials_file" — the user must create or fix
        profiles/<id>/secrets/credentials.yaml; the page was not touched.
      - "blocked" — visible CAPTCHA. "unconfirmed" — submitted but the
        result is unclear; screenshot. "error" — exception type only in
        `error`. Also "toggle_failed", "no_auth_submit", "bad_mode",
        "no_entry", "already_in_application".
      - "unsupported_auth_flow" — not a Workday host (credentials are only
        ever used on *.myworkdayjobs.com / *.myworkday.com / *.workday.com),
        or no password field. Park; the user signs in by hand.
      - "recently_submitted" — an auth submit hit this tenant < 60s ago, so
        the page was only classified. Wait, then call again.
    Lockout budget: a per-tenant ledger (profiles/<id>/runtime/
    auth_attempts.json, timestamps + outcomes only) allows at most one failed
    sign-in per tenant per 24h; after bad_credentials every call for that
    tenant returns bad_credentials without submitting until 24h pass or the
    USER asks for clear_auth_ledger."""
    return await browser.session.authenticate(mode)


@mcp.tool()
def clear_auth_ledger(tenant: str = "") -> dict:
    """ONLY on the user's explicit request (e.g. "I fixed my Workday password,
    try again"). Never call this on your own to get around a
    bad_credentials result: the ledger is what stops repeated sign-in
    attempts from locking the user's tenant account. Clears the recorded
    auth attempts for one tenant (`tenant` = host or URL, e.g.
    acme.wd5.myworkdayjobs.com) or for all tenants when empty. Returns
    {status: "cleared", tenants}."""
    return browser.session.clear_auth_ledger(tenant)


@mcp.tool()
async def open_verification_link(url: str) -> dict:
    """Open an account-verification link (from the applicant's email) in a
    NEW tab of the session, read up to 300 chars of its text, close it, and
    reload the original application tab. Only https links whose ASCII host
    is on a Workday domain (dot-boundary: *.myworkdayjobs.com,
    *.myworkday.com, *.workday.com), with no userinfo and no port, and on the
    same Workday domain as the active tab, are opened; anything else returns
    {status: "not_allowed"} untouched.
    Returns {status: "opened", text_excerpt, note}. Then call authenticate()
    (or start_application) again."""
    return await browser.session.open_verification_link(url)


@mcp.tool()
async def get_job_text() -> str:
    """Return the visible page text (use it to read the job description for a
    fit assessment)."""
    return await browser.session.get_job_text()


@mcp.tool()
async def snapshot_job(url: str, company: str = "", out_dir: str = "") -> dict:
    """Snapshot a posting for batch prep — SERVER-SIDE, so the big field list and
    job description never pass through the model's context. This is the batch
    Stage-A primitive: the caller loops it over the queue and carries neither a
    single form dump nor a JD; only a compact receipt comes back.

    It composes existing primitives (open_job -> read the form + intervention,
    get_job_text -> the JD) and WRITES the prep file directly, so it is exactly
    as ATS-agnostic as read_form (any ATS, no per-site rules). On a normal page
    it does not click or type, so it captures precisely the initially-rendered
    fields — same fidelity as a manual read_form. Exception: a page with no
    form fields (page chrome such as a language picker ignored) on a
    myworkdayjobs.com host, or one showing an Apply-like control,
    is walked to the form first (JD read before leaving the description):
    start_application, then authenticate when it reports auth_required (Stage
    A may create the tenant account). The receipt carries `entry_status` and
    `auth_status`; any auth result other than "authenticated" parks with
    park_reason "<auth_status>: <message_excerpt>". Snapshotting fills no
    application answers (only, on that path, the account login), so the active tab
    is reused (no new_tab); resume.txt is re-synced from resume.pdf as a harmless
    side effect of open_job.

    Writes `<out_dir or the active profile's prep dir>/<job-slug>.json`:
      {url, company, job_title, ats, snapshot_at, fields, jd_text}
    Field LABELS are stored verbatim — opaque/GUID/bare "Yes/No" labels INCLUDED.
    Disambiguating those against jd_text is a PREP-stage concern (the JD is in the
    same file); snapshot does not infer them.

    Returns only {url, company, job_title, ats, prep_path, field_count,
    required_count, freetext_count, status, park_reason, recaptcha_warning?}:
      - status "snapshotted": form read and file written. `freetext_count` is the
        number of multi-line/free-text fields (textarea / contenteditable / ARIA
        textbox) — the routing signal for batch prep: 0 => resolve inline
        (all-profile / closed-choice), >0 => route to a crafting agent (needs the
        voice corpus). Plain <input type=text> profile fields are NOT counted.
        When freetext_count == 0, the receipt also carries the verbatim `fields`
        array (native-<select> options where read_form has them; jd_text
        excluded) so the inline lane resolves the job WITHOUT reading the prep
        file — keeping the JD out of the main context. Combobox (react-select)
        options are NOT inlined here (they need get_field_options); their exact
        option is matched at fill time in Stage D, as it already is. When
        freetext_count > 0, `fields` is omitted (the crafting subagent reads the
        full prep file instead).
      - status "parked" (no file written): a REAL visible challenge
        (intervention.blocked), or zero fields (dead/removed posting / unreadable
        form). park_reason says which. A Greenhouse invisible-reCAPTCHA v3 signal
        is NOT a block — it comes back blocked=false, is snapshotted normally, and
        the signal is surfaced in `recaptcha_warning`."""
    opened = await browser.session.open_job(url, new_tab=False, company=company)
    interv = opened.get("intervention", {}) or {}
    fields = opened.get("fields", []) or []
    title = opened.get("title", "")
    ats = opened.get("detected_ats", "")
    base = {"url": url, "company": company, "job_title": title, "ats": ats,
            "prep_path": None, "field_count": 0, "required_count": 0,
            "entry_status": None, "auth_status": None}
    if interv.get("blocked"):
        return {**base, "status": "parked",
                "park_reason": "blocked at open: "
                + (interv.get("message") or "visible challenge")}
    jd_text = None
    if not browser.form_fields(fields) and (
            browser.is_workday_host(urlparse(url).hostname or "")
            or await browser.session.has_entry_control()):
        # A Workday job page shows only "Apply": read the JD HERE, before
        # leaving the description, then walk to the form.
        jd_text = await browser.session.get_job_text()
        entry = await browser.session.start_application()
        entry_status = base["entry_status"] = entry.get("status")
        if entry_status == "form":
            fields = entry.get("fields") or []
        elif entry_status == "auth_required":
            # Credentials are Workday-only: an "Apply" that led to iCIMS /
            # Taleo / SuccessFactors parks instead of signing in there.
            here = urlparse(entry.get("url") or browser.session.page.url).hostname
            if not browser.is_workday_host(here or ""):
                return {**base, "status": "parked",
                        "park_reason": "auth_required on a non-Workday host "
                                       "(%s); sign in manually" % (here or "?")}
            auth = await browser.session.authenticate()
            auth_status = base["auth_status"] = auth.get("status")
            if auth_status != "authenticated":
                return {**base, "status": "parked",
                        "park_reason": "%s: %s" % (
                            auth_status, auth.get("message_excerpt") or "")}
            fields = await browser.session.read_form()
        else:
            return {**base, "status": "parked",
                    "park_reason": "entry %s" % entry_status}
    if not fields:
        return {**base, "status": "parked",
                "park_reason": "no fields (dead/removed posting or unreadable form)"}
    if jd_text is None:
        jd_text = await browser.session.get_job_text()
    prep_dir = Path(out_dir) if out_dir else config.PREP_DIR
    prep_dir.mkdir(parents=True, exist_ok=True)
    prep_path = prep_dir / f"{tailor.job_slug(company, title, url)}.json"
    prep_path.write_text(
        json.dumps({"url": url, "company": company, "job_title": title,
                    "ats": ats,
                    "snapshot_at": datetime.now(timezone.utc).isoformat(),
                    "fields": fields, "jd_text": jd_text},
                   indent=2, ensure_ascii=False),
        encoding="utf-8")
    freetext_count = sum(1 for f in fields if f.get("multiline"))
    out = {**base, "prep_path": str(prep_path), "field_count": len(fields),
           "required_count": sum(1 for f in fields if f.get("required")),
           "freetext_count": freetext_count,
           "status": "snapshotted", "park_reason": None}
    # Inline lane: a job with NO free-text field can be resolved inline (no
    # subagent). Carry its `fields` on the receipt (native-<select> options
    # included; combobox options resolve at fill time) but WITHOUT jd_text — so
    # inline resolution never reads the prep file (which would pull the JD, dead
    # weight for a no-essay job, into the persistent main context). Craft jobs
    # (freetext_count > 0) get only the counts; their subagent reads the full
    # prep file (fields + jd_text) in its own disposable context.
    if freetext_count == 0:
        out["fields"] = fields
    warnings = interv.get("warnings") or []
    if warnings:
        out["recaptcha_warning"] = warnings
    return out


@mcp.tool()
async def submit_application(index: int = -1, company: str = "",
                              job_title: str = "") -> dict:
    """DESTRUCTIVE: submit the application. Only call after the user has
    explicitly confirmed. If `index` >= 0 that field is clicked, otherwise the
    submit button is located automatically (across frames, never a
    "Quick Apply" button).

    The result's `status` is VERIFIED from the page text: "submitted" only when
    an explicit success message shows; "rejected_spam" when a spam/submission-
    failure banner shows (Ashby invisible-reCAPTCHA scoring — leave the filled
    form for the user to submit manually, do NOT auto-retry); "attempted" when
    neither is seen — inspect (screenshot / check_for_intervention) and hand off
    to the user if a verification gate is blocking. A vanished form alone is NOT
    treated as success. Pass `company` and `job_title` (from the posting): the
    form is snapshotted just before the click and every filled answer is auto-
    captured into history (conservatively scoped; EEO fields never persisted);
    the application is logged to data/applications.json only on a CONFIRMED
    "submitted". The `capture` field summarizes what was persisted."""
    result = await browser.session.submit_application(index if index >= 0 else None)
    snapshot = result.pop("form_snapshot", [])
    try:
        result["capture"] = data.capture_submission(
            snapshot, company=company, job_title=job_title,
            url=result.get("current_url", ""),
            log_application=(result.get("status") == "submitted"))
    except Exception as e:  # capture must never mask a successful submit
        result["capture"] = {"error": f"{type(e).__name__}: {e}"}
    return result


# --------------------------------------------------------------------------- #
# Data tools (exact profile + past answers)
# --------------------------------------------------------------------------- #
@mcp.tool()
def get_profile_field(question_or_key: str) -> dict:
    """Resolve a form label to an EXACT stored profile value (name, email,
    phone, work authorization, etc.). Returns {matched_key, value, confidence}.
    value is null when nothing maps. This is the first source to try."""
    return data.get_profile_field(question_or_key)


@mcp.tool()
def get_profile_paths() -> dict:
    """Return the resolved filesystem paths of the ACTIVE profile — skills call
    this instead of guessing repo-relative personal paths (personal data lives
    under profiles/<id>/, not the repo root). Returns {profile_id, root,
    context_dir, data_dir, prep_dir, resumes_dir, runtime_dir, screenshot_dir}
    as absolute path strings. screenshot_dir (same as runtime_dir) is where
    screenshots and other transient run files belong."""
    prof = config.active_profile()
    return {
        "profile_id": prof.profile_id,
        "root": str(prof.root),
        "context_dir": str(prof.context_dir),
        "data_dir": str(prof.data_dir),
        "prep_dir": str(prof.prep_dir),
        "resumes_dir": str(prof.resumes_dir),
        "runtime_dir": str(prof.runtime_dir),
        "screenshot_dir": str(prof.runtime_dir),
    }


# Field kinds that are pick-from-a-list, not free prose. For these, a no-match
# result should NOT dump the cover-letter/essay corpus — there is nothing to
# "craft"; the answer is one of the widget's options.
_CLOSED_KINDS = {"combobox", "select", "radio", "checkbox"}

# Snippet caps for the genuinely-open craft path. resolve_fields is a triage
# tool — it hands back enough to see WHICH source to craft from, not the full
# corpus. Full text is one get_cover_letter_examples / search_context away.
_CTX_SNIPPET_CHARS = 480
_HIST_ANSWER_CHARS = 320


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + " …[clipped]"


def _clip_hist(rows: list) -> list:
    out = []
    for h in rows:
        r = dict(h)
        r["answer"] = _clip(r.get("answer", ""), _HIST_ANSWER_CHARS)
        # `alternates` (optional, added by the history store) are full answers
        # too — clipping only `answer` would let them walk straight past the
        # token cap this whole helper exists to enforce.
        if isinstance(r.get("alternates"), list):
            r["alternates"] = [_clip(a, _HIST_ANSWER_CHARS) if isinstance(a, str)
                               else a for a in r["alternates"]]
        out.append(r)
    return out


@mcp.tool()
def resolve_fields(fields: list[dict], company: str = "") -> list:
    """Batch-resolve MANY form fields in ONE call — the fast path that replaces
    the per-field get_profile_field → search_history → search_context loop.
    `fields` is [{index, label}, ...]; also pass `kind` (and `options` when you
    have them) from open_job/read_form for each field — closed-choice fields
    (combobox/select/radio/checkbox) skip the essay corpus on a no-match, which
    is the single biggest token saver. Pass `company` (the employer you're
    applying to) so company-scoped past answers can be reused safely. For each
    field, it runs the same source cascade and returns one row:
      - source "profile": an exact stored value → `value` (fill verbatim; still
        apply the short-answer style rules for demographic/eligibility fields).
      - source "history", confidence "high": a strong past answer (`score` ≥
        0.7) whose scope allows confident reuse (evergreen, or company-scoped
        for THIS company) → `value` to adapt (`alternatives` holds the top
        matches).
      - source "history", confidence "review": a strong match that is
        company-scoped for a DIFFERENT company or conditional (role/location-
        dependent) → adapt `value` but GATE it for user approval like a crafted
        answer; never fill it silently.
      - source "choice": a closed-choice field with no stored value → pick from
        `options` (fetch with get_field_options if empty). No essay corpus —
        for Yes/No, consent, eligibility dropdowns the answer is an option, not
        prose. `history_top` (clipped) is included when a past answer is close.
      - source "context": an OPEN free-text field with no stored value →
        `context` snippets (clipped) + `history_top` to CRAFT from; these are
        the low-confidence ones to draft and gate for user approval. Snippets
        are clipped for triage — pull full voice via get_cover_letter_examples.
    Fill the profile and confidence-"high" history rows with fill_many; craft/
    adapt the rest, get approval where needed, then fill_many those too."""
    comp = (company or "").strip().lower()
    out = []
    for f in fields:
        idx, label = f.get("index"), f.get("label", "")
        kind = (f.get("kind") or "").strip().lower()
        options = f.get("options") or []
        # A blank-label field is almost always a react-select mirror / shadow
        # input paired with a real combobox (Greenhouse renders these). It has
        # nothing to resolve — never run a lookup (an empty label would search
        # the whole corpus and dump snippets); just skip it cheaply.
        if not (label or "").strip():
            out.append({"index": idx, "label": label, "source": "skip",
                        "note": "no label — likely a shadow/mirror input paired "
                                "with a combobox; ignore unless a widget needs a "
                                "typed value"})
            continue
        pf = data.get_profile_field(label)
        if pf.get("value"):
            row = {"index": idx, "label": label, "source": "profile",
                   "value": pf["value"], "matched_key": pf.get("matched_key"),
                   "confidence": "exact"}
            if pf.get("eeo"):
                row["eeo"] = True  # voluntary self-ID: fill only in self-ID sections
            out.append(row)
            continue
        hist = data.search_history(label, top_k=3)
        strong = [h for h in hist if h["score"] >= 0.7]
        reusable = [h for h in strong
                    if h.get("scope") == "evergreen"
                    or (h.get("scope") == "company" and comp
                        and h.get("company", "").strip().lower() == comp)]
        if reusable:
            best = reusable[0]
            out.append({"index": idx, "label": label, "source": "history",
                        "value": best["answer"], "score": best["score"],
                        "confidence": "high", "alternatives": hist})
            continue
        if strong:
            best = strong[0]
            out.append({"index": idx, "label": label, "source": "history",
                        "value": best["answer"], "score": best["score"],
                        "scope": best.get("scope"),
                        "company": best.get("company", ""),
                        "confidence": "review", "alternatives": _clip_hist(hist)})
            continue
        # No stored value. A closed-choice field is answered by picking an
        # option, not by crafting from prose — so skip search_context entirely
        # (this is where the big cover-letter/essay dumps came from).
        if kind in _CLOSED_KINDS or options:
            row = {"index": idx, "label": label, "source": "choice",
                   "confidence": "craft", "options": options,
                   "history_top": _clip_hist(hist[:2])}
            if not options:
                row["note"] = ("closed-choice field — call get_field_options("
                               f"{idx}) for the option list, then fill_many")
            out.append(row)
            continue
        # Genuinely open free-text field: craft from clipped context snippets.
        ctx = context.search_context(label, top_k=3)
        for c in ctx:
            if isinstance(c, dict) and "text" in c:
                c["text"] = _clip(c["text"], _CTX_SNIPPET_CHARS)
        out.append({"index": idx, "label": label, "source": "context",
                    "confidence": "craft", "history_top": _clip_hist(hist[:2]),
                    "context": ctx})
    return out


@mcp.tool()
def search_history(question: str, top_k: int = 5) -> list:
    """Find the closest past answers to a question from history.json, ranked by
    similarity score. Use a high-scoring past answer (adapt it) before crafting
    a new one."""
    return data.search_history(question, top_k)


@mcp.tool()
def save_answer(question: str, answer: str, scope: str = "evergreen",
                company: str = "") -> dict:
    """Persist an approved answer so it is reused next time. Call this after the
    user approves a crafted or adapted answer. Questions are matched on their
    normalized form ("Country*" == "Country"), updating in place.

    Classify `scope` honestly — it controls how the answer is reused later:
      - "evergreen": a stable fact true anywhere (work auth, state, years of
        experience) → eligible for confident auto-fill.
      - "company": tailored to one employer ("Why do you want to work at X?")
        → pass `company`; confidently reused only for that same company.
      - "conditional": depends on the role/location/moment (relocation,
        salary, start date, how-did-you-hear) → always gated for review."""
    return data.save_answer(question, answer, scope=scope, company=company)


@mcp.tool()
def log_application(company: str = "", job_title: str = "", url: str = "",
                    status: str = "submitted") -> dict:
    """Log a confirmed application to data/applications.json. Use this when a
    submission was verified OUTSIDE a single submit_application call — after an
    email verification-code gate, or to back-fill a confirmed-but-unlogged
    application. Deduped on (company, job_title, url): logging the same job again
    updates the record in place rather than duplicating it. Only call once you
    have actually confirmed the submission (thank-you page), mirroring the
    'trust only a verified result' rule."""
    return data.log_application_record(company=company, job_title=job_title,
                                       url=url, status=status)


@mcp.tool()
def set_application_outcome(company: str = "", job_title: str = "", url: str = "",
                            outcome: str = "none", outcome_date: str = "") -> dict:
    """Record what an employer came BACK with on an already-logged application
    (JOB-136). Call this whenever the user reports a reply — a rejection email,
    a recruiter screen, an interview invite, an offer — or asks you to mark a
    long-silent application dead. `status` records whether we finished the form;
    `outcome` records whether they answered, and only ever arrives from a human,
    so nothing else in the system fills it in.

    `outcome` must be one of: "none" (no reply yet — also blanks any recorded
    date), "rejected", "screen", "interview", "offer", "ghosted" (silence gone
    on long enough to call it). Anything else is refused with
    {"status": "invalid"} rather than quietly widening the vocabulary.

    `outcome_date` defaults to today; pass an ISO date when back-filling. The
    target record is resolved on the SAME (company, job_title, url) dedupe key
    log_application uses, so it lands on the record a later re-log will update.
    Returns {"status": "updated"|"not_found"|"invalid"}; on "not_found" the
    application was never logged — log it first."""
    return data.set_application_outcome(company=company, job_title=job_title,
                                        url=url, outcome=outcome,
                                        outcome_date=outcome_date)


# --------------------------------------------------------------------------- #
# Knowledge tool (the local "Claude project")
# --------------------------------------------------------------------------- #
@mcp.tool()
def search_context(query: str, top_k: int = 5) -> list:
    """Retrieve relevant snippets from the local context/ knowledge base and
    resume. Use these to craft an answer when the profile and history don't
    already have one."""
    return context.search_context(query, top_k)


# --------------------------------------------------------------------------- #
# Per-job resume & cover-letter tailoring (JOB-6; on-demand, not per-apply)
# --------------------------------------------------------------------------- #
@mcp.tool()
def read_resume_template() -> dict:
    """Read the base resume.docx as an ordered, indexed list of paragraphs so you
    can plan a per-job tailoring. Returns {base, paragraph_count, paragraphs:
    [{index, text, style, is_bullet, is_empty}]}. `index` is the stable address
    to pass back in `tailor_resume` edits. Errors clearly if the user hasn't
    added a resume.docx base template yet (drop it in the active profile's
    root). This is
    step 1 of the /tailor-application flow."""
    return tailor.read_resume_template()


@mcp.tool()
def tailor_resume(company: str = "", job_title: str = "", url: str = "",
                  edits: list[dict] | None = None) -> dict:
    """Apply `edits` to a COPY of the base resume.docx and save it as
    resumes/<job-slug>/resume.docx, then export resume.pdf for upload (via MS
    Word). Formatting/layout of the template is preserved — no text-to-PDF
    regeneration.

    `edits` addresses paragraph `index` from read_resume_template (the base is
    re-read, so indices line up):
      {"op": "replace", "index": N, "text": "..."} — rewrite a paragraph, keeping
                                                      its formatting.
      {"op": "delete",  "index": N}                — remove a paragraph.
    Reorder/re-emphasize bullets to surface the most JD-relevant experience by
    `replace`-ing their text in the desired order and `delete`-ing the least
    relevant. Never fabricate experience the base resume doesn't contain.

    Pass the SAME company/job_title/url you use elsewhere for this posting — the
    folder key is shared with applications.json (data._application_key). Returns
    {slug, dir, docx_path, pdf_path, pdf_exported, edits_applied, edit_errors,
    note}."""
    return tailor.tailor_resume(company=company, job_title=job_title, url=url,
                                edits=edits or [])


@mcp.tool()
def get_cover_letter_examples() -> dict:
    """Return the FULL text of the user's own writing from context/ as few-shot
    exemplars — the voice, sentence rhythm, and structure to MATCH when drafting
    a cover letter (not a generic LLM register). Returns three buckets:
    {cover_letters:[{name,type,text}], writing_samples:[...], responses:[...],
    count}. `cover_letters` is the primary style source; `writing_samples`
    (essays/statements) secondary; `responses` are the user's own past answers to
    application questions (e.g. product/behavioral prompts) — voice AND substance
    — and the bucket auto-includes any other free-form prose the user drops into
    context/. The JD supplies the substance; these supply the voice. (Structured
    background/stories/preferences feed substance via search_context instead.)"""
    return tailor.cover_letter_examples()


@mcp.tool()
def save_cover_letter(company: str = "", job_title: str = "", url: str = "",
                      text: str = "", force: bool = False) -> dict:
    """Persist a drafted cover letter under resumes/<job-slug>/: cover_letter.txt
    (body only, for a free-text field) and cover_letter.pdf (a file for a file
    input, with the letterhead rendered by code from the profile + today's date).
    Do NOT type a letterhead in `text`; a typed one is stripped. The letter is
    linted first: if any HARD rule fails (dashes, missing salutation/sign-off,
    typed letterhead, contact details in the text, length outside 270-460
    words, parentheticals, placeholders, paragraph count) nothing is written and
    {saved: False, lint} comes back, so fix and call again. SOFT findings are
    returned in `lint.soft` and the save proceeds. force=True overrides, only with the user's
    explicit OK. Same company/job_title/url identity as tailor_resume. Always
    returns `saved` and `lint`; when saved, also slug, dir, txt_path, pdf_path,
    pdf_exported, render_warnings, note."""
    return tailor.save_cover_letter(company=company, job_title=job_title,
                                    url=url, text=text, force=force)


@mcp.tool()
def lint_text(text: str, kind: str = "answer") -> dict:
    """Style-lint a drafted free-text answer BEFORE filling it. HARD failures
    (em dash or " -- ", and for kind="cover_letter" also missing salutation /
    sign-off, a typed letterhead, contact details in the text, length outside 270-460, parentheticals) must be fixed; SOFT
    warnings (target word band 295-425, canonical opener/closer, "I'm writing to express", long parentheticals,
    unquoted "0-to-1", 3+ sentences in a row starting "I ") are advisory.
    kind: "answer" (default) or "cover_letter". Returns {ok, hard, soft,
    word_count}."""
    return tailor.lint_text(text, kind=kind)


@mcp.tool()
def get_job_artifacts(company: str = "", job_title: str = "", url: str = "") -> dict:
    """Resolve which resume / cover letter to use for a job at APPLY time. Checks
    resumes/<job-slug>/ for tailored artifacts and FALLS BACK to the default
    resume when none exist. Call this at the resume-upload step of apply-to-job /
    apply-batch: pass the returned `resume_path` to upload_resume(path=...), and
    if `has_cover_letter`, use `cover_letter_path` for a cover-letter file input
    and/or `cover_letter_text` for a free-text field. Returns {slug, dir,
    tailored, resume_path, resume_is_tailored, cover_letter_path,
    cover_letter_text, has_cover_letter}. `resume_path` is always usable."""
    return tailor.job_artifacts(company=company, job_title=job_title, url=url)


# --------------------------------------------------------------------------- #
# Search criteria (the strict acceptance bar + default search params)
# --------------------------------------------------------------------------- #
@mcp.tool()
def get_search_criteria() -> dict:
    """Return the job search config from the active profile's criteria.yaml:
    `search_defaults`
    (used to build searches) and `baseline` (the strict acceptance bar —
    titles/seniority, location/remote, salary floor). Load this before searching
    so you know which listings are acceptable."""
    return config.load_search_criteria()


# --------------------------------------------------------------------------- #
# Watchlist discovery (curated companies via public ATS board APIs)
# --------------------------------------------------------------------------- #
_STORE_MAX_AGE_HOURS = 36


@mcp.tool()
async def list_watchlist_postings(query: str = "", limit: int = 0,
                                  max_years: int = 0) -> dict:
    """Product/strategy roles across all watchlist companies, served from the
    LOCAL POSTINGS STORE (data/postings.db, refreshed by `python -m src.refresh`)
    when it's fresh (< 36h), else fetched live from the public ATS APIs.

    Store-backed results are pre-filtered DETERMINISTICALLY against the
    profile's criteria.yaml baseline (titles, excluded seniority, location/remote,
    salary floor — a disclosed range whose top end is below the floor is
    dropped; undisclosed is kept with salary_listed:false) and deduped by
    (company, title). Each posting adds: salary_source ('api'|'jd'|null),
    min_years (ADVISORY — regex from the JD, confirm on finalists), first_seen,
    is_new (appeared in the latest refresh), already_applied (matches
    applications.json). Check `source` ('store'|'live') and `last_refresh`.

    Pass `query` (natural-language keywords) to narrow, `limit` (e.g. 40) to cap
    the payload for inline ranking, and optionally `max_years` to also drop rows
    whose advisory min_years exceeds it. Deep-read finalists with get_postings."""
    if (store.store_age_hours() or 1e9) <= _STORE_MAX_AGE_HOURS:
        return store.list_postings_from_store(query=query or None,
                                              limit=limit or None,
                                              max_years=max_years or None)
    base = config.load_search_criteria().get("baseline", {})
    live = await wl.list_postings(base.get("acceptable_titles"),
                                  base.get("excluded_seniority"),
                                  query=query or None, limit=limit or None)
    live["source"] = "live"
    live["note"] = ("postings store missing or older than 36h — served a live "
                    "fetch instead; run `python -m src.refresh` to rebuild it")
    return live


@mcp.tool()
async def get_posting(url: str) -> dict:
    """Fetch the full job description for one posting URL from its ATS API (for a
    semantic close-call). Returns {found, title, description} or a note to use
    open_job + get_job_text if it can't be resolved. To read several at once,
    prefer get_postings."""
    return await wl.get_posting(url)


@mcp.tool()
async def get_postings(urls: list[str]) -> list:
    """Deep-read MANY postings in one call (concurrent) — full descriptions for a
    list of posting URLs. Use this for the find-jobs finalist set instead of one
    get_posting per URL. Returns [{url, found, title, description}, ...]."""
    return await wl.get_postings(urls)


@mcp.tool()
def list_companies() -> list:
    """Return the watchlist companies ({name, ats, slug}) from watchlist.yaml."""
    return config.load_watchlist()


@mcp.tool()
def add_company(url: str, name: str = "") -> dict:
    """Add a company to the watchlist from a board/careers URL (Greenhouse/Lever/
    Ashby/Workday). Detects the ATS + slug and appends to watchlist.yaml.
    Optionally pass a display `name`.

    A Workday board needs tenant + shard + site, so pass the FULL board URL
    (https://acme.wd5.myworkdayjobs.com/External), not the bare tenant host --
    the site cannot be resolved without a network call this tool does not make.
    `python scripts/workday_bootstrap.py <company name>` finds it for you."""
    return wl.add_company(url, name or None)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
