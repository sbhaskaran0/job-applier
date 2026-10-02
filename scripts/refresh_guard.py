"""Pre-flight for scripts/refresh.cmd.

Exit 10 = skip this run (last health file says ok:true and is < 12h old).
Exit 0  = go ahead. Fails open: any error, a missing or malformed health file,
`--force` in argv, or JOBAPPLIER_FORCE_REFRESH=1 all mean "run".

Also rotates data/refresh.log to refresh.log.1 once it passes ~1 MB.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
SKIP_WITHIN = timedelta(hours=12)
LOG_LIMIT = 1_000_000


def _rotate_log() -> None:
    log = DATA / "refresh.log"
    if log.exists() and log.stat().st_size > LOG_LIMIT:
        os.replace(log, DATA / "refresh.log.1")


def _recent_success() -> bool:
    health = json.loads((DATA / "refresh-health.json").read_text(encoding="utf-8"))
    if health.get("ok") is not True:
        return False
    run_at = datetime.fromisoformat(str(health["run_at"]).replace("Z", "+00:00"))
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=timezone.utc)
    return timedelta(0) <= datetime.now(timezone.utc) - run_at < SKIP_WITHIN


def main() -> int:
    try:
        _rotate_log()
    except Exception:
        pass
    if "--force" in sys.argv[1:] or os.environ.get("JOBAPPLIER_FORCE_REFRESH") == "1":
        return 0
    try:
        return 10 if _recent_success() else 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
