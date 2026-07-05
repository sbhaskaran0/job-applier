"""Deterministic JD extraction, run once per posting at ingest (JOB-29).

Pure regex — no LLM, no network. Populates the store fields that make the
strict baseline filter a local query instead of a per-search JD deep-read:
  - salary_min/salary_max from pay-transparency text (Greenhouse/Lever embed
    salary in the description; Ashby provides it structured via the API).
  - min_years: the role's years-of-experience ask. ADVISORY ONLY — sentence
    regexes can't tell "7+ years of product experience" (role-level) from
    "2+ years of SQL" (skill-level), so we take the max mention and finalists
    still get a JD confirm before an application is prepped.
  - seniority_flag: an excluded-seniority term found in the TITLE (word-bounded,
    so "Head" doesn't match "Headquarters" and "Intern" doesn't match
    "Internal"). Titles only: JD bodies mention "reports to the Director of X"
    too often to be a reliable exclusion signal.
"""

import re

# Annual base-salary sanity band: outside this a matched figure is a bonus cap,
# hourly rate, 401k limit, revenue number, etc.
_SALARY_LO, _SALARY_HI = 40_000, 1_000_000

# One money token: "$170,000" / "$170,000.00" / "$170K" / "$142.6k" / "170,000"
_MONEY = r"\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{2,3}(?:\.\d+)?\s*[kK])"
_SEP = r"\s*(?:-|–|—|to|through)\s*"
_RANGE_RE = re.compile(_MONEY + _SEP + _MONEY)
# Single disclosed figure, only trusted inside an explicit compensation sentence.
_SINGLE_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{2,3}(?:\.\d+)?\s*[kK])")
_COMP_WORDS = re.compile(r"salary|compensation|pay range|base pay|pay rate|annual pay",
                         re.IGNORECASE)

_YEARS_RE = re.compile(
    r"(?:at least\s+)?(\d{1,2})\s*(?:\+|plus|or more)?\s*"
    r"(?:-|–|—|to)?\s*(?:\d{1,2}\s*\+?\s*)?years?", re.IGNORECASE)
_EXPERIENCE = re.compile(r"experience|background|track record", re.IGNORECASE)


def _money_to_int(tok: str) -> int | None:
    t = tok.replace("$", "").replace(",", "").strip()
    mult = 1
    if t and t[-1] in "kK":
        t, mult = t[:-1].strip(), 1000
    try:
        return int(float(t) * mult)
    except ValueError:
        return None


def _plausible(lo: int | None, hi: int | None) -> bool:
    return (lo is not None and hi is not None
            and _SALARY_LO <= lo <= hi <= _SALARY_HI)


def extract_salary(text: str) -> tuple[int | None, int | None]:
    """Best (min, max) annual base range found in JD text, or (None, None).

    When a JD lists several ranges (geo zones, levels), returns the one with
    the highest top end — the headline role range. A lone "$X" figure counts
    only when its sentence talks about salary/compensation (min == max)."""
    if not text:
        return None, None
    best: tuple[int, int] | None = None
    for m in _RANGE_RE.finditer(text):
        lo, hi = _money_to_int(m.group(1)), _money_to_int(m.group(2))
        if _plausible(lo, hi) and (best is None or hi > best[1]):
            best = (lo, hi)
    if best:
        return best
    for sentence in re.split(r"[.;\n]", text):
        if not _COMP_WORDS.search(sentence):
            continue
        for m in _SINGLE_RE.finditer(sentence):
            v = _money_to_int(m.group(1))
            if _plausible(v, v) and (best is None or v > best[1]):
                best = (v, v)
    return best if best else (None, None)


def extract_min_years(text: str) -> int | None:
    """Max years-of-experience ask mentioned near experience language.

    Max (not min) because the headline role requirement ("12+ years") usually
    exceeds the skill-level asks ("2+ years of SQL") that are the false
    positives. Advisory — see module docstring."""
    if not text:
        return None
    best = None
    for sentence in re.split(r"[.;\n]", text):
        if not _EXPERIENCE.search(sentence):
            continue
        for m in _YEARS_RE.finditer(sentence):
            n = int(m.group(1))
            if 1 <= n <= 30 and (best is None or n > best):
                best = n
    return best


def seniority_flag(title: str, excluded: list[str] | None) -> str | None:
    """First excluded-seniority term that appears (word-bounded) in the title.
    "VP" also matches "Vice President"."""
    t = title or ""
    for term in excluded or []:
        pattern = r"\bvice\s+president\b|\bVP\b" if term.upper() == "VP" \
            else r"\b" + re.escape(term) + r"\b"
        if re.search(pattern, t, re.IGNORECASE):
            return term
    return None
