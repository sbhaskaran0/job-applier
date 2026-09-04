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
  5. last resort on a profiles-less checkout (fresh clone / git worktree —
     profiles/* is gitignored, so only profiles/_template/ ships): bootstrap a
     PLACEHOLDER profile at profiles/_scratch/ from the template, so read-only
     pipelines like `python -m src.refresh` can run. Only criteria.yaml is
     copied — never profile.yaml — so every path that would write or submit a
     real person's data still fails loudly. Marked `placeholder=True`; the
     webapp treats that as "no profile" and still shows the launch screen.
Otherwise a clear error tells the user to create a profile.
"""

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
PROFILES_DIR = REPO_DIR / "profiles"
LOCAL_CONFIG_PATH = REPO_DIR / "applyer.local.json"

LEGACY_ID = "__legacy__"
TEMPLATE_ID = "_template"
SCRATCH_ID = "_scratch"


class ProfileError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActiveProfile:
    """Path bundle for one user's local data. `legacy` maps every path onto the
    pre-profile repo-root layout so unmigrated checkouts behave exactly as
    before. `placeholder` marks the bootstrapped scratch profile — it holds no
    personal data and must never be presented to the user as a real one."""

    profile_id: str
    root: Path
    legacy: bool = field(default=False)
    placeholder: bool = field(default=False)

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


def _bootstrap_scratch() -> ActiveProfile | None:
    """Materialise the PLACEHOLDER profile at profiles/_scratch/ from the
    tracked template, so a profiles-less checkout (fresh clone, CI, a git
    worktree) can still run the read-only pipelines.

    Only criteria.yaml is copied. profile.yaml is deliberately left absent so
    config.load_user_profile() keeps raising its loud FileNotFoundError for
    every apply/tailor/form-fill path — a placeholder must never quietly stand
    in for a real person. Idempotent: an existing _scratch/ is reused as-is,
    and nothing is ever written under the tracked template."""
    template_criteria = PROFILES_DIR / TEMPLATE_ID / "criteria.yaml"
    if not template_criteria.is_file():
        return None

    root = PROFILES_DIR / SCRATCH_ID          # covered by the profiles/* ignore
    criteria = root / "criteria.yaml"
    if not criteria.exists():
        root.mkdir(parents=True, exist_ok=True)
        criteria.write_bytes(template_criteria.read_bytes())

    # One line per process: active() caches, so this branch runs at most once.
    print(
        f"[profiles] No profile found - using the PLACEHOLDER profile "
        f"'{SCRATCH_ID}' (empty search criteria, no personal data). "
        f"To create a real one: copy profiles/{TEMPLATE_ID}/ to "
        f"profiles/<your-id>/ and fill it in.",
        file=sys.stderr,
    )
    return ActiveProfile(profile_id=SCRATCH_ID, root=root, placeholder=True)


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

    # Profiles-less checkout: fall back to a clearly-marked placeholder rather
    # than killing every entry point before it does any work.
    scratch = _bootstrap_scratch()
    if scratch is not None:
        return scratch

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
