"""Local postings store: SQLite cache of the watchlist's public ATS data (JOB-27/28).

The store is a CACHE — deleting data/postings.db and re-running the refresh
rebuilds everything except first_seen history. Postings are keyed by
(ats, slug, job_id) and carry a lifecycle: first_seen on insert, last_seen
bumped every refresh that still sees them, removed_at when a SUCCESSFUL fetch
of their board no longer lists them. A failed board fetch never marks
removals (a network hiccup must not cascade into mass false removals).

Enrichment (salary-from-JD, min_years, seniority flag — src/providers/extract)
runs once at insert and again only when the content hash changes; the
seniority flag alone is recomputed every refresh because it depends on
job_criteria.yaml, which can change between runs.

The apply flow never trusts the store for liveness — get_posting still
re-verifies a role is open before an application is prepped.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from . import config
from .providers import extract, locations

_SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
  ats TEXT NOT NULL, slug TEXT NOT NULL, job_id TEXT NOT NULL,
  company TEXT, title TEXT, location TEXT, remote INTEGER,
  salary_min INTEGER, salary_max INTEGER, salary_source TEXT,
  min_years INTEGER, min_years_source TEXT,
  seniority_flag TEXT,
  url TEXT, description TEXT, posted TEXT,
  work_mode TEXT, posted_at TEXT,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, removed_at TEXT,
  content_hash TEXT,
  PRIMARY KEY (ats, slug, job_id)
);
CREATE TABLE IF NOT EXISTS refresh_runs (
  run_at TEXT NOT NULL, total_scanned INTEGER, new_count INTEGER,
  removed_count INTEGER, relisted_count INTEGER,
  companies_ok INTEGER, companies_failed TEXT
);
CREATE TABLE IF NOT EXISTS candidate_boards (
  source TEXT NOT NULL, source_key TEXT NOT NULL,
  company TEXT, ats TEXT, slug TEXT,
  status TEXT NOT NULL,        -- confirmed | unresolved | dead
  active_count INTEGER DEFAULT 0, title_matched INTEGER DEFAULT 0,
  qualifying INTEGER DEFAULT 0, on_watchlist INTEGER DEFAULT 0,
  first_seen TEXT NOT NULL, last_probed TEXT,
  PRIMARY KEY (source, source_key)
);
CREATE TABLE IF NOT EXISTS posting_locations (
  ats TEXT NOT NULL, slug TEXT NOT NULL, job_id TEXT NOT NULL,
  name TEXT NOT NULL, kind TEXT NOT NULL,   -- city | region | remote
  PRIMARY KEY (ats, slug, job_id, name)
);
CREATE INDEX IF NOT EXISTS idx_posting_locations_name ON posting_locations(name);
CREATE TABLE IF NOT EXISTS location_observations (
  raw TEXT NOT NULL, canonical TEXT NOT NULL, kind TEXT NOT NULL,
  seen INTEGER NOT NULL DEFAULT 1, last_seen TEXT,
  PRIMARY KEY (raw, canonical)
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.POSTINGS_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _apply_locations(conn: sqlite3.Connection, key: tuple,
                     location: str, remote) -> str:
    """Normalize one posting's location string: replace its posting_locations
    rows, log raw→canonical observations for curation, return work_mode."""
    norm = locations.normalize(location or "", remote_hint=bool(remote))
    conn.execute("DELETE FROM posting_locations WHERE ats=? AND slug=? "
                 "AND job_id=?", key)
    conn.executemany(
        "INSERT OR IGNORE INTO posting_locations (ats, slug, job_id, name, kind) "
        "VALUES (?,?,?,?,?)",
        [(*key, d["name"], d["kind"]) for d in norm["locations"]])
    now = _now()
    conn.executemany(
        "INSERT INTO location_observations (raw, canonical, kind, seen, last_seen) "
        "VALUES (?,?,?,1,?) ON CONFLICT(raw, canonical) "
        "DO UPDATE SET seen=seen+1, last_seen=excluded.last_seen",
        [(raw, name, kind, now) for raw, name, kind in norm["observations"]])
    return norm["work_mode"]


def _migrate(conn: sqlite3.Connection) -> None:
    """Schema v2 (JOB-55): work_mode + posted_at columns and normalized
    posting_locations, backfilled once from the existing rows."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 2:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(postings)")}
    if "work_mode" not in cols:
        conn.execute("ALTER TABLE postings ADD COLUMN work_mode TEXT")
    if "posted_at" not in cols:
        conn.execute("ALTER TABLE postings ADD COLUMN posted_at TEXT")
    rows = conn.execute(
        "SELECT ats, slug, job_id, location, remote, posted FROM postings"
    ).fetchall()
    for r in rows:
        key = (r["ats"], r["slug"], r["job_id"])
        wm = _apply_locations(conn, key, r["location"], r["remote"])
        conn.execute(
            "UPDATE postings SET work_mode=?, posted_at=? "
            "WHERE ats=? AND slug=? AND job_id=?",
            (wm, locations.parse_posted(r["posted"]), *key))
    conn.execute("PRAGMA user_version=2")
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _content_hash(p: dict) -> str:
    basis = "|".join(str(p.get(k) or "") for k in
                     ("title", "location", "salary_min", "salary_max", "description"))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _enrich(p: dict, excluded: list[str] | None) -> dict:
    """Extraction fields for one normalized posting. API salary wins over JD."""
    smin, smax, source = p.get("salary_min"), p.get("salary_max"), None
    if smin is not None or smax is not None:
        source = "api"
    else:
        smin, smax = extract.extract_salary(p.get("description", ""))
        source = "jd" if smin is not None else None
    years = extract.extract_min_years(p.get("description", ""))
    return {
        "salary_min": smin, "salary_max": smax, "salary_source": source,
        "min_years": years, "min_years_source": "jd" if years is not None else None,
        "seniority_flag": extract.seniority_flag(p.get("title", ""), excluded),
    }


def last_run(conn: sqlite3.Connection | None = None) -> dict | None:
    own = conn is None
    conn = conn or connect()
    try:
        # rowid, not run_at: two runs inside the same second share a timestamp
        # string, which would make the "latest run" ambiguous.
        row = conn.execute(
            "SELECT * FROM refresh_runs ORDER BY rowid DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def store_age_hours() -> float | None:
    """Hours since the last refresh run, or None if the store has never run."""
    if not config.POSTINGS_DB_PATH.exists():
        return None
    run = last_run()
    if not run:
        return None
    ran = datetime.fromisoformat(run["run_at"])
    return (datetime.now(timezone.utc) - ran).total_seconds() / 3600


# --------------------------------------------------------------------------- #
# refresh (called by `python -m src.refresh`)
# --------------------------------------------------------------------------- #
def refresh_from_fetch(fetch_result: dict) -> dict:
    """Ingest one fetch_all_with_status() result. Returns a summary dict the
    digest is built from. Removal safety: only boards whose fetch SUCCEEDED
    this run can have postings marked removed."""
    criteria = config.load_search_criteria().get("baseline", {})
    excluded = criteria.get("excluded_seniority") or []
    postings, errors = fetch_result["postings"], fetch_result["errors"]
    now = _now()

    conn = connect()
    try:
        new_rows, relisted = [], 0
        seen_by_board: dict[tuple, set] = {}
        for p in postings:
            key = (p["ats"], p.get("slug", ""), p.get("job_id", ""))
            if not key[1] or not key[2]:
                continue  # can't key it — skip rather than corrupt the store
            seen_by_board.setdefault((key[0], key[1]), set()).add(key[2])
            chash = _content_hash(p)
            row = conn.execute(
                "SELECT content_hash, removed_at FROM postings "
                "WHERE ats=? AND slug=? AND job_id=?", key).fetchone()
            if row is None:
                e = _enrich(p, excluded)
                wm = _apply_locations(conn, key, p["location"], p["remote"])
                conn.execute(
                    "INSERT INTO postings (ats, slug, job_id, company, title, "
                    "location, remote, salary_min, salary_max, salary_source, "
                    "min_years, min_years_source, seniority_flag, url, "
                    "description, posted, work_mode, posted_at, "
                    "first_seen, last_seen, content_hash) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*key, p["company"], p["title"], p["location"],
                     int(bool(p["remote"])), e["salary_min"], e["salary_max"],
                     e["salary_source"], e["min_years"], e["min_years_source"],
                     e["seniority_flag"], p["url"], p["description"],
                     p.get("posted"), wm, locations.parse_posted(p.get("posted")),
                     now, now, chash))
                new_rows.append({**p, **e, "first_seen": now})
            else:
                if row["removed_at"]:
                    relisted += 1
                if row["content_hash"] != chash:
                    e = _enrich(p, excluded)
                    wm = _apply_locations(conn, key, p["location"], p["remote"])
                    conn.execute(
                        "UPDATE postings SET company=?, title=?, location=?, "
                        "remote=?, salary_min=?, salary_max=?, salary_source=?, "
                        "min_years=?, min_years_source=?, seniority_flag=?, "
                        "url=?, description=?, posted=?, work_mode=?, "
                        "posted_at=?, content_hash=?, "
                        "last_seen=?, removed_at=NULL "
                        "WHERE ats=? AND slug=? AND job_id=?",
                        (p["company"], p["title"], p["location"],
                         int(bool(p["remote"])), e["salary_min"], e["salary_max"],
                         e["salary_source"], e["min_years"], e["min_years_source"],
                         e["seniority_flag"], p["url"], p["description"],
                         p.get("posted"), wm, locations.parse_posted(p.get("posted")),
                         chash, now, *key))
                else:
                    # criteria may have changed since ingest → re-derive the
                    # flag; posted isn't hashed (Greenhouse updated_at drifts),
                    # so keep posted_at current too
                    conn.execute(
                        "UPDATE postings SET last_seen=?, removed_at=NULL, "
                        "seniority_flag=?, posted=?, posted_at=? "
                        "WHERE ats=? AND slug=? AND job_id=?",
                        (now, extract.seniority_flag(p["title"], excluded),
                         p.get("posted"), locations.parse_posted(p.get("posted")),
                         *key))

        # Removal pass — ONLY over boards that fetched successfully this run.
        failed_names = {e["company"] for e in errors}
        removed = 0
        for co in config.load_watchlist():
            if co["name"] in failed_names:
                continue
            board = ((co.get("ats") or "").lower(), co.get("slug", ""))
            current = seen_by_board.get(board, set())
            active = [r["job_id"] for r in conn.execute(
                "SELECT job_id FROM postings WHERE ats=? AND slug=? "
                "AND removed_at IS NULL", board)]
            gone = [jid for jid in active if jid not in current]
            for i in range(0, len(gone), 500):
                chunk = gone[i:i + 500]
                conn.execute(
                    f"UPDATE postings SET removed_at=? WHERE ats=? AND slug=? "
                    f"AND job_id IN ({','.join('?' * len(chunk))})",
                    (now, *board, *chunk))
            removed += len(gone)

        # Run log, with consecutive-failure tracking for board health.
        prev = last_run(conn)
        prev_consec = {}
        if prev and prev.get("companies_failed"):
            prev_consec = {f["company"]: f.get("consecutive", 1)
                           for f in json.loads(prev["companies_failed"])}
        failed = [{**e, "consecutive": prev_consec.get(e["company"], 0) + 1}
                  for e in errors]
        conn.execute(
            "INSERT INTO refresh_runs (run_at, total_scanned, new_count, "
            "removed_count, relisted_count, companies_ok, companies_failed) "
            "VALUES (?,?,?,?,?,?,?)",
            (now, len(postings), len(new_rows), removed, relisted,
             len(config.load_watchlist()) - len(failed), json.dumps(failed)))
        conn.commit()
        return {"run_at": now, "total_scanned": len(postings),
                "new_count": len(new_rows), "removed_count": removed,
                "relisted_count": relisted, "new_rows": new_rows,
                "companies_failed": failed}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# deterministic baseline filter (shared by the digest and the MCP query)
# --------------------------------------------------------------------------- #
def _title_matches(title: str, keywords: list[str] | None) -> bool:
    t = (title or "").lower()
    return any(k.lower() in t for k in (keywords or []))


def _location_ok(row: dict, baseline: dict) -> bool:
    if row.get("remote") and baseline.get("remote_allowed", True):
        return True
    allowed = ((baseline.get("locations_allowed") or [])
               + (baseline.get("relocation_targets") or []))
    loc = (row.get("location") or "").lower()
    return any(a.lower() in loc for a in allowed)


def passes_baseline(row: dict, baseline: dict) -> tuple[bool, str]:
    """(passes, reason-if-not). Salary rule: a DISCLOSED range whose TOP end is
    below the floor is dropped; undisclosed passes (flagged elsewhere)."""
    if not _title_matches(row.get("title", ""), baseline.get("acceptable_titles")):
        return False, "title"
    if row.get("seniority_flag"):
        return False, f"seniority:{row['seniority_flag']}"
    if not _location_ok(row, baseline):
        return False, "location"
    floor = baseline.get("salary_floor")
    if floor and row.get("salary_max") is not None and row["salary_max"] < floor:
        return False, "salary_below_floor"
    return True, ""


# --------------------------------------------------------------------------- #
# store-backed corpus query (JOB-30) + yield stats (JOB-31)
# --------------------------------------------------------------------------- #
def _applied_keys() -> set:
    from . import data  # local import: data.py pulls in fuzzy-match machinery
    return {(data._normalize(a.get("company", "")), data._normalize(a.get("job_title", "")))
            for a in config.load_applications()}


def list_postings_from_store(query: str | None = None, limit: int | None = None,
                             max_years: int | None = None,
                             snippet_chars: int = 140) -> dict:
    """Same shape as watchlist.list_postings, served from the store: active
    postings passing the deterministic baseline, deduped by (company, title),
    plus first_seen / is_new / min_years / salary_source / already_applied.
    `max_years` (optional) additionally drops rows whose advisory min_years
    exceeds it — off by default because the signal is advisory."""
    from .providers.watchlist import _matches_query, _qtokens, _snippet

    baseline = config.load_search_criteria().get("baseline", {})
    conn = connect()
    try:
        run = last_run(conn)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM postings WHERE removed_at IS NULL "
            "ORDER BY company, title")]
        loc_map: dict[tuple, list] = {}
        for lr in conn.execute("SELECT ats, slug, job_id, name FROM "
                               "posting_locations ORDER BY rowid"):
            loc_map.setdefault((lr["ats"], lr["slug"], lr["job_id"]),
                               []).append(lr["name"])
    finally:
        conn.close()

    applied = _applied_keys()
    from . import data as _data
    run_at = run["run_at"] if run else None
    _MODE_RANK = {"remote": 0, "hybrid": 1, "onsite": 2}
    by_key: dict[tuple, dict] = {}
    light: list[dict] = []
    dropped_years = 0
    for r in rows:
        ok, _why = passes_baseline(r, baseline)
        if not ok:
            continue
        if max_years and r.get("min_years") and r["min_years"] > max_years:
            dropped_years += 1
            continue
        locs = loc_map.get((r["ats"], r["slug"], r["job_id"]), [])
        mode = r.get("work_mode") or ("remote" if r["remote"] else "onsite")
        key = (r["company"], r["title"].strip().lower())
        prev = by_key.get(key)
        if prev is not None:
            # same role posted across cities: union the locations, keep the
            # most-flexible work mode, so filters see every variant
            prev["locations"] += [n for n in locs if n not in prev["locations"]]
            if _MODE_RANK[mode] < _MODE_RANK[prev["work_mode"]]:
                prev["work_mode"] = mode
            prev["remote"] = prev["remote"] or bool(r["remote"])
            continue
        entry = {
            "company": r["company"], "title": r["title"], "location": r["location"],
            "locations": list(locs), "work_mode": mode,
            "remote": bool(r["remote"]), "salary_min": r["salary_min"],
            "salary_max": r["salary_max"], "salary_listed": r["salary_min"] is not None,
            "salary_source": r["salary_source"], "min_years": r["min_years"],
            "url": r["url"], "first_seen": r["first_seen"],
            "posted_at": r.get("posted_at"),
            "is_new": r["first_seen"] == run_at,
            "already_applied": (_data._normalize(r["company"]),
                                _data._normalize(r["title"])) in applied,
            "snippet": _snippet(r["description"] or "", snippet_chars),
        }
        by_key[key] = entry
        light.append(entry)

    matched = len(light)
    qtokens = _qtokens(query)
    if qtokens:
        light = [p for p in light if _matches_query(p, qtokens)]
    if limit and limit > 0:
        light = light[:limit]
    return {
        "postings": light, "source": "store",
        "last_refresh": run_at,
        "total_scanned": len(rows), "matched": matched, "returned": len(light),
        "dropped_over_max_years": dropped_years,
        "companies_failed": json.loads(run["companies_failed"]) if run else [],
    }


def posting_description(url: str) -> str | None:
    """Stored JD text for one posting, looked up by URL (Applyer detail view).
    None when the URL isn't in the store — callers fall back to a live read."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT description FROM postings WHERE url=?", (url,)).fetchone()
    finally:
        conn.close()
    return (row["description"] or None) if row else None


# --------------------------------------------------------------------------- #
# startup-discovery candidate ledger (JOB: YC + VC portfolio pulls)
# --------------------------------------------------------------------------- #
def count_board_baseline(postings: list[dict],
                         baseline: dict | None = None) -> tuple[int, int, int]:
    """(active, title_matched, qualifying) for a freshly-fetched board — the
    same deterministic pipeline yield_stats runs on stored postings, so a
    candidate's qualifying count matches what it would show once on the
    watchlist. Enrichment (salary-from-JD, seniority flag) runs per posting."""
    baseline = baseline if baseline is not None else \
        config.load_search_criteria().get("baseline", {})
    excluded = baseline.get("excluded_seniority") or []
    active = len(postings)
    title_matched = qualifying = 0
    for p in postings:
        if not _title_matches(p.get("title", ""), baseline.get("acceptable_titles")):
            continue
        title_matched += 1
        row = {**p, **_enrich(p, excluded)}
        if passes_baseline(row, baseline)[0]:
            qualifying += 1
    return active, title_matched, qualifying


def load_candidates(conn: sqlite3.Connection | None = None) -> dict:
    """Ledger keyed by (source, source_key) → row dict, for incremental probing."""
    own = conn is None
    conn = conn or connect()
    try:
        return {(r["source"], r["source_key"]): dict(r)
                for r in conn.execute("SELECT * FROM candidate_boards")}
    finally:
        if own:
            conn.close()


def upsert_candidate(conn: sqlite3.Connection, cand: dict) -> None:
    """Insert or update one candidate probe result. first_seen is preserved on
    update; everything else reflects the latest probe."""
    key = (cand["source"], cand["source_key"])
    existing = conn.execute(
        "SELECT first_seen FROM candidate_boards WHERE source=? AND source_key=?",
        key).fetchone()
    first_seen = existing["first_seen"] if existing else _now()
    conn.execute(
        "INSERT INTO candidate_boards (source, source_key, company, ats, slug, "
        "status, active_count, title_matched, qualifying, on_watchlist, "
        "first_seen, last_probed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source, source_key) DO UPDATE SET "
        "company=excluded.company, ats=excluded.ats, slug=excluded.slug, "
        "status=excluded.status, active_count=excluded.active_count, "
        "title_matched=excluded.title_matched, qualifying=excluded.qualifying, "
        "on_watchlist=excluded.on_watchlist, last_probed=excluded.last_probed",
        (cand["source"], cand["source_key"], cand.get("company"),
         cand.get("ats"), cand.get("slug"), cand["status"],
         cand.get("active_count", 0), cand.get("title_matched", 0),
         cand.get("qualifying", 0), int(bool(cand.get("on_watchlist"))),
         first_seen, cand.get("last_probed") or _now()))


def yield_stats() -> list[dict]:
    """Per-company sourcing yield over active postings: scanned / title-matched /
    passing the full baseline. The evidence base for watchlist rework (JOB-26)."""
    baseline = config.load_search_criteria().get("baseline", {})
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM postings WHERE removed_at IS NULL")]
    finally:
        conn.close()
    stats: dict[str, dict] = {}
    for r in rows:
        s = stats.setdefault(r["company"], {"company": r["company"], "active": 0,
                                            "title_matched": 0, "qualifying": 0})
        s["active"] += 1
        if _title_matches(r["title"], baseline.get("acceptable_titles")):
            s["title_matched"] += 1
            if passes_baseline(r, baseline)[0]:
                s["qualifying"] += 1
    return sorted(stats.values(), key=lambda s: (-s["qualifying"], -s["title_matched"],
                                                 s["company"]))
