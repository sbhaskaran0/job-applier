"""Workday sourcing provider: pull live postings from a tenant's public
myworkdayjobs board via the same keyless JSON API its own SPA calls.

Shape of the API (verified live 2026-09-09):

    POST https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
         {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    GET  https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}

No auth, no cookies, no CSRF token. Three quirks drive the whole design:

* `limit` maxes out at 20 (50/100 silently return an empty page),
* `offset` is clamped at 2000 — past that the API replays page 0 byte for byte,
  so the only way to detect the wall is to remember page 0's first externalPath,
* `total` is only trustworthy on the first page (deep offsets report 0).

The 2000-row wall is why a big board is fetched by PARTITION rather than by
paging. The board's own jobFamilyGroup facet reports a count per family, and
those counts are the true numbers: NVIDIA's sum to 2618 while `total` admits
only 2000. Every family is individually under the wall (NVIDIA's largest is
Engineering at 1723), so paging each family separately and unioning by job_id
reconstructs the COMPLETE board — including the 618 rows no amount of offset
paging can reach. The rule:

* total <  2000  -> page the FULL list, no facets ("full"). Complete, and the
  probe page is reused as page 0, so it costs at most 100 POSTs.
* total >= 2000  -> page EVERY jobFamilyGroup value separately and union them
  ("partitioned"). `total` is itself clamped at 2000, so a reported 2000 means
  "2000 or more, unknowable" and has to be treated as walled. Measured on
  NVIDIA: 141 POSTs, 2616 rows and 37 title matches, versus 100 POSTs, 2000
  rows and 30 matches for a full pass that also could not reach its PM roles
  (filed under Engineering) unless they sat before position 2000.
* a family that is ITSELF at or over the wall pages to its own wall and the
  board is flagged "partitioned_truncated".
* total >= 2000 with no jobFamilyGroup facet at all -> nothing to partition by,
  so page to the wall and flag it ("full_truncated").

`truncated` means rows are genuinely unreachable — a family hit its own wall,
the request budget cut the tail off, or the family counts don't add up to the
board. A clean partition is COMPLETE and reports False, which matters beyond
bookkeeping: store.refresh_from_fetch skips its removal pass for any board
reporting truncated, since an incomplete fetch cannot prove a posting is gone.

LAST_RUN_NOTES[slug] records the strategy, the request count, the per-family
breakdown (count vs rows actually fetched) and board_total vs partition_total —
the gap between those two IS the hidden-rows number.

The list payload carries no description, no salary and no real date, so every
posting needs a second GET. Those are FETCH-ONCE: a run asks the store which
job_ids it already has text for and re-fetches only the rest, so the steady
state is a handful of GETs (mature boards turn over ~1.1%/day) instead of one
per posting. Hydration is gated on MISSING TEXT, never on the title — that
backfills old rows, covers new ones, and self-heals if acceptable_titles is
widened later. A reused row carries its stored description, location AND posted
forward: description is part of the content hash, and location/posted exist
only in the detail payload, so re-emitting the list values would overwrite good
data with "2 Locations" and a null date.

A board is identified by the slug "tenant:shard:site" (e.g. "workday:wd5:Workday").
Tenant is case-SENSITIVE (wrong casing -> HTTP 502); site is case-insensitive.
"""

import asyncio
import random
import re
from urllib.parse import urlparse

import httpx

from .. import config

# Kept in sync with watchlist.py by value, not by import: watchlist imports this
# module at module level, so importing it back here would be circular.
_UA = "Mozilla/5.0 (compatible; job-applier-watchlist/1.0)"
_TIMEOUT = httpx.Timeout(20.0)

# Content-Type is REQUIRED on the list POST (omit it and Workday answers 500);
# httpx sets it whenever we pass json=. Accept-Language keeps postedOn/timeType
# in English. Note: robots.txt 406s if Accept is application/json, so the
# robots helper sends its own headers.
_JSON_HEADERS = {"Accept": "application/json", "Accept-Language": "en-US",
                 "User-Agent": _UA}
_TEXT_HEADERS = {"Accept": "text/plain,*/*", "User-Agent": _UA}

_PAGE_LIMIT = 20        # hard server cap; larger values return an empty page
_OFFSET_CLAMP = 2000    # offsets past this replay page 0
# Boards under the clamp are paged in full; at or above it they are partitioned
# by jobFamilyGroup. The test is `total >= _PARTITION_AT` because `total` is
# clamped at 2000 too, so a reported 2000 means "2000 or more, unknowable".
_PARTITION_AT = _OFFSET_CLAMP

_TENANT_CONCURRENCY = 5   # detail GETs in flight per board
_GLOBAL_CONCURRENCY = 10  # detail GETs in flight across the whole process

# Shards that actually resolve, most-common-first (wd1 is ~69% of tenants).
# wd2 / wd101 / wd106 / wd500 do not exist.
SHARDS = ["wd1", "wd5", "wd3", "wd12", "wd103", "wd501", "wd503", "wd108",
          "wd10", "wd105", "wd104", "wd107", "wd115", "wd502", "wd504"]

# HTTP code -> reason string surfaced in companies_failed.
_STATUS_REASONS = {
    200: "ok",
    404: "wrong_site",
    422: "wrong_shard",     # or not a Workday tenant at all
    401: "internal",        # auth-gated site
    403: "blocked",         # bot-blocked or private
    410: "retired",
    502: "tenant_casing",   # tenant path is case-sensitive
}

# Cost ceiling for one partitioned board, in list POSTs. ~8000 rows: NVIDIA
# (2618 rows across 15 families) costs 141, so this leaves a lot of headroom
# while still refusing to spend thousands of requests on an outlier tenant.
# Over budget -> the biggest families are paged, the rest recorded as skipped
# and the board marked partitioned_truncated.
_MAX_PARTITION_REQUESTS = 400

# Detail GETs per board per run. Descriptions are fetched ONCE and carried
# forward, so this only ever bounds a backfill: 3000 lets the biggest board seen
# (NVIDIA, 2616 rows) finish in a single pass, while a pathological tenant is
# spread over several runs instead of monopolising one. Measured ceiling is
# ~18.5 GETs/sec at concurrency 10, so a full 3000 is ~3 minutes worst case.
_MAX_HYDRATE_PER_RUN = 3000

# Detail GETs fail transiently under sustained load — a 12k-request backfill lost
# ~43% of them, and 40/40 of the losers returned 200 when re-probed minutes
# later. So: retry the transient ones, and stop early if a tenant is clearly
# throttling us rather than burning the run on doomed retries.
_RETRY_ATTEMPTS = 3          # one try + two retries
_RETRY_BASE_DELAY = 0.5      # seconds, doubled per attempt, plus jitter
_BREAKER_THRESHOLD = 15      # consecutive failures before a board gives up

# Soft, per-run diagnostics keyed by slug — nothing here is fatal, so fetch_workday
# records instead of raising. Read it after a fetch (or after refresh) to see how
# a board was fetched, what it cost, how much got hydrated, and whether the
# 2000-row wall bit. Overwritten on every run of the same slug.
LAST_RUN_NOTES: dict[str, dict] = {}


class WorkdayError(Exception):
    """str(e) is the taxonomy reason (see classify_status). `status` carries the
    HTTP code when there was one, so the retry path can tell a transient 429/5xx
    from a permanent 404."""

    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.status = status


class _Deferred(Exception):
    """Raised in place of a request the circuit breaker refused to make."""


# --------------------------------------------------------------------------- #
# semaphores (created lazily: an asyncio.Semaphore binds to the loop that is
# running when it is constructed, so one built at import time is useless — and
# actively wrong — under a second asyncio.run()).
# --------------------------------------------------------------------------- #
_SEMAPHORES: dict[tuple, asyncio.Semaphore] = {}


def _sem(key: str, limit: int) -> asyncio.Semaphore:
    loop_key = (id(asyncio.get_running_loop()), key, limit)
    sem = _SEMAPHORES.get(loop_key)
    if sem is None:
        sem = _SEMAPHORES[loop_key] = asyncio.Semaphore(limit)
    return sem


# --------------------------------------------------------------------------- #
# pure helpers (no network)
# --------------------------------------------------------------------------- #
_LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")
_REQ_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{1,63}$")


def parse_workday_url(url: str) -> dict | None:
    """{"tenant","shard","site","external_path","host"} for any myworkdayjobs
    URL, else None.

    Handles the board root, a /en-US/ (or /de-DE/, /en/) locale prefix, a full
    /job/... detail path, and the /wday/cxs/... API form. A host with no site
    path still parses — site comes back "" so the tenant/shard are usable for
    robots-based site enumeration.
    """
    if not url:
        return None
    p = urlparse(url if "//" in url else "https://" + url)
    host = (p.hostname or "").lower()
    labels = host.split(".")
    if len(labels) != 4 or labels[2:] != ["myworkdayjobs", "com"]:
        return None
    tenant, shard = labels[0], labels[1]
    if not tenant or not shard:
        return None

    parts = [seg for seg in p.path.split("/") if seg]
    # API form: /wday/cxs/{tenant}/{site}/...
    if len(parts) >= 4 and parts[0] == "wday" and parts[1] == "cxs":
        site, rest = parts[3], parts[4:]
    else:
        site, rest = "", []
        for i, seg in enumerate(parts):
            if _LOCALE_RE.match(seg):
                continue
            site, rest = seg, parts[i + 1:]
            break
    # Everything from the /job/ segment on is the externalPath the API wants.
    external_path = None
    if rest and rest[0].lower() == "job":
        external_path = "/" + "/".join(rest)
    return {"tenant": tenant, "shard": shard, "site": site,
            "external_path": external_path, "host": host}


def make_slug(tenant: str, shard: str, site: str) -> str:
    return f"{tenant}:{shard}:{site}"


def split_slug(slug: str) -> tuple[str, str, str]:
    """("tenant","shard","site"); ValueError on anything else."""
    parts = (slug or "").split(":")
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise ValueError(f"malformed workday slug {slug!r} "
                         "(expected 'tenant:shard:site')")
    return parts[0].strip(), parts[1].strip(), parts[2].strip()


def board_url(slug: str) -> str:
    tenant, shard, site = split_slug(slug)
    return f"https://{tenant}.{shard}.myworkdayjobs.com/{site}"


def _api_base(tenant: str, shard: str, site: str) -> str:
    return (f"https://{tenant}.{shard}.myworkdayjobs.com"
            f"/wday/cxs/{tenant}/{site}")


def classify_status(code: int) -> str:
    """HTTP code -> taxonomy reason."""
    return _STATUS_REASONS.get(code, f"http_{code}")


def job_id_for(item: dict) -> str:
    """Stable per-posting id. bulletFields[0] is the requisition id on every
    tenant seen so far ("JR-0108977", "R-50862-1", "2026-0013526"); when it is
    missing or clearly not an id, fall back to the externalPath tail (which is
    "{Title-slug}_{reqId}" and just as stable)."""
    bullets = item.get("bulletFields") or []
    if bullets:
        first = str(bullets[0] or "").strip()
        if _REQ_ID_RE.match(first) and any(ch.isdigit() for ch in first):
            return first
    path = (item.get("externalPath") or "").rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def _detail_info(detail: dict | None) -> dict:
    """Accept either the raw detail response or its jobPostingInfo sub-dict."""
    if not detail:
        return {}
    return detail.get("jobPostingInfo") or detail


def _location_text(item: dict, info: dict) -> str:
    """Every city, joined — locations.py's normalization has to see them all,
    and the list row's locationsText is often a useless "2 Locations"."""
    if info:
        names = [info.get("location") or ""]
        for extra in info.get("additionalLocations") or []:
            names.append(extra.get("descriptor", "") if isinstance(extra, dict)
                         else str(extra or ""))
        seen, out = set(), []
        for name in (n.strip() for n in names):
            if name and name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
        if out:
            return "; ".join(out)
    return item.get("locationsText") or ""


def normalize(company: dict, item: dict, detail: dict | None) -> dict:
    """One list row (+ its optional hydrated detail) as the common posting dict
    every other fetcher returns. `detail` may be the full detail response or
    just its jobPostingInfo."""
    # deferred: watchlist imports this module, so importing it at module level
    # would be a circular import (same idiom as discovery.probe_candidate).
    from .watchlist import _clean_html

    slug = company.get("slug", "") or ""
    tenant, shard, site = split_slug(slug)
    info = _detail_info(detail)
    location = _location_text(item, info)
    external_path = item.get("externalPath") or ""
    url = info.get("externalUrl") or (
        f"https://{tenant}.{shard}.myworkdayjobs.com/{site}{external_path}")
    return {
        "company": company.get("name", ""), "ats": "workday", "slug": slug,
        "job_id": job_id_for(item),
        "title": info.get("title") or item.get("title", "") or "",
        "location": location,
        "remote": "remote" in (location or "").lower(),
        # Workday exposes no structured comp anywhere in either payload.
        "salary_min": None, "salary_max": None,
        # startDate is the only real date; postedOn is a localized string
        # ("Posted 30+ Days Ago") the store cannot parse.
        "posted": info.get("startDate"),
        "url": url,
        "description": _clean_html(info.get("jobDescription", "")) if info else "",
    }


def paginate_done(page: list, offset: int, total, first_path_page0: str | None) -> bool:
    """Stop after this page? True on an empty/short page, on reaching `total`
    (only ever the value captured at offset 0), on the 2000-offset clamp, and
    when the API replays page 0's first posting (the clamp's tell)."""
    if not page:
        return True
    if offset > 0 and first_path_page0 and \
            (page[0].get("externalPath") or "") == first_path_page0:
        return True
    if len(page) < _PAGE_LIMIT:
        return True
    if total and offset + len(page) >= total:
        return True
    return offset + len(page) >= _OFFSET_CLAMP


def family_values(facets: list) -> list[dict]:
    """Every jobFamilyGroup value the board advertises, as
    [{"id","descriptor","count"}]. Empty when the tenant exposes no Job Category
    facet — the one case a walled board cannot be partitioned.

    These counts are the board's TRUE size: NVIDIA's sum to ~2614 while `total`
    admits only 2000, and the gap is exactly the rows offset paging can't reach.
    """
    out: list[dict] = []
    for facet in facets or []:
        if facet.get("facetParameter") != "jobFamilyGroup":
            continue
        for value in facet.get("values") or []:
            if value.get("id"):
                out.append({"id": value["id"],
                            "descriptor": value.get("descriptor") or value["id"],
                            "count": int(value.get("count") or 0)})
    return out


def _pages_for(count: int) -> int:
    """List POSTs needed to page `count` rows — capped, because a family over
    the wall can only ever yield _OFFSET_CLAMP rows however many it claims."""
    reachable = min(max(count, 0), _OFFSET_CLAMP)
    return max(1, -(-reachable // _PAGE_LIMIT))


# --------------------------------------------------------------------------- #
# network
# --------------------------------------------------------------------------- #
async def _list_page(client, url: str, applied_facets: dict | None,
                     offset: int) -> tuple[list[dict], dict]:
    """One page of the list endpoint: (postings, full response body)."""
    payload = {"appliedFacets": applied_facets or {},
               "limit": _PAGE_LIMIT, "offset": offset, "searchText": ""}
    r = await client.post(url, json=payload, headers=_JSON_HEADERS)
    if r.status_code != 200:
        raise WorkdayError(classify_status(r.status_code), r.status_code)
    data = r.json() or {}
    return data.get("jobPostings") or [], data


async def list_jobs(client, tenant: str, shard: str, site: str,
                    applied_facets: dict | None = None,
                    max_offset: int = _OFFSET_CLAMP,
                    page0: tuple[list[dict], dict] | None = None
                    ) -> tuple[list[dict], dict]:
    """Page the board's list endpoint. Returns (raw postings, meta) where meta
    is {"total": int|None, "truncated": bool, "facets": [...], "requests": int};
    `total` and `facets` are read once, at offset 0, because deep offsets lie.

    `page0` hands back an already-fetched offset-0 (postings, body) so a caller
    that probed for `total`/`facets` doesn't pay for that page twice. It must
    have been fetched with the same `applied_facets`.
    """
    url = _api_base(tenant, shard, site) + "/jobs"
    rows: list[dict] = []
    offset, total, facets, first_path, truncated = 0, None, [], None, False
    requests = 0

    while True:
        if offset == 0 and page0 is not None:
            page, data = page0
        else:
            page, data = await _list_page(client, url, applied_facets, offset)
            requests += 1
        if offset == 0:
            total = data.get("total")
            facets = data.get("facets") or []
            first_path = (page[0].get("externalPath") or "") if page else None
        elif first_path and page and \
                (page[0].get("externalPath") or "") == first_path:
            truncated = True          # offset clamp: the API replayed page 0
            break
        rows.extend(page)
        if paginate_done(page, offset, total, first_path):
            break
        offset += _PAGE_LIMIT
        if offset >= max_offset:
            truncated = True
            break

    if total and len(rows) < total:
        truncated = True
    if len(rows) >= _OFFSET_CLAMP:
        truncated = True    # exactly at the wall: more rows may exist, unknowably
    return rows, {"total": total, "truncated": truncated, "facets": facets,
                  "requests": requests}


async def fetch_detail(client, tenant: str, shard: str, site: str,
                       external_path: str) -> dict:
    """The posting's jobPostingInfo (description, real startDate, canonical
    externalUrl, every location). Raises WorkdayError on a non-200."""
    url = _api_base(tenant, shard, site) + (external_path or "")
    r = await client.get(url, headers=_JSON_HEADERS)
    if r.status_code != 200:
        raise WorkdayError(classify_status(r.status_code), r.status_code)
    return (r.json() or {}).get("jobPostingInfo") or {}


_SITEMAP_RE = re.compile(r"^Sitemap:\s*https?://[^/]+/([^/\s]+)/siteMap\.xml",
                         re.IGNORECASE)
_RULE_RE = re.compile(r"^(Allow|Disallow):\s*/([^/\s]+)/?\s*$", re.IGNORECASE)
_PSEUDO_SITES = {"refreshfacet", "talentcommunity"}


async def resolve_sites_from_robots(client, tenant: str, shard: str) -> dict:
    """Every site this tenant publishes, from its auto-generated robots.txt.

    {"allowed": [...], "disallowed": [...]} — a Disallow'd site is still a real
    site but is an explicit crawler opt-out, so it is never folded into
    "allowed". Pseudo-sites (refreshFacet, talentcommunity) are dropped.
    """
    url = f"https://{tenant}.{shard}.myworkdayjobs.com/robots.txt"
    allowed: list[str] = []
    disallowed: list[str] = []
    try:
        r = await client.get(url, headers=_TEXT_HEADERS)
    except Exception:
        return {"allowed": allowed, "disallowed": disallowed}
    if r.status_code != 200:
        return {"allowed": allowed, "disallowed": disallowed}

    named: list[str] = []      # Sitemap: lines name a site without a verdict
    for line in (r.text or "").splitlines():
        line = line.strip()
        m = _SITEMAP_RE.match(line)
        if m:
            named.append(m.group(1))
            continue
        m = _RULE_RE.match(line)
        if m:
            verdict, site = m.group(1).lower(), m.group(2)
            if site.lower() in _PSEUDO_SITES:
                continue
            bucket = allowed if verdict == "allow" else disallowed
            if site not in bucket:
                bucket.append(site)
    for site in named:          # sitemap-only sites: named, never opted out
        if site.lower() not in _PSEUDO_SITES and site not in allowed \
                and site not in disallowed:
            allowed.append(site)
    return {"allowed": allowed, "disallowed": disallowed}


async def sweep_shards(client, tenant: str, site: str) -> str | None:
    """Which shard hosts this tenant? Returns the shard label ("wd5") or None.

    Bootstrap/add_company ONLY — never call it from the fetcher: a slug always
    carries its shard, and a sweep costs up to 15 requests. Sequential and
    short-circuiting (wd1 alone answers ~69% of tenants).
    """
    for shard in SHARDS:
        url = _api_base(tenant, shard, site) + "/jobs"
        payload = {"appliedFacets": {}, "limit": _PAGE_LIMIT, "offset": 0,
                   "searchText": ""}
        try:
            r = await client.post(url, json=payload, headers=_JSON_HEADERS)
        except Exception:
            continue            # NXDOMAIN / TLS / timeout -> just not this one
        if r.status_code == 200:
            return shard
    return None


# --------------------------------------------------------------------------- #
# fetcher (registered as _FETCHERS["workday"] in watchlist.py)
# --------------------------------------------------------------------------- #
def _union_by_job_id(*passes: list[dict]) -> list[dict]:
    """Merge list-pass results, deduped by job_id — the same role shows up in
    both the full and the narrowed pass. Where two rows share an id, keep the
    one carrying an externalPath: without it a row cannot be hydrated, linked
    or applied to (NVIDIA emits an id-only row for a job it also lists properly).
    Rows with no id at all are kept and left for the usable/normalize guards."""
    merged: dict = {}
    order: list = []
    keyless = 0
    for rows in passes:
        for item in rows:
            key = job_id_for(item)
            if not key:
                keyless += 1
                key = ("_keyless", keyless)
            existing = merged.get(key)
            if existing is None:
                merged[key] = item
                order.append(key)
            elif not existing.get("externalPath") and item.get("externalPath"):
                merged[key] = item
    return [merged[key] for key in order]


async def _fetch_partitioned(client, tenant: str, shard: str, site: str,
                             families: list[dict]) -> tuple[list[dict], dict]:
    """Page every jobFamilyGroup value separately and union the results.

    Each family is individually under the 2000-row wall on every tenant seen so
    far (NVIDIA's largest is Engineering at 1723), so the union reconstructs the
    COMPLETE board — including the rows plain offset paging can never reach.
    Returns (rows, info) where info carries the per-family breakdown, the
    request count, and whether anything stayed unreachable.
    """
    planned, skipped, budget = [], [], _MAX_PARTITION_REQUESTS
    # Biggest families first: if the budget runs out, it runs out on the tail.
    for fam in sorted(families, key=lambda f: -f["count"]):
        cost = _pages_for(fam["count"])
        if cost <= budget:
            planned.append(fam)
            budget -= cost
        else:
            skipped.append(fam)

    passes: list[list[dict]] = []
    breakdown: dict = {}
    requests, listed, truncated = 0, 0, bool(skipped)
    for fam in planned:
        rows, meta = await list_jobs(client, tenant, shard, site,
                                     applied_facets={"jobFamilyGroup": [fam["id"]]})
        requests += meta.get("requests", 0)
        listed += len(rows)
        passes.append(rows)
        # A family big enough to hit its OWN wall leaves rows unreachable.
        fam_truncated = bool(meta.get("truncated"))
        truncated = truncated or fam_truncated
        breakdown[fam["descriptor"]] = {"count": fam["count"], "rows": len(rows)}
        if fam_truncated:
            breakdown[fam["descriptor"]]["truncated"] = True
    for fam in skipped:
        breakdown[fam["descriptor"]] = {"count": fam["count"], "rows": 0,
                                        "skipped": "over request budget"}

    return _union_by_job_id(*passes), {
        "requests": requests, "listed": listed, "families": breakdown,
        "partition_total": sum(f["count"] for f in families),
        "truncated": truncated,
    }


def _stored_details(slug: str) -> dict:
    """{job_id: {"description", "location", "posted"}} already held for this
    board — the fetch-once map.

    Workday's list payload has none of these three, so they cost one extra HTTP
    GET per posting and then effectively never change. Anything in here is
    carried forward instead of re-fetched. All three matter, not just the text:
    `location` is part of the content hash, so re-emitting the list's useless
    "2 Locations" rewrites the row AND makes it fail the baseline location gate,
    and `posted` is rewritten on every ingest branch, so re-emitting the list's
    absent date wipes the real one.

    A store that won't read degrades to an empty map (hydrate everything), never
    to a failed sourcing run.
    """
    from .. import store        # deferred: see normalize() for why
    try:
        return store.stored_detail_fields("workday", slug) or {}
    except Exception:
        return {}


def _retryable(exc: Exception) -> bool:
    """Transient failures worth another go: timeouts, dropped connections, 429
    and 5xx. Never 404/410 — the posting is genuinely gone — and never 401/403,
    where retrying a block only makes it worse and looks abusive."""
    if isinstance(exc, WorkdayError):
        status = exc.status
        return status is not None and (status == 429 or status >= 500)
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


def _backoff(attempt: int) -> float:
    """Exponential with jitter, so a throttled board's retries don't re-collide
    in lockstep."""
    delay = _RETRY_BASE_DELAY * (2 ** attempt)
    return delay + random.uniform(0, delay / 2)


class _HydrateState:
    """Per-board hydration bookkeeping plus the circuit breaker.

    A tenant that is throttling us fails everything, and grinding through three
    attempts each just prolongs the block. After `threshold` consecutive
    failures the board stops hydrating for the rest of the run; the remainder is
    deferred, not lost, and the next run picks it up.
    """

    def __init__(self, threshold: int | None = None):
        self.threshold = _BREAKER_THRESHOLD if threshold is None else threshold
        self.consecutive = 0
        self.tripped = False
        self.retried = 0
        self.failed = 0
        self.deferred = 0

    def succeeded(self):
        self.consecutive = 0

    def gave_up(self):
        self.failed += 1
        self.consecutive += 1
        if self.threshold and self.consecutive >= self.threshold:
            self.tripped = True


async def _attempt(client, tenant, shard, site, path, state) -> dict:
    async with _sem("workday:global", _GLOBAL_CONCURRENCY):
        async with _sem(f"workday:{tenant}:{shard}", _TENANT_CONCURRENCY):
            # Checked here, not before queueing: by the time a posting reaches
            # the front of the queue the board may already have given up.
            if state.tripped:
                raise _Deferred()
            return await fetch_detail(client, tenant, shard, site, path)


async def _hydrate(client, tenant, shard, site, item, state) -> dict | None:
    """One posting's detail, retried on transient failures. Never raises — a bad
    detail must not kill the board — but every outcome is counted in `state`."""
    path = item.get("externalPath") or ""
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            detail = await _attempt(client, tenant, shard, site, path, state)
            state.succeeded()
            return detail
        except _Deferred:
            state.deferred += 1
            return None
        except Exception as exc:
            if not _retryable(exc) or attempt == _RETRY_ATTEMPTS - 1:
                state.gave_up()
                return None
            state.retried += 1
            # Sleep OUTSIDE the semaphores, so backing off doesn't idle a slot.
            await asyncio.sleep(_backoff(attempt))
    return None                 # unreachable: every branch above returns


async def fetch_workday(client, company: dict) -> list[dict]:
    """All normalized postings for one Workday board.

    Pages the full list when the board fits under the 2000-row wall, and
    partitions it by jobFamilyGroup when it doesn't (see the module docstring).
    Title-prefilters before hydrating, then deep-reads only the survivors.
    Non-survivors still come back (description "") — they fail the title gate in
    passes_baseline anyway, and the store needs to see them so an unrelated role
    that is still listed isn't marked removed.

    Raises WorkdayError(reason) on a board-level HTTP failure; a truncated run
    is a soft condition recorded in LAST_RUN_NOTES[slug], not an exception.
    """
    # deferred: watchlist imports this module (circular at module level).
    from .watchlist import prefilter

    slug = company.get("slug", "") or ""
    tenant, shard, site = split_slug(slug)

    # One probe page for `total` + `facets`; reused as page 0 of a full run, so
    # the common case pays nothing for it.
    url = _api_base(tenant, shard, site) + "/jobs"
    probe = await _list_page(client, url, None, 0)
    requests = 1
    all_facets = probe[1].get("facets") or []
    total = probe[1].get("total")

    walled = total is not None and total >= _PARTITION_AT
    families = family_values(all_facets) if walled else []

    partition_total, breakdown = None, {}
    if families:
        # Walled board with a Job Category facet: page every family separately.
        # Each is under the wall on its own, so the union is the COMPLETE board
        # — no keyword guessing about which families matter, and the 618 rows
        # NVIDIA hides behind its clamped `total` come back.
        rows, info = await _fetch_partitioned(client, tenant, shard, site,
                                              families)
        requests += info["requests"]
        listed, breakdown = info["listed"], info["families"]
        partition_total = info["partition_total"]
        truncated = info["truncated"]
        # Families cannot account for the whole board -> some rows belong to no
        # family and no partition can reach them.
        if total and partition_total < total:
            truncated = True
        strategy = "partitioned_truncated" if truncated else "partitioned"
    else:
        # Under the wall, or walled with no facet to partition by.
        rows, meta = await list_jobs(client, tenant, shard, site, page0=probe)
        listed = len(rows)
        requests += meta.get("requests", 0)
        truncated = bool(meta.get("truncated"))
        strategy = "full_truncated" if truncated else "full"

    baseline = {}
    try:
        baseline = config.load_search_criteria().get("baseline") or {}
    except Exception:
        pass
    # NVIDIA serves the odd degenerate row — {"bulletFields": ["JR2001987"]},
    # no title and no externalPath. It can't be hydrated, linked or applied to,
    # and watchlist.prefilter indexes p["title"] directly, so an untitled row
    # would take the whole board down with a KeyError.
    usable = [item for item in rows if item.get("externalPath")]
    skipped = len(rows) - len(usable)
    titled = [item if "title" in item else {**item, "title": ""}
              for item in usable]
    # prefilter is now only for the title_matched note — hydration is gated on
    # MISSING TEXT, not on the title. That backfills old rows, covers new ones,
    # and self-heals if the user widens acceptable_titles later: a row that was
    # never title-matched has no stored text, so it hydrates on the next run
    # instead of staying blank forever.
    survivors = prefilter(titled, baseline.get("acceptable_titles"),
                          baseline.get("excluded_seniority"))

    # Presence of stored text is the "detail already fetched" signal.
    stored = _stored_details(slug)
    need = [item for item in usable
            if not (stored.get(job_id_for(item)) or {}).get("description")]
    # Bounded per run so a pathological board can't monopolise a refresh; the
    # remainder is picked up next run, so coverage converges either way.
    to_fetch = need[:_MAX_HYDRATE_PER_RUN]
    deferred = len(need) - len(to_fetch)

    state = _HydrateState()
    details = await asyncio.gather(
        *(_hydrate(client, tenant, shard, site, item, state)
          for item in to_fetch))
    by_id = {id(item): detail for item, detail in zip(to_fetch, details)}

    out, fetched, reused = [], 0, 0
    for item in usable:
        detail = by_id.get(id(item))
        row = normalize(company, item, detail)
        # The store keys on (ats, slug, job_id) and silently drops empties.
        if not row["slug"] or not row["job_id"]:
            continue
        if detail:
            fetched += 1
        else:
            kept = stored.get(row["job_id"]) or {}
            if kept.get("description"):
                # Carry the whole detail-only set forward, not just the text —
                # see _stored_details for why location and posted matter.
                row["description"] = kept["description"]
                if kept.get("location"):
                    row["location"] = kept["location"]
                    row["remote"] = "remote" in kept["location"].lower()
                if kept.get("posted"):
                    row["posted"] = kept["posted"]
                reused += 1
        out.append(row)
    hydrated = fetched + reused

    LAST_RUN_NOTES[slug] = {
        "strategy": strategy,   # full | full_truncated | partitioned[_truncated]
        "requests": requests,   # list POSTs this board cost (probe included)
        "rows": len(out), "listed": listed, "deduped": listed - len(rows),
        "skipped_unusable": skipped, "title_matched": len(survivors),
        # hydrated = rows carrying text at all; the split says how they got it.
        "hydrated": hydrated, "hydrated_fetched": fetched,
        "hydrated_from_store": reused,
        # deferred = over the per-run budget, plus whatever the circuit breaker
        # refused once the board started failing. Neither is lost: both come
        # back next run.
        "hydrate_deferred": deferred + state.deferred,
        "hydrate_retried": state.retried,       # extra attempts spent
        "hydrate_failed": state.failed,         # still failed after retries
        "hydrate_breaker": state.tripped,       # board gave up early
        "detail_failed": state.failed,          # kept as the older name

        # board_total is what the API ADMITS (clamped at 2000); partition_total
        # is the summed facet counts, i.e. the board's TRUE size. The gap
        # between them is the hidden-rows number — the whole case for
        # partitioning. None when the board never needed partitioning.
        "board_total": total, "partition_total": partition_total,
        "families": breakdown,  # descriptor -> {count, rows, [truncated|skipped]}
        # False means this fetch is COMPLETE, and store.refresh_from_fetch
        # resumes its removal pass on that basis. Never set it unless rows
        # really are unreachable.
        "truncated": truncated,
    }
    return out


# --------------------------------------------------------------------------- #
# single-posting deep read (called from watchlist.get_posting's workday branch)
# --------------------------------------------------------------------------- #
async def get_posting(client, url: str) -> dict | None:
    """Deep-read one Workday posting URL, or None if it isn't a resolvable
    Workday detail URL (caller then falls back to open_job + get_job_text)."""
    from .watchlist import _strip_boilerplate, _clean_html   # deferred: circular

    parsed = parse_workday_url(url)
    if not parsed or not parsed["site"] or not parsed["external_path"]:
        return None
    try:
        info = await fetch_detail(client, parsed["tenant"], parsed["shard"],
                                  parsed["site"], parsed["external_path"])
    except Exception:
        return None
    if not info:
        return None
    return {"url": info.get("externalUrl") or url, "found": True,
            "title": info.get("title"),
            "description": _strip_boilerplate(
                _clean_html(info.get("jobDescription", "")))}
