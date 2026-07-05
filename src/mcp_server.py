"""job-applier MCP server.

Exposes the browser, data, knowledge, and discovery components as tools that
Claude Code calls. Claude Code is the agentic loop; these tools are the hands
and the memory. Run via:  python -m src.mcp_server   (stdio transport).
"""

from mcp.server.fastmcp import FastMCP

from . import browser, config, context, data
from .providers import watchlist as wl

mcp = FastMCP("job-applier")


# --------------------------------------------------------------------------- #
# Browser tools (ATS-agnostic; act on the one live session)
# --------------------------------------------------------------------------- #
@mcp.tool()
async def open_job(url: str) -> dict:
    """Open a job posting / application page in the live browser (non-headless,
    so the user can watch). One-shot: returns {title, detected_ats, resume_synced,
    intervention:{blocked,signals,message}, fields:[...]} — i.e. it ALSO runs the
    CAPTCHA/intervention check and reads the form, so you don't need separate
    check_for_intervention / read_form calls right after opening. If
    intervention.blocked is true, stop and ask the user to clear it. Re-call
    read_form only if the page later navigates/changes."""
    return await browser.session.open_job(url)


@mcp.tool()
async def read_form() -> list:
    """Read the live page and all iframes; return every fillable field as
    {index, kind, label, options, required, current_value, group}. Works on any
    ATS with no per-site rules. Re-call after navigation. Use the returned
    index with fill_field / upload_resume / submit_application."""
    return await browser.session.read_form()


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
async def upload_resume(index: int = -1, path: str = "") -> dict:
    """Attach the resume. Pass `index` >= 0 to use a specific file field from
    read_form; omit it (or -1) to auto-locate a file input anywhere on the page,
    including hidden ones behind an "Attach"/dropzone widget (Greenhouse/Ashby),
    preferring a resume/CV input over a cover-letter one. Uses the project resume
    file (resume.pdf/.docx if present, else resume.txt) unless `path` is given."""
    return await browser.session.upload_resume(index if index >= 0 else None,
                                               path or None)


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
async def get_job_text() -> str:
    """Return the visible page text (use it to read the job description for a
    fit assessment)."""
    return await browser.session.get_job_text()


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
def resolve_fields(fields: list[dict], company: str = "") -> list:
    """Batch-resolve MANY form fields in ONE call — the fast path that replaces
    the per-field get_profile_field → search_history → search_context loop.
    `fields` is [{index, label}, ...]; pass `company` (the employer you're
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
      - source "context": no stored value → `context` snippets (+ `history_top`)
        to CRAFT from; these are the low-confidence ones to draft and gate for
        user approval.
    Fill the profile and confidence-"high" history rows with fill_many; craft/
    adapt the rest, get approval where needed, then fill_many those too."""
    comp = (company or "").strip().lower()
    out = []
    for f in fields:
        idx, label = f.get("index"), f.get("label", "")
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
                        "confidence": "review", "alternatives": hist})
            continue
        out.append({"index": idx, "label": label, "source": "context",
                    "confidence": "craft", "history_top": hist[:2],
                    "context": context.search_context(label, top_k=3)})
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
# Search criteria (the strict acceptance bar + default search params)
# --------------------------------------------------------------------------- #
@mcp.tool()
def get_search_criteria() -> dict:
    """Return the job search config from job_criteria.yaml: `search_defaults`
    (used to build searches) and `baseline` (the strict acceptance bar —
    titles/seniority, location/remote, salary floor). Load this before searching
    so you know which listings are acceptable."""
    return config.load_search_criteria()


# --------------------------------------------------------------------------- #
# Watchlist discovery (curated companies via public ATS board APIs)
# --------------------------------------------------------------------------- #
@mcp.tool()
async def list_watchlist_postings(query: str = "", limit: int = 0) -> dict:
    """Pull live product/strategy roles across all watchlist companies (public
    Greenhouse/Lever/Ashby APIs), pre-filtered to the acceptable titles and
    seniority from job_criteria.yaml and deduped by (company, title). Returns
    {postings:[{company,title,location,remote,salary_min,salary_max,url,snippet}],
    total_scanned, matched, returned, companies_failed}.

    Pass `query` (natural-language keywords, e.g. "fintech product strategy") to
    narrow the corpus and `limit` (e.g. 40) to cap the payload so you can rank it
    inline without a subagent. `matched` = all roles that passed the strict
    filter; `returned` = after query/limit. Deep-read finalists with
    get_postings (batch) or get_posting (single)."""
    base = config.load_search_criteria().get("baseline", {})
    return await wl.list_postings(base.get("acceptable_titles"),
                                  base.get("excluded_seniority"),
                                  query=query or None, limit=limit or None)


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
    Ashby). Detects the ATS + slug and appends to watchlist.yaml. Optionally pass
    a display `name`."""
    return wl.add_company(url, name or None)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
