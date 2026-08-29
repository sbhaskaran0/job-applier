#!/usr/bin/env python3
"""One-time migration: move personal data from the legacy repo-root layout into
profiles/<id>/ (the profile system, src/profiles.py).

Usage:
    python scripts/migrate_profile.py [profile_id] [--repo <path>]

profile_id defaults to "siddharth". --repo defaults to this script's
parent-parent (the repo root) and exists so the migration can be tested
against a synthetic fixture tree.

What it does, in order:
  1. Refuses to run if profiles/<id>/ already exists or user_profile.yaml is
     absent from the repo root.
  2. Splits user_profile.yaml into profile.yaml (everything else, comments
     preserved) + eeo.yaml (the 5 voluntary EEO entries, local-only) in a
     temp location, and VERIFIES the split round-trips before touching
     anything else. Any verification failure aborts with nothing moved.
  3. Moves each personal artifact (criteria, resume files, context/, data
     files, resumes/) into profiles/<id>/, skipping missing ones with a note.
  4. Creates runtime/ and data/prep/, writes applyer.local.json.

It does NOT touch git (untracking the moved paths is a separate follow-up).
Repo-root PNGs (current_page.png etc.) are disposable and are left alone.
"""

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required (pip install pyyaml).")
    sys.exit(1)

EEO_KEYS = [
    "gender",
    "race_ethnicity",
    "hispanic_latino",
    "veteran_status",
    "disability_status",
]

EEO_KEY_RE = re.compile(r"^(" + "|".join(EEO_KEYS) + r")\s*:")
EEO_COMMENT_RE = re.compile(r"eeo|self[-\s]?id", re.IGNORECASE)

EEO_HEADER = """\
# Voluntary EEO self-identification — LOCAL ONLY.
# This file stays on this machine: it is gitignored, never synced, and never
# uploaded anywhere. Values are filled only into voluntary self-ID sections of
# application forms. Delete a value (or a whole entry, or this file) to opt
# out — those sections are then left blank on applications.
"""

# (repo-relative source, profile-relative destination)
MOVES = [
    ("job_criteria.yaml", "criteria.yaml"),
    ("resume.pdf", "resume.pdf"),
    ("resume.docx", "resume.docx"),
    ("resume.txt", "resume.txt"),
    ("context", "context"),
    ("data/history.json", "data/history.json"),
    ("data/applications.json", "data/applications.json"),
    ("data/prep", "data/prep"),
    ("data/token_usage.jsonl", "data/token_usage.jsonl"),
    ("resumes", "resumes"),
]


def split_eeo_lines(original_text: str):
    """Line-based split of user_profile.yaml.

    Returns (profile_text, dropped_keys). profile_text is the original text
    with each EEO key's block removed: the `key:` line, its indented
    continuation lines, and the immediately-preceding contiguous comment block
    when that block mentions EEO/self-identification. All other lines —
    including load-bearing comments — pass through untouched.
    """
    lines = original_text.splitlines()
    kept: list[str] = []
    dropped_keys: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = EEO_KEY_RE.match(line)
        if m:
            # Drop the contiguous comment block immediately above, if it is
            # about EEO/self-identification.
            j = len(kept)
            while j > 0 and kept[j - 1].lstrip().startswith("#"):
                j -= 1
            block = kept[j:]
            if block and any(EEO_COMMENT_RE.search(b) for b in block):
                del kept[j:]
            dropped_keys.append(m.group(1))
            i += 1
            # Drop indented continuation lines of this key's block.
            while i < len(lines) and lines[i].strip() and lines[i][0] in (" ", "\t"):
                i += 1
            continue
        kept.append(line)
        i += 1

    # Cosmetic: collapse runs of blank lines left behind by dropped blocks.
    cleaned: list[str] = []
    for line in kept:
        if line.strip() == "" and cleaned and cleaned[-1].strip() == "":
            continue
        cleaned.append(line)
    while cleaned and cleaned[-1].strip() == "":
        cleaned.pop()
    return "\n".join(cleaned) + "\n", dropped_keys


def build_and_verify_split(user_profile_path: Path, tmp_dir: Path):
    """Perform the split into tmp_dir and verify it round-trips.

    Returns (profile_tmp, eeo_tmp) paths on success; raises SystemExit(1) with
    a report on any verification failure. Nothing outside tmp_dir is touched.
    """
    original_text = user_profile_path.read_text(encoding="utf-8")
    original = yaml.safe_load(original_text) or {}

    profile_text, dropped_keys = split_eeo_lines(original_text)
    eeo_entries = {k: original[k] for k in EEO_KEYS if k in original}

    eeo_text = EEO_HEADER + yaml.safe_dump(
        eeo_entries, sort_keys=False, default_flow_style=False, allow_unicode=True
    )

    profile_tmp = tmp_dir / "profile.yaml"
    eeo_tmp = tmp_dir / "eeo.yaml"
    profile_tmp.write_text(profile_text, encoding="utf-8")
    eeo_tmp.write_text(eeo_text, encoding="utf-8")

    # --- verification (all on the temp files; nothing has moved yet) -------
    errors = []
    new_profile = yaml.safe_load(profile_tmp.read_text(encoding="utf-8")) or {}
    new_eeo = yaml.safe_load(eeo_tmp.read_text(encoding="utf-8")) or {}

    leaked = [k for k in EEO_KEYS if k in new_profile]
    if leaked:
        errors.append(f"profile.yaml still contains EEO keys: {leaked}")

    if new_eeo != eeo_entries:
        errors.append(
            "eeo.yaml does not contain exactly the original EEO entries.\n"
            f"  expected: {eeo_entries}\n  got:      {new_eeo}"
        )

    merged = dict(new_profile)
    merged.update(new_eeo)
    if merged != original:
        missing = {k: v for k, v in original.items() if merged.get(k) != v}
        extra = {k: v for k, v in merged.items() if k not in original}
        errors.append(
            "merged profile.yaml + eeo.yaml does not equal the original parse.\n"
            f"  lost/changed: {missing}\n  extraneous:   {extra}"
        )

    if errors:
        print("EEO split verification FAILED — aborting before any moves.")
        print("(Nothing was changed: user_profile.yaml and all other files "
              "are untouched.)")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    print("EEO split verified:")
    print(f"  - profile.yaml parses clean, no EEO keys present")
    print(f"  - eeo.yaml holds exactly {len(eeo_entries)} EEO entries "
          f"({', '.join(eeo_entries) or 'none'}), values match the original")
    print(f"  - merged union == original user_profile.yaml parse "
          f"({len(original)} top-level keys)")
    if set(EEO_KEYS) - set(eeo_entries):
        print(f"  - note: EEO keys absent from the original (nothing to split): "
              f"{sorted(set(EEO_KEYS) - set(eeo_entries))}")
    return profile_tmp, eeo_tmp


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate legacy repo-root personal data into profiles/<id>/."
    )
    parser.add_argument("profile_id", nargs="?", default="siddharth",
                        help='profile directory name (default: "siddharth")')
    parser.add_argument("--repo", type=Path,
                        default=Path(__file__).resolve().parent.parent,
                        help="repo root to migrate (default: this script's "
                             "parent-parent; override to test on a fixture)")
    args = parser.parse_args()

    repo = args.repo.resolve()
    profile_root = repo / "profiles" / args.profile_id
    user_profile = repo / "user_profile.yaml"

    # --- preconditions -----------------------------------------------------
    if profile_root.exists():
        print(f"ERROR: {profile_root} already exists — refusing to migrate "
              f"over it. Remove/rename it first if you really want to re-run.")
        return 1
    if not user_profile.exists():
        print(f"ERROR: {user_profile} not found — nothing to migrate. "
              f"(Already migrated, or wrong --repo?)")
        return 1

    print(f"Migrating repo-root personal data -> {profile_root}\n")

    # --- step 1: EEO split + verify, entirely in a temp dir ----------------
    with tempfile.TemporaryDirectory(prefix="profile_migration_") as td:
        tmp_dir = Path(td)
        profile_tmp, eeo_tmp = build_and_verify_split(user_profile, tmp_dir)

        # --- step 2: split verified — now (and only now) start moving ------
        print()
        profile_root.mkdir(parents=True)
        (profile_root / "data").mkdir()

        moved, skipped = [], []

        shutil.move(str(profile_tmp), str(profile_root / "profile.yaml"))
        shutil.move(str(eeo_tmp), str(profile_root / "eeo.yaml"))
        user_profile.unlink()
        moved.append("user_profile.yaml -> profile.yaml + eeo.yaml (split; "
                     "original removed)")

        for src_rel, dst_rel in MOVES:
            src = repo / src_rel
            dst = profile_root / dst_rel
            if not src.exists():
                skipped.append(src_rel)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved.append(f"{src_rel} -> profiles/{args.profile_id}/{dst_rel}")

    # --- step 3: dirs + local config --------------------------------------
    (profile_root / "runtime").mkdir(exist_ok=True)
    (profile_root / "data" / "prep").mkdir(parents=True, exist_ok=True)

    local_cfg = repo / "applyer.local.json"
    local_cfg.write_text(
        json.dumps({"profile": args.profile_id}, indent=2) + "\n",
        encoding="utf-8",
    )

    # --- summary -----------------------------------------------------------
    print("Moved:")
    for m in moved:
        print(f"  - {m}")
    if skipped:
        print("Skipped (not present at repo root):")
        for s in skipped:
            print(f"  - {s}")
    print("Created:")
    print(f"  - profiles/{args.profile_id}/runtime/")
    print(f"  - profiles/{args.profile_id}/data/prep/")
    print(f"  - applyer.local.json ({{\"profile\": \"{args.profile_id}\"}})")

    print("\nNOT done by this script (follow-ups):")
    print("  - git untracking of the moved paths (git rm --cached ...) — "
          "handled separately (Lane E). Until then `git status` will show "
          "the old tracked files as deleted.")
    print("  - repo-root PNGs (current_page.png etc.) were left in place; "
          "they are disposable runtime artifacts.")
    print("\nMigration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
