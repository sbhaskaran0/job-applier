"""Active-profile resolution — the root of every per-user path.

All personal artifacts (profile facts, EEO self-ID, criteria, resume, context
knowledge base, history, application log, tailored resumes, prep files, runtime
state) live under profiles/<id>/. Shared assets (watchlist, postings store,
location aliases, discovery config) stay at the repo root.

Resolution order for the active profile:
  1. JOB_APPLIER_PROFILE environment variable (per-session override — set by
     the webapp chat bridge or tests)
  2. applyer.local.json at the repo root: {"profile": "<id>"} (per-machine
     selection, gitignored)
  3. exactly one real profile directory under profiles/ (ignoring _template)
  4. legacy repo-root layout (pre-migration checkouts: user_profile.yaml at
     the repo root) — keeps old clones working until scripts/migrate_profile.py
     has run
Otherwise a clear error tells the user to create a profile.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
PROFILES_DIR = REPO_DIR / "profiles"
LOCAL_CONFIG_PATH = REPO_DIR / "applyer.local.json"

LEGACY_ID = "__legacy__"


class ProfileError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActiveProfile:
    """Path bundle for one user's local data. `legacy` maps every path onto the
    pre-profile repo-root layout so unmigrated checkouts behave exactly as
    before."""

    profile_id: str
    root: Path
    legacy: bool = field(default=False)

    # --- personal config -------------------------------------------------
    @property
    def profile_yaml(self) -> Path:
        return self.root / ("user_profile.yaml" if self.legacy else "profile.yaml")

    @property
    def eeo_yaml(self) -> Path:
        return self.root / "eeo.yaml"  # local-only; merged at read time, never synced

    @property
    def criteria_yaml(self) -> Path:
        return self.root / ("job_criteria.yaml" if self.legacy else "criteria.yaml")

    # --- knowledge base + resume -----------------------------------------
    @property
    def context_dir(self) -> Path:
        return self.root / "context"

    @property
    def resume_txt(self) -> Path:
        return self.root / "resume.txt"

    @property
    def resume_pdf(self) -> Path:
        return self.root / "resume.pdf"

    @property
    def resume_docx(self) -> Path:
        return self.root / "resume.docx"

    # --- per-user data ----------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def history_json(self) -> Path:
        return self.data_dir / "history.json"

    @property
    def applications_json(self) -> Path:
        return self.data_dir / "applications.json"

    @property
    def prep_dir(self) -> Path:
        return self.data_dir / "prep"

    @property
    def resumes_dir(self) -> Path:
        return self.root / "resumes"  # tailored per-job artifacts

    # --- runtime (screenshots, persistent browser profile) ---------------
    @property
    def runtime_dir(self) -> Path:
        # Legacy keeps runtime files at the repo root (current_page.png etc.)
        return self.root if self.legacy else self.root / "runtime"

    @property
    def browser_dir(self) -> Path:
        return (self.root if self.legacy else self.runtime_dir) / "browser"

    @property
    def secrets_dir(self) -> Path:
        return self.root / "secrets"

    def ensure_dirs(self) -> None:
        for d in (self.context_dir, self.data_dir, self.prep_dir,
                  self.resumes_dir, self.runtime_dir):
            d.mkdir(parents=True, exist_ok=True)


def _profile_dirs() -> list[Path]:
    if not PROFILES_DIR.exists():
        return []
    return [p for p in sorted(PROFILES_DIR.iterdir())
            if p.is_dir() and not p.name.startswith("_")]


def _from_id(profile_id: str) -> ActiveProfile:
    root = PROFILES_DIR / profile_id
    if not root.is_dir():
        raise ProfileError(
            f"Profile '{profile_id}' not found at {root}. "
            f"Available: {[p.name for p in _profile_dirs()] or 'none'}"
        )
    return ActiveProfile(profile_id=profile_id, root=root)


def _resolve() -> ActiveProfile:
    env_id = os.environ.get("JOB_APPLIER_PROFILE", "").strip()
    if env_id:
        return _from_id(env_id)

    if LOCAL_CONFIG_PATH.exists():
        try:
            cfg = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            cfg = {}
        cfg_id = str(cfg.get("profile", "")).strip()
        if cfg_id:
            return _from_id(cfg_id)

    dirs = _profile_dirs()
    if len(dirs) == 1:
        return ActiveProfile(profile_id=dirs[0].name, root=dirs[0])
    if len(dirs) > 1:
        raise ProfileError(
            f"Multiple profiles exist ({[p.name for p in dirs]}) and none is "
            f"selected. Set JOB_APPLIER_PROFILE or write "
            f'{{"profile": "<id>"}} to {LOCAL_CONFIG_PATH.name}.'
        )

    # Legacy repo-root layout (pre-migration checkout)
    if (REPO_DIR / "user_profile.yaml").exists():
        return ActiveProfile(profile_id=LEGACY_ID, root=REPO_DIR, legacy=True)

    raise ProfileError(
        "No profile found. Copy profiles/_template/ to profiles/<your-id>/ and "
        "fill it in (or run scripts/migrate_profile.py on a legacy checkout)."
    )


_active: ActiveProfile | None = None


def active() -> ActiveProfile:
    """Resolve (and cache for this process) the active profile."""
    global _active
    if _active is None:
        _active = _resolve()
        _active.ensure_dirs()
    return _active


def reset() -> None:
    """Drop the cached resolution (tests / profile switching)."""
    global _active
    _active = None
