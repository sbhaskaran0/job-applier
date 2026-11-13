"""Repeating form sections ("My Experience": Work Experience, Education).

Pure helpers for BrowserSession.fill_repeating_section: which page field means
which profile-entry key, how a "YYYY-MM" profile date is written into each
date-widget shape, how a profile degree is matched to a dropdown option, how
scanned fields split into one group per entry, and which existing groups
already hold (or partly hold) an entry so a re-run never duplicates one.

Everything here is DOM-free except the JS strings. `SECTION_JS` runs in the
page AFTER read_form has tagged fields with data-jaidx. It locates a section
by its HEADING TEXT, its own container and document order (ATS-agnostic: no
data-automation-id), and reports, per field inside that section, the group it
sits in, a stable element id, the nearby label naming a split date ("From"
above separate Month / Year boxes), and any committed "pill" text (typeahead
widgets that clear their <input> and render the choice as a chip).
"""

import re

MAX_ENTRIES = 8          # entries filled per call
MAX_ADD_CLICKS = 2       # Add clicks per entry before giving up
ADD_WAIT_S = 5.0         # per click; 2 clicks -> <= 10s per entry
DESC_ECHO = 60           # long values are truncated to this in results
ERROR_ECHO = 120         # error strings are truncated to this

_EXP_NAMES = (r"work experience|experience|professional experience|"
              r"employment history|work history|employment")
_EDU_NAMES = r"education|education history|academic history"

# key -> regex on the NORMALIZED label (lowercase, no "*", single spaces).
# Order matters: the first hit wins, so specific patterns come first.
_DATE_START = r"^from$|start date|^start$|date from|from date|^dates? from"
_DATE_END = r"^to$|end date|^end$|date to|to date|^dates? to"

SECTIONS = {
    "experience": {
        "names": _EXP_NAMES,
        "heading": r"^(?:%s)$" % _EXP_NAMES,
        "subhead": r"^(?:%s)\s*#?\s*\d+$" % _EXP_NAMES,
        "identity": ("company", "title"),
        "order": ("current", "title", "company", "location", "employment_type",
                  "start", "end", "description"),
        "labels": (
            ("current", r"currently work|currently (employed|working)|"
                        r"current (role|job|position|employer)|i work here|"
                        r"present (role|job)"),
            ("title", r"job title|position title|role title|^title$|^position$|"
                      r"^role$"),
            ("company", r"company|employer|organi[sz]ation"),
            ("employment_type", r"employment type|job type"),
            ("location", r"location|^city$"),
            ("description", r"description|responsibilit|duties|summary"),
            ("start", _DATE_START),
            ("end", _DATE_END),
        ),
    },
    "education": {
        "names": _EDU_NAMES,
        "heading": r"^(?:%s)$" % _EDU_NAMES,
        "subhead": r"^(?:%s)\s*#?\s*\d+$" % _EDU_NAMES,
        "identity": ("school",),
        "order": ("school", "degree", "field_of_study", "gpa", "start", "end"),
        "labels": (
            ("school", r"school|university|institution|college"),
            ("field_of_study", r"field of study|major|discipline|"
                               r"area of study|concentration"),
            ("degree", r"degree|qualification"),
            ("gpa", r"\bgpa\b|grade point"),
            # "Overall Result" alone is NOT assumed to be a GPA: it maps to a
            # pseudo-key that is only used when the field is required and the
            # value is numeric (see gpa_fields).
            ("overall_result", r"^overall result$|^result$"),
            ("start", _DATE_START),
            ("end", _DATE_END + r"|actual or expected|graduation|"
                                r"expected|completion"),
        ),
    },
}

GPA_POLICIES = ("always", "if_required", "never")
DATE_KEYS = ("start", "end")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def clip(value, n: int = DESC_ECHO) -> str:
    s = " ".join(str(value or "").split())
    return s if len(s) <= n else s[: n - 3] + "..."


def truthy(v) -> bool:
    """Explicit boolean parse: bool("false") is True, which is how a quoted
    YAML "false" would otherwise tick "I currently work here"."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v == 1
    return str(v or "").strip().lower() in ("true", "yes", "y", "1")


def entry_value(entry: dict, key: str) -> str:
    v = entry.get(key)
    if isinstance(v, bool):
        return "true" if v else "false"
    return "" if v is None else str(v).strip()


# --------------------------------------------------------------------------- #
# labels -> keys
# --------------------------------------------------------------------------- #
def norm_label(text: str) -> str:
    t = (text or "").lower().replace("*", " ")
    t = re.sub(r"\(\s*required\s*\)|\brequired\b", " ", t)
    t = re.sub(r"[^a-z0-9/ ]+", " ", t)
    return " ".join(t.split())


def label_key(label: str, section: str) -> str | None:
    """Map a field label to an entry key for `section`, or None."""
    n = norm_label(label)
    if not n:
        return None
    for key, pat in SECTIONS[section]["labels"]:
        if re.search(pat, n):
            return key
    return None


_MONTH_RE = re.compile(r"\bmonth\b|^mm$")
_YEAR_RE = re.compile(r"\byear\b|^yyyy$")
_PART_WORDS_RE = re.compile(r"\b(month|year|mm|yyyy)\b")


def _part_of(text) -> str | None:
    n = norm_label(text)
    m, y = bool(_MONTH_RE.search(n)), bool(_YEAR_RE.search(n))
    if m and not y:
        return "month"
    if y and not m:
        return "year"
    return None


def date_part(desc: dict) -> str | None:
    """'month' / 'year' when the field is one box of a split date widget
    (shape b), judged by aria-label, placeholder, then label ("Start Month",
    "Year", "MM"). A box naming both ("Month/Year") is a single-box date."""
    for src in (desc.get("aria"), desc.get("placeholder"), desc.get("label")):
        p = _part_of(src)
        if p:
            return p
    return None


def field_key(desc: dict, section: str) -> str | None:
    """Entry key for a scanned field. The label minus any month/year word
    decides ("Start Month" -> start). Only a split-date box whose own label
    says nothing else ("Year") borrows the context label ("From" / "To");
    an ordinary unmapped field never inherits a neighbour's label."""
    label = desc.get("label") or ""
    rest = " ".join(_PART_WORDS_RE.sub(" ", norm_label(label)).split())
    own = label_key(rest, section) if rest else None
    if own is None and date_part(desc):
        own = label_key(desc.get("ctx"), section)
        if own is None:
            rest_aria = _PART_WORDS_RE.sub(" ", norm_label(desc.get("aria")))
            own = label_key(rest_aria, section)
    if own is None and not label.strip():
        own = label_key(desc.get("aria"), section)
    return own


# --------------------------------------------------------------------------- #
# dates
# --------------------------------------------------------------------------- #
def split_date(value: str):
    """'2021-05' -> ('2021', '05'); '2021' -> ('2021', None); junk -> None."""
    m = re.match(r"^\s*(\d{4})(?:[-/](\d{1,2}))?\s*$", str(value or ""))
    if not m:
        return None
    mon = m.group(2)
    if mon is not None:
        if not 1 <= int(mon) <= 12:
            return None
        mon = "%02d" % int(mon)
    return m.group(1), mon


def single_date_shape(desc: dict) -> str:
    """How a ONE-box date wants its value: 'mm/yyyy' (default, shape a),
    'yyyy-mm', 'yyyy', or 'date' (native type=date)."""
    itype = (desc.get("itype") or "").lower()
    if itype == "month":
        return "yyyy-mm"
    if itype == "date":
        return "date"
    ph = (desc.get("placeholder") or "").lower()
    if "mm" in ph and "yyyy" in ph:
        return "mm/yyyy" if ph.index("mm") < ph.index("yyyy") else "yyyy-mm"
    if ph.strip() == "yyyy":
        return "yyyy"
    return "mm/yyyy"


def date_shape(desc: dict) -> str:
    """The shape for any date field: a split part, or the single-box shape."""
    part = desc.get("part")
    if part == "year":
        return "year"
    if part == "month":
        return ("month_name" if desc.get("kind") in ("select", "combobox")
                else "month")
    return single_date_shape(desc)


def format_date(value: str, shape: str) -> str | None:
    """Write a profile 'YYYY-MM' date for one widget shape. None when the
    profile date lacks a part the widget needs (never invent a month)."""
    parts = split_date(value)
    if not parts:
        return None
    year, mon = parts
    if shape in ("year", "yyyy"):
        return year
    if mon is None:
        return None
    if shape == "month":
        return mon
    if shape == "month_name":
        return _MONTHS[int(mon) - 1]
    if shape == "mm/yyyy":
        return "%s/%s" % (mon, year)
    if shape == "yyyy-mm":
        return "%s-%s" % (year, mon)
    if shape == "date":
        return "%s-%s-01" % (year, mon)
    return None


def _date_agrees(field: dict, value: str) -> bool:
    exp = format_date(value, date_shape(field))
    got = (field.get("current_value") or "").strip()
    if exp is None or not got:
        return False
    a, b = _digits(exp), _digits(got)
    if a and b:
        return a.lstrip("0") == b.lstrip("0")
    return _cmp(exp) == _cmp(got)


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


# --------------------------------------------------------------------------- #
# GPA
# --------------------------------------------------------------------------- #
def gpa_allowed(policy: str, required: bool) -> bool:
    policy = policy if policy in GPA_POLICIES else "if_required"
    if policy == "always":
        return True
    if policy == "never":
        return False
    return bool(required)


def is_required(desc: dict) -> bool:
    return bool(desc.get("required") or desc.get("req")
                or "*" in (desc.get("label") or ""))


def gpa_fields(group: list, value: str) -> list:
    """GPA targets in a group: fields labelled GPA / grade point, or — only
    when there are none — a bare "Overall Result" that is required while the
    value is numeric."""
    fs = [f for f in group if f.get("key") == "gpa"]
    if fs:
        return fs
    if re.match(r"^\d+(\.\d+)?$", str(value or "").strip()):
        return [f for f in group
                if f.get("key") == "overall_result" and is_required(f)]
    return []


# --------------------------------------------------------------------------- #
# degrees
# --------------------------------------------------------------------------- #
_DEG_ABBR = {
    "ba": "bachelor of arts", "ab": "bachelor of arts",
    "bs": "bachelor of science", "bsc": "bachelor of science",
    "beng": "bachelor of engineering", "bba": "bachelor of business administration",
    "bfa": "bachelor of fine arts",
    "ma": "master of arts", "ms": "master of science", "msc": "master of science",
    "meng": "master of engineering", "mba": "master of business administration",
    "mfa": "master of fine arts", "mpp": "master of public policy",
    "mph": "master of public health",
    "phd": "doctor of philosophy", "dphil": "doctor of philosophy",
    "jd": "juris doctor", "md": "doctor of medicine",
}
_DEG_FULL = sorted(set(_DEG_ABBR.values()), key=len, reverse=True)
_DEG_LEVELS = (
    ("bachelor", ("bachelor", "bachelors", "bachelors degree", "bachelor degree",
                  "undergraduate degree")),
    ("master", ("master", "masters", "masters degree", "master degree",
                "graduate degree")),
    ("doctorate", ("doctorate", "doctoral degree", "doctorate degree",
                   "doctoral", "phd")),
    ("associate", ("associate", "associates", "associates degree",
                   "associate degree")),
    ("high_school", ("high school", "high school diploma")),
)


def deg_norm(s) -> str:
    t = str(s or "").lower().replace(".", "").replace("'", "").replace("’", "")
    t = t.replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def degree_canon(s) -> str:
    """'B.S.' / 'BS' / 'Bachelor of Science (BS)' / 'Bachelor of Science in
    Economics' -> 'bachelor of science'. An unknown degree keeps its own
    normalized spelling, minus any parenthetical."""
    n = deg_norm(s)
    if n in _DEG_ABBR:
        return _DEG_ABBR[n]
    for full in _DEG_FULL:
        if n == full or n.startswith(full + " "):
            return full
    return deg_norm(re.sub(r"\(.*?\)", " ", str(s or "")))


def degree_level(s) -> str | None:
    c = degree_canon(s)
    for level, _ in _DEG_LEVELS:
        if c.startswith(level.replace("_", " ")):
            return level
    if c.startswith("doctor") or c.startswith("phd"):
        return "doctorate"
    return None


def _is_generic_degree(option: str) -> bool:
    n = deg_norm(option)
    return "degree" in n.split() or " of " not in (" " + n + " ")


def match_degree(value: str, options: list) -> str | None:
    """Pick the option that IS the profile's degree, or None (-> the caller
    reports `unmatched` with the options; it never picks a different specific
    degree). Tiers:
      1. exact normalized spelling;
      2. same canonical degree (BS <-> Bachelor of Science <-> Bachelor of
         Science (BS));
      3. an option's specific degree that the profile degree extends
         ("Master of Public Policy" for "Master of Public Policy in X") —
         the longest such option wins;
      4. generic level names ("Bachelor's Degree") — ONLY against options
         that are themselves generic, and only by exact name."""
    if not value or not options:
        return None
    pn, pc = deg_norm(value), degree_canon(value)
    for o in options:
        if deg_norm(o) == pn:
            return o
    for o in options:
        if degree_canon(o) == pc:
            return o
    ext = [o for o in options if not _is_generic_degree(o)
           and pc.startswith(degree_canon(o) + " ")]
    if ext:
        return max(ext, key=lambda o: len(degree_canon(o)))
    level = degree_level(value)
    names = {deg_norm(n) for lv, ns in _DEG_LEVELS if lv == level for n in ns}
    for o in options:
        if _is_generic_degree(o) and deg_norm(o) in names:
            return o
    return None


# --------------------------------------------------------------------------- #
# grouping
# --------------------------------------------------------------------------- #
def group_fields(fields: list, section: str) -> list:
    """Split a section's scanned fields (document order, each with `group`
    from SECTION_JS) into one list per entry group, keys attached.

    With a numbered sub-heading per group ("Work Experience 2") the ordinal
    decides. Without sub-headings a new group starts only when an IDENTITY
    key (company / title / school) repeats. Fields before the first
    sub-heading are page chrome and dropped."""
    rows = []
    for f in fields:
        f = dict(f)
        f["key"] = field_key(f, section)
        f["part"] = date_part(f) if f["key"] in DATE_KEYS else None
        rows.append(f)

    def ordinal(f):
        g = f.get("group")
        return g if isinstance(g, int) and g >= 0 else None

    if any(ordinal(f) is not None for f in rows):
        by: dict = {}
        for f in rows:
            g = ordinal(f)
            if g is not None:
                by.setdefault(g, []).append(f)
        return [by[g] for g in sorted(by)]
    ident = SECTIONS[section]["identity"]
    groups, cur, seen = [], [], set()
    for f in rows:
        if f["key"] in ident and f["key"] in seen:
            groups.append(cur)
            cur, seen = [], set()
        if f["key"]:
            seen.add(f["key"])
        cur.append(f)
    groups.append(cur)
    return [g for g in groups if any(f["key"] for f in g)]


def anchor(group: list):
    """A group's stable handle: the element id of its first field (stamped
    by SECTION_JS, survives re-scans and the To field disappearing)."""
    return group[0].get("eid") if group else None


def find_group(groups: list, anchor_id):
    for g in groups:
        if any(f.get("eid") == anchor_id for f in g):
            return g
    return None


def refind_group(groups: list, entry: dict, section: str, pos=None):
    """Re-identify `entry`'s group once its anchor element is gone, or None.

    A framework re-render can REPLACE a group's elements wholesale, which
    drops the stable ids SECTION_JS stamped on them (live Capital One
    Workday, first pass: every newly added group "vanished" right after
    Job Title + Company were typed, and a second call then completed it by
    content). So: by content first, the group that holds this entry
    (match_score, ties broken by distance from the last known position
    `pos`); then, when nothing identifying has been written yet, the group
    still at `pos` if it is blank or consistent with the entry. Never a group
    that holds some other entry."""
    scored = []
    for j, g in enumerate(groups):
        s = match_score(g, entry, section)
        if s is not None:
            scored.append((-s, abs(j - pos) if pos is not None else 0, j))
    if scored:
        return groups[min(scored)[2]]
    if pos is not None and 0 <= pos < len(groups):
        g = groups[pos]
        if not group_has_identity(g, section) and (
                group_is_blank(g) or consistent(g, entry, section)):
            return g
    return None


def locate_group(groups: list, anchor_id, entry: dict, section: str, pos=None):
    """(group, its position, re-found?) for an entry's group: by anchor
    element when it still exists, else by refind_group. (None, None, False)
    when neither finds it."""
    for j, g in enumerate(groups):
        if any(f.get("eid") == anchor_id for f in g):
            return g, j, False
    g = refind_group(groups, entry, section, pos)
    if g is None:
        return None, None, False
    return g, next(j for j, x in enumerate(groups) if x is g), True


def _cmp(text) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())


def has_value(f: dict) -> bool:
    return bool((f.get("current_value") or "").strip())


def group_values(group: list) -> dict:
    """key -> first non-empty current value in the group."""
    out: dict = {}
    for f in group:
        k = f.get("key")
        if k and has_value(f) and k not in out:
            out[k] = f["current_value"].strip()
    return out


def group_is_blank(group: list) -> bool:
    return not any(has_value(f) for f in group if f.get("key"))


def group_has_identity(group: list, section: str) -> bool:
    vals = group_values(group)
    return any(vals.get(k) for k in SECTIONS[section]["identity"])


def _contains(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 3 and re.search(r"\b%s\b" % re.escape(short), long_) is not None


_CO_SUFFIX = {"inc", "incorporated", "llc", "llp", "ltd", "limited", "corp",
              "corporation", "co", "company", "plc", "gmbh", "the", "lp", "sa",
              "ag", "pbc"}


def company_norm(s) -> str:
    t = _cmp(str(s or "").replace("&", " and "))
    return " ".join(w for w in t.split() if w not in _CO_SUFFIX)


def company_match(a, b) -> bool:
    return _contains(company_norm(a), company_norm(b))


_TITLE_ABBR = {"sr": "senior", "snr": "senior", "jr": "junior", "mgr": "manager",
               "mngr": "manager", "assoc": "associate", "dir": "director",
               "vp": "vice president", "eng": "engineer", "engr": "engineer",
               "pm": "product manager", "&": "and"}


def title_tokens(s) -> set:
    t = str(s or "").lower().replace("&", " and ")
    words = re.sub(r"[^a-z0-9]+", " ", t).split()
    out = []
    for w in words:
        out.extend(_TITLE_ABBR.get(w, w).split())
    return set(out)


def title_overlap(a, b) -> float:
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def school_match(a, b) -> bool:
    return _contains(_cmp(a), _cmp(b))


def _key_fields(group: list, key: str) -> list:
    return [f for f in group if f.get("key") == key and has_value(f)]


def _dates_agree(group: list, key: str, value: str):
    """None when the group holds no value for `key`, else whether every
    filled box of that date agrees with the profile value."""
    fs = _key_fields(group, key)
    if not fs:
        return None
    return all(_date_agrees(f, value) for f in fs)


def match_score(group: list, entry: dict, section: str):
    """How well an existing group holds `entry` (higher is better), or None.

    Experience: the company must agree (containment, suffixes like Inc /
    LLC ignored), plus a loose title (token overlap >= 0.5 after Sr ->
    Senior, Mgr -> Manager, & -> and) OR the same start date. A group with
    only a company (title and start still blank) matches on company. A group
    with a title but no company needs the title and a non-conflicting start.
    Education: the school must agree; degree / end raise the score."""
    vals = group_values(group)
    if section == "education":
        gs = vals.get("school")
        if not gs or not school_match(gs, entry.get("school")):
            return None
        score = 1.0
        if vals.get("degree") and (
                match_degree(entry.get("degree"), [vals["degree"]])
                or _cmp(vals["degree"]) == _cmp(entry.get("degree"))):
            score += 1
        if _dates_agree(group, "end", entry_value(entry, "end")):
            score += 1
        return score
    gc, gt = vals.get("company"), vals.get("title")
    if not gc and not gt:
        return None
    score = 0.0
    t_ok = False
    if gt:
        if _cmp(gt) == _cmp(entry.get("title")):
            score += 3
            t_ok = True
        else:
            ov = title_overlap(gt, entry.get("title"))
            if ov >= 0.5:
                score += 1 + ov
                t_ok = True
    s_agree = _dates_agree(group, "start", entry_value(entry, "start"))
    if s_agree:
        score += 1
    if gc:
        if not company_match(gc, entry.get("company")):
            return None
        score += 1
        if (gt or s_agree is not None) and not (t_ok or s_agree):
            return None
        return score
    if not t_ok or s_agree is False:
        return None
    return score


def consistent(group: list, entry: dict, section: str) -> bool:
    """Could this orphan group (values but no identity) be a half-filled
    copy of `entry`? Every keyed value it holds must agree with the entry."""
    current = truthy(entry.get("current"))
    for f in group:
        k = f.get("key")
        if not k or not has_value(f):
            continue
        v = f["current_value"].strip()
        if k == "current":
            if (v == "checked") != current:
                return False
        elif k in DATE_KEYS:
            if k == "end" and current:
                return False
            if not _date_agrees(f, entry_value(entry, k)):
                return False
        elif k == "degree":
            ev = entry.get("degree")
            if not (match_degree(ev, [v]) or _cmp(v) == _cmp(ev)):
                return False
        elif k in ("gpa", "overall_result"):
            if _cmp(v) != _cmp(entry.get("gpa")):
                return False
        elif not _contains(_cmp(v), _cmp(entry_value(entry, k))):
            return False
    return True


def plan_groups(groups: list, entries: list, section: str) -> dict:
    """Decide, BEFORE anything is clicked, where each entry goes.

    -> {matched: {entry_i: group_j}, orphans: {entry_i: group_j},
        unresolved: [group_j], blanks: [group_j], foreign: [group_j]}

    * matched: an existing group already holds the entry (best score wins,
      greedily, so a promotion at one company pairs exact titles first);
    * orphans: groups with values but no identity, each given to the FIRST
      remaining entry it is consistent with, in page order;
    * unresolved: orphans no entry is consistent with — the caller must not
      click Add while any exist;
    * blanks: empty groups, reused in order before Add is clicked;
    * foreign: groups holding some other role/school — left alone."""
    pairs = []
    for i, e in enumerate(entries):
        for j, g in enumerate(groups):
            s = match_score(g, e, section)
            if s is not None:
                pairs.append((-s, i, j))
    matched, used = {}, set()
    for _, i, j in sorted(pairs):
        if i not in matched and j not in used:
            matched[i] = j
            used.add(j)
    orphans, unresolved, blanks, foreign = {}, [], [], []
    for j, g in enumerate(groups):
        if j in used:
            continue
        if group_is_blank(g):
            blanks.append(j)
        elif group_has_identity(g, section):
            foreign.append(j)
        else:
            for i, e in enumerate(entries):
                if i not in matched and i not in orphans and consistent(g, e, section):
                    orphans[i] = j
                    break
            else:
                unresolved.append(j)
    return {"matched": matched, "orphans": orphans, "unresolved": unresolved,
            "blanks": blanks, "foreign": foreign}


# --------------------------------------------------------------------------- #
# In-page JS
# --------------------------------------------------------------------------- #
# Committed "pill" text for a typeahead / prompt field: Workday-style prompts
# clear the <input> on commit and render the choice as a chip (role=option /
# aria-selected node, or a node with a remove button) beside it. Searched in
# the field's own wrapper (the widest ancestor, up to 4 levels, holding no
# other scanned field), skipping the field's own aria-controls popup. Matched
# by ARIA role and structure only.
_PILL_FN = r"""
  // The field's own wrapper: the widest ancestor (up to 4 levels) holding no
  // OTHER tagged field and no heading.
  const pillWrap = (el) => {
    let wrap = null, node = el.parentElement;
    for (let d = 0; node && d < 4; d++, node = node.parentElement) {
      if (Array.from(node.querySelectorAll('[data-jaidx]')).some(x => x !== el)) break;
      if (/^(H[1-6])$/.test(node.tagName)) break;
      wrap = node;
    }
    return wrap;
  };
  const pillOf = (el) => {
    const pclean = (t) => (t || '').replace(/\s+/g, ' ').trim();
    const pvis = (n) => { const r = n.getBoundingClientRect();
      const s = window.getComputedStyle(n);
      return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 1 && r.height > 1; };
    const tag = el.tagName.toLowerCase();
    const it = (el.getAttribute('type') || '').toLowerCase();
    if (tag !== 'input' || ['checkbox', 'radio', 'file', 'password'].includes(it)) return '';
    const popId = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
    const pop = popId ? document.getElementById(popId) : null;
    const wrap = pillWrap(el);
    if (!wrap) return '';
    const textOf = (n) => {
      let t = pclean(n.innerText || n.textContent);
      n.querySelectorAll('button,[role=button]').forEach(b => {
        const bt = pclean(b.innerText || b.textContent); if (bt) t = t.replace(bt, ''); });
      return pclean(t);
    };
    const cands = Array.from(wrap.querySelectorAll('[role=option], [aria-selected="true"]'));
    wrap.querySelectorAll('button,[role=button]').forEach(b => {
      const lab = pclean(b.getAttribute('aria-label') || b.innerText || b.textContent);
      if (/^(remove|delete|clear)\b|^[x×✕]$/i.test(lab) && b.parentElement &&
          b.parentElement !== wrap) cands.push(b.parentElement);
    });
    const seen = [];
    for (const n of cands) {
      if (pop && pop.contains(n)) continue;
      if (n === el || n.contains(el) || n.tagName === 'LABEL') continue;
      if (!pvis(n)) continue;
      const t = textOf(n);
      if (!t || /^\d+ items? selected$/i.test(t) || seen.includes(t)) continue;
      seen.push(t);
    }
    return seen.join(', ');
  };
"""

# JS: the pill text for the field at data-jaidx=idx ('' when none).
PILL_JS = r"""
(idx) => {
  __PILL_FN__
  const el = document.querySelector('[data-jaidx="' + idx + '"]');
  return el ? pillOf(el) : '';
}
""".replace("__PILL_FN__", _PILL_FN)

# In-page locator. Arg: {heading, subhead, names} regex sources.
# Returns null when no heading matches, else
#   {add, ambiguous_add, add_count, add_label, heading,
#    fields: [{index|null, rcb|null, eid, group, ctx, aria, placeholder,
#              itype, req, pill, label?, checked?}],
#    pickers: [{group, ctx, label}]}
# Tags the chosen Add control data-jaadd="1" and each non-native
# [role=checkbox] data-jarcb=<n>; stamps data-jagid (stable ids) on fields.
SECTION_JS = r"""
(spec) => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const isVisible = (el) => {
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  __PILL_FN__
  const secRe = new RegExp(spec.heading, 'i');
  const subRe = new RegExp(spec.subhead, 'i');
  const nameRe = new RegExp('^(?:' + spec.names + ')$', 'i');
  const headText = (h) => clean(h.innerText || h.textContent)
    .replace(/\s*\*\s*$/, '').replace(/\s*\(\d+\)\s*$/, '');
  // b comes after a in document order
  const after = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
  document.querySelectorAll('[data-jaadd]').forEach(e => e.removeAttribute('data-jaadd'));
  document.querySelectorAll('[data-jarcb]').forEach(e => e.removeAttribute('data-jarcb'));
  const root = document.documentElement;
  const gid = (el) => {
    if (!el.getAttribute('data-jagid')) {
      const n = parseInt(root.getAttribute('data-jagidn') || '0', 10) + 1;
      root.setAttribute('data-jagidn', String(n));
      el.setAttribute('data-jagid', String(n));
    }
    return el.getAttribute('data-jagid');
  };
  const heads = Array.from(document.querySelectorAll(
    'h1,h2,h3,h4,h5,h6,[role=heading]')).filter(isVisible);
  const sec = heads.find(h => secRe.test(headText(h)));
  if (!sec) return null;
  const isSub = (h) => subRe.test(headText(h));
  // The section's own container, if it has one: the nearest section-like
  // ancestor holding no OTHER section heading.
  let box = sec.closest('section,[role=group],fieldset,[role=region]');
  if (box && heads.some(h => h !== sec && !isSub(h) && box.contains(h) && !sec.contains(h)))
    box = null;
  // End of the section: the next heading of ANY level that is not one of its
  // numbered group sub-headings (and, with a container, lies outside it).
  // Levels are not trusted: role=heading often carries no aria-level.
  let end = null;
  for (const h of heads) {
    if (h === sec || !after(sec, h) || sec.contains(h) || isSub(h)) continue;
    if (box && box.contains(h)) continue;
    end = h; break;
  }
  const inRange = (el) => (!box || box.contains(el)) && after(sec, el) &&
                          (!end || after(el, end));
  const subs = heads.filter(h => inRange(h) && isSub(h));
  const groupOf = (el) => subs.filter(s => after(s, el)).length - 1;

  const ctxOf = (el) => {
    let node = el.parentElement;
    for (let d = 0; node && d < 5; d++, node = node.parentElement) {
      if (node.contains(sec) || subs.some(s => node.contains(s))) break;
      if (node.tagName === 'FIELDSET') {
        const lg = node.querySelector(':scope > legend');
        if (lg && clean(lg.innerText)) return clean(lg.innerText);
      }
      if (node.getAttribute('role') === 'group') {
        const ids = node.getAttribute('aria-labelledby');
        if (ids) {
          const t = clean(ids.split(/\s+/).map(i => {
            const e = document.getElementById(i); return e ? e.innerText : '';
          }).join(' '));
          if (t && !subRe.test(t)) return t;
        }
        const al = clean(node.getAttribute('aria-label'));
        if (al && !subRe.test(al)) return al;
      }
      const labs = Array.from(node.querySelectorAll('label, legend')).filter(l =>
        !l.contains(el) && !(el.id && l.htmlFor === el.id) && after(l, el) &&
        clean(l.innerText));
      if (labs.length) return clean(labs[labs.length - 1].innerText);
    }
    return '';
  };
  const labelOf = (el) => {
    const al = clean(el.getAttribute('aria-label'));
    if (al) return al;
    const ids = el.getAttribute('aria-labelledby');
    if (ids) {
      const t = clean(ids.split(/\s+/).map(i => {
        const e = document.getElementById(i); return e ? e.innerText : ''; }).join(' '));
      if (t) return t;
    }
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) return clean(l.innerText);
    }
    const t = clean(el.innerText || el.textContent);
    if (t) return t;
    const w = el.closest('label') || (el.parentElement && el.parentElement.querySelector('label'));
    return w ? clean(w.innerText) : '';
  };

  const fields = [];
  let rcbN = 0, lastField = null;
  for (const el of Array.from(document.querySelectorAll('[data-jaidx], [role=checkbox]'))) {
    if (!inRange(el)) continue;
    const tagged = el.hasAttribute('data-jaidx');
    if (!tagged && !isVisible(el)) continue;
    const lab = el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null;
    const row = {
      index: tagged ? parseInt(el.getAttribute('data-jaidx'), 10) : null,
      rcb: null, eid: gid(el), group: groupOf(el), ctx: ctxOf(el),
      aria: clean(el.getAttribute('aria-label')),
      placeholder: clean(el.getAttribute('placeholder')),
      itype: (el.getAttribute('type') || '').toLowerCase(),
      req: !!(el.required || el.getAttribute('aria-required') === 'true' ||
              (lab && /\*/.test(lab.innerText || ''))),
      pill: tagged ? pillOf(el) : '',
    };
    if (!tagged) {
      el.setAttribute('data-jarcb', String(rcbN));
      row.rcb = rcbN++;
      row.label = labelOf(el);
      row.checked = el.getAttribute('aria-checked') === 'true';
    }
    fields.push(row);
    lastField = el;
  }

  // Add control. Its label must be bare ("Add", "Add Another") or name THIS
  // section ("Add Work Experience"); "Add Language" etc. are another thing.
  const ADD = /^add(?:\s+(?:another|new|more|an?))?\b\s*(.*)$/i;
  const addOk = (t) => {
    const m = ADD.exec(t);
    if (!m) return false;
    const rest = clean(m[1]).replace(/[+\s]+$/, '');
    return !rest || nameRe.test(rest);
  };
  const isAdd = (b) => {
    const labels = [clean(b.innerText || b.textContent), clean(b.getAttribute('aria-label'))]
      .filter(Boolean);
    return labels.some(l => ADD.test(l)) && labels.every(l => !ADD.test(l) || addOk(l));
  };
  const buttons = Array.from(document.querySelectorAll('button, [role=button]'))
    .filter(b => isVisible(b));
  let adds;
  if (box) {
    adds = buttons.filter(b => inRange(b) && isAdd(b));
  } else {
    // no own container: the FIRST Add after the last group, before the next
    // heading of any level
    const from = lastField || sec;
    adds = buttons.filter(b => after(from, b) && (!end || after(b, end)) &&
                               !(lastField && lastField.contains(b)) && isAdd(b)).slice(0, 1);
  }
  const add = adds.length === 1 ? adds[0] : null;
  if (add) add.setAttribute('data-jaadd', '1');

  const PICK = /calendar|date ?picker|choose (a )?date|select (a )?date|open date/i;
  const pickers = [];
  for (const b of buttons) {
    if (!inRange(b) || isAdd(b)) continue;
    const t = clean(b.innerText || b.textContent);
    const a = clean(b.getAttribute('aria-label'));
    if (PICK.test(a) || PICK.test(t) || PICK.test(b.getAttribute('title') || '') ||
        b.getAttribute('aria-haspopup') === 'dialog')
      pickers.push({group: groupOf(b), ctx: ctxOf(b), label: a || t});
  }
  return {add: !!add, ambiguous_add: adds.length > 1, add_count: adds.length,
          add_label: add ? clean(add.innerText || add.getAttribute('aria-label')) : '',
          heading: headText(sec), fields: fields, pickers: pickers};
}
""".replace("__PILL_FN__", _PILL_FN)
