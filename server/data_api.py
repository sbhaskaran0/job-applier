"""REST endpoints serving the agent's real data files to the Applyer frontend.

Read paths reuse the same modules the MCP server uses (src.store, src.config,
src.providers.watchlist) so the UI shows exactly what /find-jobs would see.
Write paths are deliberately narrow: watchlist append, whitelisted profile
string fields (comment-preserving line edits), and resume/context uploads.
"""

import asyncio
import json
import re
import shutil
from pathlib import Path

import yaml

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel

from src import config, refresh as refresh_job, store
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


@router.get("/applications")
def applications():
    return {"applications": config.load_applications()}


# --------------------------------------------------------------------------- #
# profile (read + whitelisted write-back) / context files
# --------------------------------------------------------------------------- #
@router.get("/profile")
def profile():
    prof = config.load_user_profile()
    facts = {k: v for k, v in prof.items()
             if k in EDITABLE_PROFILE_KEYS and isinstance(v, str)}
    eeo_present = sorted(k for k, v in prof.items()
                         if isinstance(v, dict) and v.get("eeo"))
    filled = sum(1 for v in facts.values() if str(v).strip())
    return {
        "facts": facts,
        "eeo_fields_present": eeo_present,
        "completeness": round(100 * filled / max(1, len(EDITABLE_PROFILE_KEYS))),
        "resume_pdf": (config.BASE_DIR / "resume.pdf").exists(),
        "resume_docx": config.base_resume_docx() is not None,
        "context_files": _context_files(),
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


@router.put("/profile")
def update_profile(body: ProfileUpdate):
    """Comment-preserving line edits: replace the value of whitelisted
    `key: "..."` lines in user_profile.yaml; append missing keys at the end
    of the flat-facts section (before the EEO block)."""
    bad = [k for k in body.facts if k not in EDITABLE_PROFILE_KEYS]
    if bad:
        raise HTTPException(400, f"non-editable keys: {bad}")
    text = config.USER_PROFILE_PATH.read_text(encoding="utf-8")
    for key, value in body.facts.items():
        value = value.replace('"', "'").strip()
        pattern = re.compile(rf'^{re.escape(key)}:\s*".*"\s*$', re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(f'{key}: "{value}"', text, count=1)
        else:
            text = text.rstrip("\n") + f'\n{key}: "{value}"\n'
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
# uploads (resume → project root, knowledge docs → context/)
# --------------------------------------------------------------------------- #
_RESUME_EXTS = {".pdf", ".docx", ".txt"}
_CONTEXT_EXTS = set(_CONTEXT_KINDS)


@router.post("/upload/resume")
async def upload_resume(file: UploadFile):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _RESUME_EXTS:
        raise HTTPException(400, f"resume must be one of {sorted(_RESUME_EXTS)}")
    dest = config.BASE_DIR / f"resume{ext}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    synced = config.sync_resume_text_from_pdf() if ext == ".pdf" else False
    return {"saved": dest.name, "text_synced": synced}


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
    ], "note": "Connect/disconnect is done in Claude Code (/mcp or claude.ai "
               "connector settings); this page reflects detected status."}
