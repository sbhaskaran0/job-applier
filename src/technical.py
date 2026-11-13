"""Which tool outcomes are TECHNICAL failures.

The orchestrating skill needs one bit per result: "the tool could not operate
this widget / page" (route to a fallback lane: another browser, a human) as
opposed to "we have no (matching) answer for this field" (route to the
answer cascade). The statuses already say what happened; this module adds
NO second vocabulary, only `technical: true` on results whose existing status
means the tool, not the answer, failed. Absent = not a technical failure.

Kept as data so the skill routing and the code read the same table
(TECHNICAL_STATUSES; the two conditional rules are in `field_is_technical`).
"""

from . import repeating as rep

# tool -> statuses that are always technical failures
TECHNICAL_STATUSES = {
    # fill_field / fill_many; plus "uncommitted" WITHOUT `options` (below)
    "fill": ("error", "unsupported_field"),
    # fill_repeating_section, top level; plus `partial` when any row is
    # technical or orphan_groups blocked an Add
    "repeating": ("no_add_button", "ambiguous_add", "add_failed",
                  "new_group_not_blank"),
    # fill_repeating_section rows (row["status"] or row["error"]); plus
    # "uncommitted" without options, and not_found on an identity / start /
    # current key (the widget could not be located)
    "repeating_row": ("error", "group_vanished", "unsupported_date_widget"),
    "advance_step": ("no_change",),
    "start_application": ("no_entry", "blocked"),
    "authenticate": ("blocked", "unconfirmed", "error", "toggle_failed",
                     "no_auth_submit", "unsupported_auth_flow"),
    "upload_resume": ("error",),
    "submit_application": ("no_submit_button", "attempted", "rejected_spam"),
}


def field_is_technical(res: dict) -> bool:
    """A fill result (fill_field / fill_many item / repeating row)."""
    st = res.get("status")
    if st in TECHNICAL_STATUSES["fill"]:
        return True
    # uncommitted WITH options = "pick one of these" (an answer problem);
    # without options the widget itself could not be operated
    return st == "uncommitted" and not res.get("options")


def _row_is_technical(row: dict, section: str) -> bool:
    if row.get("status") in TECHNICAL_STATUSES["repeating_row"] or \
            row.get("error") in TECHNICAL_STATUSES["repeating_row"]:
        return True
    if row.get("status") == "not_found":
        must = set(rep.SECTIONS.get(section, {}).get("identity", ())) | {
            "start", "current"}
        return row.get("key") in must
    return field_is_technical(row)


def mark(tool: str, res):
    """Add `technical: true` where the existing status means a technical
    failure. Mutates and returns `res` (a dict, or a list for fill_many)."""
    if isinstance(res, list):
        for r in res:
            if isinstance(r, dict):
                mark(tool, r)
        return res
    if not isinstance(res, dict):
        return res
    if tool == "fill":
        tech = field_is_technical(res)
    elif tool == "repeating":
        section = res.get("section") or ""
        tech = res.get("status") in TECHNICAL_STATUSES["repeating"]
        for e in res.get("filled") or []:
            for row in e.get("fields") or []:
                if _row_is_technical(row, section):
                    row["technical"] = True
                    tech = True
        if res.get("orphan_groups") or any(
                err.get("error") in TECHNICAL_STATUSES["repeating"]
                for err in res.get("errors") or []):
            tech = True
    else:
        tech = res.get("status") in TECHNICAL_STATUSES.get(tool, ())
    if tech:
        res["technical"] = True
    return res
