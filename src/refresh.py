"""Headless watchlist refresh: `python -m src.refresh` (JOB-28/31).

Pure Python — no LLM, no browser, no Claude session. Safe to run from any
scheduler (Windows Task Scheduler via scripts/refresh.cmd, cron, launchd) or
by hand. Fetches every watchlist board, upserts into data/postings.db, and
regenerates data/digest-latest.md (new baseline-passing roles, board health,
per-company yield).
"""

import asyncio
import sys

from . import config, store
from .providers import watchlist as wl

_DARK_RUNS = 3  # consecutive failed fetches before a board is called out


def _fmt_salary(row: dict) -> str:
    lo, hi = row.get("salary_min"), row.get("salary_max")
    if lo is None:
        return "not listed"
    tag = " (from JD)" if row.get("salary_source") == "jd" else ""
    return (f"${lo:,.0f}–${hi:,.0f}{tag}" if hi and hi != lo else f"${lo:,.0f}{tag}")


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
            lines.append(f"- **{p['company']} — {p['title']}** · "
                         f"{p['location'] or 'location n/a'}"
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

    lines += ["", "## Yield per company (active postings)", "",
              "| Company | Active | Title-matched | Qualifying |",
              "|---|---|---|---|"]
    for s in store.yield_stats():
        lines.append(f"| {s['company']} | {s['active']} | "
                     f"{s['title_matched']} | {s['qualifying']} |")
    lines += ["", "_Qualifying = passes titles/seniority/location/salary-floor "
              "deterministically. Yield informs the JOB-26 watchlist rework._", ""]

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
