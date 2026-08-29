#!/usr/bin/env python3
"""Audit what the title gate silently throws away (JOB-115).

The deterministic baseline in src/store.py rejects in a fixed order —
title, then seniority, then location, then salary floor — and the title
gate is by far the largest filter: on a full store it discards ~94% of all
active rows. `passes_baseline` returns the reason, but nothing keeps it:
the rows vanish between one refresh and the next and nobody can review
whether `job_criteria.yaml`'s `acceptable_titles` is drawn too tightly.

This report makes that discard auditable. It re-runs the individual
predicates (it does NOT change them) and keeps only the rows that fail the
title gate ALONE — seniority, location and salary all pass. Those are the
rows a single edit to `acceptable_titles` would let through, so they are
the only ones worth reading.

Two tiers, because they need different reactions:

  Tier 1 - non-contiguous phrase hits. Every (non-stopword) token of some
    configured phrase is present in the title, just not adjacent:
    "Product Marketing Manager" against the phrase "product manager".
    `_title_matches` is a substring test, so these miss by word order
    alone. These are the actionable near-misses.

  Tier 2 - generic single-token hits. The title shares a token with the
    vocabulary but matches no full phrase ("Staff Engineer" via "staff").
    Mostly noise, but the shape of the noise tells you which tokens are
    too generic to be worth widening on.

Usage:  python scripts/near_misses.py
        python -m src.refresh --near-misses

Writes data/near-misses-latest.md. The report is regenerated from the
store on every run, exactly like data/discovery-latest.md, so it lives in
DATA_DIR and is gitignored rather than committed.
"""

import re
import sys
from pathlib import Path

# Run as a plain script (`python scripts/near_misses.py`) as well as via the
# refresh module, so the repo root has to be importable either way.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, store  # noqa: E402

# Regenerated every run, never committed - same contract as DISCOVERY_REPORT_PATH.
# Defined here rather than in src/config.py: this is the only consumer, and
# config.py is being rewritten wholesale on another branch.
REPORT_PATH = config.DATA_DIR / "near-misses-latest.md"

# Glue words dropped when deriving tokens from the configured title phrases.
# "Strategy and Operations", "Strategy & Operations" and "Chief of Staff"
# contribute `and`, `of` and `&`, each of which appears in a large fraction of
# ALL job titles. Left in, they make almost every discarded row a "near miss"
# and drown the signal the report exists to surface.
_STOPWORDS = {"and", "of", "&"}

# How many example titles to print per group before collapsing the tail. The
# suppressed remainder is always stated - a silent cap would read as coverage.
_EXAMPLES_PER_GROUP = 8


def _tokens(text: str) -> list[str]:
    """Lowercased alphanumeric word tokens, stopwords removed."""
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if t not in _STOPWORDS]


def _title_only_failures(rows: list[dict], baseline: dict) -> list[dict]:
    """Active rows that fail ONLY the title gate.

    `passes_baseline` short-circuits on title, so its reason string cannot
    tell us whether such a row would ALSO have failed later gates. The
    predicates are re-run individually here - read-only, no behaviour change.
    """
    floor = baseline.get("salary_floor")
    out = []
    for r in rows:
        if store._title_matches(r.get("title", ""), baseline.get("acceptable_titles")):
            continue                                    # not a discard at all
        if r.get("seniority_flag"):
            continue                                    # would fail seniority too
        if not store._location_ok(r, baseline):
            continue                                    # would fail location too
        if (floor and r.get("salary_max") is not None
                and r["salary_max"] < floor):
            continue                                    # would fail the floor too
        out.append(r)
    return out


def _classify(rows: list[dict], phrases: list[str]) -> tuple[list, list]:
    """Split title-only failures into Tier 1 (all tokens of some phrase present,
    just not adjacent) and Tier 2 (shares a token, matches no whole phrase)."""
    phrase_tokens = [(p, set(_tokens(p))) for p in phrases]
    phrase_tokens = [(p, t) for p, t in phrase_tokens if t]
    vocab = {tok for _p, toks in phrase_tokens for tok in toks}

    tier1, tier2 = [], []
    for r in rows:
        present = set(_tokens(r.get("title", "")))
        if not present:
            continue
        hits = sorted(p for p, toks in phrase_tokens if toks <= present)
        if hits:
            tier1.append({**r, "matched": hits})
        else:
            shared = sorted(present & vocab)
            if shared:
                tier2.append({**r, "matched": shared})
    return tier1, tier2


def _role_key(row: dict) -> tuple:
    """Same (company, title) identity list_postings_from_store and
    store.company_spread dedupe on, so headline counts are comparable across
    the two reports instead of being inflated by per-city duplicate rows."""
    return (row.get("company"), (row.get("title") or "").strip().lower())


def _group(rows: list[dict], key) -> list[tuple]:
    """[(key, rows)] sorted by row count desc, then key - biggest group first."""
    groups: dict = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    return sorted(groups.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))


def _by_company_lines(rows: list[dict], noun: str) -> list[str]:
    lines = []
    for company, group in _group(rows, lambda r: r.get("company") or "(unknown)"):
        roles = _group(group, lambda r: (r.get("title") or "").strip())
        lines.append(f"- **{company}** — {len(group)} "
                     f"{noun if len(group) != 1 else noun.rstrip('s')} "
                     f"suppressed ({len(roles)} distinct)")
        for title, rs in roles[:_EXAMPLES_PER_GROUP]:
            via = ", ".join(rs[0]["matched"][:3])
            extra = f" x{len(rs)}" if len(rs) > 1 else ""
            lines.append(f"  - {title}{extra} — via {via}")
        if len(roles) > _EXAMPLES_PER_GROUP:
            lines.append(f"  - ...and {len(roles) - _EXAMPLES_PER_GROUP} "
                         f"more distinct titles not listed")
    return lines


def _by_match_lines(rows: list[dict], label: str) -> list[str]:
    """Rows counted once per match, so a title hitting two tokens appears under
    both - these group totals overlap and do not sum to the tier total."""
    counts: dict = {}
    for r in rows:
        for m in r["matched"]:
            entry = counts.setdefault(m, {"rows": 0, "roles": set()})
            entry["rows"] += 1
            entry["roles"].add(_role_key(r))
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1]["rows"], kv[0]))
    lines = [f"| {label} | Rows suppressed | Distinct roles |",
             "| --- | ---: | ---: |"]
    for match, e in ordered:
        lines.append(f"| {match} | {e['rows']} | {len(e['roles'])} |")
    return lines


def build_report() -> str:
    baseline = config.load_search_criteria().get("baseline", {})
    phrases = baseline.get("acceptable_titles") or []

    conn = store.connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM postings WHERE removed_at IS NULL")]
    finally:
        conn.close()

    title_only = _title_only_failures(rows, baseline)
    tier1, tier2 = _classify(title_only, phrases)
    t1_roles = {_role_key(r) for r in tier1}

    lines = [
        "# Title-gate near misses",
        "",
        f"Active postings scanned: **{len(rows):,}**. Of those, "
        f"**{len(title_only):,}** fail the title gate and nothing else — "
        "they clear seniority, location and the salary floor, so widening "
        "`acceptable_titles` is all that stands between them and the "
        "qualifying corpus.",
        "",
        f"- **Tier 1 (non-contiguous phrase hits):** {len(tier1):,} rows / "
        f"**{len(t1_roles):,} distinct roles** — every token of a configured "
        "phrase is in the title, just not adjacent. The actionable list.",
        f"- **Tier 2 (generic single-token hits):** {len(tier2):,} rows — "
        "shares a token with the vocabulary but matches no whole phrase. "
        "Mostly noise; read it for which tokens are too generic.",
        "",
        "_Stopwords `and` / `of` / `&` are excluded when deriving tokens from "
        "the configured phrases: they come from \"Strategy and Operations\", "
        "\"Strategy & Operations\" and \"Chief of Staff\" and appear in a large "
        "fraction of all job titles, so keeping them would mark nearly every "
        "discarded row a near miss. Distinct roles use the same "
        "(company, title) key as the qualifying corpus, so per-city duplicates "
        "do not inflate the headline._",
        "",
    ]

    lines += ["## Tier 1 — non-contiguous phrase hits", ""]
    if tier1:
        lines += ["### By company", ""] + _by_company_lines(tier1, "rows")
        lines += ["", "### By matched phrase", ""] + _by_match_lines(tier1, "Phrase")
    else:
        lines.append("_None in the current store._")
    lines.append("")

    lines += ["## Tier 2 — generic single-token hits", ""]
    if tier2:
        lines += ["### By matched token", ""] + _by_match_lines(tier2, "Token")
        lines += ["", "### By company", ""] + _by_company_lines(tier2, "rows")
    else:
        lines.append("_None in the current store._")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    REPORT_PATH.write_text(build_report(), encoding="utf-8")
    # ASCII only: scheduled runs print to a cp1252 Windows console.
    print(f"near-miss report -> {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
