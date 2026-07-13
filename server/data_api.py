"""REST endpoints serving the agent's real data files to the Applyer frontend.

Read paths reuse the same modules the MCP server uses (src.store, src.config,
src.providers.watchlist) so the UI shows exactly what /find-jobs would see.
Write paths are deliberately narrow: watchlist append, whitelisted profile
string fields (comment-preserving line edits), and resume/context uploads.
"""

import json
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel

from src import config, store
from src.providers.watchlist import add_company, detect_ats_slug

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
