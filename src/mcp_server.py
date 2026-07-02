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
    so the user can watch). Returns page title and the detected ATS. Call
    read_form() next to see the fields."""
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
    the option by label or value), and radio/checkbox (pass 'yes'/'no')."""
    return await browser.session.fill_field(index, value)


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
    {blocked, signals, message}. If blocked, STOP and ask the user to complete
    it in the visible browser window, then continue once they confirm."""
    return await browser.session.detect_blockers()


@mcp.tool()
async def get_job_text() -> str:
    """Return the visible page text (use it to read the job description for a
    fit assessment)."""
    return await browser.session.get_job_text()


@mcp.tool()
async def submit_application(index: int = -1) -> dict:
    """DESTRUCTIVE: submit the application. Only call after the user has
    explicitly confirmed. If `index` >= 0 that field is clicked, otherwise a
    submit button is located automatically."""
    return await browser.session.submit_application(index if index >= 0 else None)


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
def search_history(question: str, top_k: int = 5) -> list:
    """Find the closest past answers to a question from history.json, ranked by
    similarity score. Use a high-scoring past answer (adapt it) before crafting
    a new one."""
    return data.search_history(question, top_k)


@mcp.tool()
def save_answer(question: str, answer: str) -> dict:
    """Persist an approved answer so it is reused next time. Call this after the
    user approves a crafted answer."""
    return data.save_answer(question, answer)


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
async def list_watchlist_postings() -> dict:
    """Pull live product/strategy roles across all watchlist companies (public
    Greenhouse/Lever/Ashby APIs), pre-filtered to the acceptable titles and
    seniority from job_criteria.yaml and deduped. Returns
    {postings:[{company,title,location,remote,salary_min,salary_max,url,snippet}],
    total_scanned, matched, companies_failed}. This is the corpus to rank
    semantically against the user's query + the strict criteria; deep-read
    finalists with get_posting."""
    base = config.load_search_criteria().get("baseline", {})
    return await wl.list_postings(base.get("acceptable_titles"),
                                  base.get("excluded_seniority"))


@mcp.tool()
async def get_posting(url: str) -> dict:
    """Fetch the full job description for one posting URL from its ATS API (for a
    semantic close-call). Returns {found, title, description} or a note to use
    open_job + get_job_text if it can't be resolved."""
    return await wl.get_posting(url)


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
