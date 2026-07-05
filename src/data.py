"""Structured data layer: exact profile lookup and past-answer search.

These are deterministic tools. They report *what* they found and *how well*
it matched; Claude decides whether a match is good enough to use.
"""

import re
from datetime import date
from difflib import SequenceMatcher

from . import config

# Reuse scopes for saved answers. Only "evergreen" answers may be reused
# confidently anywhere; "company" answers are confident only for the same
# company; "conditional" answers (role/location-dependent) are always gated.
VALID_SCOPES = ("evergreen", "company", "conditional")

# Maps a canonical profile key -> phrases that commonly appear in the matching
# form question/label. Matching is substring-based on a normalized question.
# Order matters: more specific keys should come before generic ones.
ALIASES: dict[str, list[str]] = {
    "requires_sponsorship": [
        "require sponsorship", "need sponsorship", "visa sponsorship",
        "sponsorship now or in the future", "require visa", "sponsor",
        "work permit",
    ],
    "work_authorization": [
        "authorized to work", "legally authorized", "legally allowed to work",
        "work authorization", "eligible to work", "right to work",
    ],
    "willing_to_relocate": [
        "willing to relocate", "open to relocation", "able to relocate",
        "relocate to",
    ],
    "first_name": ["first name", "given name", "legal first name"],
    "last_name": ["last name", "family name", "surname", "legal last name"],
    "full_name": ["full name", "your name", "legal name", "name*", "name "],
    "email": ["email", "e-mail"],
    "phone": ["phone", "mobile", "telephone", "contact number"],
    "linkedin_url": ["linkedin"],
    "github_url": ["github"],
    "portfolio_url": ["portfolio"],
    "website_url": ["website", "personal site", "url"],
    "address": ["street address", "address line", "mailing address"],
    "city": ["city", "current city"],
    "state": ["state", "province", "region"],
    "country": ["country", "permanent country", "country of residence"],
    "location": ["location", "where are you based", "current location"],
    "current_company": ["current company", "current employer", "present company"],
    "current_title": ["current title", "current role", "job title", "current position"],
    "years_experience": ["years of experience", "years experience", "how many years"],
    "how_did_you_hear": ["how did you hear", "how did you find", "referral source"],
    "desired_salary": ["salary", "compensation expectation", "expected salary"],
    "notice_period": ["notice period", "how soon", "start date availability"],
    "gender": ["gender"],
    "pronouns": ["pronoun"],
    "race_ethnicity": ["race", "ethnicity"],
    "hispanic_latino": ["hispanic", "latino"],
    "veteran_status": ["veteran"],
    "disability_status": ["disability"],
}

# Phrases that disqualify an alias match even when its phrase appears. E.g.
# "...for the location(s) you selected..." inside a sponsorship or remote-work
# question must not resolve to the user's location.
ALIAS_EXCLUDE: dict[str, list[str]] = {
    "location": ["remote", "sponsor", "relocat", "work permit",
                 "anticipate working", "authorized"],
    "city": ["remote", "sponsor", "work permit"],
}


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def _tokens(text: str) -> set[str]:
    return set(_normalize(text).split())


def _unwrap_profile_value(raw):
    """A profile value may be a scalar or a dict like {value: ..., eeo: true}
    (the dict form marks voluntary EEO self-ID data). Returns (value, is_eeo)."""
    if isinstance(raw, dict):
        return (raw.get("value") or None), bool(raw.get("eeo"))
    return (raw or None), False


def get_profile_field(question_or_key: str) -> dict:
    """Resolve a form label (or a raw key) to an exact profile value.

    Returns {"matched_key", "value", "confidence", "eeo"}. `value` is None when
    no field maps to the question. `confidence` is "exact" when a value is
    found. `eeo` is True for voluntary self-identification values — fill them
    only into voluntary self-ID sections, and never store them in history.
    """
    profile = config.load_user_profile()
    norm = _normalize(question_or_key)

    # 1) direct key hit (e.g. Claude passes "email" straight through)
    if question_or_key in profile:
        val, eeo = _unwrap_profile_value(profile.get(question_or_key))
        return {"matched_key": question_or_key, "value": val,
                "confidence": "exact" if val else "empty", "eeo": eeo}

    # 2) alias phrase match against the normalized question
    for key, phrases in ALIASES.items():
        if key not in profile:
            continue
        if any(x in norm for x in ALIAS_EXCLUDE.get(key, ())):
            continue
        for phrase in phrases:
            if phrase in norm:
                val, eeo = _unwrap_profile_value(profile.get(key))
                return {"matched_key": key, "value": val,
                        "confidence": "exact" if val else "empty", "eeo": eeo}

    return {"matched_key": None, "value": None, "confidence": "none",
            "eeo": False}


def _similarity(a: str, b: str) -> float:
    na, nb = _normalize(a), _normalize(b)
    ratio = SequenceMatcher(None, na, nb).ratio()
    ta, tb = _tokens(a), _tokens(b)
    overlap = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    # blend sequence ratio with token-set overlap
    return round(0.5 * ratio + 0.5 * overlap, 3)


def search_history(question: str, top_k: int = 5) -> list[dict]:
    """Return the closest past {question, answer} pairs, ranked by similarity.
    Rows carry the entry's reuse metadata (scope/company/date); an entry with
    no recorded scope is treated as "conditional" so it never auto-fills."""
    history = config.load_history()
    scored = [
        {"question": e.get("question", ""), "answer": e.get("answer", ""),
         "scope": e.get("scope") or "conditional",
         "company": e.get("company", ""), "date": e.get("date", ""),
         "score": _similarity(question, e.get("question", ""))}
        for e in history
    ]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def _upsert_answer(history: list, question: str, answer: str, scope: str,
                   company: str, keep_existing_scope: bool) -> str:
    """Insert or update one entry, matching on the *normalized* question so
    label decoration ("Country*" vs "Country") can't fork duplicates. The raw
    label is kept (refreshed on update) for display and similarity scoring."""
    if scope not in VALID_SCOPES:
        scope = "conditional"
    key = _normalize(question)
    today = date.today().isoformat()
    for entry in history:
        if _normalize(entry.get("question", "")) == key:
            entry["question"] = question
            entry["answer"] = answer
            if not keep_existing_scope or not entry.get("scope"):
                entry["scope"] = scope
            if company or not keep_existing_scope:
                entry["company"] = company
            entry["date"] = today
            return "updated"
    history.append({"question": question, "answer": answer, "scope": scope,
                    "company": company, "date": today})
    return "added"


def save_answer(question: str, answer: str, scope: str = "evergreen",
                company: str = "") -> dict:
    """Persist an approved answer so it can be reused. Matches existing entries
    on the normalized question (updates in place), otherwise appends. An
    explicit save overwrites the entry's scope/company classification."""
    history = config.load_history()
    status = _upsert_answer(history, question, answer, scope, company,
                            keep_existing_scope=False)
    config.save_history(history)
    return {"status": status, "count": len(history)}


# Question phrases that mark an answer as situation-dependent: true for this
# role/location/moment but not safely reusable verbatim elsewhere.
_CONDITIONAL_MARKERS = (
    "relocat", "this position", "this role", "commut", "onsite", "on site",
    "hybrid", "office", "start date", "notice period", "salary",
    "compensation", "how did you hear", "how soon", "available",
)


def _classify_scope(question: str, answer: str, kind: str, company: str) -> str:
    """Conservative scope for an auto-captured answer. Company-flavored or
    essay-length answers are "company"; situation-dependent ones are
    "conditional"; only short stable facts default to "evergreen"."""
    q, a = _normalize(question), _normalize(answer)
    comp = _normalize(company)
    if comp and (comp in q or comp in a):
        return "company"
    if kind == "textarea" or len(answer) > 150:
        return "company"
    if any(m in q for m in _CONDITIONAL_MARKERS):
        return "conditional"
    return "evergreen"


def capture_submission(fields: list[dict], company: str = "",
                       job_title: str = "", url: str = "",
                       log_application: bool = True) -> dict:
    """Structural capture at submit time: persist every filled answer from a
    form snapshot into history and (when `log_application` — i.e. the submit
    was actually confirmed) log the application to applications.json.

    Skips file fields, empty values, EEO self-ID fields (never persisted
    anywhere), and — for history — fields the profile already answers (the
    profile is their canonical source). Radio/checkbox rows are captured only
    when checked and only when the row's label is the group question rather
    than the option text (e.g. Ashby button-groups). Auto-captured entries get
    a conservative scope; an entry that already has a scope (e.g. from an
    explicit save_answer) keeps it."""
    history = config.load_history()
    added = updated = 0
    submitted: list[dict] = []
    for f in fields:
        label = (f.get("label") or "").strip()
        kind = f.get("kind", "")
        value = (f.get("current_value") or "").strip()
        if not label or kind == "file":
            continue
        if kind in ("radio", "checkbox"):
            if value != "checked":
                continue
            option = (f.get("option_value") or "").strip() or "Yes"
            # native radios label the *option* ("Yes"); only capture when the
            # label reads as the question itself
            if _normalize(label) == _normalize(option) or len(label) < 12:
                continue
            value = option
        if not value:
            continue
        pf = get_profile_field(label)
        if pf.get("eeo"):
            continue  # sensitive self-ID data: never persisted anywhere
        submitted.append({"question": label, "answer": value})
        if pf.get("value"):
            continue  # profile is the canonical source for this field
        scope = _classify_scope(label, value, kind, company)
        status = _upsert_answer(history, label, value, scope, company
                                if scope == "company" else "",
                                keep_existing_scope=True)
        if status == "added":
            added += 1
        else:
            updated += 1
    config.save_history(history)

    result = {"answers_added": added, "answers_updated": updated,
              "history_count": len(history), "application_logged": False}
    if log_application:
        logged = log_application_record(company=company, job_title=job_title,
                                        url=url, status="submitted",
                                        fields=submitted)
        result["application_logged"] = True
        result["applications_count"] = logged.get("applications_count")
    return result


def _application_key(company: str, job_title: str, url: str) -> tuple:
    return (_normalize(company), _normalize(job_title), (url or "").strip())


def log_application_record(company: str = "", job_title: str = "", url: str = "",
                           status: str = "submitted",
                           fields: list | None = None) -> dict:
    """Append (or update) one application record in applications.json, deduped on
    (company, job_title, url). This is the application-only log path used when a
    submit completes OUTSIDE a single submit_application call — e.g. after an
    email-verification code gate, or to back-fill a confirmed-but-unlogged
    submission. `capture_submission` routes through here on a confirmed submit,
    so a later explicit log of the same job updates in place instead of
    duplicating."""
    applications = config.load_applications()
    key = _application_key(company, job_title, url)
    for a in applications:
        if _application_key(a.get("company", ""), a.get("job_title", ""),
                            a.get("url", "")) == key:
            a["status"] = status
            a["date"] = date.today().isoformat()
            if fields:
                a["fields"] = fields
            config.save_applications(applications)
            return {"status": "updated", "applications_count": len(applications)}
    applications.append({"company": company, "job_title": job_title, "url": url,
                         "date": date.today().isoformat(), "status": status,
                         "fields": fields or []})
    config.save_applications(applications)
    return {"status": "added", "applications_count": len(applications)}
