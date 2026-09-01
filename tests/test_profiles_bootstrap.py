"""Profiles-less-checkout bootstrap tests (JOB-131): profiles/* is gitignored,
so a fresh clone or git worktree ships only profiles/_template/ and every entry
point used to die in _resolve(). The fallback materialises a PLACEHOLDER
profile at profiles/_scratch/ — criteria.yaml ONLY, never profile.yaml — so
read-only pipelines run while anything that would touch a real person's data
still fails loudly.

Standalone on purpose — the project ships no test runner, so
`python tests/test_profiles_bootstrap.py` runs the whole file; the plain
`test_*` functions also collect under pytest if one is ever added. CI installs
nothing but pyyaml, so this file imports src.profiles and the stdlib and
NOTHING else (no src.config, no server.data_api — those pull yaml/fastapi).
"""

import io
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import profiles                     # noqa: E402

# what the real tracked template carries; only criteria.yaml may be copied.
TEMPLATE_FILES = {
    "criteria.yaml": "baseline:\n  acceptable_titles: []\n  salary_floor: 0\n",
    "profile.yaml": "full_name: ''\nemail: ''\n",
    "eeo.yaml": "gender: ''\n",
    "README.md": "# template\n",
}


def _make_template(profiles_dir: Path) -> Path:
    root = profiles_dir / profiles.TEMPLATE_ID
    (root / "context").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    for name, body in TEMPLATE_FILES.items():
        (root / name).write_text(body, encoding="utf-8")
    return root


def _make_real_profile(profiles_dir: Path, name: str) -> Path:
    root = profiles_dir / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "criteria.yaml").write_text("baseline: {}\n", encoding="utf-8")
    (root / "profile.yaml").write_text("full_name: Real Person\n", encoding="utf-8")
    return root


def _tree(root: Path) -> dict:
    """path -> bytes for every file below root (used to prove nothing moved)."""
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


class _Case:
    """A temp repo tree with src.profiles pointed at it. Captures stderr so a
    test can assert on the placeholder warning (or its absence)."""

    def __init__(self, repo: Path, err: io.StringIO):
        self.repo = repo
        self.profiles_dir = repo / "profiles"
        self.err = err

    @property
    def stderr(self) -> str:
        return self.err.getvalue()

    @property
    def scratch(self) -> Path:
        return self.profiles_dir / profiles.SCRATCH_ID


@contextmanager
def case(env_profile=None, local_config=None):
    """Point the module constants at a throwaway tree. Saves/restores
    JOB_APPLIER_PROFILE (a leaked env var silently poisons later cases) and
    resets the active() cache on both sides."""
    saved = (profiles.REPO_DIR, profiles.PROFILES_DIR, profiles.LOCAL_CONFIG_PATH)
    saved_env = os.environ.get("JOB_APPLIER_PROFILE")
    saved_err = sys.stderr
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "profiles").mkdir()
        profiles.REPO_DIR = repo
        profiles.PROFILES_DIR = repo / "profiles"
        profiles.LOCAL_CONFIG_PATH = repo / "applyer.local.json"
        if env_profile is None:
            os.environ.pop("JOB_APPLIER_PROFILE", None)
        else:
            os.environ["JOB_APPLIER_PROFILE"] = env_profile
        if local_config is not None:
            profiles.LOCAL_CONFIG_PATH.write_text(local_config, encoding="utf-8")
        err = io.StringIO()
        sys.stderr = err
        profiles.reset()
        try:
            yield _Case(repo, err)
        finally:
            sys.stderr = saved_err
            profiles.reset()
            profiles.REPO_DIR, profiles.PROFILES_DIR, profiles.LOCAL_CONFIG_PATH = saved
            if saved_env is None:
                os.environ.pop("JOB_APPLIER_PROFILE", None)
            else:
                os.environ["JOB_APPLIER_PROFILE"] = saved_env


def _raises(fn):
    try:
        fn()
    except profiles.ProfileError as exc:
        return exc
    raise AssertionError("expected ProfileError, got none")


# --- AC 2: a real profile resolves exactly as before ----------------------

def test_single_real_profile_is_unchanged():
    with case() as c:
        _make_template(c.profiles_dir)
        _make_real_profile(c.profiles_dir, "siddharth")
        prof = profiles.active()
        assert prof.profile_id == "siddharth", prof.profile_id
        assert prof.placeholder is False
        assert prof.legacy is False
        assert prof.root == c.profiles_dir / "siddharth"
        assert not c.scratch.exists(), "scratch profile created alongside a real one"
        assert c.stderr == "", c.stderr


def test_explicit_selection_of_a_real_profile_is_unchanged():
    for kwargs in ({"env_profile": "siddharth"},
                   {"local_config": '{"profile": "siddharth"}'}):
        with case(**kwargs) as c:
            _make_template(c.profiles_dir)
            _make_real_profile(c.profiles_dir, "siddharth")
            _make_real_profile(c.profiles_dir, "other")
            prof = profiles.active()
            assert prof.profile_id == "siddharth", kwargs
            assert prof.placeholder is False
            assert not c.scratch.exists()
            assert c.stderr == "", c.stderr


def test_legacy_layout_still_wins_over_the_bootstrap():
    with case() as c:
        _make_template(c.profiles_dir)
        (c.repo / "user_profile.yaml").write_text("full_name: Old\n", encoding="utf-8")
        prof = profiles.active()
        assert prof.profile_id == profiles.LEGACY_ID
        assert prof.legacy is True and prof.placeholder is False
        assert not c.scratch.exists()
        assert c.stderr == "", c.stderr


# --- AC 3: the hard errors survive ----------------------------------------

def test_named_profile_that_does_not_exist_still_raises():
    with case(env_profile="nope") as c:
        _make_template(c.profiles_dir)
        exc = _raises(profiles.active)
        assert "not found" in str(exc), exc
        assert not c.scratch.exists(), "bootstrapped past an explicit selection"


def test_local_config_naming_a_missing_profile_still_raises():
    with case(local_config='{"profile": "nope"}') as c:
        _make_template(c.profiles_dir)
        exc = _raises(profiles.active)
        assert "not found" in str(exc), exc
        assert not c.scratch.exists()


def test_two_real_profiles_with_none_selected_still_raise():
    with case() as c:
        _make_template(c.profiles_dir)
        _make_real_profile(c.profiles_dir, "alice")
        _make_real_profile(c.profiles_dir, "bob")
        exc = _raises(profiles.active)
        assert "Multiple profiles" in str(exc), exc
        assert not c.scratch.exists()


def test_no_template_and_no_profile_still_raises():
    with case() as c:
        exc = _raises(profiles.active)
        assert "No profile found" in str(exc), exc
        assert not c.scratch.exists()


# --- AC 1/4/5: the bootstrap itself ---------------------------------------

def test_profiles_less_checkout_bootstraps_a_placeholder():
    with case() as c:
        template = _make_template(c.profiles_dir)
        prof = profiles.active()
        assert prof.profile_id == profiles.SCRATCH_ID, prof.profile_id
        assert prof.placeholder is True
        assert prof.legacy is False
        assert prof.root == c.scratch
        assert prof.criteria_yaml.read_bytes() == \
            (template / "criteria.yaml").read_bytes()
        # exactly one warning line, and it says how to get a real profile
        lines = [ln for ln in c.stderr.splitlines() if ln.strip()]
        assert len(lines) == 1, c.stderr
        assert profiles.TEMPLATE_ID in lines[0] and "PLACEHOLDER" in lines[0], lines[0]
        # cached: a second active() must not repeat the warning
        profiles.active()
        assert len([ln for ln in c.stderr.splitlines() if ln.strip()]) == 1


def test_bootstrap_copies_criteria_only_never_profile_yaml():
    with case() as c:
        _make_template(c.profiles_dir)
        prof = profiles.active()
        # the load-bearing safety property: no profile.yaml -> every apply /
        # tailor / form-fill path keeps raising its loud FileNotFoundError
        assert not prof.profile_yaml.exists(), prof.profile_yaml
        assert not (c.scratch / "profile.yaml").exists()
        assert not prof.eeo_yaml.exists()
        assert not (c.scratch / "resume.pdf").exists()
        files = set(_tree(c.scratch))
        assert files == {"criteria.yaml"}, files


def test_bootstrap_writes_nothing_outside_the_scratch_root():
    with case() as c:
        _make_template(c.profiles_dir)
        before = _tree(c.profiles_dir / profiles.TEMPLATE_ID)
        profiles.active()                       # calls ensure_dirs() too
        after = _tree(c.profiles_dir / profiles.TEMPLATE_ID)
        assert after == before, "the tracked template was modified"
        touched = {p.relative_to(c.profiles_dir).parts[0]
                   for p in c.profiles_dir.rglob("*")}
        assert touched == {profiles.TEMPLATE_ID, profiles.SCRATCH_ID}, touched
        # ensure_dirs() ran against the scratch root, not the repo root
        assert (c.scratch / "data").is_dir()
        assert not (c.repo / "data").exists()
        assert not (c.repo / "runtime").exists()


def test_bootstrap_is_idempotent_and_never_overwrites():
    with case() as c:
        _make_template(c.profiles_dir)
        c.scratch.mkdir()
        (c.scratch / "criteria.yaml").write_text("baseline: {edited: true}\n",
                                                 encoding="utf-8")
        (c.scratch / "keepme.txt").write_text("mine\n", encoding="utf-8")
        prof = profiles.active()
        assert prof.profile_id == profiles.SCRATCH_ID
        assert prof.criteria_yaml.read_text(encoding="utf-8") == \
            "baseline: {edited: true}\n"
        assert (c.scratch / "keepme.txt").read_text(encoding="utf-8") == "mine\n"


def test_a_real_profile_created_later_takes_over_from_the_scratch():
    with case() as c:
        _make_template(c.profiles_dir)
        assert profiles.active().placeholder is True
        _make_real_profile(c.profiles_dir, "siddharth")
        profiles.reset()
        prof = profiles.active()
        # _scratch is underscore-prefixed, so it never counts as a candidate
        assert prof.profile_id == "siddharth", prof.profile_id
        assert prof.placeholder is False


def test_scratch_id_is_ignored_by_the_profile_listing():
    with case() as c:
        _make_template(c.profiles_dir)
        profiles.active()
        assert profiles.SCRATCH_ID.startswith("_")
        assert profiles._profile_dirs() == []


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  %s" % name)
        except AssertionError as exc:
            failed += 1
            print("FAIL  %s: %s" % (name, exc))
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
