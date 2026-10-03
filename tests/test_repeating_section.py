"""fill_repeating_section: Workday "My Experience" repeating groups.

Pinned here (no browser; Playwright is stubbed):
  * label -> entry-key mapping for both sections, incl. split-date boxes whose
    meaning comes from the context label ("From" above Month / Year);
  * date formatting for each widget shape (MM/YYYY text, YYYY-MM, YYYY,
    separate Month / Year, native month/date) and that a missing month is
    never invented;
  * grouping by sub-heading ordinal and, without sub-headings, by a repeated
    slot;
  * idempotency: existing groups that already hold an entry are left alone,
    blank ones are reused before Add is clicked;
  * the orchestration against a fake page: "I currently work here" is set
    BEFORE To and To is skipped for a current role, gpa_policy, the 8-entry
    cap, unsupported_date_widget, add_failed, and that results never echo a
    long description;
  * data.get_profile_field never returns a list-valued (structured) block,
    and the zip / postal code aliases.

Standalone -- `python tests/test_repeating_section.py`.
"""

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_pw = types.ModuleType("playwright")
_pw_async = types.ModuleType("playwright.async_api")
_pw_async.async_playwright = lambda: None
sys.modules.setdefault("playwright", _pw)
sys.modules.setdefault("playwright.async_api", _pw_async)

from src import browser, config, data          # noqa: E402
from src import repeating as rep               # noqa: E402


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------
def test_label_key_experience():
    table = {
        "Job Title*": "title", "Company*": "company", "Location": "location",
        "I currently work here": "current", "From*": "start", "To": "end",
        "Role Description": "description", "Employer": "company",
        "Start Date": "start", "End Date (required)": "end",
        "Employment Type": "employment_type", "Favourite colour": None,
    }
    for label, want in table.items():
        assert rep.label_key(label, "experience") == want, (label, want)


def test_label_key_education():
    table = {
        "School or University*": "school", "Degree*": "degree",
        "Field of Study": "field_of_study", "Overall Result (GPA)": "gpa",
        "From": "start", "To (Actual or Expected)*": "end",
        "Graduation Date": "end", "Major": "field_of_study",
        "Job Title": None,
    }
    for label, want in table.items():
        assert rep.label_key(label, "education") == want, (label, want)


def test_split_date_boxes_take_their_key_from_context():
    month = {"label": "Month", "aria": "Month", "ctx": "From*"}
    year = {"label": "Year", "aria": "Year", "ctx": "To"}
    # Workday-style: label[for] names the Month box "From", aria says Month
    month_for = {"label": "From*", "aria": "Month", "ctx": ""}
    assert rep.field_key(month, "experience") == "start"
    assert rep.date_part(month) == "month"
    assert rep.field_key(year, "experience") == "end"
    assert rep.date_part(year) == "year"
    assert rep.field_key(month_for, "experience") == "start"
    assert rep.date_part(month_for) == "month"
    assert rep.date_part({"label": "From", "placeholder": "MM/YYYY"}) is None


def test_date_formatting_each_shape():
    assert rep.format_date("2021-05", "mm/yyyy") == "05/2021"
    assert rep.format_date("2021-5", "mm/yyyy") == "05/2021"
    assert rep.format_date("2021-05", "yyyy-mm") == "2021-05"
    assert rep.format_date("2021-05", "yyyy") == "2021"
    assert rep.format_date("2021-05", "year") == "2021"
    assert rep.format_date("2021-05", "month") == "05"
    assert rep.format_date("2021-05", "month_name") == "May"
    assert rep.format_date("2021-05", "date") == "2021-05-01"
    # a year-only profile date never gets an invented month
    assert rep.format_date("2021", "mm/yyyy") is None
    assert rep.format_date("2021", "month") is None
    assert rep.format_date("2021", "year") == "2021"
    assert rep.format_date("", "year") is None
    assert rep.format_date("2021-13", "mm/yyyy") is None


def test_single_date_shape_from_placeholder_and_type():
    assert rep.single_date_shape({"placeholder": "MM/YYYY"}) == "mm/yyyy"
    assert rep.single_date_shape({"placeholder": "YYYY-MM"}) == "yyyy-mm"
    assert rep.single_date_shape({"placeholder": "YYYY"}) == "yyyy"
    assert rep.single_date_shape({"itype": "month"}) == "yyyy-mm"
    assert rep.single_date_shape({"itype": "date"}) == "date"
    assert rep.single_date_shape({}) == "mm/yyyy"      # spec default


def test_gpa_policy():
    assert rep.gpa_allowed("always", False) is True
    assert rep.gpa_allowed("never", True) is False
    assert rep.gpa_allowed("if_required", False) is False
    assert rep.gpa_allowed("if_required", True) is True
    assert rep.gpa_allowed("bogus", False) is False      # -> if_required


def test_degree_candidates_only_generalize_the_level():
    c = rep.degree_candidates("Bachelor of Science")
    assert c[0] == "Bachelor of Science" and "Bachelor's Degree" in c
    assert "Master's Degree" in rep.degree_candidates("MBA")
    assert rep.degree_candidates("Certificate") == ["Certificate"]
    opts = ["High School Diploma", "Bachelor's Degree", "Master's Degree"]
    pick = browser.BrowserSession._match_option(
        rep.degree_candidates("Bachelor of Science"), opts)
    assert pick == "Bachelor's Degree", pick


def _f(i, label, value="", group=-1, **kw):
    return {"index": i, "label": label, "current_value": value,
            "group": group, "kind": kw.pop("kind", "text"), **kw}


def test_grouping_by_subheading_and_by_repeated_slot():
    rows = [_f(1, "Job Title", group=0), _f(2, "Company", group=0),
            _f(3, "Job Title", group=1), _f(4, "Company", group=1)]
    gs = rep.group_fields(rows, "experience")
    assert [[f["index"] for f in g] for g in gs] == [[1, 2], [3, 4]]
    # no sub-headings: a repeated (key, part) slot starts a new group, and the
    # Month and Year boxes of one date do NOT split a group
    rows = [_f(1, "Job Title"), _f(2, "From", aria="Month"),
            _f(3, "Year", aria="Year", ctx="From"), _f(4, "Job Title"),
            _f(5, "Company")]
    gs = rep.group_fields(rows, "experience")
    assert [[f["index"] for f in g] for g in gs] == [[1, 2, 3], [4, 5]], gs


def test_idempotent_match_and_blank_reuse():
    entries = [{"company": "Globex", "title": "Senior PM"},
               {"company": "Acme Corp", "title": "Product Manager"},
               {"company": "Initech", "title": "Analyst"}]
    groups = rep.group_fields(
        [_f(1, "Job Title", "product manager", 0), _f(2, "Company", "ACME CORP, Inc.", 0),
         _f(3, "Job Title", "", 1), _f(4, "Company", "", 1)], "experience")
    matched, reusable = rep.plan_groups(groups, entries, "experience")
    assert matched == {1: 0}, matched
    assert reusable == [1], reusable
    # a title that merely contains another is NOT the same role
    g = rep.group_fields([_f(1, "Job Title", "Senior Product Manager", 0),
                          _f(2, "Company", "Acme Corp", 0)], "experience")
    assert not rep.entry_matches(g[0], entries[1], "experience")
    eg = rep.group_fields([_f(1, "School", "University of Example", 0)],
                          "education")
    assert rep.entry_matches(eg[0], {"school": "university of example"},
                             "education")


def test_clip_truncates_to_60():
    out = rep.clip("x" * 500)
    assert len(out) == 60 and out.endswith("..."), out


# --------------------------------------------------------------------------
# orchestration against a fake page
# --------------------------------------------------------------------------
def _exp_group(n, title="", company="", single_dates=False, picker_only=False):
    rows = [("Job Title*", title), ("Company*", company), ("Location", ""),
            ("I currently work here", "", "checkbox")]
    out = [{"label": r[0], "current_value": r[1],
            "kind": r[2] if len(r) > 2 else "text"} for r in rows]
    if picker_only:
        pass
    elif single_dates:
        out += [{"label": "From", "placeholder": "MM/YYYY"},
                {"label": "To", "placeholder": "MM/YYYY", "to": True}]
    else:
        out += [{"label": "From*", "aria": "Month"},
                {"label": "Year", "aria": "Year", "ctx": "From*"},
                {"label": "Month", "aria": "Month", "ctx": "To", "to": True},
                {"label": "Year", "aria": "Year", "ctx": "To", "to": True}]
    out.append({"label": "Role Description", "kind": "text"})
    for f in out:
        f.setdefault("kind", "text")
        f.setdefault("current_value", "")
        f["group"] = n
    return out


def _edu_group(n, school=""):
    out = [{"label": "School or University*", "current_value": school},
           {"label": "Degree*"}, {"label": "Field of Study"},
           {"label": "Overall Result (GPA)"},
           {"label": "To (Actual or Expected)*", "placeholder": "YYYY"}]
    for f in out:
        f.setdefault("kind", "text")
        f.setdefault("current_value", "")
        f["group"] = n
    return out


class FakeSession(browser.BrowserSession):
    """A page of field groups. fill_field writes into it; ticking the
    current-role box removes the group's To boxes (as Workday does)."""

    def __init__(self, section, groups, make_group, add_works=True,
                 pickers=()):
        super().__init__()
        self.tabs = {0: {"page": object(), "fields": {}, "filled": False}}
        self.active_id = 0
        self.section, self.groups, self.make_group = section, groups, make_group
        self.add_works, self.pickers = add_works, set(pickers)
        self.calls, self.add_clicks = [], 0

    def _rows(self):
        rows, i = [], 0
        for g in self.groups:
            for f in g:
                f["index"] = i
                rows.append(dict(f))
                i += 1
        return rows

    async def _section_layout(self, section):
        return {"frame": None, "add": True, "add_label": "Add",
                "groups": rep.group_fields(self._rows(), section),
                "picker_keys": self.pickers}

    async def _add_group(self, section, n_before):
        self.add_clicks += 1
        if not self.add_works:
            return False, await self._section_layout(section), 2
        self.groups.append(self.make_group(len(self.groups)))
        return True, await self._section_layout(section), 1

    async def fill_field(self, index, value):
        self._mark_filled()
        for g in self.groups:
            for f in g:
                if f.get("index") != index:
                    continue
                self.calls.append((f["label"], value))
                if f["kind"] == "checkbox":
                    on = value == "true"
                    f["current_value"] = "checked" if on else ""
                    if on:
                        g[:] = [x for x in g if not x.get("to")]
                else:
                    f["current_value"] = value
                return {"index": index, "kind": f["kind"], "status": "filled",
                        "value": value, "verified": True}
        raise ValueError("unknown index %s" % index)


def _run(s, section, entries, policy="always"):
    return asyncio.run(s.fill_repeating_section(section, entries=entries,
                                                gpa_policy=policy))


LONG = "Led a very long and detailed initiative " * 5


def test_current_role_sets_checkbox_before_to_and_skips_to():
    s = FakeSession("experience", [], _exp_group)
    res = _run(s, "experience", [{"company": "Globex", "title": "PM",
                                  "start": "2022-03", "end": "", "current": True,
                                  "description": LONG}])
    labels = [c[0] for c in s.calls]
    assert labels[0] == "I currently work here", labels
    assert ("From*", "03") in s.calls and ("Year", "2022") in s.calls, s.calls
    assert not any(c[1] == "" for c in s.calls), s.calls
    assert res["status"] == "ok" and res["added"] == 1, res
    assert s.filled
    desc = [f for f in res["filled"][0]["fields"] if f["key"] == "description"]
    assert len(desc[0]["value"]) <= 60 and LONG not in repr(res), desc


def test_past_role_fills_to_and_leaves_box_unticked():
    s = FakeSession("experience", [], _exp_group)
    res = _run(s, "experience", [{"company": "Acme", "title": "PM",
                                  "start": "2019-06", "end": "2022-02",
                                  "current": False}])
    assert ("Month", "02") in s.calls and ("Year", "2022") in s.calls, s.calls
    cur = [f for f in res["filled"][0]["fields"] if f["key"] == "current"]
    assert cur[0]["status"] == "already_set", cur
    assert "I currently work here" not in [c[0] for c in s.calls]


def test_single_box_dates_use_mm_yyyy():
    s = FakeSession("experience", [], lambda n: _exp_group(n, single_dates=True))
    _run(s, "experience", [{"company": "A", "title": "B", "start": "2020-01",
                            "end": "2021-12"}])
    assert ("From", "01/2020") in s.calls and ("To", "12/2021") in s.calls, s.calls


def test_picker_only_date_reports_unsupported():
    s = FakeSession("experience", [], lambda n: _exp_group(n, picker_only=True),
                    pickers={"start", "end"})
    res = _run(s, "experience", [{"company": "A", "title": "B",
                                  "start": "2020-01", "end": "2021-12"}])
    st = {f["key"]: f["status"] for f in res["filled"][0]["fields"]}
    assert st["start"] == "unsupported_date_widget", st
    assert st["end"] == "unsupported_date_widget", st
    assert res["status"] == "partial", res["status"]


def test_gpa_policy_in_flow():
    entry = {"school": "Example U", "degree": "BS", "end": "2018-05",
             "gpa": "3.66"}
    for policy, want in (("always", True), ("never", False),
                         ("if_required", False)):
        s = FakeSession("education", [], _edu_group)
        res = _run(s, "education", [entry], policy)
        gpa = [f for f in res["filled"][0]["fields"] if f["key"] == "gpa"][0]
        assert (gpa["status"] == "filled") is want, (policy, gpa)
        if not want:
            assert gpa["reason"] == "gpa_policy"
    # an empty GPA is never filled even under "always"
    s = FakeSession("education", [], _edu_group)
    res = _run(s, "education", [dict(entry, gpa="")], "always")
    gpa = [f for f in res["filled"][0]["fields"] if f["key"] == "gpa"][0]
    assert gpa["reason"] == "no_value", gpa
    assert ("To (Actual or Expected)*", "2018") in s.calls, s.calls


def test_idempotent_flow_skips_present_and_reuses_blank():
    groups = [_exp_group(0, "Product Manager", "Acme Corp"), _exp_group(1)]
    s = FakeSession("experience", groups, _exp_group)
    res = _run(s, "experience", [{"company": "Globex", "title": "Senior PM"},
                                 {"company": "acme corp", "title": "product manager"}])
    assert res["added"] == 0 and s.add_clicks == 0, res
    assert res["filled"][0]["group"] == 1, res["filled"]
    assert {"entry": 1, "reason": "already_present", "group": 0} in res["skipped"]
    res = _run(s, "experience", [{"company": "Globex", "title": "Senior PM"},
                                 {"company": "Acme Corp", "title": "Product Manager"}])
    assert res["status"] == "already_present" and not res["filled"], res


def test_entry_cap():
    s = FakeSession("experience", [], _exp_group)
    entries = [{"company": "C%d" % i, "title": "T%d" % i} for i in range(11)]
    res = _run(s, "experience", entries)
    assert res["added"] == rep.MAX_ENTRIES == 8, res["added"]
    capped = [k["entry"] for k in res["skipped"] if k["reason"] == "entry_cap"]
    assert capped == [8, 9, 10], capped


def test_add_failure_stops_and_reports():
    s = FakeSession("experience", [], _exp_group, add_works=False)
    res = _run(s, "experience", [{"company": "A", "title": "B"},
                                 {"company": "C", "title": "D"}])
    assert res["status"] == "add_failed", res
    assert s.add_clicks == 1, s.add_clicks
    assert {"entry": 1, "reason": "add_failed"} in res["skipped"], res


def test_bad_section_and_no_entries():
    s = FakeSession("experience", [], _exp_group)
    assert _run(s, "skills", [{"a": 1}])["status"] == "bad_section"
    assert _run(s, "experience", [])["status"] == "no_entries"
    assert not s.calls


# --------------------------------------------------------------------------
# data layer
# --------------------------------------------------------------------------
PROFILE = {
    "email": "x@example.test", "zip": "90001", "city": "Springfield",
    "gpa_policy": "always",
    "experience": [{"company": "A", "title": "B"},
                   {"company": "", "title": "", "current": False}],
    "education": [{"school": "U"}],
}


def _with_profile(fn):
    real = config.load_user_profile
    config.load_user_profile = lambda: PROFILE
    try:
        return fn()
    finally:
        config.load_user_profile = real


def test_no_list_values_through_get_profile_field():
    def go():
        for q in ("experience", "education", "Experience", "Work Experience",
                  "Education", "Previous experience"):
            r = data.get_profile_field(q)
            assert not isinstance(r.get("value"), (list, dict)), (q, r)
            assert r.get("matched_key") not in ("experience", "education"), (q, r)
        assert data.get_profile_field("experience")["value"] is None
    _with_profile(go)


def test_profile_section_drops_blank_placeholders():
    def go():
        assert data.profile_section("experience") == [{"company": "A", "title": "B"}]
        assert data.profile_section("education") == [{"school": "U"}]
        assert data.profile_section("email") == []
    _with_profile(go)


def test_zip_aliases():
    def go():
        for q in ("Zip", "Zip Code*", "Postal Code", "Postcode",
                  "Zip/Postal Code", "ZIP / Postal Code"):
            r = data.get_profile_field(q)
            assert r["matched_key"] == "zip" and r["value"] == "90001", (q, r)
        assert data.get_profile_field("City")["matched_key"] == "city"
    _with_profile(go)


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
