#!/usr/bin/env python3
"""Find and validate Workday tenant/shard/site triples for watchlist.yaml.

The repo sources jobs from a curated `watchlist.yaml` of companies on
Greenhouse, Lever and Ashby. Workday is a different shape: instead of one
slug there are three independent parts - `tenant`, `shard` (e.g. `wd5`) and
`site` - and a wrong guess on any one of them returns an HTTP error rather
than an empty result. Getting a company onto Workday into the watchlist by
hand means trawling careers pages for the right combination and then
probing it, which does not scale past a handful of companies.

This script automates that trawl. It matches human company names (as they
appear in watchlist.yaml, e.g. "Capital One", "S&P Global") against a public
tenant index, validates every candidate triple against the live Workday API,
and - when a candidate comes back wrong - runs the same auto-repairs a human
would try by hand (sweep known shards on a 422, look for the right site via
robots.txt on a 404). It never writes to watchlist.yaml; it only prints
ready-to-paste YAML plus a status report for everything that did not pan
out, so a human stays the one deciding what actually gets committed.

Usage:
    python scripts/workday_bootstrap.py NAME [NAME ...]
    python scripts/workday_bootstrap.py --watchlist-names
    python scripts/workday_bootstrap.py --tenant nvidia --site NVIDIAExternalCareerSite
    python scripts/workday_bootstrap.py --tenant nvidia
    python scripts/workday_bootstrap.py --url https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite

Nothing is ever written to watchlist.yaml. This script only proposes
candidates for a human to paste in; the orchestrator seeds the real file
separately.
"""

import argparse
import asyncio
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# Run as a plain script, so the repo root has to be importable either way.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402
from src.providers import workday as wd  # noqa: E402

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

INDEX_URL = (
    "https://raw.githubusercontent.com/Feashliaa/job-board-aggregator/"
    "main/data/workday_companies.json"
)
# OS temp dir, not the repo - this is a downloaded third-party artifact,
# not something the repo should track or gitignore-dance around.
INDEX_CACHE_PATH = Path(tempfile.gettempdir()) / "job_applier_workday_index.json"

USER_AGENT = "job-applier-workday-bootstrap/1.0 (+https://github.com/sbhaskaran0/job-applier)"
REQUEST_TIMEOUT = 20.0

# Live shards in frequency order, used when we have to guess-and-check a shard
# ourselves (the --tenant-only robots flow, where no site is known yet to hand
# to wd.sweep_shards). Borrowed from the provider rather than restated here:
# two copies of this list WILL drift the first time Workday adds a shard.
SHARD_FREQUENCY = wd.SHARDS

# Site-name scoring for auto-picking among several candidate sites for one
# tenant (index has multiple rows per tenant, robots.txt often lists several
# paths). Positive hints suggest a real external candidate board; negative
# hints suggest campus/intern/internal/agency portals nobody wants sourced.
POSITIVE_SITE_HINTS = ("external", "career", "corporate", "jobs")
NEGATIVE_SITE_HINTS = (
    "campus", "intern", "student", "university", "early", "agency",
    "contractor", "internal", "broadbean", "equest",
)

# Global request concurrency is capped here regardless of --concurrency,
# per the etiquette note: this runs from a residential IP and is fine, but
# datacenter-style hammering gets 403-blocked.
MAX_CONCURRENCY_CEILING = 10

# Minimum length for a normalized name/tenant fragment to be considered for
# prefix/substring matching (exact matches are always considered regardless
# of length). Keeps two-character noise out of the substring pass.
_MIN_FUZZY_LEN = 4

# Repair recursion guard - a candidate can chain at most this many repairs
# (e.g. wrong_site -> robots -> that site is also wrong_shard -> sweep)
# before we give up rather than loop forever on a pathological board.
_MAX_REPAIR_DEPTH = 3


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

@dataclass
class Result:
    company: str
    tenant: str
    shard: str
    site: str
    status: str                      # ok | empty | wrong_shard | wrong_site |
                                      # tenant_casing | internal | blocked |
                                      # retired | error | unmatched
    total: int | None
    repairs: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class RunState:
    """Shared mutable state for one run: robots-Disallow exclusions (never
    recommended, listed separately) and a light 403 backoff so a burst of
    blocks slows the whole run down instead of hammering through it."""

    def __init__(self):
        self.excluded: set[tuple[str, str, str]] = set()
        self._streak_403 = 0
        self._lock = asyncio.Lock()

    async def note_response(self, status_code: int) -> float:
        async with self._lock:
            if status_code == 403:
                self._streak_403 += 1
            else:
                self._streak_403 = 0
            streak = self._streak_403
        return min(2 ** streak, 30) if streak >= 2 else 0.0


# --------------------------------------------------------------------------
# Name / index matching (pure - no network)
# --------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Lowercase, alphanumerics only - the normalization both the company
    name and the tenant field go through before comparison."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def parse_index(raw: list) -> dict[str, list[tuple[str, str]]]:
    """"tenant|shard|site" strings -> {tenant: [(shard, site), ...]}."""
    out: dict[str, list[tuple[str, str]]] = {}
    for entry in raw:
        parts = str(entry).split("|")
        if len(parts) != 3:
            continue
        tenant, shard, site = parts
        out.setdefault(tenant, []).append((shard, site))
    return out


def score_site(site: str) -> int:
    """Higher is a more likely real external candidate board."""
    s = site.lower()
    score = sum(1 for w in POSITIVE_SITE_HINTS if w in s)
    score -= 2 * sum(1 for w in NEGATIVE_SITE_HINTS if w in s)
    return score


def distinct_sites(entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """[(shard, site), ...] -> [(site, shard)] deduped by site, keeping the
    first shard the index listed for that site."""
    seen: dict[str, str] = {}
    for shard, site in entries:
        seen.setdefault(site, shard)
    return list(seen.items())


def pick_sites(entries: list[tuple[str, str]], all_sites: bool) -> list[tuple[str, str]]:
    """[(site, shard)] candidates for one tenant, best-scored first. Only the
    top one unless --all-sites."""
    scored = sorted(distinct_sites(entries), key=lambda ss: (-score_site(ss[0]), ss[0]))
    return scored if all_sites else scored[:1]


def pick_site_names(names: list[str], all_sites: bool) -> list[str]:
    """Same scoring, but over plain site-name strings (robots.txt results,
    which carry no shard of their own - the shard is already fixed)."""
    scored = sorted(set(names), key=lambda s: (-score_site(s), s))
    return scored if all_sites else scored[:1]


def match_tenants(name: str, index: dict[str, list]) -> list[str]:
    """Normalize both sides; exact match first, then prefix/substring.
    Company names in watchlist.yaml are human names ("Capital One",
    "S&P Global", "Fidelity") whose tenants are "capitalone", "spgi", "fmr" -
    exact matching misses those on purpose. Unmatched names are reported,
    never silently dropped (see main())."""
    n = normalize(name)
    if not n:
        return []
    tenants = sorted(index)
    exact = [t for t in tenants if t == n]
    if exact:
        return exact
    if len(n) < _MIN_FUZZY_LEN:
        return []
    prefix = [t for t in tenants if len(t) >= _MIN_FUZZY_LEN and (t.startswith(n) or n.startswith(t))]
    # "n in t" (the requested name fully inside a longer tenant, e.g. "fidelity"
    # inside "americanfidelity") is a reasonably strong signal down to
    # _MIN_FUZZY_LEN. "t in n" (a short tenant string turning up embedded in an
    # unrelated longer name, e.g. tenant "pcom" inside "totallymadeupcompanyxyz")
    # gets noisy fast at that length, so it needs a longer tenant to count.
    substr = [
        t for t in tenants
        if t not in prefix and (
            (len(t) >= _MIN_FUZZY_LEN and n in t)
            or (len(t) >= _MIN_FUZZY_LEN + 1 and t in n)
        )
    ]
    return prefix + substr


def candidates_for_name(name: str, index: dict, all_sites: bool) -> tuple[list[tuple[str, str, str]], list[str]]:
    """-> ([(tenant, shard, site), ...], matched_tenants). Empty candidate
    list with an empty tenant list means genuinely unmatched."""
    matched = match_tenants(name, index)
    out = []
    for tenant in matched:
        for site, shard in pick_sites(index[tenant], all_sites):
            out.append((tenant, shard, site))
    return out, matched


# --------------------------------------------------------------------------
# Index download / cache
# --------------------------------------------------------------------------

def load_index(refresh: bool) -> dict[str, list[tuple[str, str]]]:
    if not refresh and INDEX_CACHE_PATH.exists():
        try:
            raw = json.loads(INDEX_CACHE_PATH.read_text(encoding="utf-8"))
            return parse_index(raw)
        except (OSError, json.JSONDecodeError):
            pass  # fall through to a fresh download
    resp = httpx.get(INDEX_URL, headers={"User-Agent": USER_AGENT}, timeout=30.0)
    resp.raise_for_status()
    raw = resp.json()
    try:
        INDEX_CACHE_PATH.write_text(json.dumps(raw), encoding="utf-8")
    except OSError:
        pass  # cache is a convenience, not a requirement
    return parse_index(raw)


# --------------------------------------------------------------------------
# Live validation (the one thing this script implements itself - it needs
# the raw HTTP status code, not an exception, to drive the repair chain)
# --------------------------------------------------------------------------

async def raw_validate(
    client: httpx.AsyncClient, state: RunState, tenant: str, shard: str, site: str,
) -> tuple[str, int | None]:
    """POST the CxS jobs endpoint directly. Returns (status, total) where
    status is wd.classify_status()'s taxonomy, refined with "empty" for a
    200 whose body has total == 0 (a valid but empty/retired board - the API
    facts this script was built against call this out explicitly)."""
    url = f"https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    headers = {
        "Content-Type": "application/json",  # required - omitting it gives a 500
        "Accept": "application/json",
        "Accept-Language": "en-US",
        "User-Agent": USER_AGENT,
    }
    body = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
    try:
        resp = await client.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
    except httpx.HTTPError as e:
        return f"error: {e}", None

    delay = await state.note_response(resp.status_code)
    if delay:
        await asyncio.sleep(delay)

    status = wd.classify_status(resp.status_code)
    total = None
    if resp.status_code == 200:
        try:
            total = resp.json().get("total")
        except ValueError:
            total = None
        if total == 0:
            status = "empty"
    return status, total


async def guess_shard_via_robots(
    client: httpx.AsyncClient, tenant: str,
) -> tuple[str | None, dict]:
    """Used only when --tenant is given with no --site and the tenant is not
    in the downloaded index, so there is no shard to hand to
    wd.resolve_sites_from_robots(). Walks the frequency-ordered shard list
    until one host answers robots.txt at all. (A contract helper that folds
    this shard-guessing into resolve_sites_from_robots itself would remove
    the need for this - see the closing report.)"""
    for shard in SHARD_FREQUENCY:
        try:
            robots = await wd.resolve_sites_from_robots(client, tenant, shard)
        except wd.WorkdayError:
            continue
        if robots.get("allowed") or robots.get("disallowed"):
            return shard, robots
    return None, {}


# --------------------------------------------------------------------------
# Repair chain
# --------------------------------------------------------------------------

async def resolve_candidate(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    state: RunState,
    company: str,
    tenant: str,
    shard: str,
    site: str,
    all_sites: bool,
    depth: int = 0,
    repairs: list[str] | None = None,
) -> list[Result]:
    """Validate one (tenant, shard, site) triple; on a repairable failure,
    apply the matching auto-repair and recurse. Always returns a list of
    Result (usually one - more than one only when --all-sites causes a
    wrong_site repair to fan out over several robots-resolved candidates)."""
    repairs = repairs or []
    if depth > _MAX_REPAIR_DEPTH:
        return [Result(company, tenant, shard, site, "error: repair depth exceeded", None, repairs)]

    async with sem:
        status, total = await raw_validate(client, state, tenant, shard, site)

    if status in ("ok", "empty"):
        return [Result(company, tenant, shard, site, status, total, repairs)]

    if status == "wrong_shard":
        try:
            async with sem:
                new_shard = await wd.sweep_shards(client, tenant, site)
        except wd.WorkdayError as e:
            new_shard = None
            repairs = repairs + [f"wrong_shard(422): sweep_shards raised {e}"]
        if new_shard and new_shard != shard:
            return await resolve_candidate(
                client, sem, state, company, tenant, new_shard, site, all_sites,
                depth + 1, repairs + [f"wrong_shard(422): {shard} -> swept to {new_shard}"],
            )
        return [Result(company, tenant, shard, site, "wrong_shard", None,
                        repairs + ["wrong_shard(422): sweep_shards found no working shard"])]

    if status == "wrong_site":
        try:
            async with sem:
                robots = await wd.resolve_sites_from_robots(client, tenant, shard)
        except wd.WorkdayError as e:
            return [Result(company, tenant, shard, site, "wrong_site", None,
                            repairs + [f"wrong_site(404): robots lookup raised {e}"])]
        for d in robots.get("disallowed") or []:
            state.excluded.add((tenant, shard, d))
        allowed = robots.get("allowed") or []
        cand_sites = pick_site_names(allowed, all_sites)
        if not cand_sites:
            note = "wrong_site(404): robots.txt had no allowed candidate site"
            if robots.get("disallowed"):
                note += " (only Disallow-listed sites found - excluded, not recommended)"
            return [Result(company, tenant, shard, site, "wrong_site", None, repairs + [note])]

        fanned: list[Result] = []
        for cand in cand_sites:
            fanned.extend(await resolve_candidate(
                client, sem, state, company, tenant, shard, cand, all_sites,
                depth + 1, repairs + [f"wrong_site(404): robots-resolved '{cand}'"],
            ))
        good = [r for r in fanned if r.ok or r.status == "empty"]
        if not good:
            return fanned
        return good if all_sites else [max(good, key=lambda r: r.total or 0)]

    if status == "tenant_casing":
        return [Result(company, tenant, shard, site, "tenant_casing", None,
                        repairs + ["tenant_casing(502): tenant path is case-sensitive - "
                                   "verify exact casing by hand, not brute-forced"])]

    # 401 internal / 403 blocked / 410 retired / network error - no repair.
    return [Result(company, tenant, shard, site, status, None, repairs)]


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def render_yaml(results: list[Result]) -> list[str]:
    ok = [r for r in results if r.ok]
    ok.sort(key=lambda r: -(r.total or 0))
    seen: dict[str, Result] = {}
    also: dict[str, list[str]] = {}
    for r in ok:
        slug = wd.make_slug(r.tenant, r.shard, r.site)
        if slug in seen:
            also.setdefault(slug, []).append(r.company)
            continue
        seen[slug] = r

    lines = []
    for slug, r in sorted(seen.items(), key=lambda kv: -(kv[1].total or 0)):
        note_parts = [f"{r.total} job{'s' if r.total != 1 else ''}"]
        if r.repairs:
            note_parts.append("repaired: " + " | ".join(r.repairs))
        if also.get(slug):
            note_parts.append("also matched: " + ", ".join(also[slug]))
        lines.append(f"  - name: {r.company}  # {'; '.join(note_parts)}")
        lines.append("    ats: workday")
        lines.append(f"    slug: {slug}")
    return lines


_NEXT_STEP = {
    "unmatched": "verify the company is really on Workday; try --tenant/--url directly",
    "wrong_shard": "sweep_shards found nothing; check the tenant name and shard by hand",
    "wrong_site": "robots.txt had no usable candidate; try --tenant NAME --all-sites",
    "tenant_casing": "tenant path is case-sensitive (502); find the exact casing by hand",
    "internal": "board requires internal auth (401); likely not externally usable",
    "blocked": "requests blocked (403); retry later or with lower --concurrency",
    "retired": "board is retired (410); drop from consideration",
    "empty": "valid board, currently 0 postings; recheck later or skip for now",
}


def next_step_for(result: Result) -> str:
    base = result.status.split(":", 1)[0].strip()
    if base.startswith("error"):
        return "network/parse error - retry: " + result.status
    return _NEXT_STEP.get(base, "inspect manually")


def render_status_table(results: list[Result]) -> list[str]:
    not_ok = [r for r in results if r.status not in ("ok",)]
    if not not_ok:
        return ["(nothing to report - every candidate validated ok)"]
    rows = [("NAME", "TENANT", "TRIED", "REASON", "NEXT STEP")]
    for r in not_ok:
        tried = " -> ".join(r.repairs) if r.repairs else "initial validation only"
        rows.append((r.company, r.tenant or "-", tried, r.status, next_step_for(r)))
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    lines = []
    for i, row in enumerate(rows):
        line = " | ".join(cell.ljust(widths[j]) for j, cell in enumerate(row))
        lines.append(line)
        if i == 0:
            lines.append("-+-".join("-" * w for w in widths))
    return lines


def render_excluded(state: RunState) -> list[str]:
    if not state.excluded:
        return []
    lines = ["", "Excluded (robots Disallow - never recommended):"]
    for tenant, shard, site in sorted(state.excluded):
        lines.append(f"  {tenant}:{shard}:{site}")
    return lines


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

async def run_names(
    client, sem, state, names: list[str], index: dict, all_sites: bool, limit: int | None,
) -> list[Result]:
    unmatched: list[Result] = []
    triples: list[tuple[str, str, str, str]] = []  # (company, tenant, shard, site)
    for name in names:
        cands, matched = candidates_for_name(name, index, all_sites)
        if not matched:
            unmatched.append(Result(name, "", "", "", "unmatched", None,
                                     ["no tenant found in the downloaded index"]))
            continue
        for tenant, shard, site in cands:
            triples.append((name, tenant, shard, site))

    if limit is not None:
        triples = triples[:limit]

    tasks = [
        resolve_candidate(client, sem, state, company, tenant, shard, site, all_sites)
        for company, tenant, shard, site in triples
    ]
    results: list[Result] = list(unmatched)
    for fut in await asyncio.gather(*tasks):
        results.extend(fut)
    return results


async def run_tenant(
    client, sem, state, tenant: str, site: str | None, index: dict, all_sites: bool,
) -> list[Result]:
    if site:
        try:
            async with sem:
                shard = await wd.sweep_shards(client, tenant, site)
        except wd.WorkdayError as e:
            return [Result(tenant, tenant, "", site, f"error: sweep_shards raised {e}", None, [])]
        if not shard:
            return [Result(tenant, tenant, "", site, "wrong_shard", None,
                            ["sweep_shards found no working shard among the known list"])]
        return await resolve_candidate(
            client, sem, state, tenant, tenant, shard, site, all_sites,
            repairs=[f"shard sweep (--tenant/--site): matched {shard}"],
        )

    # --tenant with no --site: resolve candidate sites via robots.txt.
    shard = None
    if tenant in index:
        # Most-listed shard in the index for this tenant, as a starting guess.
        shard = distinct_sites(index[tenant])[0][1]
        try:
            async with sem:
                robots = await wd.resolve_sites_from_robots(client, tenant, shard)
        except wd.WorkdayError:
            robots = {}
        if not (robots.get("allowed") or robots.get("disallowed")):
            shard = None  # index's shard guess didn't even serve robots.txt
    else:
        robots = {}

    if shard is None:
        async with sem:
            shard, robots = await guess_shard_via_robots(client, tenant)
    if shard is None:
        return [Result(tenant, tenant, "", "", "wrong_site", None,
                        ["could not find a working shard via index or robots.txt sweep; "
                         "try --tenant NAME --site SITE or --url"])]

    for d in robots.get("disallowed") or []:
        state.excluded.add((tenant, shard, d))
    cand_sites = pick_site_names(robots.get("allowed") or [], all_sites)
    if not cand_sites:
        return [Result(tenant, tenant, shard, "", "wrong_site", None,
                        ["robots.txt on the resolved shard had no allowed candidate site"])]

    tasks = [
        resolve_candidate(
            client, sem, state, tenant, tenant, shard, cand, all_sites,
            repairs=[f"robots.txt resolution (--tenant only): candidate '{cand}'"],
        )
        for cand in cand_sites
    ]
    results: list[Result] = []
    for fut in await asyncio.gather(*tasks):
        results.extend(fut)
    return results


async def run_url(client, sem, state, url: str, all_sites: bool) -> list[Result]:
    parsed = wd.parse_workday_url(url)
    if parsed is None:
        return [Result(url, "", "", "", "error: not a recognizable Workday board URL", None, [])]
    tenant, shard, site = parsed["tenant"], parsed["shard"], parsed["site"]
    return await resolve_candidate(client, sem, state, tenant, tenant, shard, site, all_sites)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Find and validate Workday tenant/shard/site triples for watchlist.yaml. "
                     "Prints ready-to-paste YAML and a status report; never writes to watchlist.yaml.",
    )
    p.add_argument("names", nargs="*", help="Company names to match against the tenant index.")
    p.add_argument("--watchlist-names", action="store_true",
                    help="Take names from watchlist.yaml (config.load_watchlist()) instead of argv.")
    p.add_argument("--tenant", help="A known Workday tenant slug to validate directly.")
    p.add_argument("--site", help="Used with --tenant: sweep shards for this one tenant/site.")
    p.add_argument("--url", help="Parse and validate a single Workday board URL.")
    p.add_argument("--all-sites", action="store_true",
                    help="Validate every candidate site for a tenant instead of only the best-scored one.")
    p.add_argument("--refresh-index", action="store_true",
                    help="Force a fresh download of the tenant index instead of using the cached copy.")
    p.add_argument("--concurrency", type=int, default=8,
                    help="Max concurrent validation requests (default 8, hard ceiling %d)." % MAX_CONCURRENCY_CEILING)
    p.add_argument("--limit", type=int, default=None,
                    help="Cap how many candidates are validated this run (name/--watchlist-names modes only).")
    return p


async def amain(args: argparse.Namespace) -> int:
    concurrency = max(1, min(args.concurrency, MAX_CONCURRENCY_CEILING))
    if concurrency != args.concurrency:
        print(f"note: --concurrency clamped to {concurrency} (etiquette ceiling {MAX_CONCURRENCY_CEILING})")

    state = RunState()
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(http2=False) as client:
        if args.url:
            results = await run_url(client, sem, state, args.url, args.all_sites)
        elif args.tenant:
            index = load_index(args.refresh_index) if not args.site else {}
            results = await run_tenant(client, sem, state, args.tenant, args.site, index, args.all_sites)
        else:
            if args.watchlist_names:
                names = [c.get("name") for c in config.load_watchlist() if c.get("name")]
            else:
                names = list(args.names)
            if not names:
                print("Nothing to do: give NAME(s), --watchlist-names, --tenant, or --url. "
                      "See --help.")
                return 2
            index = load_index(args.refresh_index)
            results = await run_names(client, sem, state, names, index, args.all_sites, args.limit)

    print("=== Ready-to-paste YAML (paste under the existing `companies:` key) ===")
    yaml_lines = render_yaml(results)
    if yaml_lines:
        print("\n".join(yaml_lines))
    else:
        print("  (no candidate validated ok)")
    print()
    print("=== Status: everything that did NOT come out ok ===")
    print("\n".join(render_status_table(results)))
    excluded_lines = render_excluded(state)
    if excluded_lines:
        print("\n".join(excluded_lines))
    print()
    print("Nothing was written to watchlist.yaml - this script only proposes; "
          "paste the entries you want into the real file by hand.")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
