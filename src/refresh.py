"""Headless watchlist refresh: `python -m src.refresh` (JOB-28/31).

Pure Python — no LLM, no browser, no Claude session. Safe to run from any
scheduler (Windows Task Scheduler via scripts/refresh.cmd, cron, launchd) or
by hand. Fetches every watchlist board, upserts into data/postings.db, and
regenerates data/digest-latest.md (new baseline-passing roles, board health,
per-company yield).
"""

import asyncio
import sys
from datetime import date, timedelta

# `data` is imported at module level rather than locally the way
# store._applied_keys does it: store defers the import because it pulls in
# fuzzy-match machinery on a hot path, but data.py's own imports are stdlib
# plus src.config (i.e. pyyaml), so this adds no third dependency to the
# refresh import path — which matters, because CI's Tests step installs only
# `pyyaml httpx` and would break on anything heavier.
from . import config, data, store
from .providers import watchlist as wl

# Consecutive failed fetches before a board is called out. Defined in store.py
# (JOB-137) because the store's own aggregates filter on the same threshold —
# one source of truth, re-exported here so the digest's own reads read locally.
_DARK_RUNS = store._DARK_RUNS

# JOB-136: how long a submitted application waits before the digest asks for an
# outcome, and how many rows the ask renders before it truncates. The cap is
# pinned rather than left to taste: the log already holds ~90 submitted records,
# and an uncapped list would bury the board-health and yield sections under it.
_AWAITING_DAYS = 14
_AWAITING_LIMIT = 15


def _fmt_salary(row: dict) -> str:
    lo, hi = row.get("salary_min"), row.get("salary_max")
    if lo is None:
        return "not listed"
    tag = " (from JD)" if row.get("salary_source") == "jd" else ""
    return (f"${lo:,.0f}–${hi:,.0f}{tag}" if hi and hi != lo else f"${lo:,.0f}{tag}")


def _awaiting_outcome(applications: list, today: date | None = None) -> list[dict]:
    """Submitted applications old enough to have expected a reply that still
    carry no outcome — oldest first (JOB-136).

    This is the one input the response-rate goal depends on and the one input
    nothing collects: `status` is written automatically by the apply path, but
    `outcome` only ever arrives from a human, so without a prompt the numerator
    stays zero forever and the rate is a lie rather than a measurement.

    Two judgement calls, both deliberate:
      * `outcome == "none"` counts as STILL AWAITING, not as answered. "none" is
        APPLICATION_OUTCOMES' no-reply sentinel, not a recorded reply — the
        vocabulary's way to say "they went quiet and I'm calling it" is
        "ghosted", which does clear the row.
      * A record with a missing or unparseable `date` is skipped rather than
        listed. We cannot honestly claim something is 14 days old when we do not
        know when it happened, and a digest that nags about undated records
        trains the reader to skip the section.
    """
    cutoff = ((today or date.today()) - timedelta(days=_AWAITING_DAYS)).isoformat()
    out = []
    for a in applications or []:
        if not isinstance(a, dict):
            continue  # junk tolerated, not raised on — same as outcome stats
        if str(a.get("status") or "") not in data.SUBMITTED_STATUSES:
            continue
        if str(a.get("outcome") or "none").strip().lower() != "none":
            continue
        when = str(a.get("date") or "").strip()[:10]
        try:
            date.fromisoformat(when)
        except ValueError:
            continue
        if when <= cutoff:  # ISO dates sort lexicographically
            out.append(a)
    return sorted(out, key=lambda a: str(a.get("date") or ""))


def build_digest(summary: dict) -> str:
    baseline = config.load_search_criteria().get("baseline", {})
    new_passing = [p for p in summary["new_rows"]
                   if store.passes_baseline(p, baseline)[0]]
    lines = [
        "# Watchlist digest",
        "",
        f"Refreshed **{summary['run_at']}** — {summary['total_scanned']:,} postings "
        f"scanned, {summary['new_count']} new, {summary['removed_count']} removed, "
        f"{summary['relisted_count']} relisted.",
        "",
        f"## New postings passing the baseline ({len(new_passing)})",
        "",
    ]
    if new_passing:
        for p in sorted(new_passing, key=lambda p: (p["company"], p["title"])):
            years = f" · {p['min_years']}+ yrs (advisory)" if p.get("min_years") else ""
            # JOB-138: the board gave a country and no city, so this passed on
            # "we don't know where", not on a match. Say so rather than let it
            # read like every other line.
            unverified = (" · _location unverified_"
                          if store.location_indeterminate(p, baseline) else "")
            lines.append(f"- **{p['company']} — {p['title']}** · "
                         f"{p['location'] or 'location n/a'}{unverified}"
                         f"{' · remote' if p.get('remote') else ''} · "
                         f"{_fmt_salary(p)}{years}\n  {p['url']}")
    else:
        lines.append("_None this run._")

    dark = [f for f in summary["companies_failed"]
            if f.get("consecutive", 1) >= _DARK_RUNS]
    lines += ["", "## Board health", ""]
    if summary["companies_failed"]:
        for f in summary["companies_failed"]:
            marker = " ⚠️ DARK — fix slug or drop from watchlist" \
                if f in dark else ""
            lines.append(f"- {f['company']}: failed {f.get('consecutive', 1)} "
                         f"consecutive run(s) — {f['reason']}{marker}")
    else:
        lines.append("_All boards fetched clean._")

    # Awaiting outcome (JOB-136): the digest's one ASK rather than one more
    # report. Placed under board health so it reads as the second thing the
    # user owes the loop, above the reference tables.
    applications = config.load_applications()
    awaiting = _awaiting_outcome(applications)
    outcomes = data.application_outcome_stats(applications)
    lines += ["", f"## Awaiting outcome ({len(awaiting)})", ""]
    # The denominator rides along on purpose: "0.0%" alone reads as failure,
    # "0.0% (0 of 89 submitted)" reads as un-measured, which is the truth.
    # response_rate is 0.0 and never None/ZeroDivisionError on an empty log.
    lines.append(
        f"_Response rate so far: **{outcomes['response_rate'] * 100:.1f}%** "
        f"({outcomes['responded']} replied of {outcomes['submitted']} "
        f"submitted; {outcomes['by_outcome']['none']} record(s) carry no "
        f"outcome)._")
    lines.append("")
    if awaiting:
        lines.append(f"Submitted {_AWAITING_DAYS}+ days ago with nothing "
                     f"recorded back — set one with the `set_application_outcome` "
                     f"tool or the Applications page:")
        lines.append("")
        for a in awaiting[:_AWAITING_LIMIT]:
            url = a.get("url") or ""
            lines.append(
                f"- {a.get('date') or 'date n/a'} · "
                f"**{a.get('company') or '(unknown)'} — "
                f"{a.get('job_title') or '(untitled)'}** "
                f"({a.get('status')})" + (f"\n  {url}" if url else ""))
        if len(awaiting) > _AWAITING_LIMIT:
            lines.append(f"_... and {len(awaiting) - _AWAITING_LIMIT} more._")
    else:
        lines.append("_Nothing waiting — every submitted application older "
                     f"than {_AWAITING_DAYS} days has an outcome recorded._")

    lines += ["", "## Yield per company (active postings)", "",
              "| Company | Active | Title-matched | Qualifying |",
              "|---|---|---|---|"]
    for s in store.yield_stats():
        # STALE (JOB-137): a board dark for _DARK_RUNS runs keeps its row so a
        # quietly-404'd board can't be mistaken for one that just posts nothing,
        # but its rows are frozen history and are excluded from the corpus
        # totals in the concentration section below.
        stale = " ⚠️ STALE" if s.get("stale") else ""
        lines.append(f"| {s['company']}{stale} | {s['active']} | "
                     f"{s['title_matched']} | {s['qualifying']} |")
    lines += ["", "_Qualifying = passes titles/seniority/location/salary-floor "
              "deterministically. All three columns count distinct roles — a "
              "role cross-posted to several cities counts once, unlike the "
              "'New postings' list above which still lists every city variant "
              "separately, so the two sections won't add up. Yield informs "
              "the JOB-26 watchlist rework. STALE rows belong to a board that "
              f"has failed {_DARK_RUNS}+ consecutive fetches: the counts are "
              "the last thing we saw, not live supply, and they are excluded "
              "from the corpus totals below._", ""]

    # Company concentration (JOB-113): the yield table above answers "which
    # boards produce?" but not "are we fishing in one pond?" -- that took a
    # hand count until now.
    spread = store.company_spread()
    lines += ["## Company concentration", ""]
    for label, side, unit in (
            ("Qualifying corpus", spread["qualifying"], "distinct roles"),
            ("Applications logged", spread["applications"], "records")):
        top = ", ".join(f"{t['company']} {t['count']}" for t in side["top5"])
        lines.append(f"- **{label}:** {side['total']:,} {unit} across "
                     f"{side['companies']} companies — top 5 = "
                     f"{side['top5_share_pct']}%"
                     + (f" ({top})" if top else ""))
    status_mix = ", ".join(f"{k} {v}" for k, v in
                           spread["applications"]["by_status"].items())
    if status_mix:
        lines.append(f"  - application status mix: {status_mix}")
    lines += ["", "_Qualifying roles are deduped by (company, title): one role "
              "listed in several cities counts once, unlike the per-city rows in "
              "the yield table above. Applications count every logged record, "
              "submitted or manual._", ""]
    return "\n".join(lines)


async def run() -> dict:
    fetch = await wl.fetch_all_with_status()
    summary = store.refresh_from_fetch(fetch)
    config.DIGEST_PATH.write_text(build_digest(summary), encoding="utf-8")
    return summary


def main() -> int:
    # `--near-misses` reports on what the title gate already discarded (JOB-115)
    # and returns without touching the network. Checked by hand rather than with
    # argparse to keep the no-flag path byte-for-byte what it has always been.
    if "--near-misses" in sys.argv[1:]:
        # Loaded by path off BASE_DIR rather than imported: scripts/ is not a
        # package, and this keeps the flag working from any working directory.
        import importlib.util
        path = config.BASE_DIR / "scripts" / "near_misses.py"
        spec = importlib.util.spec_from_file_location("near_misses", path)
        near_misses = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(near_misses)
        return near_misses.main()

    summary = asyncio.run(run())
    # ASCII only: scheduled runs print to a cp1252 Windows console, which
    # cannot encode unicode punctuation.
    print(f"refresh ok: {summary['total_scanned']:,} scanned | "
          f"{summary['new_count']} new | {summary['removed_count']} removed | "
          f"{summary['relisted_count']} relisted | "
          f"{len(summary['companies_failed'])} board(s) failed "
          f"-> {config.DIGEST_PATH}")
    for f in summary["companies_failed"]:
        print(f"  failed: {f['company']} ({f.get('consecutive', 1)}x) - {f['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
