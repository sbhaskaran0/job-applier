"""`technical: true` separates "the tool could not operate this widget /
page" from "we have no (matching) answer", reusing the existing statuses.

Pinned: the per-tool table, the two conditional rules (uncommitted without
options; not_found only on identity / start / current keys), the repeating
section's row + top-level marking, and that the MCP tools carry the flag
(fill_field also turns a raised exception into a branchable status).

Standalone -- `python tests/test_technical_flags.py`.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import test_workday_entry_auth as T      # noqa: E402  (stubs playwright)
from src import browser, technical        # noqa: E402

mark = technical.mark


def test_fill_results():
    assert mark("fill", {"status": "error"})["technical"] is True
    assert mark("fill", {"status": "uncommitted"})["technical"] is True
    assert "technical" not in mark("fill", {"status": "uncommitted",
                                            "options": ["A", "B"]})
    for st in ("filled", "unmatched", "use_fill_credential"):
        assert "technical" not in mark("fill", {"status": st}), st
    items = mark("fill", [{"status": "filled"}, {"status": "error"}])
    assert "technical" not in items[0] and items[1]["technical"] is True


def test_repeating_rows_and_top_level():
    res = {"section": "experience", "status": "partial", "errors": [],
           "filled": [{"entry": 0, "fields": [
               {"key": "title", "status": "filled", "verified": True},
               {"key": "location", "status": "error", "error": "group_vanished"},
               {"key": "start", "status": "unsupported_date_widget"},
               {"key": "company", "status": "not_found"},
               {"key": "employment_type", "status": "not_found"},
               {"key": "description", "status": "skipped", "reason": "no_value"},
           ]}]}
    mark("repeating", res)
    rows = {r["key"]: r for r in res["filled"][0]["fields"]}
    assert res["technical"] is True
    for k in ("location", "start", "company"):
        assert rows[k].get("technical") is True, k
    for k in ("title", "employment_type", "description"):
        assert "technical" not in rows[k], k
    # an answer gap only (required_empty, unmatched degree): not technical
    gap = {"section": "education", "status": "partial", "errors": [],
           "required_empty": [{"entry": 0, "key": "gpa"}],
           "filled": [{"entry": 0, "fields": [
               {"key": "degree", "status": "unmatched", "options": ["BA"]}]}]}
    assert "technical" not in mark("repeating", gap)
    for st in ("no_add_button", "ambiguous_add", "add_failed",
               "new_group_not_blank"):
        assert mark("repeating", {"section": "experience", "status": st,
                                  "filled": [], "errors": []})["technical"]
    assert mark("repeating", {"section": "experience", "status": "partial",
                              "filled": [], "errors": [],
                              "orphan_groups": [1]})["technical"]
    for st in ("ok", "already_present", "section_not_found", "no_entries"):
        assert "technical" not in mark("repeating", {"section": "experience",
                                                     "status": st,
                                                     "filled": [], "errors": []})


def test_page_level_tools():
    table = {
        "advance_step": (("no_change",), ("advanced", "blocked_validation",
                                          "is_submit_page", "entry_page")),
        "start_application": (("no_entry", "blocked"),
                              ("form", "auth_required", "already_in_application")),
        "authenticate": (("blocked", "unconfirmed", "error", "toggle_failed",
                          "no_auth_submit", "unsupported_auth_flow"),
                         ("authenticated", "account_exists", "bad_credentials",
                          "no_credentials", "email_verification_required",
                          "recently_submitted", "account_created_signin_required")),
        "upload_resume": (("error",), ("uploaded", "no_cover_slot")),
        "submit_application": (("no_submit_button", "attempted", "rejected_spam"),
                               ("submitted",)),
    }
    for tool, (tech, fine) in table.items():
        for st in tech:
            assert mark(tool, {"status": st}).get("technical") is True, (tool, st)
        for st in fine:
            assert "technical" not in mark(tool, {"status": st}), (tool, st)


class Fake:
    async def fill_field(self, index, value):
        raise RuntimeError("Timeout 30000ms exceeded")

    async def fill_many(self, items):
        return [{"index": 0, "status": "uncommitted"}]

    async def advance_step(self):
        return {"status": "no_change"}

    async def authenticate(self, mode="auto"):
        return {"status": "unconfirmed"}

    async def start_application(self):
        return {"status": "form"}


def test_mcp_tools_carry_the_flag():
    ms = T._load_mcp_server()
    real = browser.session
    browser.session = Fake()
    try:
        r = asyncio.run(ms.fill_field(3, "x"))
        assert r["status"] == "error" and r["technical"] is True, r
        assert r["index"] == 3 and "Timeout" in r["error"], r
        assert asyncio.run(ms.fill_many([{"index": 0, "value": "x"}]))[0]["technical"]
        assert asyncio.run(ms.advance_step())["technical"] is True
        assert asyncio.run(ms.authenticate())["technical"] is True
        assert "technical" not in asyncio.run(ms.start_application())
    finally:
        browser.session = real


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
        except Exception as exc:
            failed += 1
            print("ERROR %s: %s: %s" % (name, type(exc).__name__, exc))
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
