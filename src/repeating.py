"""Repeating form sections ("My Experience": Work Experience, Education).

Pure helpers for BrowserSession.fill_repeating_section: which page field means
which profile-entry key, how a "YYYY-MM" profile date is written into each
date-widget shape, how scanned fields split into one group per entry, and
which existing groups already hold an entry (so a re-run never duplicates).

Everything here is DOM-free except `SECTION_JS`, which runs in the page AFTER
read_form has tagged fields with data-jaidx. It locates a section by its
HEADING TEXT and document order (ATS-agnostic: no data-automation-id), and
reports, per tagged field inside that section, the group it sits in and the
nearby label that names a split date ("From" above separate Month / Year
boxes).
"""

import re

MAX_ENTRIES = 8          # entries filled per call
MAX_ADD_CLICKS = 2       # Add clicks per entry before giving up
ADD_WAIT_S = 5.0         # per click; 2 clicks -> <= 10s per entry
DESC_ECHO = 60           # long values are truncated to this in results

_EXP_NAMES = (r"work experience|experience|professional experience|"
              r"employment history|work history|employment")
_EDU_NAMES = r"education|education history|academic history"

# key -> regex on the NORMALIZED label (lowercase, no "*", single spaces).
# Order matters: the first hit wins, so specific patterns come first.
_DATE_START = r"^from$|start date|^start$|date from|from date|^dates? from"
_DATE_END = r"^to$|end date|^end$|date to|to date|^dates? to"

SECTIONS = {
    "experience": {
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
        "heading": r"^(?:%s)$" % _EDU_NAMES,
        "subhead": r"^(?:%s)\s*#?\s*\d+$" % _EDU_NAMES,
        "identity": ("school",),
        "order": ("school", "degree", "field_of_study", "gpa", "start", "end"),
        "labels": (
            ("school", r"school|university|institution|college"),
            ("field_of_study", r"field of study|major|discipline|"
                               r"area of study|concentration"),
            ("degree", r"degree|qualification"),
            ("gpa", r"overall result|gpa|grade point|^grade$"),
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


def date_part(desc: dict) -> str | None:
    """'month' / 'year' when the field is one box of a split date widget
    (shape b), judged by its own label, aria-label or placeholder."""
    for src in (desc.get("aria"), desc.get("placeholder"), desc.get("label")):
        n = norm_label(src)
        if n in ("month", "mm"):
            return "month"
        if n in ("year", "yyyy"):
            return "year"
    return None


def field_key(desc: dict, section: str) -> str | None:
    """Entry key for a scanned field. A Month/Year box carries no meaning of
    its own, so its key comes from the context label ("From" / "To")."""
    own = None if date_part({"label": desc.get("label")}) else \
        label_key(desc.get("label"), section)
    if own is None:
        own = label_key(desc.get("ctx"), section)
    if own is None and not date_part(desc):
        own = label_key(desc.get("aria"), section)
    return own


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


_DEGREE_LEVELS = (
    (r"^(b\.?\s?[sa]\.?|bsc|bachelor)", ("Bachelor's Degree", "Bachelors Degree",
                                         "Bachelor's", "Bachelors", "Bachelor")),
    (r"^(m\.?\s?[sa]\.?|msc|mba|m\.?eng|master)", ("Master's Degree",
                                                   "Masters Degree", "Master's",
                                                   "Masters", "Master")),
    (r"^(ph\.?\s?d|doctor)", ("Doctorate", "Doctoral Degree", "PhD", "Ph.D.",
                              "Doctor")),
    (r"^associate", ("Associate's Degree", "Associates Degree", "Associate")),
    (r"^high school", ("High School Diploma", "High School")),
)


def degree_candidates(degree: str) -> list:
    """The profile's degree first, then its generic level spellings, so a
    'Bachelor of Science' can land on a 'Bachelor's Degree' option. Only the
    LEVEL is generalized; nothing about the degree is invented."""
    out = [degree] if degree else []
    low = (degree or "").strip().lower()
    for pat, names in _DEGREE_LEVELS:
        if re.match(pat, low):
            out.extend(n for n in names if n not in out)
            break
    return out


def group_fields(fields: list, section: str) -> list:
    """Split a section's scanned fields (in document order, each with `group`
    from SECTION_JS) into one list per entry group, keys attached.

    When the page renders a numbered sub-heading per group ("Work Experience
    2"), that ordinal decides. Without sub-headings, a new group starts when
    a (key, date-part) slot repeats. Fields before the first sub-heading that
    map to no key are page chrome and dropped."""
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
            if g is None:
                continue
            by.setdefault(g, []).append(f)
        return [by[g] for g in sorted(by)]
    groups, cur, seen = [], [], set()
    for f in rows:
        slot = (f["key"], f["part"])
        if f["key"] and slot in seen:
            groups.append(cur)
            cur, seen = [], set()
        if f["key"]:
            seen.add(slot)
        cur.append(f)
    if any(f["key"] for f in cur):
        groups.append(cur)
    return [g for g in groups if any(f["key"] for f in g)]


def _cmp(text) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())


def group_values(group: list) -> dict:
    """key -> first non-empty current value in the group."""
    out: dict = {}
    for f in group:
        k = f.get("key")
        v = (f.get("current_value") or "").strip()
        if k and v and k not in out:
            out[k] = v
    return out


def group_is_blank(group: list) -> bool:
    return not any((f.get("current_value") or "").strip() for f in group
                   if f.get("key"))


def _contains(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 3 and re.search(r"\b%s\b" % re.escape(short), long_) is not None


def entry_matches(group: list, entry: dict, section: str) -> bool:
    """Does an existing group already hold this entry? Every identity key the
    group has a value for must agree (title exactly, company/school allowing
    'Acme' ~ 'Acme Inc'), and at least one must be present."""
    vals = group_values(group)
    hit = False
    for k in SECTIONS[section]["identity"]:
        g, e = _cmp(vals.get(k)), _cmp(entry.get(k))
        if not g:
            continue
        ok = (g == e) if k == "title" else _contains(g, e)
        if not ok:
            return False
        hit = True
    return hit


def plan_groups(groups: list, entries: list, section: str):
    """-> (matched {entry_i: group_j}, reusable [blank group_j ...]).
    Each existing group is claimed by at most one entry."""
    matched, used = {}, set()
    for i, e in enumerate(entries):
        for j, g in enumerate(groups):
            if j not in used and entry_matches(g, e, section):
                matched[i] = j
                used.add(j)
                break
    reusable = [j for j, g in enumerate(groups)
                if j not in used and group_is_blank(g)]
    return matched, reusable


def clip(value, n: int = DESC_ECHO) -> str:
    s = " ".join(str(value or "").split())
    return s if len(s) <= n else s[: n - 3] + "..."


def entry_value(entry: dict, key: str) -> str:
    v = entry.get(key)
    if isinstance(v, bool):
        return "true" if v else "false"
    return "" if v is None else str(v).strip()


# --------------------------------------------------------------------------- #
# In-page locator. Arg: {heading, subhead} regex sources (case-insensitive).
# Returns null when no heading matches, else
#   {add: bool, add_label, heading,
#    fields: [{index, group, ctx, aria, placeholder, itype, req}],
#    pickers: [{group, ctx, label}]}
# and tags the section's Add control with data-jaadd="1".
# --------------------------------------------------------------------------- #
SECTION_JS = r"""
(spec) => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const isVisible = (el) => {
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const secRe = new RegExp(spec.heading, 'i');
  const subRe = new RegExp(spec.subhead, 'i');
  const headText = (h) => clean(h.innerText || h.textContent)
    .replace(/\s*\*\s*$/, '').replace(/\s*\(\d+\)\s*$/, '');
  const level = (h) => {
    const m = /^H([1-6])$/.exec(h.tagName);
    if (m) return +m[1];
    const a = parseInt(h.getAttribute('aria-level') || '', 10);
    return isNaN(a) ? 2 : a;
  };
  // b comes after a in document order
  const after = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
  document.querySelectorAll('[data-jaadd]').forEach(e => e.removeAttribute('data-jaadd'));
  const heads = Array.from(document.querySelectorAll(
    'h1,h2,h3,h4,h5,h6,[role=heading]')).filter(isVisible);
  const sec = heads.find(h => secRe.test(headText(h)));
  if (!sec) return null;
  const lv = level(sec);
  let end = null;
  for (const h of heads) {
    if (h === sec || !after(sec, h) || sec.contains(h)) continue;
    if (subRe.test(headText(h))) continue;     // "Work Experience 2"
    if (level(h) <= lv) { end = h; break; }
  }
  const inRange = (el) => after(sec, el) && (!end || after(el, end));
  const subs = heads.filter(h => inRange(h) && subRe.test(headText(h)));
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

  const fields = [];
  for (const el of Array.from(document.querySelectorAll('[data-jaidx]'))) {
    if (!inRange(el)) continue;
    const lab = el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null;
    fields.push({
      index: parseInt(el.getAttribute('data-jaidx'), 10),
      group: groupOf(el), ctx: ctxOf(el),
      aria: clean(el.getAttribute('aria-label')),
      placeholder: clean(el.getAttribute('placeholder')),
      itype: (el.getAttribute('type') || '').toLowerCase(),
      req: !!(el.required || el.getAttribute('aria-required') === 'true' ||
              (lab && /\*/.test(lab.innerText || ''))),
    });
  }
  const PICK = /calendar|date ?picker|choose (a )?date|select (a )?date|open date/i;
  const pickers = [];
  let add = null;
  for (const b of Array.from(document.querySelectorAll('button, [role=button]'))) {
    if (!inRange(b) || !isVisible(b)) continue;
    const t = clean(b.innerText || b.textContent);
    const a = clean(b.getAttribute('aria-label'));
    if (/^add(\s+another)?\b/i.test(t) || /^add(\s+another)?\b/i.test(a)) add = b;
    else if (PICK.test(a) || PICK.test(t) || PICK.test(b.getAttribute('title') || '') ||
             b.getAttribute('aria-haspopup') === 'dialog')
      pickers.push({group: groupOf(b), ctx: ctxOf(b), label: a || t});
  }
  if (add) add.setAttribute('data-jaadd', '1');
  return {add: !!add, add_label: add ? clean(add.innerText || add.getAttribute('aria-label')) : '',
          heading: headText(sec), fields: fields, pickers: pickers};
}
"""
