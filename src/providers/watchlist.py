"""Watchlist provider: pull live postings from companies' public ATS board APIs.

Keyless and free — Greenhouse/Lever/Ashby publish these JSON endpoints. Fetches
all watchlist companies concurrently, normalizes each posting to a common shape,
and (for the search corpus) pre-filters to product/strategy titles so the result
set is small enough for Claude to rank semantically.
"""

import asyncio
import html
import re
from urllib.parse import urlparse, parse_qs

import httpx

from .. import config

_UA = "Mozilla/5.0 (compatible; job-applier-watchlist/1.0)"
_TIMEOUT = httpx.Timeout(20.0)

# Default title prefilter for the search corpus: PHRASES (not bare words like
# "product", which matches sales/eng titles). The MCP tool overrides these with
# the criteria's acceptable_titles + excluded_seniority.
DEFAULT_KEYWORDS = [
    "product manager", "product management", "product lead", "head of product",
    "group product manager", "principal product", "product operations",
    "product strategy", "corporate strategy", "business strategy",
    "strategy & operations", "strategy and operations", "strategy & ops",
    "chief of staff", "bizops", "business operations",
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clean_html(raw: str) -> str:
    if not raw:
        return ""
    # Greenhouse content is HTML-escaped HTML (e.g. "&lt;p&gt;")
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return " ".join(text.split())


def _is_remote_text(location: str) -> bool:
    return "remote" in (location or "").lower()


# --------------------------------------------------------------------------- #
# per-ATS fetch + normalize
# --------------------------------------------------------------------------- #
async def _fetch_greenhouse(client: httpx.AsyncClient, company: dict) -> list[dict]:
    slug = company["slug"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    r = await client.get(url)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = (j.get("location") or {}).get("name", "") or ""
        desc = _clean_html(j.get("content", ""))
        out.append({
            "company": company["name"], "ats": "greenhouse",
            "title": j.get("title", ""), "location": loc,
            "remote": _is_remote_text(loc),
            "salary_min": None, "salary_max": None,  # not structured on Greenhouse
            "posted": j.get("updated_at"),
            # embed URL renders the fillable application form directly
            "url": f"https://boards.greenhouse.io/embed/job_app?for={slug}&token={j.get('id')}",
            "description": desc,
        })
    return out


def _ashby_salary(job: dict):
    comp = job.get("compensation") or {}
    for c in comp.get("summaryComponents") or []:
        if c.get("compensationType") == "Salary":
            return c.get("minValue"), c.get("maxValue")
    return None, None


async def _fetch_ashby(client: httpx.AsyncClient, company: dict) -> list[dict]:
    slug = company["slug"]
    url = (f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
           "?includeCompensation=true")
    r = await client.get(url)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        smin, smax = _ashby_salary(j)
        out.append({
            "company": company["name"], "ats": "ashby",
            "title": j.get("title", ""), "location": j.get("location", "") or "",
            "remote": bool(j.get("isRemote")),
            "salary_min": smin, "salary_max": smax,
            "posted": j.get("publishedAt"),
            "url": j.get("applyUrl") or j.get("jobUrl"),
            "description": j.get("descriptionPlain", "") or "",
        })
    return out


async def _fetch_lever(client: httpx.AsyncClient, company: dict) -> list[dict]:
    slug = company["slug"]
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = await client.get(url)
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        loc = cats.get("location", "") or ""
        out.append({
            "company": company["name"], "ats": "lever",
            "title": j.get("text", ""), "location": loc,
            "remote": _is_remote_text(loc) or (cats.get("commitment", "") or "").lower() == "remote",
            "salary_min": None, "salary_max": None,
            "posted": j.get("createdAt"),
            "url": j.get("applyUrl") or j.get("hostedUrl"),
            "description": _clean_html(j.get("descriptionPlain") or j.get("description", "")),
        })
    return out


_FETCHERS = {"greenhouse": _fetch_greenhouse, "ashby": _fetch_ashby, "lever": _fetch_lever}


# --------------------------------------------------------------------------- #
# aggregate
# --------------------------------------------------------------------------- #
async def fetch_all_with_status() -> dict:
    """Fetch every watchlist company concurrently. Returns
    {"postings": [...], "errors": [{company, reason}]}."""
    companies = config.load_watchlist()
    postings: list[dict] = []
    errors: list[dict] = []

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _UA},
                                 follow_redirects=True) as client:
        async def one(co):
            fetcher = _FETCHERS.get((co.get("ats") or "").lower())
            if not fetcher:
                return co["name"], None, f"unknown ats '{co.get('ats')}'"
            try:
                return co["name"], await fetcher(client, co), None
            except Exception as e:  # network / 404 / shape change
                return co["name"], None, f"{type(e).__name__}: {e}"

        for name, rows, err in await asyncio.gather(*(one(c) for c in companies)):
            if err:
                errors.append({"company": name, "reason": err})
            else:
                postings.extend(rows)
    return {"postings": postings, "errors": errors}


async def fetch_all() -> list[dict]:
    """All normalized postings across the watchlist (no title filter)."""
    return (await fetch_all_with_status())["postings"]


def prefilter(postings: list[dict], keywords: list[str] | None = None,
              exclude: list[str] | None = None) -> list[dict]:
    """Keep postings whose title contains a positive phrase and none of the
    `exclude` terms (e.g. excluded seniority like Director/VP/Associate). Shrinks
    thousands of roles to the product/strategy-relevant set Claude ranks."""
    kws = [k.lower() for k in (keywords or DEFAULT_KEYWORDS)]
    neg = [x.lower() for x in (exclude or [])]
    out = []
    for p in postings:
        t = (p["title"] or "").lower()
        if any(k in t for k in kws) and not any(x in t for x in neg):
            out.append(p)
    return out


def _snippet(text: str, n: int = 600) -> str:
    return text[:n] + ("…" if len(text) > n else "")


def _qtokens(query: str | None) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9 ]+", " ", (query or "").lower()).split() if t]


def _matches_query(p: dict, qtokens: list[str]) -> bool:
    """Lenient keyword narrowing so the corpus fits inline — real semantic
    ranking is still Claude's job over what this returns."""
    if not qtokens:
        return True
    hay = f"{p['title']} {p['company']} {p.get('location', '')} {p.get('snippet', '')}".lower()
    return any(t in hay for t in qtokens)


async def list_postings(keywords: list[str] | None = None,
                        exclude: list[str] | None = None,
                        query: str | None = None,
                        limit: int | None = None,
                        snippet_chars: int = 140) -> dict:
    """Corpus for semantic search: prefiltered, deduped, lightweight postings +
    status. Titles + company + location + salary + a short snippet give Claude
    enough to rank semantically; it deep-reads finalists via get_posting.

    `query` keyword-narrows the corpus and `limit` caps how many are returned,
    keeping the payload small enough to rank inline (no subagent detour). Dedup
    is by (company, title) so one role posted across many cities collapses to a
    single entry. `matched` counts everything that passed the strict title/
    seniority prefilter; `returned` is what survived query/limit.
    """
    data = await fetch_all_with_status()
    filtered = prefilter(data["postings"], keywords, exclude)

    seen, light = set(), []
    for p in filtered:
        key = (p["company"], p["title"].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        light.append({
            "company": p["company"], "title": p["title"], "location": p["location"],
            "remote": p["remote"], "salary_min": p["salary_min"],
            "salary_max": p["salary_max"], "url": p["url"],
            "snippet": _snippet(p["description"], snippet_chars),
        })

    matched = len(light)
    qtokens = _qtokens(query)
    if qtokens:
        light = [p for p in light if _matches_query(p, qtokens)]
    if limit and limit > 0:
        light = light[:limit]
    return {
        "postings": light,
        "total_scanned": len(data["postings"]),
        "matched": matched,
        "returned": len(light),
        "companies_failed": data["errors"],
    }


# --------------------------------------------------------------------------- #
# single-posting deep read
# --------------------------------------------------------------------------- #
async def get_posting(url: str) -> dict:
    """Full description for a single posting, fetched from its ATS API by parsing
    the ATS + ids out of `url`. Falls back to a note if it can't be resolved
    (use open_job + get_job_text in that case)."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _UA},
                                 follow_redirects=True) as client:
        if "greenhouse" in host:
            q = parse_qs(parsed.query)
            slug = (q.get("for") or [None])[0]
            token = (q.get("token") or q.get("gh_jid") or [None])[0]
            if slug and token:
                r = await client.get(
                    f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{token}")
                if r.status_code == 200:
                    j = r.json()
                    return {"url": url, "found": True, "title": j.get("title"),
                            "description": _clean_html(j.get("content", ""))}
        elif "ashbyhq" in host:
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2:
                slug, jid = parts[0], parts[1]
                r = await client.get(
                    f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
                    "?includeCompensation=true")
                if r.status_code == 200:
                    for j in r.json().get("jobs", []):
                        if j.get("id") == jid:
                            return {"url": url, "found": True, "title": j.get("title"),
                                    "description": j.get("descriptionPlain", "")}
        elif "lever" in host:
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2:
                slug, jid = parts[0], parts[1]
                r = await client.get(
                    f"https://api.lever.co/v0/postings/{slug}/{jid}?mode=json")
                if r.status_code == 200:
                    j = r.json()
                    return {"url": url, "found": True, "title": j.get("text"),
                            "description": _clean_html(
                                j.get("descriptionPlain") or j.get("description", ""))}
    return {"url": url, "found": False,
            "note": "Could not resolve via ATS API; use open_job + get_job_text."}


async def get_postings(urls: list[str]) -> list[dict]:
    """Deep-read several postings concurrently — one round-trip for the whole
    finalist set instead of one get_posting call per URL."""
    if not urls:
        return []
    return list(await asyncio.gather(*(get_posting(u) for u in urls)))


# --------------------------------------------------------------------------- #
# watchlist management
# --------------------------------------------------------------------------- #
def detect_ats_slug(url: str):
    """Parse (ats, slug) from a board/careers URL, or None."""
    p = urlparse(url if "//" in url else "https://" + url)
    host = (p.hostname or "").lower()
    parts = [seg for seg in p.path.split("/") if seg]
    q = parse_qs(p.query)
    if "greenhouse" in host:
        if "embed" in parts and q.get("for"):
            return "greenhouse", q["for"][0]
        # boards.greenhouse.io/{slug} or job-boards.greenhouse.io/{slug}
        if parts:
            return "greenhouse", parts[0]
    if "lever.co" in host and parts:
        return "lever", parts[0]
    if "ashbyhq.com" in host and parts:
        return "ashby", parts[0]
    return None


def add_company(url: str, name: str | None = None) -> dict:
    """Detect the ATS + slug from a board/careers URL and append the company to
    watchlist.yaml (preserving the file's comments by appending text)."""
    got = detect_ats_slug(url)
    if not got:
        return {"status": "error",
                "reason": "Could not detect ATS/slug. Supported: greenhouse, lever, ashby."}
    ats, slug = got
    existing = config.load_watchlist()
    if any(c.get("slug") == slug and c.get("ats") == ats for c in existing):
        return {"status": "exists", "ats": ats, "slug": slug}
    display = name or slug.replace("-", " ").replace("_", " ").title()
    block = f"  - name: {display}\n    ats: {ats}\n    slug: {slug}\n"
    with open(config.WATCHLIST_PATH, "a", encoding="utf-8") as f:
        f.write(block)
    return {"status": "added", "name": display, "ats": ats, "slug": slug}
