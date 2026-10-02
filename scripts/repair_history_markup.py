#!/usr/bin/env python3
"""One-time repair: strip leaked tool-call markup out of already-saved answers.

Phase 5 (2026-09-16): three records in the active profile's history.json
(indices 187-189, all scope "evergreen", dated 2026-09-16) carry a genuine,
complete answer immediately followed by the literal text

    </answer>
    <parameter name="scope">evergreen

i.e. the *next* tool-call parameter got appended to the answer text instead
of being parsed as markup. `src.data._sanitize_answer` now stops this
recurring at the write boundary (save_answer / capture_submission); this
script is the one-shot fix for the three records that were written before
that guard existed. Truncating at the marker was verified lossless against
all three real records before this script was written.

Usage:
    python scripts/repair_history_markup.py            # dry run (default)
    python scripts/repair_history_markup.py --apply    # write the repair

Dry run only prints what WOULD change; it never touches history.json.
"""

import argparse
import sys
from pathlib import Path

# Run as a plain script, so the repo root has to be importable either way
# (same convention as scripts/near_misses.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, data  # noqa: E402

# How much of the before/after text to show per record. Full essay-length
# answers are 500+ chars; printing them whole would bury the one line that
# actually matters (what got cut) in scrollback.
_PREVIEW_CHARS = 80


def _preview(text: str) -> str:
    """Truncate for console display only -- never affects what gets written."""
    text = (text or "").replace("\n", " ")
    if len(text) <= _PREVIEW_CHARS:
        return text
    return text[:_PREVIEW_CHARS] + "..."


def find_affected(history: list) -> list[dict]:
    """Records whose saved answer would change under _sanitize_answer."""
    affected = []
    for i, entry in enumerate(history):
        before = entry.get("answer", "")
        after = data._sanitize_answer(before)
        if after != before:
            affected.append({"index": i, "before": before, "after": after,
                             "question": entry.get("question", ""),
                             "scope": entry.get("scope", ""),
                             "date": entry.get("date", "")})
    return affected


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Strip leaked tool-call markup from saved history answers."
    )
    parser.add_argument("--apply", action="store_true",
                        help="write the repair (default: dry run, print only)")
    args = parser.parse_args()

    history = config.load_history()
    affected = find_affected(history)

    if not affected:
        print("No affected records found. history.json is clean.")
        return 0

    print("%d affected record(s) of %d total:\n" % (len(affected), len(history)))
    for rec in affected:
        print("[%d] scope=%s date=%s" % (rec["index"], rec["scope"], rec["date"]))
        print("  question: %s" % _preview(rec["question"]))
        print("  before:   %s" % _preview(rec["before"]))
        print("  after:    %s" % _preview(rec["after"]))
        print()

    if not args.apply:
        print("Dry run only -- nothing written. Re-run with --apply to write "
              "the repair to history.json.")
        return 0

    for rec in affected:
        history[rec["index"]]["answer"] = rec["after"]
    config.save_history(history)
    print("Applied: %d record(s) repaired and written to history.json." %
          len(affected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
