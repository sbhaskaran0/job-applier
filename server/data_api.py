"""REST endpoints serving the agent's real data files to the Applyer frontend.

Read paths reuse the same modules the MCP server uses (src.store, src.config,
src.providers.watchlist) so the UI shows exactly what /find-jobs would see.
Write paths are deliberately narrow: watchlist append, whitelisted profile
string fields (comment-preserving line edits), and resume/context uploads.
"""

import asyncio
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import yaml

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel

from src import (config, data as appdata, profiles as profiles_mod,
                 refresh as refresh_job, store)
from src.providers.watchlist import add_company, detect_ats_slug, get_posting

router = APIRouter(prefix="/api")

# Profile keys the UI may edit — simple string facts only. EEO entries are
# dicts flagged eeo:true and are never exposed or written by the web API.
EDITABLE_PROFILE_KEYS = [
    "first_name", "last_name", "full_name", "email", "phone",
    "address", "city", "state", "country", "location",
    "linkedin_url", "github_url", "portfolio_url", "website_url",
    "current_company", "current_title", "years_experience",
    "work_authorization", "requires_sponsorship", "willing_to_relocate",
    "how_did_you_hear", "desired_salary", "notice_period",
]

_CONTEXT_KINDS = {".md": "notes", ".txt": "notes", ".pdf": "document"}


def _ats_from_url(url: str) -> str:
    got = detect_ats_slug(url or "")
    return got[0] if got else ""


# --------------------------------------------------------------------------- #
# status / postings / applications
# --------------------------------------------------------------------------- #
@router.get("/status")
def status():
    run = store.last_run() if config.POSTINGS_DB_PATH.exists() else None
    new_count = 0
    if run:
        result = store.list_postings_from_store()
        new_count = sum(1 for p in result["postings"] if p["is_new"])
    return {
        "last_refresh": run["run_at"] if run else None,
        "store_age_hours": store.store_age_hours(),
        "new_qualifying": new_count,
        "watchlist_count": len(config.load_watchlist()),
    }


@router.get("/postings")
def postings(query: str | None = None):
    if not config.POSTINGS_DB_PATH.exists():
        return {"postings": [], "last_refresh": None,
                "note": "postings store missing — run: python -m src.refresh"}
    result = store.list_postings_from_store(query=query)
    for p in result["postings"]:
        p["ats"] = _ats_from_url(p["url"])
    return result


# One refresh at a time — a second click while a run is in flight gets a 409
# instead of a duplicate board sweep.
_refresh_lock = asyncio.Lock()


@router.post("/refresh")
async def refresh_postings():
    """Run the same LLM-free ingest as `python -m src.refresh`: fetch every
    watchlist board, upsert data/postings.db, regenerate the digest."""
    if _refresh_lock.locked():
        raise HTTPException(409, "a refresh is already running")
    async with _refresh_lock:
        try:
            summary = await refresh_job.run()
        except Exception as exc:  # surface board-sweep failures to the UI
            raise HTTPException(500, f"refresh failed: {exc}")
    return {
        "run_at": summary["run_at"],
        "total_scanned": summary["total_scanned"],
        "new_count": summary["new_count"],
        "removed_count": summary["removed_count"],
        "relisted_count": summary["relisted_count"],
        "boards_failed": [f["company"] for f in summary["companies_failed"]],
    }


@router.get("/posting")
async def posting_detail(url: str):
    """Full job description for one posting — served from the store when
    cached, else a live ATS API read (the same deep-read /find-jobs uses)."""
    if config.POSTINGS_DB_PATH.exists():
        description = store.posting_description(url)
        if description:
            return {"url": url, "found": True, "source": "store",
                    "description": description}
    got = await get_posting(url)
    got["source"] = "live"
    return got


def _application_key(a: dict) -> str:
    """Opaque row identity the UI hands back to the outcome endpoint. It is the
    apply path's own dedupe key (`src.data._application_key`) joined on "|" —
    safe as a single string because `_normalize` strips everything outside
    `[a-z0-9 ]`, so no part can ever contain the separator. The UI must treat it
    as opaque; the server resolves it by recomputing the key for each record
    rather than parsing it back into fields."""
    return "|".join(appdata._application_key(
        a.get("company", ""), a.get("job_title", ""), a.get("url", "")))


def _application_row(a: dict) -> dict:
    """One applications-table row: the stored record plus the JOB-107 outcome
    keys defaulted and its `key` stamped on. Built as a SHALLOW COPY on purpose
    — a GET must never write defaults back into the dicts config handed us, so
    records on disk keep exactly the shape the apply flow wrote (no `outcome`
    key at all on everything logged before outcomes existed)."""
    return {**a,
            "outcome": a.get("outcome") or "none",
            "outcome_date": a.get("outcome_date") or "",
            "key": _application_key(a)}


@router.get("/applications")
def applications():
    records = config.load_applications()
    return {"applications": [_application_row(a) for a in records
                             if isinstance(a, dict)],
            "stats": appdata.application_outcome_stats(records)}


class OutcomeSet(BaseModel):
    key: str
    outcome: str
    outcome_date: str = ""


@router.post("/applications/outcome")
def set_outcome(body: OutcomeSet):
    """Record whether the company ever replied. The key travels in the BODY, not
    the path: `_application_key` falls back to the normalized URL when
    (company, title) is incomplete, and that fallback contains `://` and `/` —
    Starlette percent-decodes the path before routing, so an encoded key could
    never survive a single-segment `{key}` match."""
    if body.outcome not in appdata.APPLICATION_OUTCOMES:
        raise HTTPException(
            400, f"outcome must be one of {list(appdata.APPLICATION_OUTCOMES)}")
    records = config.load_applications()
    target = next((a for a in records if isinstance(a, dict)
                   and _application_key(a) == body.key), None)
    if target is None:
        raise HTTPException(404, f"no application matches key {body.key!r}")
    result = appdata.set_application_outcome(
        company=target.get("company", ""), job_title=target.get("job_title", ""),
        url=target.get("url", ""), outcome=body.outcome,
        outcome_date=body.outcome_date)
    if result.get("status") != "updated":
        raise HTTPException(400, result.get("reason", result.get("status", "")))
    return {"application": _application_row(result["application"]),
            "stats": appdata.application_outcome_stats(
                config.load_applications())}


# --------------------------------------------------------------------------- #
# profile (read + whitelisted write-back) / context files
# --------------------------------------------------------------------------- #
@router.get("/profile")
def profile():
    prof = config.load_user_profile()
    ap = config.active_profile()
    facts = {k: v for k, v in prof.items()
             if k in EDITABLE_PROFILE_KEYS and isinstance(v, str)}
    eeo_present = sorted(k for k, v in prof.items()
                         if isinstance(v, dict) and v.get("eeo"))
    filled = sum(1 for v in facts.values() if str(v).strip())
    return {
        "facts": facts,
        "eeo_fields_present": eeo_present,
        "completeness": round(100 * filled / max(1, len(EDITABLE_PROFILE_KEYS))),
        "resume_pdf": ap.resume_pdf.exists(),
        "resume_docx": config.base_resume_docx() is not None,
        "context_files": _context_files(),
        "profile_id": ap.profile_id,
        "profile_dir": ("repo root (legacy)" if ap.legacy
                        else f"profiles/{ap.profile_id}"),
        # Windows st_ctime is directory creation time — good enough for a label
        "created": datetime.fromtimestamp(ap.root.stat().st_ctime)
                           .strftime("%d %b %Y"),
    }


def _context_files() -> list[dict]:
    files = []
    for p in sorted(config.CONTEXT_DIR.iterdir()):
        if p.is_file() and p.suffix.lower() in _CONTEXT_KINDS:
            files.append({"name": p.name,
                          "kind": _CONTEXT_KINDS[p.suffix.lower()],
                          "size": p.stat().st_size})
    return files


class ProfileUpdate(BaseModel):
    facts: dict[str, str]


def _set_profile_fact(text: str, key: str, value: str) -> str:
    """Comment-preserving line edit: replace the value of a `key: "..."` line
    (keeping any trailing comment — the template annotates most lines); append
    the key at EOF when it doesn't exist yet."""
    value = value.replace('"', "'").strip()
    pattern = re.compile(
        rf'^({re.escape(key)}:\s*)"[^"]*"(\s*(?:#.*)?)$', re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(
            lambda m: f'{m.group(1)}"{value}"{m.group(2)}', text, count=1)
    return text.rstrip("\n") + f'\n{key}: "{value}"\n'


@router.put("/profile")
def update_profile(body: ProfileUpdate):
    """Comment-preserving line edits to the active profile's profile.yaml,
    whitelisted keys only."""
    bad = [k for k in body.facts if k not in EDITABLE_PROFILE_KEYS]
    if bad:
        raise HTTPException(400, f"non-editable keys: {bad}")
    text = config.USER_PROFILE_PATH.read_text(encoding="utf-8")
    for key, value in body.facts.items():
        text = _set_profile_fact(text, key, value)
    config.USER_PROFILE_PATH.write_text(text, encoding="utf-8")
    return profile()


# --------------------------------------------------------------------------- #
# job criteria (read + comment-preserving write-back, JOB-55)
# --------------------------------------------------------------------------- #
def _criteria_payload() -> dict:
    crit = config.load_search_criteria()
    sd, base = crit.get("search_defaults", {}), crit.get("baseline", {})
    return {
        "titles": base.get("acceptable_titles") or [],
        "search_titles": sd.get("titles") or [],
        "locations": base.get("locations_allowed") or [],
        "acceptable_seniority": base.get("acceptable_seniority") or [],
        "excluded_seniority": base.get("excluded_seniority") or [],
        "salary_floor": base.get("salary_floor"),
        "date_posted_days": sd.get("date_posted_days"),
        "remote_ok": bool(base.get("remote_allowed", True)),
        "yoe": [base.get("yoe_min", 0), base.get("yoe_max", 15)],
    }


@router.get("/criteria")
def criteria():
    return _criteria_payload()


def _yaml_set_scalar(text: str, key: str, value, add_under_baseline=False) -> str:
    """Replace the value on a `key:` line anywhere in the file, preserving
    indentation and any trailing comment. Optionally append the key at EOF
    (the baseline section runs to EOF) when it doesn't exist yet."""
    rendered = ("true" if value is True else "false" if value is False
                else str(value))
    pattern = re.compile(
        rf"^(\s*{re.escape(key)}:\s*)([^#\n]*?)(\s*#.*)?$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(
            lambda m: f"{m.group(1)}{rendered}{m.group(3) or ''}",
            text, count=1)
    if add_under_baseline:
        return text.rstrip("\n") + f"\n  {key}: {rendered}\n"
    return text


def _yaml_set_inline_list(text: str, key: str, values: list[str]) -> str:
    rendered = json.dumps(values, ensure_ascii=False)
    return _yaml_set_scalar(text, key, rendered)


def _yaml_set_block_list(text: str, key: str, values: list[str]) -> str:
    """Rewrite a `key:` block list (two-space-indented `- item` lines). The
    block's interior comment lines are preserved, re-emitted directly under the
    key so hand-written context (e.g. JOB-26 notes) survives the rewrite.
    Trailing comments after the last item belong to the NEXT key and stay."""
    lines = text.split("\n")
    key_re = re.compile(rf"^(\s*){re.escape(key)}:\s*(#.*)?$")
    start = next((i for i, ln in enumerate(lines) if key_re.match(ln)), None)
    if start is None:
        return text
    indent = key_re.match(lines[start]).group(1)
    item_re = re.compile(rf"^{indent}\s+- ")
    comment_re = re.compile(r"^\s*(#|$)")
    last_item = start
    i = start + 1
    while i < len(lines) and (item_re.match(lines[i]) or comment_re.match(lines[i])):
        if item_re.match(lines[i]):
            last_item = i
        i += 1
    interior_comments = [ln for ln in lines[start + 1:last_item + 1]
                         if comment_re.match(ln) and ln.strip()]
    block = ([lines[start]] + interior_comments
             + [f'{indent}  - "{v}"' for v in values])
    return "\n".join(lines[:start] + block + lines[last_item + 1:])


class CriteriaUpdate(BaseModel):
    titles: list[str] | None = None
    locations: list[str] | None = None
    acceptable_seniority: list[str] | None = None
    excluded_seniority: list[str] | None = None
    salary_floor: int | None = None
    date_posted_days: int | None = None
    remote_ok: bool | None = None
    yoe: list[int] | None = None


@router.put("/criteria")
def update_criteria(body: CriteriaUpdate):
    """Comment-preserving edits to job_criteria.yaml. Only the keys present in
    the body are touched; every query path reloads the file live, so a save
    re-scopes the postings page, digest, and /find-jobs immediately."""
    if not config.JOB_CRITERIA_PATH.exists():
        raise HTTPException(404, "job_criteria.yaml not found")
    current = _criteria_payload()
    text = config.JOB_CRITERIA_PATH.read_text(encoding="utf-8")
    if body.titles is not None and body.titles != current["titles"]:
        text = _yaml_set_block_list(text, "acceptable_titles", body.titles)
    if body.locations is not None:
        text = _yaml_set_inline_list(text, "locations_allowed", body.locations)
    if body.acceptable_seniority is not None:
        text = _yaml_set_inline_list(
            text, "acceptable_seniority", body.acceptable_seniority)
    if body.excluded_seniority is not None:
        text = _yaml_set_inline_list(
            text, "excluded_seniority", body.excluded_seniority)
    if body.salary_floor is not None:
        text = _yaml_set_scalar(text, "salary_floor", body.salary_floor)
    if body.date_posted_days is not None:
        text = _yaml_set_scalar(text, "date_posted_days", body.date_posted_days)
    if body.remote_ok is not None:
        text = _yaml_set_scalar(text, "remote_allowed", body.remote_ok)
        text = _yaml_set_scalar(text, "remote_ok", body.remote_ok)
    if body.yoe is not None and len(body.yoe) == 2:
        lo, hi = sorted(int(v) for v in body.yoe)
        if "yoe_min" not in text:
            text = (text.rstrip("\n")
                    + "\n\n  # Advisory YoE window shown in the postings UI "
                    "filters (JOB-55).\n")
        text = _yaml_set_scalar(text, "yoe_min", lo, add_under_baseline=True)
        text = _yaml_set_scalar(text, "yoe_max", hi, add_under_baseline=True)
    try:
        parsed = yaml.safe_load(text)
        if not (isinstance(parsed, dict) and "baseline" in parsed):
            raise ValueError("lost structure")
    except Exception:
        raise HTTPException(500, "criteria edit produced invalid YAML; not saved")
    config.JOB_CRITERIA_PATH.write_text(text, encoding="utf-8")
    return _criteria_payload()


# --------------------------------------------------------------------------- #
# watchlist
# --------------------------------------------------------------------------- #
@router.get("/watchlist")
def watchlist():
    companies = config.load_watchlist()
    stats = {s["company"]: s for s in store.yield_stats()} \
        if config.POSTINGS_DB_PATH.exists() else {}
    out = []
    for c in companies:
        s = stats.get(c["name"], {})
        out.append({"name": c["name"], "ats": c.get("ats", ""),
                    "slug": c.get("slug", ""),
                    "active": s.get("active", 0),
                    "qualifying": s.get("qualifying", 0)})
    out.sort(key=lambda w: (-w["qualifying"], -w["active"], w["name"]))
    return {"companies": out}


class WatchlistAdd(BaseModel):
    url: str


@router.post("/watchlist")
def watchlist_add(body: WatchlistAdd):
    result = add_company(body.url.strip())
    if result.get("status") == "error":
        raise HTTPException(400, result.get("reason", "could not add"))
    return result


# --------------------------------------------------------------------------- #
# profiles — list / activate / create (profile-system UI)
# --------------------------------------------------------------------------- #
def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def _profile_summary(root: Path, active_id: str | None) -> dict:
    prof = _read_yaml(root / "profile.yaml")
    facts = {k: v for k, v in prof.items()
             if k in EDITABLE_PROFILE_KEYS and isinstance(v, str)}
    filled = sum(1 for v in facts.values() if v.strip())
    apps_path = root / "data" / "applications.json"
    try:
        apps = json.loads(apps_path.read_text(encoding="utf-8")) \
            if apps_path.exists() else []
    except (json.JSONDecodeError, OSError):
        apps = []
    tracked = [apps_path, root / "data" / "history.json", root / "profile.yaml"]
    mtimes = [p.stat().st_mtime for p in tracked if p.exists()]
    name = (prof.get("full_name") or "").strip() or root.name
    return {
        "id": root.name,
        "name": name,
        "applications": len(apps) if isinstance(apps, list) else 0,
        "completeness": round(100 * filled / max(1, len(EDITABLE_PROFILE_KEYS))),
        "resume": (root / "resume.pdf").exists() or (root / "resume.docx").exists(),
        "last_used": (datetime.fromtimestamp(max(mtimes)).isoformat()
                      if mtimes else None),
        "created": datetime.fromtimestamp(root.stat().st_ctime).isoformat(),
        "active": root.name == active_id,
    }


@router.get("/profiles")
def profiles_list():
    """Every profile on this machine + which one is active. `active_id: null`
    means the launch screen must interrupt (several profiles and none chosen,
    or none at all)."""
    try:
        prof = config.active_profile()
        # The bootstrapped placeholder profile is not a real selection: report
        # it as no profile so a brand-new user still gets the launch screen.
        active_id = None if prof.placeholder else prof.profile_id
    except profiles_mod.ProfileError:
        active_id = None
    dirs = [p for p in sorted(profiles_mod.PROFILES_DIR.iterdir())
            if p.is_dir() and not p.name.startswith("_")] \
        if profiles_mod.PROFILES_DIR.exists() else []
    out = [_profile_summary(d, active_id) for d in dirs]
    if not out and active_id == profiles_mod.LEGACY_ID:
        # pre-migration checkout: surface the repo-root layout as one profile
        prof = _read_yaml(profiles_mod.REPO_DIR / "user_profile.yaml")
        out = [{"id": active_id,
                "name": (prof.get("full_name") or "").strip() or "This machine",
                "applications": 0, "completeness": 0, "resume": True,
                "last_used": None, "created": None, "active": True}]
    return {"profiles": out, "active_id": active_id}


class ProfileSelect(BaseModel):
    id: str


@router.post("/profiles/activate")
def profiles_activate(body: ProfileSelect):
    """Switch the active profile: persist the machine default and re-resolve
    every lazily-bound config path in this process. Spawned chat sessions
    inherit the env var, and the MCP server picks up applyer.local.json on its
    next start."""
    pid = body.id.strip()
    root = profiles_mod.PROFILES_DIR / pid
    if pid.startswith("_") or not root.is_dir():
        raise HTTPException(404, f"profile '{pid}' not found")
    profiles_mod.LOCAL_CONFIG_PATH.write_text(
        json.dumps({"profile": pid}, indent=2) + "\n", encoding="utf-8")
    os.environ["JOB_APPLIER_PROFILE"] = pid
    profiles_mod.reset()
    return profiles_list()


class ProfileCreate(BaseModel):
    name: str


@router.post("/profiles")
def profiles_create(body: ProfileCreate):
    """New profile = copy of the tracked profiles/_template, named, activated."""
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "a name is required")
    pid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "profile"
    root = profiles_mod.PROFILES_DIR / pid
    if root.exists():
        raise HTTPException(409, f"profile '{pid}' already exists")
    template = profiles_mod.PROFILES_DIR / "_template"
    if not template.is_dir():
        raise HTTPException(500, "profiles/_template is missing from this checkout")
    shutil.copytree(template, root)
    ppath = root / "profile.yaml"
    if ppath.exists():
        text = ppath.read_text(encoding="utf-8")
        first, _, last = name.partition(" ")
        for key, value in (("full_name", name), ("first_name", first),
                           ("last_name", last.strip())):
            text = _set_profile_fact(text, key, value)
        ppath.write_text(text, encoding="utf-8")
    return profiles_activate(ProfileSelect(id=pid))


# --------------------------------------------------------------------------- #
# EEO self-identification — statuses only; values never leave the server
# --------------------------------------------------------------------------- #
EEO_FIELDS = [
    ("gender", "Gender"),
    ("race_ethnicity", "Race / ethnicity"),
    ("hispanic_latino", "Hispanic or Latino"),
    ("veteran_status", "Veteran status"),
    ("disability_status", "Disability"),
]

_PREFER_NOT_RE = re.compile(r"decline|prefer not|don'?t wish|not to answer", re.I)


@router.get("/eeo")
def eeo_status():
    """Which voluntary self-ID questions are answered in the profile's
    local-only eeo.yaml — Set / Prefer not to say / Not set. The values
    themselves are never returned by any endpoint."""
    data = _read_yaml(config.EEO_PATH)
    fields = []
    for key, label in EEO_FIELDS:
        raw = data.get(key)
        value = raw.get("value") if isinstance(raw, dict) else raw
        s = str(value or "").strip()
        state = ("not_set" if not s
                 else "prefer_not" if _PREFER_NOT_RE.search(s) else "set")
        fields.append({"key": key, "label": label, "status": state})
    return {"present": any(f["status"] != "not_set" for f in fields),
            "fields": fields}


class EEOUpdate(BaseModel):
    values: dict[str, str]


@router.put("/eeo")
def eeo_update(body: EEOUpdate):
    """Write self-ID answers to the local-only eeo.yaml. Only keys present in
    the body are touched; an empty string clears that answer. The edit form
    never pre-fills, so current values stay off the wire."""
    valid = {k for k, _ in EEO_FIELDS}
    bad = [k for k in body.values if k not in valid]
    if bad:
        raise HTTPException(400, f"unknown EEO keys: {bad}")
    data = _read_yaml(config.EEO_PATH)
    for key, value in body.values.items():
        value = value.strip()
        if value:
            data[key] = {"value": value, "eeo": True}
        else:
            data.pop(key, None)
    if data:
        header = (
            "# Voluntary EEO self-identification — LOCAL-ONLY.\n"
            "# Merged into the profile at read time; never synced or uploaded,\n"
            "# never written to answer history or the application log.\n")
        config.EEO_PATH.write_text(
            header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8")
    elif config.EEO_PATH.exists():
        config.EEO_PATH.unlink()
    return eeo_status()


@router.delete("/eeo")
def eeo_remove():
    """Delete the local eeo.yaml outright — the agent then leaves voluntary
    self-ID sections blank."""
    if config.EEO_PATH.exists():
        config.EEO_PATH.unlink()
    return eeo_status()


# --------------------------------------------------------------------------- #
# cloud account (M2/JOB-86 — no cloud service yet, answer honestly)
# --------------------------------------------------------------------------- #
@router.get("/account")
def account():
    return {"linked": False,
            "note": "Cloud sync ships with the hosted backend (M2). "
                    "Local-only is a fully supported end state."}


class TokenVerify(BaseModel):
    token: str


@router.post("/account/verify")
def account_verify(body: TokenVerify):
    # applyer.app doesn't exist yet (JOB-86): there is nothing to contact, so
    # report the service unreachable rather than pretending to verify.
    return {"ok": False, "reason": "unreachable",
            "detail": "Applyer's cloud service isn't live yet — keep "
                      "everything on this device for now."}


# --------------------------------------------------------------------------- #
# uploads (resume → profile root, knowledge docs → context/)
# --------------------------------------------------------------------------- #
_RESUME_EXTS = {".pdf", ".docx", ".txt"}
_CONTEXT_EXTS = set(_CONTEXT_KINDS)


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(
    r"(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}")


def _resume_prefill(text: str) -> dict[str, str]:
    """Instant regex-level prefill from the résumé text (email/phone) — the
    wizard shows these as found-fact chips the moment the upload lands."""
    out = {}
    if m := _EMAIL_RE.search(text):
        out["email"] = m.group(0)
    if m := _PHONE_RE.search(text):
        out["phone"] = m.group(0).strip()
    return out


@router.post("/upload/resume")
async def upload_resume(file: UploadFile):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _RESUME_EXTS:
        raise HTTPException(400, f"resume must be one of {sorted(_RESUME_EXTS)}")
    dest = config.active_profile().root / f"resume{ext}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    synced = config.sync_resume_text_from_pdf() if ext == ".pdf" else False
    text = ""
    if ext == ".txt":
        text = dest.read_text(encoding="utf-8", errors="replace")
    elif config.RESUME_TEXT_PATH.exists():
        text = config.RESUME_TEXT_PATH.read_text(encoding="utf-8",
                                                 errors="replace")
    return {"saved": dest.name, "text_synced": synced,
            "prefill": _resume_prefill(text)}


@router.post("/upload/context")
async def upload_context(file: UploadFile):
    name = Path(file.filename or "").name  # strip any path components
    ext = Path(name).suffix.lower()
    if not name or ext not in _CONTEXT_EXTS:
        raise HTTPException(400, f"context files must be one of {sorted(_CONTEXT_EXTS)}")
    dest = config.CONTEXT_DIR / name
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"saved": name, "context_files": _context_files()}


class ContextPaste(BaseModel):
    title: str = ""
    text: str
    kind: str = "pasted"  # 'pasted' (past answers) | 'story' (told here)


@router.post("/context/paste")
def context_paste(body: ContextPaste):
    """Wizard step 4: pasted past answers / stories land in the knowledge base
    as markdown files — immediately searchable (search_context scans live)."""
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "nothing to add")
    prefix = "story" if body.kind == "story" else "pasted"
    stem = re.sub(r"[^A-Za-z0-9]+", "-", body.title.strip()).strip("-")[:60]
    base = f"{prefix}-{stem}" if stem else \
        f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    dest = config.CONTEXT_DIR / f"{base}.md"
    n = 2
    while dest.exists():
        dest = config.CONTEXT_DIR / f"{base}-{n}.md"
        n += 1
    title = body.title.strip() or ("A story" if body.kind == "story"
                                   else "Pasted answer")
    dest.write_text(f"# {title}\n\n{text}\n", encoding="utf-8")
    return {"saved": dest.name, "context_files": _context_files()}


@router.delete("/context/{name}")
def context_delete(name: str):
    clean = Path(name).name
    target = config.CONTEXT_DIR / clean
    if clean != name or target.suffix.lower() not in _CONTEXT_EXTS \
            or not target.is_file():
        raise HTTPException(404, "no such context file")
    target.unlink()
    return {"removed": clean, "context_files": _context_files()}


# --------------------------------------------------------------------------- #
# bug reports — append-only JSONL the daily dev-loop agent triages. Repo-level
# data/ (not per-profile): bugs are about the app, not the person applying.
# --------------------------------------------------------------------------- #
class BugReport(BaseModel):
    text: str
    page: str = ""


@router.post("/bug-report")
def bug_report(body: BugReport):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "nothing to report")
    try:
        profile_id = profiles_mod.active().profile_id
    except Exception:
        profile_id = None
    rec = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "profile": profile_id,
        "page": body.page.strip()[:40],
        "text": text[:4000],
        "status": "open",
    }
    path = config.DATA_DIR / "bug-reports.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"saved": True}


@router.get("/bug-reports")
def bug_reports():
    path = config.DATA_DIR / "bug-reports.jsonl"
    if not path.exists():
        return {"reports": []}
    reports = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                reports.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn/corrupt line must not hide the rest
    return {"reports": reports}


# --------------------------------------------------------------------------- #
# connections (status display; authorization itself happens in Claude Code)
# --------------------------------------------------------------------------- #
def _claude_cli_available() -> bool:
    """The chat bridge can run Claude Code if the CLI is on PATH OR the Agent
    SDK ships its bundled CLI (it does on this install)."""
    if shutil.which("claude"):
        return True
    try:
        import claude_agent_sdk
        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled"
        return any(bundled.glob("claude*"))
    except ImportError:
        return False


@router.get("/connections")
def connections():
    claude_cli = _claude_cli_available()
    job_applier = False
    mcp_json = config.BASE_DIR / ".mcp.json"
    if mcp_json.exists():
        try:
            job_applier = "job-applier" in json.loads(
                mcp_json.read_text(encoding="utf-8")).get("mcpServers", {})
        except json.JSONDecodeError:
            pass
    # Gmail/Linear are configured in the user-level Claude Code config
    # (~/.claude.json) or as claude.ai connectors — detect by name.
    user_cfg_text = ""
    user_cfg = Path.home() / ".claude.json"
    if user_cfg.exists():
        try:
            user_cfg_text = user_cfg.read_text(encoding="utf-8").lower()
        except OSError:
            pass
    return {"connections": [
        {"id": "claude", "name": "Claude Code", "mono": "CC", "required": True,
         "connected": claude_cli,
         "short": "The reasoning engine",
         "desc": "The reasoner that drives every command. The chat spawns real "
                 "Claude Code sessions in this repo via the Agent SDK."},
        {"id": "mcp", "name": "job-applier MCP server", "mono": "JA",
         "required": True, "connected": job_applier,
         "short": "Browser + your data (33 tools)",
         "desc": "Local MCP server — gives the agent a real Chrome browser "
                 "(Playwright), your profile/history, and live jobs."},
        {"id": "gmail", "name": "Gmail", "mono": "GM", "required": False,
         "connected": "gmail" in user_cfg_text,
         "short": "Fetches email verification codes",
         "desc": "Lets the agent fetch email verification codes (e.g. "
                 "Greenhouse 8-char codes) and re-submit on its own."},
        {"id": "linear", "name": "Linear", "mono": "LN", "required": False,
         "connected": "linear" in user_cfg_text,
         "short": "Optional task tracking",
         "desc": "Keeps the JOB-* backlog in sync when the agent completes "
                 "dev tasks. Not needed for applying."},
    ],
        # The Gmail connector's account email isn't discoverable locally (it
        # lives in claude.ai connector state). When a future check can read
        # it, set it here — the wizard's step-7 mismatch drawer keys off it.
        "gmail_account": None,
        "note": "Connect/disconnect is done in Claude Code (/mcp or claude.ai "
                "connector settings); this page reflects detected status."}
