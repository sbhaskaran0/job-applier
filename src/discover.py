"""Headless startup discovery: `python -m src.discover`.

Pure Python — no LLM, no browser, no Claude session (same lane as src.refresh).
Enumerates candidate startups from company-list feeds (YC directory + Consider
VC portfolio boards; config in discovery.yaml), confirms each against the public
ATS board APIs, counts roles passing the job_criteria baseline, and records the
result in the candidate_boards ledger (data/postings.db). Regenerates
data/discovery-latest.md proposing watchlist additions — companies with ≥1
qualifying role that aren't on the watchlist yet.

Nothing is added to watchlist.yaml automatically: the curated watchlist stays
hand-approved. Point add_company at a proposed board to adopt it.

Incremental: a candidate confirmed/dead in the last `reprobe_after_days` is
skipped, so re-runs only probe new or stale boards. `max_probes_per_run` caps
work per run; the remainder stays queued for the next run.
"""

import asyncio
import sys
from datetime import datetime, timezone

import httpx

from . import config, store
from .providers import discovery as disc


def _needs_probe(cand: dict, ledger: dict, reprobe_days: int) -> bool:
    row = ledger.get((cand["source"], cand["source_key"]))
    if not row or not row.get("last_probed"):
        return True
    try:
        last = datetime.fromisoformat(row["last_probed"])
    except (ValueError, TypeError):
        return True
    age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400
    return age_days >= reprobe_days


def _select(candidates: list[dict], ledger: dict, reprobe_days: int,
            budget: int) -> tuple[list[dict], int]:
    """New candidates first, then stale ones (oldest probe first). Returns
    (to_probe, queued_count) after applying the per-run budget."""
    due = [c for c in candidates if _needs_probe(c, ledger, reprobe_days)]

    def sort_key(c):
        row = ledger.get((c["source"], c["source_key"]))
        # never-probed (no row / no last_probed) sort first via empty string
        return row["last_probed"] if row and row.get("last_probed") else ""

    due.sort(key=sort_key)
    if budget and budget > 0 and len(due) > budget:
        return due[:budget], len(due) - budget
    return due, 0


async def run() -> dict:
    cfg = config.load_discovery_config()
    baseline = config.load_search_criteria().get("baseline", {})
    limits = cfg.get("limits") or {}
    budget = limits.get("max_probes_per_run", 250)
    concurrency = limits.get("probe_concurrency", 8)
    reprobe_days = limits.get("reprobe_after_days", 14)

    watchset = {((c.get("ats") or "").lower(), c.get("slug"))
                for c in config.load_watchlist()}

    candidates, source_errors = await disc.gather_candidates(cfg)
    ledger = store.load_candidates()
    to_probe, queued = _select(candidates, ledger, reprobe_days, budget)

    sem = asyncio.Semaphore(concurrency)
    results = []
    if to_probe:
        async with httpx.AsyncClient(timeout=disc._TIMEOUT,
                                     headers={"User-Agent": disc._UA},
                                     follow_redirects=True) as client:
            results = await asyncio.gather(*(
                disc.probe_candidate(client, c, baseline, watchset, sem)
                for c in to_probe))

    conn = store.connect()
    try:
        for r in results:
            store.upsert_candidate(conn, r)
        conn.commit()
    finally:
        conn.close()

    return {
        "candidates_seen": len(candidates),
        "probed": len(results),
        "queued": queued,
        "source_errors": source_errors,
        "confirmed_this_run": sum(1 for r in results if r["status"] == "confirmed"),
    }


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def _fmt_int(n) -> str:
    return f"{n:,}" if isinstance(n, int) else str(n)


def build_report(summary: dict) -> str:
    """Render data/discovery-latest.md from the full ledger (not just this run),
    so the report is a standing view of every confirmed candidate."""
    ledger = store.load_candidates()
    rows = list(ledger.values())
    confirmed = [r for r in rows if r["status"] == "confirmed"]
    proposals = sorted(
        [r for r in confirmed if r["qualifying"] >= 1 and not r["on_watchlist"]],
        key=lambda r: (-r["qualifying"], -r["title_matched"], r["company"] or ""))
    on_wl = [r for r in confirmed if r["on_watchlist"]]

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# Startup discovery",
        "",
        f"Ran **{now}** — {summary['candidates_seen']:,} candidates enumerated, "
        f"{summary['probed']} probed this run "
        f"({summary['confirmed_this_run']} newly confirmed), "
        f"{summary['queued']} queued for next run.",
        "",
        f"## Proposed watchlist additions ({len(proposals)})",
        "",
        "_Confirmed boards with ≥1 role passing your baseline, not yet on the "
        "watchlist. Adopt one with `add_company <url>`._",
        "",
    ]
    if proposals:
        lines += ["| Company | Qualifying | Title-matched | Active | Source | Board URL |",
                  "|---|---|---|---|---|---|"]
        for r in proposals:
            lines.append(
                f"| {r['company']} | {r['qualifying']} | {r['title_matched']} | "
                f"{r['active_count']} | {r['source']} | "
                f"{disc.board_url(r['ats'], r['slug'])} |")
    else:
        lines.append("_None yet — run again after the queue drains, or add more "
                     "sources to discovery.yaml._")

    # source yield summary
    by_source: dict[str, dict] = {}
    for r in rows:
        s = by_source.setdefault(r["source"], {"seen": 0, "confirmed": 0, "dead": 0,
                                               "unresolved": 0})
        s["seen"] += 1
        s[r["status"]] = s.get(r["status"], 0) + 1

    lines += ["", "## Source yield (whole ledger)", "",
              "| Source | Seen | Confirmed | Unresolved | Dead |",
              "|---|---|---|---|---|"]
    for src in sorted(by_source):
        s = by_source[src]
        lines.append(f"| {src} | {s['seen']} | {s.get('confirmed', 0)} | "
                     f"{s.get('unresolved', 0)} | {s.get('dead', 0)} |")

    lines += ["", f"_{len(on_wl)} confirmed board(s) already on the watchlist "
              f"(counted, not proposed)._"]
    if summary["source_errors"]:
        lines += ["", "## Source errors", ""]
        for e in summary["source_errors"]:
            lines.append(f"- {e['source']}: {e['reason']}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    summary = asyncio.run(run())
    config.DISCOVERY_REPORT_PATH.write_text(build_report(summary), encoding="utf-8")
    # ASCII only: scheduled runs print to a cp1252 Windows console.
    print(f"discover ok: {summary['candidates_seen']:,} candidates | "
          f"{summary['probed']} probed | {summary['confirmed_this_run']} confirmed "
          f"| {summary['queued']} queued "
          f"-> {config.DISCOVERY_REPORT_PATH}")
    for e in summary["source_errors"]:
        print(f"  source error: {e['source']} - {e['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
