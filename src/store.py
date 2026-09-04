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
    """Additive migrations, each gated on its own PRAGMA user_version step so
    a database at any prior version catches up without re-running old ones."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 2:
        # Schema v2 (JOB-55): work_mode + posted_at columns and normalized
        # posting_locations, backfilled once from the existing rows.
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
    if version < 3:
        # Schema v3 (dev-loop metrics): persist per-run sourcing yield.
        # Historic rows stay NULL — the counts can't be honestly reconstructed
        # because the criteria in force at the time are unknown.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(refresh_runs)")}
        if "new_qualifying" not in cols:
            conn.execute(
                "ALTER TABLE refresh_runs ADD COLUMN new_qualifying INTEGER")
        if "new_title_matched" not in cols:
            conn.execute(
                "ALTER TABLE refresh_runs ADD COLUMN new_title_matched INTEGER")
        conn.execute("PRAGMA user_version=3")
        conn.commit()
    if version < 4:
        # Schema v4: distinct-role (deduped by company+title) counterparts to
        # the v3 columns — a role cross-posted to several cities inflates the
        # raw new_qualifying/new_title_matched, so this is the number that
        # matches what the digest and feed actually show. Both are kept:
        # v3 stays raw so its trend history stays continuous. Old rows NULL.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(refresh_runs)")}
        if "new_qualifying_roles" not in cols:
            conn.execute(
                "ALTER TABLE refresh_runs ADD COLUMN new_qualifying_roles INTEGER")
        if "new_title_matched_roles" not in cols:
            conn.execute(
                "ALTER TABLE refresh_runs ADD COLUMN new_title_matched_roles INTEGER")
        conn.execute("PRAGMA user_version=4")
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


# Consecutive failed fetches before a board is called dark. Lives here rather
# than in refresh.py (where it used to) because the store's own aggregates now
# need it too, and two copies of a threshold drift (JOB-137).
_DARK_RUNS = 3


def dark_boards(threshold: int = _DARK_RUNS,
                conn: sqlite3.Connection | None = None) -> set:
    """Company names whose board has failed `threshold` consecutive fetches.

    Read off the latest refresh_runs row, whose `companies_failed` JSON already
    carries the `consecutive` counter refresh_from_fetch maintains. Keyed by
    COMPANY NAME, matching both that counter's key and the postings.company
    column, so the same set filters run summaries and stored rows alike.

    Callers use this to stop a dark board's stale rows being counted as live
    (JOB-137). Note it is a REPORTING filter only — nothing here deletes or
    marks rows, because a board can go dark from a run of network failures and
    come back, and the removal pass deliberately never acts on a failed fetch.
    Fails closed to an empty set on a store that has never run or a malformed
    log: over-counting stale rows is a smaller lie than hiding live ones.
    """
    run = last_run(conn)
    if not run:
        return set()
    try:
        failed = json.loads(run["companies_failed"] or "[]")
    except (TypeError, ValueError):
        return set()
    return {f["company"] for f in failed
            if isinstance(f, dict) and f.get("company")
            and f.get("consecutive", 1) >= threshold}


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
    digest is built from.

    Two removal passes, and the distinction between them is the whole safety
    story (JOB-137). The FETCH-FAILURE pass keeps everything: only boards whose
    fetch SUCCEEDED this run can have postings marked removed, because a network
    hiccup must never cascade into mass false removals. The CONFIG-REMOVAL pass
    retires everything: an active row whose (ats, slug) is in no watchlist entry
    is orphaned and gets marked removed, because a board vanishing from
    watchlist.yaml is a deliberate edit, never a transient blip.

    Without the second pass, dropping or re-slugging a board strands its rows as
    permanently active — invisible to the removal loop, which only iterates
    boards still IN the watchlist, and invisible to the dark-board filter, which
    only sees boards still failing. Repointing Temporal Technologies from
    greenhouse/temporaltechnologies to ashby/temporal is the first edit that
    would have hit it, with 53 undead rows carrying dead 404 apply URLs.
    """
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
        watchlist = config.load_watchlist()
        failed_names = {e["company"] for e in errors}
        removed = 0
        for co in watchlist:
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

        # Orphaned-board sweep (JOB-137) — the config-removal half of the
        # contract in this function's docstring. The loop above can only ever
        # reach boards still listed in watchlist.yaml, so a board that was
        # dropped or re-slugged leaves its rows active forever. Keyed on
        # (ats, slug) rather than company name because a repoint keeps the name
        # and changes exactly this pair.
        #
        # Guarded on a non-empty watchlist: load_watchlist() returns [] for a
        # missing or unreadable watchlist.yaml, and treating that as "every
        # board was deliberately removed" would retire the entire store on a
        # config read error. An empty watchlist is a failure to read config,
        # not a decision.
        watch_boards = {((co.get("ats") or "").lower(), co.get("slug", ""))
                        for co in watchlist}
        if watch_boards:
            orphaned = [(r["ats"], r["slug"], r["job_id"]) for r in conn.execute(
                "SELECT ats, slug, job_id FROM postings WHERE removed_at IS NULL")
                if (r["ats"], r["slug"]) not in watch_boards]
            conn.executemany(
                "UPDATE postings SET removed_at=? "
                "WHERE ats=? AND slug=? AND job_id=?",
                [(now, *k) for k in orphaned])
            removed += len(orphaned)

        # Run log, with consecutive-failure tracking for board health.
        prev = last_run(conn)
        prev_consec = {}
        if prev and prev.get("companies_failed"):
            prev_consec = {f["company"]: f.get("consecutive", 1)
                           for f in json.loads(prev["companies_failed"])}
        failed = [{**e, "consecutive": prev_consec.get(e["company"], 0) + 1}
                  for e in errors]
        # Per-run sourcing yield (schema v3): of this run's NEW postings, how
        # many title-match / pass the full baseline under the criteria in
        # force right now. Previously computed for the digest and discarded.
        new_title_matched = sum(
            1 for p in new_rows
            if _title_matches(p.get("title", ""),
                              criteria.get("acceptable_titles")))
        new_qualifying = sum(
            1 for p in new_rows if passes_baseline(p, criteria)[0])
        # Distinct-role equivalents (schema v4): filter the raw new rows
        # first, THEN collapse to role keys, so a role that only qualifies in
        # one of its cross-posted cities still counts.
        new_title_matched_roles = len({
            _role_key(r) for r in new_rows
            if _title_matches(r.get("title", ""), criteria.get("acceptable_titles"))})
        new_qualifying_roles = len({
            _role_key(r) for r in new_rows if passes_baseline(r, criteria)[0]})
        conn.execute(
            "INSERT INTO refresh_runs (run_at, total_scanned, new_count, "
            "removed_count, relisted_count, companies_ok, companies_failed, "
            "new_qualifying, new_title_matched, "
            "new_qualifying_roles, new_title_matched_roles) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now, len(postings), len(new_rows), removed, relisted,
             len(watchlist) - len(failed), json.dumps(failed),
             new_qualifying, new_title_matched,
             new_qualifying_roles, new_title_matched_roles))
        conn.commit()
        return {"run_at": now, "total_scanned": len(postings),
                "new_count": len(new_rows), "removed_count": removed,
                "relisted_count": relisted, "new_rows": new_rows,
                "new_qualifying": new_qualifying,
                "new_title_matched": new_title_matched,
                "new_qualifying_roles": new_qualifying_roles,
                "new_title_matched_roles": new_title_matched_roles,
                "companies_failed": failed}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# deterministic baseline filter (shared by the digest and the MCP query)
# --------------------------------------------------------------------------- #
def _title_matches(title: str, keywords: list[str] | None) -> bool:
    t = (title or "").lower()
    return any(k.lower() in t for k in (keywords or []))


def _role_key(row: dict) -> tuple:
    """Identity for a role independent of which city it's posted in — the
    same role cross-posted to several cities collapses to one key. Applies to
    both DB rows and freshly-fetched dicts, hence the defensive .get."""
    return (row["company"], (row.get("title") or "").strip().lower())


def _location_ok(row: dict, baseline: dict) -> bool:
    return not _location_reason(row, baseline)


def _location_reason(row: dict, baseline: dict) -> str:
    """Empty when the row's location is workable, else the failure reason.

    A remote row is workable unless it is positively scoped to a country the
    user cannot work from (JOB-123): "Remote - India" and "Canada - Remote (ON,
    AB, BC, or NS Only)" are remote, but not remote *for this user*. The check
    lives in providers/locations (it owns the location vocabulary) and is a
    DENY-list — it needs positive foreign evidence AND no allowed signal, so
    bare "Remote", empty, and anything unparseable keep passing.

    Which countries count as allowed is the baseline's optional
    `allowed_countries` knob; when absent it derives to the US plus any country
    named in locations_allowed / relocation_targets. Non-remote rows are
    untouched: they still take the locations_allowed substring match.
    """
    allowed = ((baseline.get("locations_allowed") or [])
               + (baseline.get("relocation_targets") or []))
    if row.get("remote") and baseline.get("remote_allowed", True):
        if locations.foreign_scope(row.get("location") or "",
                                   baseline.get("allowed_countries"), allowed):
            return "location:foreign_remote"
        return ""
    loc = (row.get("location") or "").lower()
    return "" if any(a.lower() in loc for a in allowed) else "location"


def passes_baseline(row: dict, baseline: dict) -> tuple[bool, str]:
    """(passes, reason-if-not). Salary rule: a DISCLOSED range whose TOP end is
    below the floor is dropped; undisclosed passes (flagged elsewhere).
    Seniority is computed here from the row's title and THIS baseline's
    excluded_seniority — never from the stored seniority_flag column, which
    reflects whichever profile's criteria ran the last refresh."""
    if not _title_matches(row.get("title", ""), baseline.get("acceptable_titles")):
        return False, "title"
    flag = extract.seniority_flag(row.get("title", ""),
                                  baseline.get("excluded_seniority"))
    if flag:
        return False, f"seniority:{flag}"
    why = _location_reason(row, baseline)
    if why:
        return False, why
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
    failed_baseline = 0
    hidden_by_reason: dict[str, int] = {}
    for r in rows:
        ok, _why = passes_baseline(r, baseline)
        if not ok:
            failed_baseline += 1
            hidden_by_reason[_why] = hidden_by_reason.get(_why, 0) + 1
            continue
        if max_years and r.get("min_years") and r["min_years"] > max_years:
            dropped_years += 1
            continue
        locs = loc_map.get((r["ats"], r["slug"], r["job_id"]), [])
        mode = r.get("work_mode") or ("remote" if r["remote"] else "onsite")
        key = _role_key(r)
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
        # hidden_by_criteria is the total; hidden_by_reason breaks it down by
        # passes_baseline reason (JOB-123) and sums to it.
        "hidden_by_criteria": failed_baseline,
        "hidden_by_reason": hidden_by_reason,
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
    watchlist. Enrichment (salary-from-JD, seniority flag) runs per posting.
    Counts are distinct roles (company+title), not raw city-variant rows —
    filtered first, then collapsed, same order as yield_stats."""
    baseline = baseline if baseline is not None else \
        config.load_search_criteria().get("baseline", {})
    excluded = baseline.get("excluded_seniority") or []
    active = len({_role_key(p) for p in postings})
    title_keys: set = set()
    qualifying_keys: set = set()
    for p in postings:
        if not _title_matches(p.get("title", ""), baseline.get("acceptable_titles")):
            continue
        title_keys.add(_role_key(p))
        row = {**p, **_enrich(p, excluded)}
        if passes_baseline(row, baseline)[0]:
            qualifying_keys.add(_role_key(p))
    return active, len(title_keys), len(qualifying_keys)


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
    passing the full baseline. The evidence base for watchlist rework (JOB-26).
    Counts are distinct roles (company+title) — a role cross-posted to several
    cities counts once, filtered first and collapsed to keys second so a role
    that only qualifies in one city still counts.

    A DARK board's rows (JOB-137) keep their row here with `stale: True` rather
    than being dropped: the board still has stored postings and hiding them
    would make a board that quietly 404'd look identical to one that legitimately
    posts nothing. What they must NOT do is count as live supply — so stale rows
    are sorted last and every aggregate over this table (company_spread, the
    digest's corpus counts) excludes them.
    """
    baseline = config.load_search_criteria().get("baseline", {})
    conn = connect()
    try:
        dark = dark_boards(conn=conn)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM postings WHERE removed_at IS NULL")]
    finally:
        conn.close()
    active_keys: dict[str, set] = {}
    title_keys: dict[str, set] = {}
    qualifying_keys: dict[str, set] = {}
    for r in rows:
        active_keys.setdefault(r["company"], set()).add(_role_key(r))
        if _title_matches(r["title"], baseline.get("acceptable_titles")):
            title_keys.setdefault(r["company"], set()).add(_role_key(r))
            if passes_baseline(r, baseline)[0]:
                qualifying_keys.setdefault(r["company"], set()).add(_role_key(r))
    stats = [{"company": c, "active": len(active_keys[c]),
              "title_matched": len(title_keys.get(c, ())),
              "qualifying": len(qualifying_keys.get(c, ())),
              "stale": c in dark}
             for c in active_keys]
    return sorted(stats, key=lambda s: (s["stale"], -s["qualifying"],
                                        -s["title_matched"], s["company"]))


def yield_history(days: int = 30) -> list[dict]:
    """Per-day sourcing yield from refresh_runs (schema v3/v4), newest first.
    A day can hold several runs (scheduled + manual): counts are summed,
    board failures come from the day's last run. new_qualifying and its
    distinct-role counterpart new_qualifying_roles are None for days whose
    runs all predate the respective column."""
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT rowid, * FROM refresh_runs "
            "WHERE date(run_at) >= date('now', ?) ORDER BY rowid",
            (f"-{int(days)} days",))]
    finally:
        conn.close()
    by_day: dict[str, dict] = {}
    for r in rows:
        day = (r["run_at"] or "")[:10]
        d = by_day.setdefault(day, {"date": day, "runs": 0, "new_count": 0,
                                    "removed_count": 0, "new_qualifying": None,
                                    "new_title_matched": None,
                                    "new_qualifying_roles": None,
                                    "new_title_matched_roles": None,
                                    "total_scanned": 0, "boards_failed": 0})
        d["runs"] += 1
        d["new_count"] += r.get("new_count") or 0
        d["removed_count"] += r.get("removed_count") or 0
        d["total_scanned"] = max(d["total_scanned"], r.get("total_scanned") or 0)
        for k in ("new_qualifying", "new_title_matched",
                  "new_qualifying_roles", "new_title_matched_roles"):
            if r.get(k) is not None:
                d[k] = (d[k] or 0) + r[k]
        d["boards_failed"] = len(json.loads(r.get("companies_failed") or "[]"))
    return sorted(by_day.values(), key=lambda d: d["date"], reverse=True)


# --------------------------------------------------------------------------- #
# company-concentration stats (JOB-113)
# --------------------------------------------------------------------------- #
_TOP_N = 5


def _concentration(counts: dict[str, int]) -> dict:
    """total / distinct-company / top-N share for a company -> count histogram.
    An empty histogram reports zeros instead of dividing by zero, so the digest
    still renders on a fresh store or a missing application log."""
    total = sum(counts.values())
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_TOP_N]
    share = round(100.0 * sum(n for _, n in top) / total, 1) if total else 0.0
    return {
        "total": total,
        "companies": len(counts),
        "top5_share_pct": share,
        "top5": [{"company": c, "count": n} for c, n in top],
    }


def company_spread() -> dict:
    """Company concentration at both ends of the funnel — the qualifying corpus
    and the application log — so posting/application spread is self-reporting
    instead of hand-counted every time someone asks (JOB-113).

    The qualifying side counts DISTINCT ROLES on the same
    (company, title.strip().lower()) key list_postings_from_store dedupes on:
    one role posted across five cities is five rows but one opportunity, and
    counting raw rows is exactly what inflates the per-company yield table.

    The application side counts EVERY record in the log, not only
    status == "submitted": a manual submission is still an application spent on
    that company, and _applied_keys already treats the two identically. The
    per-status breakdown rides along so the distinction stays visible.

    Rows belonging to a DARK board are excluded from the qualifying side
    (JOB-137). A board that has 404'd for days is not supply — counting its
    frozen rows would report a corpus that no longer exists and would let a
    dead board keep inflating the top-5 concentration share.

    Pure aggregation over the current store — nothing is persisted, so this
    answers "how concentrated are we?" exactly, but not "how did that change
    since yesterday?". A day-over-day trend needs refresh_runs columns; see the
    JOB-113 notes before adding them.
    """
    baseline = config.load_search_criteria().get("baseline", {})
    conn = connect()
    try:
        dark = dark_boards(conn=conn)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM postings WHERE removed_at IS NULL")]
    finally:
        conn.close()

    roles = {(r["company"], (r["title"] or "").strip().lower())
             for r in rows
             if r["company"] not in dark and passes_baseline(r, baseline)[0]}
    qualifying: dict[str, int] = {}
    for company, _title in roles:
        qualifying[company] = qualifying.get(company, 0) + 1

    by_company: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for a in config.load_applications():
        company = a.get("company") or "(unknown)"
        by_company[company] = by_company.get(company, 0) + 1
        status = a.get("status") or "(unknown)"
        by_status[status] = by_status.get(status, 0) + 1

    return {
        "qualifying": _concentration(qualifying),
        "applications": {**_concentration(by_company),
                         "by_status": dict(sorted(by_status.items()))},
    }
