"""Workday tenant-account registry (per profile: data/workday_accounts.json).

Which Workday tenants (keyed by host, e.g. acme.wd5.myworkdayjobs.com) have an
account for the user's email, and whether we can sign in to it:

  active                  we created it, or signed in to it successfully
  exists_unknown_password create-account said it already exists and we did
                          NOT sign in; parked for a manual sign-in
  sso_only                the tenant offers no email/password sign-in

`authenticate` decides from this whether to sign in (active), create (no
entry) or park (exists_unknown_password / sso_only). A row is
{email, status, created_at, last_sign_in_at, source}. NEVER a password or
anything derived from one: `record` takes no secret argument, and `load`
drops any key outside FIELDS so a hand-edited file cannot smuggle one into a
tool result. Timestamps are ISO-8601 UTC strings.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("active", "exists_unknown_password", "sso_only")
FIELDS = ("email", "status", "created_at", "last_sign_in_at", "source")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean(row) -> dict | None:
    if not isinstance(row, dict) or row.get("status") not in STATUSES:
        return None
    return {k: row.get(k) for k in FIELDS}


def load(path) -> dict:
    """host -> row. A missing or unreadable file is an empty registry."""
    if path is None:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for host, row in raw.items():
        r = _clean(row)
        if r is not None:
            out[str(host).strip().lower()] = r
    return out


def save(path, reg: dict) -> None:
    if path is None:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = {h: _clean(r) for h, r in (reg or {}).items() if _clean(r)}
    p.write_text(json.dumps(clean, indent=1, sort_keys=True), encoding="utf-8")


def record(reg: dict, host: str, status: str, email: str = "",
           created: bool = False, signed_in: bool = False,
           source: str = "authenticate") -> dict:
    """Upsert `host` in the in-memory registry `reg` and return the row.
    `created` stamps created_at (kept if already set), `signed_in` stamps
    last_sign_in_at. No secret is accepted here by design."""
    if status not in STATUSES:
        raise ValueError("status must be one of %s" % ", ".join(STATUSES))
    host = (host or "").strip().lower()
    row = dict(reg.get(host) or {k: None for k in FIELDS})
    now = _now()
    row.update(status=status, source=source)
    if email:
        row["email"] = email
    if created and not row.get("created_at"):
        row["created_at"] = now
    if signed_in:
        row["last_sign_in_at"] = now
    reg[host] = {k: row.get(k) for k in FIELDS}
    return reg[host]


def entries(reg: dict) -> list[dict]:
    """Rows for display, sorted by host (no secrets exist to strip)."""
    return [{"tenant": h, **r} for h, r in sorted((reg or {}).items())]
