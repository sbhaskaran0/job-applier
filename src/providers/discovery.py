"""Startup-discovery sources: enumerate candidate companies from company-list
feeds, then confirm each against the public ATS board APIs.

Two source kinds today (config: discovery.yaml):
  - **YC directory** — the yc-oss mirror of YC's public company index. Gives a
    company name + website but no ATS slug, so we guess slug variants from the
    domain/name and probe all three ATSes (the JOB-26 board-sweep technique).
  - **Consider VC boards** — VC portfolio job boards powered by Consider
    (a16z, USV, …). Each job's applyUrl points at the company's REAL ATS, so the
    (ats, slug) falls out exactly — no guessing.

A "probe" fetches a candidate's board via the existing watchlist fetchers and
counts roles passing the job_criteria baseline (store.count_board_baseline), so
a candidate's qualifying number is exactly what it would show once added to the
watchlist. Pure Python, LLM-free — same lane as src/refresh.py.
"""

import asyncio
import re
from urllib.parse import urlparse

import httpx

from .. import config
from . import watchlist as wl

_UA = "Mozilla/5.0 (compatible; job-applier-discovery/1.0)"
_TIMEOUT = httpx.Timeout(30.0)

YC_ALL = "https://yc-oss.github.io/api/companies/all.json"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _domain(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url if "//" in url else "https://" + url).hostname or ""
    return host.lower().removeprefix("www.") or None


def board_url(ats: str, slug: str) -> str:
    """A board URL add_company understands, reconstructed from (ats, slug)."""
    if ats == "greenhouse":
        return f"https://boards.greenhouse.io/{slug}"
    if ats == "ashby":
        return f"https://jobs.ashbyhq.com/{slug}"
    if ats == "lever":
        return f"https://jobs.lever.co/{slug}"
    return slug


def _slug_variants(company: str | None, domain: str | None) -> list[str]:
    """High-probability ATS slug guesses for a company with no known board.
    Ordered most-likely-first; short-circuit probing stops at the first hit."""
    variants: list[str] = []
    if domain:
        variants.append(domain.split(".")[0])
    name = (company or "").lower()
    norm = re.sub(r"[^a-z0-9]", "", name)
    if norm:
        variants.append(norm)
    hyph = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    if hyph:
        variants.append(hyph)
    out: list[str] = []
    for v in variants:
        if v and len(v) >= 2 and v not in out:
            out.append(v)
    return out


# --------------------------------------------------------------------------- #
# source: YC directory
# --------------------------------------------------------------------------- #
async def yc_candidates(client: httpx.AsyncClient, cfg: dict) -> list[dict]:
    r = await client.get(YC_ALL)
    r.raise_for_status()
    hiring_only = cfg.get("hiring_only", True)
    min_team = cfg.get("min_team_size", 0) or 0
    statuses = set(cfg.get("statuses") or [])
    out = []
    for c in r.json():
        if hiring_only and not c.get("isHiring"):
            continue
        if min_team and (c.get("team_size") or 0) < min_team:
            continue
        if statuses and c.get("status") not in statuses:
            continue
        out.append({
            "source": "yc",
            "source_key": c.get("slug") or c.get("name"),
            "company": c.get("name"),
            "domain": _domain(c.get("website")),
            "ats": None, "slug": None,
        })
    return out


# --------------------------------------------------------------------------- #
# source: Consider VC portfolio boards
# --------------------------------------------------------------------------- #
async def consider_candidates(client: httpx.AsyncClient, board: dict,
                              max_pages: int = 200) -> list[dict]:
    """Page a Consider board's jobs; resolve each job's applyUrl to (ats, slug).
    Deduped by (ats, slug) — one company appears once regardless of open-role
    count. Cursor pagination via meta.sequence."""
    base = board["url"].rstrip("/")
    endpoint = f"{base}/api-boards/search-jobs"
    body_base = {"board": {"id": board["board_id"], "isParent": True}, "query": {}}
    seen: dict[tuple, dict] = {}
    seq = None
    for _ in range(max_pages):
        meta = {"size": 100}
        if seq:
            meta["sequence"] = seq
        r = await client.post(endpoint, json={**body_base, "meta": meta})
        if r.status_code != 200:
            break
        d = r.json()
        jobs = d.get("jobs") or []
        if not jobs:
            break
        for j in jobs:
            got = wl.detect_ats_slug(j.get("applyUrl") or "")
            if not got:
                continue
            ats, slug = got
            if ats not in wl._FETCHERS or (ats, slug) in seen:
                continue
            seen[(ats, slug)] = {
                "source": f"consider:{board['name']}",
                "source_key": f"{ats}:{slug}",
                "company": j.get("companyName") or slug,
                "domain": _domain(j.get("companyDomain")),
                "ats": ats, "slug": slug,
            }
        seq = (d.get("meta") or {}).get("sequence")
        if not seq:
            break
    return list(seen.values())


# --------------------------------------------------------------------------- #
# gather + dedupe candidates across all enabled sources
# --------------------------------------------------------------------------- #
async def gather_candidates(cfg: dict) -> tuple[list[dict], list[dict]]:
    """Enumerate every enabled source. Returns (candidates, source_errors).
    Candidates already resolved to (ats, slug) are deduped globally by board;
    unresolved (YC) candidates keep their per-source identity."""
    errors: list[dict] = []
    resolved: dict[tuple, dict] = {}   # (ats, slug) -> candidate
    unresolved: dict[tuple, dict] = {} # (source, source_key) -> candidate

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _UA},
                                 follow_redirects=True) as client:
        tasks = []
        yc = cfg.get("yc") or {}
        if yc.get("enabled"):
            tasks.append(("yc", yc_candidates(client, yc)))
        page_cap = ((cfg.get("limits") or {}).get("max_pages_per_board")) or 200
        for board in cfg.get("consider_boards") or []:
            tasks.append((f"consider:{board.get('name')}",
                          consider_candidates(client, board, page_cap)))

        for (label, coro) in tasks:
            try:
                cands = await coro
            except Exception as e:
                errors.append({"source": label, "reason": f"{type(e).__name__}: {e}"})
                continue
            for c in cands:
                if c.get("ats") and c.get("slug"):
                    resolved.setdefault((c["ats"], c["slug"]), c)
                else:
                    unresolved[(c["source"], c["source_key"])] = c

    # A board resolved by a Consider feed shouldn't also be guess-probed by YC.
    return list(resolved.values()) + list(unresolved.values()), errors


# --------------------------------------------------------------------------- #
# probe one candidate → confirmed board + baseline counts
# --------------------------------------------------------------------------- #
async def _fetch_board(client: httpx.AsyncClient, ats: str, slug: str) -> list[dict] | None:
    """Fetch a board via the watchlist ATS fetchers. None on any failure/404."""
    fetcher = wl._FETCHERS.get(ats)
    if not fetcher:
        return None
    try:
        return await fetcher(client, {"name": slug, "ats": ats, "slug": slug})
    except Exception:
        return None


async def probe_candidate(client: httpx.AsyncClient, cand: dict, baseline: dict,
                          watchset: set, sem: asyncio.Semaphore) -> dict:
    """Confirm a candidate's board and count qualifying roles. For resolved
    candidates it fetches the known board; for unresolved ones it tries slug
    variants across the ATSes, short-circuiting on the first live board."""
    from .. import store

    async def probe(ats: str, slug: str):
        async with sem:
            return await _fetch_board(client, ats, slug)

    result = {**cand, "status": "unresolved", "active_count": 0,
              "title_matched": 0, "qualifying": 0, "on_watchlist": 0}

    if cand.get("ats") and cand.get("slug"):
        postings = await probe(cand["ats"], cand["slug"])
        attempts = [(cand["ats"], cand["slug"], postings)]
    else:
        attempts = []
        for slug in _slug_variants(cand.get("company"), cand.get("domain")):
            hit = None
            for ats in ("greenhouse", "ashby", "lever"):
                postings = await probe(ats, slug)
                if postings is not None and len(postings) > 0:
                    hit = (ats, slug, postings)
                    break
            if hit:
                attempts = [hit]
                break

    for ats, slug, postings in attempts:
        if postings is None:
            continue
        active, tm, q = store.count_board_baseline(postings, baseline)
        result.update({"ats": ats, "slug": slug, "status": "confirmed",
                       "active_count": active, "title_matched": tm, "qualifying": q,
                       "on_watchlist": int((ats, slug) in watchset)})
        return result

    # nothing lived — keep any resolved (ats,slug) but mark it dead vs unresolved
    result["status"] = "dead" if cand.get("ats") else "unresolved"
    return result
