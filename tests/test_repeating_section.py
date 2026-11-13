"""fill_repeating_section: Workday "My Experience" repeating groups.

Pinned here (no browser; Playwright is stubbed):
  * label -> entry-key mapping for both sections, incl. split-date boxes
    ("Start Month", a bare "Year" under "From") and that an unmapped field
    never inherits a neighbour's label; "Overall Result" is not assumed GPA;
  * date formatting for each widget shape and that a missing month is never
    invented; a native type=date result says day_assumed;
  * degree matching: B.S. / BS / "Bachelor of Science in Economics" land on
    "Bachelor of Science (BS)" even when "Bachelor of Arts (BA)" is listed
    first; generic level names only match generic options; no confident
    match -> `unmatched`, nothing picked;
  * grouping by sub-heading ordinal, or (without one) only when an identity
    key repeats;
  * idempotency: loose company/title/start matching, promotion pairing,
    fill-blanks that never overwrite, orphan groups reused (run 2 and 3 do
    not grow the page), unresolvable orphans block Add;
  * orchestration: "I currently work here" before To (native and
    [role=checkbox]), a missing checkbox does NOT skip To, explicit boolean
    parse, gpa_policy (+ unknown-policy warning), the 8-entry cap indexed by
    the PROFILE list, add_failed, blur-revert detection, new group found by
    diff wherever it is inserted, required-empty / identity-not-found honesty,
    no long description echoed, error strings clipped;
  * data.get_profile_field never returns a list-valued block; zip aliases.

Standalone -- `python tests/test_repeating_section.py`.
"""

import asyncio
import itertools
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
# labels and dates
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
        "Cumulative grade point average": "gpa",
        "Overall Result": "overall_result",
        "From": "start", "To (Actual or Expected)*": "end",
        "Graduation Date": "end", "Major": "field_of_study",
        "Job Title": None,
    }
    for label, want in table.items():
        assert rep.label_key(label, "education") == want, (label, want)


def test_split_date_boxes():
    cases = [
        ({"label": "Month", "aria": "Month", "ctx": "From*"}, "start", "month"),
        ({"label": "Year", "aria": "Year", "ctx": "To"}, "end", "year"),
        ({"label": "From*", "aria": "Month", "ctx": ""}, "start", "month"),
        ({"label": "Start Month"}, "start", "month"),
        ({"label": "End Year*"}, "end", "year"),
        ({"label": "Graduation Year"}, "end", "year"),
    ]
    for desc, key, part in cases:
        sec = "education" if "Graduation" in desc["label"] else "experience"
        assert rep.field_key(desc, sec) == key, desc
        assert rep.date_part(desc) == part, desc
    assert rep.date_part({"label": "From", "placeholder": "MM/YYYY"}) is None
    assert rep.date_part({"label": "Month/Year"}) is None


def test_unmapped_field_does_not_inherit_context():
    assert rep.field_key({"label": "Notes", "ctx": "From"}, "experience") is None
    assert rep.field_key({"label": "Anything else?", "ctx": "Job Title"},
                         "experience") is None


def test_date_formatting_each_shape():
    assert rep.format_date("2021-05", "mm/yyyy") == "05/2021"
    assert rep.format_date("2021-5", "mm/yyyy") == "05/2021"
    assert rep.format_date("2021-05", "yyyy-mm") == "2021-05"
    assert rep.format_date("2021-05", "yyyy") == "2021"
    assert rep.format_date("2021-05", "year") == "2021"
    assert rep.format_date("2021-05", "month") == "05"
    assert rep.format_date("2021-05", "month_name") == "May"
    assert rep.format_date("2021-05", "date") == "2021-05-01"
    assert rep.format_date("2021", "mm/yyyy") is None     # never invent a month
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
    assert rep.single_date_shape({}) == "mm/yyyy"


def test_gpa_policy_and_truthy():
    assert rep.gpa_allowed("always", False) is True
    assert rep.gpa_allowed("never", True) is False
    assert rep.gpa_allowed("if_required", False) is False
    assert rep.gpa_allowed("if_required", True) is True
    for v in (True, "true", "Yes", "1", 1, "y"):
        assert rep.truthy(v), v
    for v in (False, "false", "no", "0", 0, "", None, "False"):
        assert not rep.truthy(v), v


# --------------------------------------------------------------------------
# degrees
# --------------------------------------------------------------------------
SPECIFIC = ["High School Diploma", "Bachelor of Arts (BA)",
            "Bachelor of Science (BS)", "Master of Arts (MA)",
            "Master of Science (MS)", "Master of Business Administration (MBA)",
            "Doctor of Philosophy (PhD)"]
GENERIC = ["High School Diploma", "Associate's Degree", "Bachelor's Degree",
           "Master's Degree", "Doctorate"]


def test_degree_reviewer_repros_pick_bs_not_ba():
    for v in ("B.S.", "BS", "bs", "Bachelor of Science",
              "Bachelor of Science in Economics", "B.Sc."):
        assert rep.match_degree(v, SPECIFIC) == "Bachelor of Science (BS)", v
    assert rep.match_degree("BA", SPECIFIC) == "Bachelor of Arts (BA)"
    assert rep.match_degree("M.A.", SPECIFIC) == "Master of Arts (MA)"
    assert rep.match_degree("MS", SPECIFIC) == "Master of Science (MS)"
    assert rep.match_degree("MBA", SPECIFIC) == \
        "Master of Business Administration (MBA)"
    assert rep.match_degree("Ph.D.", SPECIFIC) == "Doctor of Philosophy (PhD)"
    assert rep.match_degree("Master of Science in Data Science", SPECIFIC) == \
        "Master of Science (MS)"


def test_degree_master_of_x_maps_to_its_option():
    opts = ["Master of Urban Planning (MUP)", "Master's Degree", "Master of Arts (MA)"]
    assert rep.match_degree("Master of Urban Planning", opts) == opts[0]
    assert rep.match_degree("Master of Urban Planning in Transport", opts) == opts[0]


def test_degree_generic_only_against_generic_options():
    assert rep.match_degree("Bachelor of Science", GENERIC) == "Bachelor's Degree"
    assert rep.match_degree("B.S.", GENERIC) == "Bachelor's Degree"
    assert rep.match_degree("MBA", GENERIC) == "Master's Degree"
    assert rep.match_degree("PhD", GENERIC) == "Doctorate"
    # a generic name never lands on a DIFFERENT specific degree
    assert rep.match_degree("B.S.", ["Bachelor of Arts (BA)", "Master of Arts (MA)"]) is None
    assert rep.match_degree("Bachelor's Degree", ["Bachelor of Arts (BA)"]) is None
    assert rep.match_degree("Certificate", SPECIFIC) is None
    assert rep.match_degree("", SPECIFIC) is None


# --------------------------------------------------------------------------
# grouping and matching
# --------------------------------------------------------------------------
_EID = itertools.count(1)


def mk(label, value="", group=-1, **kw):
    return {"index": kw.pop("index", None), "eid": str(next(_EID)), "label": label,
            "current_value": value, "group": group,
            "kind": kw.pop("kind", "text"), **kw}


def test_grouping_by_subheading():
    rows = [mk("Job Title", group=0), mk("Company", group=0),
            mk("Job Title", group=1), mk("Company", group=1)]
    gs = rep.group_fields(rows, "experience")
    assert [len(g) for g in gs] == [2, 2]


def test_fallback_grouping_splits_only_on_identity_repeat():
    rows = [mk("Job Title"), mk("Company"), mk("From", aria="Month"),
            mk("Year", aria="Year", ctx="From"), mk("To", aria="Month"),
            mk("Year", aria="Year", ctx="To"), mk("Notes"), mk("Location"),
            mk("Location"),            # a repeated NON-identity key: no split
            mk("Job Title"), mk("Company")]
    gs = rep.group_fields(rows, "experience")
    assert [len(g) for g in gs] == [9, 2], [len(g) for g in gs]


def _grp(section, *pairs):
    return rep.group_fields([mk(lab, val, group=0, **kw) for lab, val, kw in
                             [(p[0], p[1], p[2] if len(p) > 2 else {}) for p in pairs]],
                            section)[0]


def test_company_and_title_loose_match():
    g = _grp("experience", ("Job Title", "Sr. Product Mgr"), ("Company", "ACME, Inc."))
    assert rep.match_score(g, {"company": "Acme", "title": "Senior Product Manager"},
                           "experience") is not None
    g2 = _grp("experience", ("Job Title", "Head of Growth"), ("Company", "Acme LLC"))
    assert rep.match_score(g2, {"company": "Acme", "title": "Senior Product Manager"},
                           "experience") is None
    # a different title but the same start month still matches
    g3 = _grp("experience", ("Job Title", "Head of Growth"), ("Company", "Acme Corp"),
              ("From", "06", {"aria": "Month"}),
              ("Year", "2019", {"aria": "Year", "ctx": "From"}))
    assert rep.match_score(g3, {"company": "Acme", "title": "Product Manager",
                                "start": "2019-06"}, "experience") is not None
    # another company never matches
    g4 = _grp("experience", ("Job Title", "Product Manager"), ("Company", "Initech"))
    assert rep.match_score(g4, {"company": "Acme", "title": "Product Manager"},
                           "experience") is None


def test_promotion_pairs_exact_titles():
    groups = [_grp("experience", ("Job Title", "Senior Product Manager"),
                   ("Company", "Acme")),
              _grp("experience", ("Job Title", "Product Manager"), ("Company", "Acme"))]
    entries = [{"company": "Acme", "title": "Product Manager"},
               {"company": "Acme", "title": "Senior Product Manager"}]
    plan = rep.plan_groups(groups, entries, "experience")
    assert plan["matched"] == {0: 1, 1: 0}, plan


def test_orphans_planned_and_unresolved():
    orphan = _grp("education", ("School or University", ""),
                  ("Degree", "Bachelor of Science (BS)", {"kind": "combobox"}))
    bad = _grp("education", ("School or University", ""),
               ("Degree", "Master of Arts (MA)", {"kind": "combobox"}))
    blank = _grp("education", ("School or University", ""), ("Degree", ""))
    entries = [{"school": "Example U", "degree": "B.S."}]
    plan = rep.plan_groups([orphan, bad, blank], entries, "education")
    assert plan["orphans"] == {0: 0}, plan
    assert plan["unresolved"] == [1] and plan["blanks"] == [2], plan


def test_clip_truncates():
    out = rep.clip("x" * 500)
    assert len(out) == 60 and out.endswith("..."), out
    assert len(rep.clip("e" * 500, rep.ERROR_ECHO)) == 120


def test_new_group_found_by_diff_anywhere():
    old = [mk("Job Title", "A", 0), mk("Company", "B", 0)]
    new = [mk("Job Title", "", 1), mk("Company", "", 1)]
    lay = {"groups": rep.group_fields(new + old, "experience")}
    known = {f["eid"] for f in old}
    got = browser.BrowserSession._new_blank_groups(lay, known)
    assert got == [new[0]["eid"]], got
    assert browser.BrowserSession._new_blank_groups(
        {"groups": rep.group_fields(old, "experience")}, known) is None


# --------------------------------------------------------------------------
# orchestration against a fake page
# --------------------------------------------------------------------------
def exp_group(title="", company="", dates="split", box="checkbox", req_loc=False,
              no_company=False):
    out = [mk("Job Title*", title)]
    if not no_company:
        out.append(mk("Company*", company))
    out.append(mk("Location*" if req_loc else "Location"))
    if box == "checkbox":
        out.append(mk("I currently work here", kind="checkbox"))
    elif box == "aria":
        out.append(mk("I currently work here", kind="aria_checkbox", rcb=0))
    if dates == "split":
        out += [mk("From*", aria="Month"), mk("Year", aria="Year", ctx="From*"),
                mk("Month", aria="Month", ctx="To", to=True),
                mk("Year", aria="Year", ctx="To", to=True)]
    elif dates == "single":
        out += [mk("From", placeholder="MM/YYYY"),
                mk("To", placeholder="MM/YYYY", to=True)]
    elif dates == "native":
        out += [mk("From", itype="date"), mk("To", itype="date", to=True)]
    out.append(mk("Role Description"))
    return out


def edu_group(school="", degree="", gpa_label="Overall Result (GPA)",
              gpa_req=False, degree_opts=None):
    out = [mk("School or University*", school),
           mk("Degree*", degree, kind="combobox", widget="listbox",
              options=degree_opts or SPECIFIC),
           mk("Field of Study"), mk(gpa_label, required=gpa_req),
           mk("To (Actual or Expected)*", placeholder="YYYY")]
    return out


class FakeKeyboard:
    async def press(self, key):
        return None


class FakePage:
    keyboard = FakeKeyboard()

    async def wait_for_timeout(self, ms):
        return None


class FakeSession(browser.BrowserSession):
    """A page of field groups. fill_field writes into it; ticking the
    current-role box removes the group's To boxes (as Workday does)."""

    def __init__(self, groups, make_group, add_works=True, pickers=(),
                 revert=(), insert_top=False):
        super().__init__()
        self.tabs = {0: {"page": FakePage(), "fields": {}, "filled": False}}
        self.active_id = 0
        self.groups, self.make_group = groups, make_group
        self.add_works, self.pickers = add_works, set(pickers)
        self.revert, self.insert_top = set(revert), insert_top
        self.calls, self.add_clicks = [], 0

    def _all(self):
        i = 0
        for gi, g in enumerate(self.groups):
            for f in g:
                f["index"] = None if f["kind"] == "aria_checkbox" else i
                f["group"] = gi
                i += 1
                yield gi, g, f

    async def _section_layout(self, section):
        rows = [dict(f) for _, _, f in self._all()]
        return {"frame": None, "add": True, "ambiguous_add": False,
                "groups": rep.group_fields(rows, section),
                "picker_keys": self.pickers}

    async def _add_group(self, section, known):
        self.add_clicks += 1
        if not self.add_works:
            return "add_failed", 2, []
        g = self.make_group()
        if self.insert_top:
            self.groups.insert(0, g)
        else:
            self.groups.append(g)
        lay = await self._section_layout(section)
        new = self._new_blank_groups(lay, known)
        return ("ok" if new else "new_group_not_blank"), 1, new or []

    def _find(self, pred):
        for gi, g, f in self._all():
            if pred(f):
                return g, f
        raise ValueError("no such field")

    def _set(self, g, f, value):
        self.calls.append((f["label"], value))
        if f["kind"] in ("checkbox", "aria_checkbox"):
            on = value == "true"
            f["current_value"] = "checked" if on else ""
            if on:
                g[:] = [x for x in g if not x.get("to")]
        else:
            f["current_value"] = value

    async def fill_field(self, index, value):
        self._mark_filled()
        g, f = self._find(lambda x: x.get("index") == index)
        self._set(g, f, value)
        return {"index": index, "kind": f["kind"], "status": "filled",
                "value": value, "verified": True}

    async def _toggle_aria_checkbox(self, f, value):
        g, real = self._find(lambda x: x.get("eid") == f["eid"])
        self._set(g, real, value)
        return {"status": "filled", "verified": True, "value": value}

    async def _read_back(self, index):
        _, f = self._find(lambda x: x.get("index") == index)
        return "" if f["label"] in self.revert else f["current_value"]

    async def get_field_options(self, index):
        _, f = self._find(lambda x: x.get("index") == index)
        return {"index": index, "options": f.get("options") or []}

    async def _pill(self, index):
        return ""


def _run(s, section, entries, policy="always"):
    return asyncio.run(s.fill_repeating_section(section, entries=entries,
                                                gpa_policy=policy))


def _rows(res, pos=0):
    for e in res["filled"]:
        if e["entry"] == pos:
            return e["fields"]
    return []


LONG = "Led a very long and detailed initiative " * 5
CUR = {"company": "Globex", "title": "PM", "location": "X", "start": "2022-03",
       "end": "", "current": True, "description": LONG}
PAST = {"company": "Acme", "title": "PM", "location": "Y", "start": "2019-06",
        "end": "2022-02", "current": False, "description": "d"}


def test_current_role_sets_checkbox_before_to_and_skips_to():
    s = FakeSession([], exp_group)
    res = _run(s, "experience", [CUR])
    labels = [c[0] for c in s.calls]
    assert labels[0] == "I currently work here", labels
    assert ("From*", "03") in s.calls and ("Year", "2022") in s.calls, s.calls
    assert not any(c[0] == "Month" for c in s.calls), s.calls
    assert res["status"] == "ok" and res["added"] == 1, res
    assert s.filled
    desc = [f for f in _rows(res) if f["key"] == "description"]
    assert len(desc[0]["value"]) <= 60 and LONG not in repr(res), desc


def test_aria_checkbox_current_role():
    s = FakeSession([], lambda: exp_group(box="aria"))
    res = _run(s, "experience", [CUR])
    cur = [f for f in _rows(res) if f["key"] == "current"]
    assert cur and cur[0]["status"] == "filled", cur
    assert not any(c[0] == "Month" for c in s.calls), s.calls


def test_string_false_does_not_tick_current():
    s = FakeSession([], exp_group)
    _run(s, "experience", [dict(PAST, current="false")])
    assert "I currently work here" not in [c[0] for c in s.calls], s.calls
    assert ("Month", "02") in s.calls and ("Year", "2022") in s.calls, s.calls


def test_missing_checkbox_reports_current_and_keeps_to():
    s = FakeSession([], lambda: exp_group(box=None))
    res = _run(s, "experience", [dict(CUR, end="")])
    rows = {f["key"]: f for f in _rows(res)}
    assert rows["current"]["status"] == "not_found", rows
    assert rows["end"].get("reason") == "no_value", rows       # reported, not skipped as current
    assert res["status"] == "partial", res["status"]


def test_single_box_dates_and_native_day_assumed():
    s = FakeSession([], lambda: exp_group(dates="single"))
    _run(s, "experience", [PAST])
    assert ("From", "06/2019") in s.calls and ("To", "02/2022") in s.calls, s.calls
    s = FakeSession([], lambda: exp_group(dates="native"))
    res = _run(s, "experience", [PAST])
    start = [f for f in _rows(res) if f["key"] == "start"][0]
    assert start.get("day_assumed") is True and start["value"] == "2019-06-01", start


def test_blur_revert_is_uncommitted():
    s = FakeSession([], exp_group, revert={"From*"})
    res = _run(s, "experience", [PAST])
    m = [f for f in _rows(res) if f["key"] == "start" and f.get("part") == "month"][0]
    assert m["status"] == "uncommitted" and m["verified"] is False, m
    assert res["status"] == "partial"


def test_picker_only_date_reports_unsupported():
    s = FakeSession([], lambda: exp_group(dates=None), pickers={"start", "end"})
    res = _run(s, "experience", [PAST])
    st = {f["key"]: f["status"] for f in _rows(res)}
    assert st["start"] == st["end"] == "unsupported_date_widget", st
    assert res["status"] == "partial"


def test_gpa_policy_in_flow():
    entry = {"school": "Example U", "degree": "BS", "end": "2018-05", "gpa": "3.66"}
    for policy, want in (("always", True), ("never", False), ("if_required", False)):
        s = FakeSession([], edu_group)
        res = _run(s, "education", [entry], policy)
        gpa = [f for f in _rows(res) if f["key"] == "gpa"][0]
        assert (gpa["status"] == "filled") is want, (policy, gpa)
        assert res["gpa_policy_used"] == policy
    s = FakeSession([], edu_group)
    res = _run(s, "education", [dict(entry, gpa="")], "always")
    assert [f for f in _rows(res) if f["key"] == "gpa"][0]["reason"] == "no_value"
    assert ("To (Actual or Expected)*", "2018") in s.calls, s.calls
    assert ("Degree*", "Bachelor of Science (BS)") in s.calls, s.calls
    s = FakeSession([], edu_group)
    res = _run(s, "education", [entry], "sometimes")
    assert res["gpa_policy_used"] == "if_required" and res.get("warnings"), res


def test_bare_overall_result_only_when_required_and_numeric():
    entry = {"school": "Example U", "degree": "BS", "end": "2018-05", "gpa": "3.66"}
    s = FakeSession([], lambda: edu_group(gpa_label="Overall Result", gpa_req=True))
    _run(s, "education", [entry])
    assert ("Overall Result", "3.66") in s.calls, s.calls
    s = FakeSession([], lambda: edu_group(gpa_label="Overall Result"))
    _run(s, "education", [entry])
    assert "Overall Result" not in [c[0] for c in s.calls], s.calls
    s = FakeSession([], lambda: edu_group(gpa_label="Overall Result", gpa_req=True))
    _run(s, "education", [dict(entry, gpa="First Class Honours")])
    assert "Overall Result" not in [c[0] for c in s.calls], s.calls


def test_unmatched_degree_picks_nothing():
    s = FakeSession([], lambda: edu_group(degree_opts=["Bachelor of Arts (BA)",
                                                       "Master of Arts (MA)"]))
    res = _run(s, "education", [{"school": "U", "degree": "B.S.", "end": "2018"}])
    deg = [f for f in _rows(res) if f["key"] == "degree"][0]
    assert deg["status"] == "unmatched" and deg["options"], deg
    assert "Degree*" not in [c[0] for c in s.calls], s.calls
    assert res["status"] == "partial"


def test_matched_group_fill_blanks_never_overwrites():
    g = exp_group("Product Mgr", "Acme, Inc.")
    s = FakeSession([g], exp_group)
    res = _run(s, "experience", [dict(PAST, title="Product Manager")])
    assert res["added"] == 0 and s.add_clicks == 0, res
    assert res["filled"][0]["result"] == "completed_blanks", res
    labels = [c[0] for c in s.calls]
    assert "Job Title*" not in labels and "Company*" not in labels, labels
    assert ("Location", "Y") in s.calls, s.calls
    res = _run(s, "experience", [dict(PAST, title="Product Manager")])
    assert res["status"] == "already_present" and not res["filled"], res
    assert {"entry": 0, "reason": "already_present"} in res["skipped"]


def test_orphan_reused_and_no_growth_over_three_runs():
    # a run died after ticking "current": no title/company, box checked
    orphan = exp_group()
    orphan[3]["current_value"] = "checked"
    orphan[:] = [f for f in orphan if not f.get("to")]
    s = FakeSession([orphan], exp_group)
    for run in range(3):
        res = _run(s, "experience", [CUR])
        assert s.add_clicks == 0 and len(s.groups) == 1, (run, res)
    assert s.groups[0][0]["current_value"] == "PM"


def test_unresolved_orphan_blocks_add():
    orphan = exp_group()
    orphan[2]["current_value"] = "Somewhere Else"     # location conflicts
    s = FakeSession([orphan], exp_group)
    res = _run(s, "experience", [CUR, PAST])
    assert s.add_clicks == 0 and res["orphan_groups"] == [0], res
    assert res["status"] == "partial", res["status"]
    assert {"entry": 0, "reason": "orphan_groups"} in res["skipped"], res


def test_new_group_inserted_at_top_is_the_one_filled():
    s = FakeSession([exp_group("Other", "Initech")], exp_group, insert_top=True)
    res = _run(s, "experience", [PAST])
    assert s.groups[0][1]["current_value"] == "Acme", s.groups[0]
    assert s.groups[1][1]["current_value"] == "Initech"
    assert res["status"] == "ok", res


def test_required_empty_and_identity_not_found_are_partial():
    s = FakeSession([], lambda: exp_group(req_loc=True))
    res = _run(s, "experience", [dict(PAST, location="")])
    assert res["status"] == "partial" and res["required_empty"], res
    s = FakeSession([], lambda: exp_group(no_company=True))
    res = _run(s, "experience", [PAST])
    comp = [f for f in _rows(res) if f["key"] == "company"][0]
    assert comp["status"] == "not_found" and res["status"] == "partial", res


def test_entry_cap_indexes_profile_list():
    s = FakeSession([], exp_group)
    entries = [{"company": "C%d" % i, "title": "T%d" % i} for i in range(11)]
    entries.insert(1, "junk")                 # non-dict still occupies slot 1
    res = _run(s, "experience", entries)
    assert res["added"] == rep.MAX_ENTRIES == 8, res["added"]
    capped = [k["entry"] for k in res["skipped"] if k["reason"] == "entry_cap"]
    assert capped == [9, 10, 11], capped
    assert [e["entry"] for e in res["filled"]] == [0, 2, 3, 4, 5, 6, 7, 8]


def test_add_failure_stops_and_reports():
    s = FakeSession([], exp_group, add_works=False)
    res = _run(s, "experience", [PAST, CUR])
    assert res["status"] == "add_failed", res
    assert s.add_clicks == 1, s.add_clicks
    assert {"entry": 1, "reason": "add_failed"} in res["skipped"], res


def test_bad_section_and_no_entries():
    s = FakeSession([], exp_group)
    assert _run(s, "skills", [{"a": 1}])["status"] == "bad_section"
    assert _run(s, "experience", [])["status"] == "no_entries"
    assert not s.calls


def test_error_strings_clipped():
    class Boom(FakeSession):
        async def fill_field(self, index, value):
            raise RuntimeError("x" * 500)
    s = Boom([], exp_group)
    res = _run(s, "experience", [PAST])
    errs = [f for f in _rows(res) if f.get("error")]
    assert errs and all(len(f["error"]) <= 120 for f in errs), errs


# --------------------------------------------------------------------------
# re-rendered groups and the end-of-section re-read (live Capital One,
# 2026-11-13: groups "vanished" after Job Title + Company on the first pass;
# School / Field of Study reported verified:true over empty boxes)
# --------------------------------------------------------------------------
class Remount(FakeSession):
    """Typing the Company re-renders the group: every element is new, so
    every stamped id changes (values carried over)."""

    def _set(self, g, f, value):
        super()._set(g, f, value)
        if f["label"] == "Company*":
            for x in g:
                x["eid"] = str(next(_EID))


def test_remounted_group_is_refound_not_vanished():
    s = Remount([], exp_group)
    res = _run(s, "experience", [PAST, dict(PAST, company="Initech",
                                            title="Analyst", location="Z")])
    rows = [f for e in res["filled"] for f in e["fields"]]
    assert not any(f.get("error") == "group_vanished" for f in rows), rows
    assert res["status"] == "ok", res
    assert all(e.get("regrouped") == 1 for e in res["filled"]), res["filled"]
    assert ("Location", "Y") in s.calls and ("Location", "Z") in s.calls, s.calls
    assert ("Role Description", "d") in s.calls, s.calls
    assert "_eid" not in repr(res)


def test_refind_group_by_content_then_position():
    a = _grp("experience", ("Job Title", "PM"), ("Company", "Acme"))
    b = _grp("experience", ("Job Title", "Analyst"), ("Company", "Initech"))
    blank = _grp("experience", ("Job Title", ""), ("Company", ""))
    entry = {"company": "Initech", "title": "Analyst"}
    assert rep.refind_group([a, b, blank], entry, "experience", 2) is b
    # nothing identifying written yet: the blank group still at `pos`
    fresh = {"company": "Globex", "title": "VP"}
    assert rep.refind_group([a, b, blank], fresh, "experience", 2) is blank
    # ...but never a group holding some other entry
    assert rep.refind_group([a, b, blank], fresh, "experience", 0) is None
    assert rep.refind_group([a, b], fresh, "experience", None) is None
    g, pos, refound = rep.locate_group([a, b], b[0]["eid"], entry, "experience")
    assert g is b and pos == 1 and refound is False
    g, pos, refound = rep.locate_group([a, b], "gone", entry, "experience", 0)
    assert g is b and pos == 1 and refound is True


class DropsTypedText(FakeSession):
    """A search/prompt widget whose fill_field (as the old code did) read
    the typed text back as verified:true, while the widget dropped it."""

    def _set(self, g, f, value):
        if f["label"] in ("School or University*", "Field of Study"):
            self.calls.append((f["label"], value))
            return
        super()._set(g, f, value)


def test_verified_true_over_an_empty_box_is_downgraded():
    s = DropsTypedText([], edu_group)
    res = _run(s, "education", [{"school": "Example U", "degree": "BS",
                                 "field_of_study": "Economics", "end": "2018-05"}])
    rows = {f["key"]: f for f in _rows(res)}
    for k in ("school", "field_of_study"):
        assert rows[k]["verified"] is False and rows[k]["status"] == "uncommitted", rows[k]
        assert rows[k]["observed_value"] == "", rows[k]
    assert rows["degree"]["verified"] is True, rows["degree"]
    assert any(r["key"] == "school" for r in res.get("required_empty", [])), res
    assert res["status"] == "partial", res["status"]


class LosesHandle(FakeSession):
    """fill_field could not read back (the element was replaced mid-verify)
    but the value did land."""

    async def fill_field(self, index, value):
        r = await super().fill_field(index, value)
        g, f = self._find(lambda x: x.get("index") == index)
        if f["label"] == "Location":
            r.update(verified=None, note="could not verify")
        return r


def test_unverifiable_fill_is_settled_by_the_reread():
    s = LosesHandle([], exp_group)
    res = _run(s, "experience", [PAST])
    loc = [f for f in _rows(res) if f["key"] == "location"][0]
    assert loc["verified"] is True and loc.get("verified_by") == "section_reread", loc


# --------------------------------------------------------------------------
# data layer
# --------------------------------------------------------------------------
PROFILE = {
    "email": "x@example.test", "zip": "90001", "city": "Springfield",
    "gpa_policy": "always",
    "experience": [{"company": "", "title": "", "current": False},
                   {"company": "A", "title": "B"}],
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


def test_profile_section_drops_blanks_but_keeps_positions():
    def go():
        assert data.profile_section("experience") == [{"company": "A", "title": "B"}]
        assert data.profile_section_items("experience") == \
            [(1, {"company": "A", "title": "B"})]
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
