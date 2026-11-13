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

# The line between a "short stable fact" and an essay-length answer. Used both
# to classify an auto-captured answer's scope (below) and to gate the
# alternates union (Phase 6): only short answers are unioned, so a re-crafted
# essay never bloats a row with two multi-paragraph variants. Backed by the
# actual data (2026-09-16, 192 records): 139 are <= 150 chars and the
# shortest genuine essay is 185 -- 150 cleanly separates the two populations.
SHORT_ANSWER_CHARS = 150

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
    # Before city/state/country: "Zip/Postal Code" must never fall through to
    # a broader key. "zip" also covers "zip code" and "zip/postal code"
    # (normalized to "zip postal code").
    "zip": ["zip", "postal code", "postcode", "post code"],
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
    # "state" is a substring of "statement" / "estate"; a question that
    # names the country is handled by ALIAS_MASK below.
    "state": ["statement", "estate"],
}

# Text removed from the normalized question BEFORE a key's phrases are
# matched (the question can still match on its other words). "Armed Forces
# of the United States of America" and the military-spouse question used to
# resolve to `state` ("California") at confidence exact, purely because
# "united states" contains "state"; "What state do you live in (United
# States only)?" must still resolve to `state`.
ALIAS_MASK: dict[str, list[str]] = {
    "state": ["united states"],
}

# Labels that ARE a key's metadata field, not the field itself: matched
# against the WHOLE normalized label (an optional "required" tail allowed),
# so only the bare metadata label is excluded. Phone pickers sit beside the
# number on Workday-style forms ("Phone Device Type*", "Country Phone
# Code*", "Phone Extension"); resolving them to the number at confidence
# "exact" typed "310-..." into a device-type / country picker. A NUMBER
# label that merely mentions them ("Phone number (with country code)",
# "Phone Number (include extension)") still resolves to `phone`. "Country
# Phone Code" falls through to `country`, the value its options are named
# by ("United States of America (+1)").
ALIAS_EXCLUDE_LABEL: dict[str, "re.Pattern"] = {
    "phone": re.compile(
        r"(country )?(phone |telephone |mobile )?(device )?type( of phone)?"
        r"|type of (phone|telephone|device)"
        r"|(country )?(phone |telephone |mobile )?(country |dialing |calling )?code"
        r"|(phone |telephone |mobile )?extension|ext"
        r"|(country |dialing |calling )(phone )?code"),
}
_LABEL_TAIL_RE = re.compile(r"(\s+required)+$")


def _normalize(text: str) -> str:
    # NOTE: this deliberately leaves runs of whitespace intact. It feeds
    # `_similarity`'s SequenceMatcher and `_upsert_answer`'s question identity,
    # so widening it would change which past answers auto-fill a form — a
    # correctness change, not a cleanup. Use `role_key` for join identity.
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def _tokens(text: str) -> set[str]:
    return set(_normalize(text).split())


def role_key(company: str, title: str) -> tuple:
    """The identity of a ROLE — the single key both sides of "have I already
    applied to this posting?" must agree on.

    A role's identity is (company, title). Not the URL: the submit URL and the
    retry/success URL differ (Greenhouse embed vs. canonical, `?gh_src`
    tracking params, a post-submit redirect), which is how one application
    once split into duplicate records on a retry. And not the postings PK
    `(ats, slug, job_id)`, which is BOARD identity — one role cross-posted to
    five cities is five rows with five job_ids but one opportunity.

    Whitespace runs are collapsed, which `_normalize` alone does not do. That
    omission was a live bug: `_normalize` turns punctuation into a space
    without joining the result, so "Senior Product Manager, Consumer" became
    "senior product manager  consumer" (two spaces) while the same role logged
    as "Senior Product Manager - Consumer" became "... manager   consumer"
    (three). The keys compared unequal, and a Plaid role the user had already
    applied to on 2026-07-09 kept resurfacing as never-applied — against a
    board that hard-limits applications per candidate. 63 of 93 stored
    applications had a double space in their key, so most of the log was one
    punctuation edit away from the same failure.
    """
    return (" ".join(_normalize(company).split()),
            " ".join(_normalize(title).split()))


# Profile keys holding STRUCTURED lists (the My Experience repeating sections).
# They are filled only by fill_repeating_section, never as a scalar answer.
PROFILE_SECTIONS = ("experience", "education")


def profile_section(section: str) -> list:
    """The structured entries for a repeating section (see
    profile_section_items), without their positions."""
    return [e for _, e in profile_section_items(section)]


def profile_section_items(section: str) -> list:
    """The structured entries for a repeating section ("experience" /
    "education") from the active profile as (position_in_profile_list, dict)
    pairs, or [] when the block is missing or malformed. Non-dict items, and
    placeholder entries with no non-empty text value (the template's blank
    example), are dropped; positions still index the profile's own list."""
    if section not in PROFILE_SECTIONS:
        return []
    raw = config.load_user_profile().get(section)
    if not isinstance(raw, list):
        return []
    return [(i, dict(e)) for i, e in enumerate(raw) if isinstance(e, dict) and any(
        isinstance(v, (str, int, float)) and not isinstance(v, bool)
        and str(v).strip() for v in e.values())]


def _is_structured(raw) -> bool:
    """A list, or a dict that is not the {value, eeo} scalar wrapper."""
    return isinstance(raw, (list, tuple)) or (
        isinstance(raw, dict) and "value" not in raw)


def _unwrap_profile_value(raw):
    """A profile value may be a scalar or a dict like {value: ..., eeo: true}
    (the dict form marks voluntary EEO self-ID data). Returns (value, is_eeo).
    A structured value (list, or a dict with no `value`) is never a scalar
    answer and unwraps to None."""
    if _is_structured(raw):
        return None, False
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

    # 0) structured blocks (experience/education lists, gpa policy) are not
    # form answers: they go through fill_repeating_section only.
    if question_or_key in profile and _is_structured(profile.get(question_or_key)):
        return {"matched_key": None, "value": None, "confidence": "none",
                "eeo": False, "note": "structured profile block; use "
                                      "fill_repeating_section"}

    # 1) direct key hit (e.g. Claude passes "email" straight through)
    if question_or_key in profile:
        val, eeo = _unwrap_profile_value(profile.get(question_or_key))
        return {"matched_key": question_or_key, "value": val,
                "confidence": "exact" if val else "empty", "eeo": eeo}

    # 2) alias phrase match against the normalized question
    for key, phrases in ALIASES.items():
        if key not in profile or _is_structured(profile.get(key)):
            continue
        if any(x in norm for x in ALIAS_EXCLUDE.get(key, ())):
            continue
        whole = ALIAS_EXCLUDE_LABEL.get(key)
        if whole is not None and whole.fullmatch(
                _LABEL_TAIL_RE.sub("", " ".join(norm.split()))):
            continue
        text = norm
        for m in ALIAS_MASK.get(key, ()):
            text = text.replace(m, " ")
        for phrase in phrases:
            if phrase in text:
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


def _history_row(entry: dict, question: str) -> dict:
    """Build one search_history result row from a raw history entry.

    Rebuilding a fresh dict per row (rather than reusing `entry`) means any
    key added to the schema later is silently dropped here unless it is
    listed explicitly -- exactly what happened to `alternates` (Phase 6)
    before this carried it through. Keep this in mind before adding a new
    persisted field without a matching line here.
    """
    row = {"question": entry.get("question", ""), "answer": entry.get("answer", ""),
           "scope": entry.get("scope") or "conditional",
           "company": entry.get("company", ""), "date": entry.get("date", ""),
           "score": _similarity(question, entry.get("question", ""))}
    alternates = entry.get("alternates")
    if alternates:
        row["alternates"] = alternates
    return row


def search_history(question: str, top_k: int = 5) -> list[dict]:
    """Return the closest past {question, answer} pairs, ranked by similarity.
    Rows carry the entry's reuse metadata (scope/company/date); an entry with
    no recorded scope is treated as "conditional" so it never auto-fills."""
    history = config.load_history()
    scored = [_history_row(e, question) for e in history]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


# Markers left behind when a tool call's XML-ish envelope leaks into a saved
# answer. Seen for real on 2026-09-16 (indices 187-189 of a 192-row history):
# a genuine, complete answer immediately followed by the literal text
#   </answer>\n<parameter name="scope">evergreen
# i.e. the *next* tool-call parameter got appended to the answer text instead
# of being parsed as markup. Truncating at the first such marker was verified
# lossless against all three real records. Both shapes are matched by their
# full tag, never by a bare "<" or the word "parameter" alone, so a legitimate
# answer that happens to mention either (e.g. "the <parameter> tuning knob")
# is left untouched.
_LEAKED_MARKUP_RE = re.compile(r"</answer>|<parameter\s+name=")


# Labels that denote a secret. Belt-and-braces beside the scanner's
# `sensitive` flag: a password must reach neither history nor the applications
# log, whatever the field metadata says.
_SECRET_LABEL_RE = re.compile(r"pass(word|code)", re.I)


def _is_secret_label(label: str) -> bool:
    return bool(_SECRET_LABEL_RE.search(label or ""))


def _sanitize_answer(answer: str) -> str:
    """Strip leaked tool-call markup from an answer before it is persisted.

    Applied once, at the _upsert_answer write boundary, so both save_answer
    and capture_submission are covered by a single call site -- sanitizing
    only the historical records (scripts/repair_history_markup.py) without
    also sanitizing new writes would let the corruption recur on the very
    next save.
    """
    if not answer:
        return answer
    m = _LEAKED_MARKUP_RE.search(answer)
    if m:
        answer = answer[:m.start()]
    return answer.rstrip()


def _upsert_answer(history: list, question: str, answer: str, scope: str,
                   company: str, keep_existing_scope: bool) -> str:
    """Insert or update one entry, matching on the *normalized* question so
    label decoration ("Country*" vs "Country") can't fork duplicates. The raw
    label is kept (refreshed on update) for display and similarity scoring.

    On update, when both the outgoing and incoming answers are short
    (<= SHORT_ANSWER_CHARS) and differ after normalization, the outgoing
    answer is folded into an "alternates" list instead of being discarded
    outright -- a later re-save often reflects a rephrasing, not a
    correction, and the earlier wording can still be the better fit for a
    similar-but-not-identical question. Long (essay-length) answers keep the
    old overwrite-with-no-history behavior: unioning two multi-paragraph
    essays would bloat every row for no real benefit. `alternates` is
    deduped on _normalize, newest-displaced first, capped at 5.
    """
    if scope not in VALID_SCOPES:
        scope = "conditional"
    answer = _sanitize_answer(answer)
    key = _normalize(question)
    today = date.today().isoformat()
    for entry in history:
        if _normalize(entry.get("question", "")) == key:
            old_answer = entry.get("answer", "")
            if (old_answer and len(old_answer) <= SHORT_ANSWER_CHARS
                    and len(answer) <= SHORT_ANSWER_CHARS
                    and _normalize(old_answer) != _normalize(answer)):
                seen = {_normalize(answer)}
                alternates = []
                for cand in [old_answer] + list(entry.get("alternates") or []):
                    ck = _normalize(cand)
                    if ck in seen:
                        continue
                    seen.add(ck)
                    alternates.append(cand)
                    if len(alternates) == 5:
                        break
                entry["alternates"] = alternates
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
    explicit save overwrites the entry's scope/company classification.
    Refuses password-labelled questions (secrets are never stored here)."""
    if _is_secret_label(question):
        return {"status": "error",
                "error": "refusing to save a password/secret answer"}
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
    if kind == "textarea" or len(answer) > SHORT_ANSWER_CHARS:
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
        if f.get("sensitive") or kind == "password" or _is_secret_label(label):
            continue  # secrets: never into history or the applications log
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


def _normalize_url(url: str) -> str:
    """Strip the volatile parts of an application URL so the same posting keys
    identically across loads: drop the query string and fragment (tracking
    params like ?gh_src, react-router hashes) and any trailing slash."""
    u = (url or "").strip().lower()
    u = u.split("#", 1)[0].split("?", 1)[0]
    return u.rstrip("/")


def _application_key(company: str, job_title: str, url: str) -> tuple:
    """Dedupe identity for applications.json. An application's identity is
    (company, role): the URL is only a fallback locator when the title is
    missing. The submit URL and the retry/success URL differ (embed vs.
    canonical, tracking params, a post-submit redirect), so including the raw
    URL in the key split one application into duplicate records on a retry
    (JOB-24) — hence the URL is used only when (company, title) is incomplete.

    Built on `role_key` so the log's own dedupe identity and the store's
    already-applied match are the SAME function. When they were two functions
    they drifted, and a posting whose title differed from the logged one by a
    single comma read as never-applied."""
    c, t = role_key(company, job_title)
    if c and t:
        return (c, t, "")
    return (c, t, _normalize_url(url))


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
            # Mutate the matched record in place; never rebuild it as a fresh
            # dict. The submit vocabulary (`status`) and the reply vocabulary
            # (`outcome`/`outcome_date`, JOB-107) are orthogonal and are written
            # by different actors: an apply run re-logging an already-tracked
            # job knows nothing about the outcome a human recorded later. A
            # dict-replacement here would silently erase that reply — and with
            # it the response-rate numerator — every time a retry re-logged.
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


# --------------------------------------------------------------------------- #
# application outcomes (JOB-107)
# --------------------------------------------------------------------------- #
# `status` records whether WE finished the form; `outcome` records whether the
# company ever came back. The two are orthogonal on purpose — a flawless submit
# that is never answered is still a zero. Both keys are purely additive: a
# record written before this existed has no `outcome` key and every reader below
# defaults it to "none", so there is no migration and no schema version.
APPLICATION_OUTCOMES = ("none", "rejected", "screen", "interview", "offer",
                        "ghosted")

# "Did they respond at all?" — a rejection IS a response (they read it and
# decided); "ghosted" is an explicit human note that silence has gone on long
# enough to call, and stays a non-response.
_RESPONDED_OUTCOMES = frozenset(APPLICATION_OUTCOMES) - {"none", "ghosted"}

# The response-rate denominator: statuses meaning a form we actually completed.
# "attempted"/"parked" applications were never delivered, so counting them would
# depress the rate with jobs no employer ever saw.
SUBMITTED_STATUSES = ("submitted", "manual_submission")


def set_application_outcome(company: str = "", job_title: str = "", url: str = "",
                            outcome: str = "none", outcome_date: str = "") -> dict:
    """Record the company's response on one application record (JOB-107).

    The target is resolved with `_application_key` — the SAME dedupe identity
    the apply path logs under — so an outcome set from the UI lands on the very
    record a later re-log will update, and the two can never drift apart.

    `outcome_date` defaults to today for any real outcome; setting the outcome
    back to "none" blanks it, because a date with no outcome is a lie about when
    something happened. Returns {"status": "updated"|"not_found"|"invalid"} plus
    the stored record on success so a caller can echo it without re-reading."""
    outcome = (outcome or "none").strip().lower()
    if outcome not in APPLICATION_OUTCOMES:
        return {"status": "invalid",
                "reason": f"outcome must be one of {list(APPLICATION_OUTCOMES)}",
                "outcome": outcome}
    applications = config.load_applications()
    key = _application_key(company, job_title, url)
    for a in applications:
        if not isinstance(a, dict):
            continue
        if _application_key(a.get("company", ""), a.get("job_title", ""),
                            a.get("url", "")) == key:
            a["outcome"] = outcome
            a["outcome_date"] = ("" if outcome == "none"
                                 else (outcome_date or "").strip()
                                 or date.today().isoformat())
            config.save_applications(applications)
            return {"status": "updated", "application": dict(a),
                    "applications_count": len(applications)}
    return {"status": "not_found", "company": company, "job_title": job_title,
            "applications_count": len(applications)}


def application_outcome_stats(applications: list | None = None) -> dict:
    """Response rate as a number, not a vibe (JOB-107).

    Pure and side-effect free: pass a list of records already parsed elsewhere
    (an external metric collector reading data/applications.json directly does
    exactly that — no profile resolution, no filesystem access), or pass nothing
    and the active profile's log is loaded. The records handed in are never
    mutated; defaults are applied to local copies of the values only.

    Denominator choice, stated once here so every surface agrees: `submitted`
    counts records whose `status` is in SUBMITTED_STATUSES — a form we actually
    completed. `attempted`/`parked` records never reached an employer, so they
    cannot fairly count against us. `responded` counts every record whose
    outcome is a real reply (see _RESPONDED_OUTCOMES); it is deliberately NOT
    filtered to submitted records, because a reply is evidence the employer saw
    the application whatever we recorded about the submit.

    `response_rate` is `responded / submitted` rounded to 4 places, and is 0.0
    — never None, never a ZeroDivisionError — when nothing has been submitted.
    0.0 is an honest answer and callers must render it as one. Junk is tolerated
    rather than raised on: non-dict entries are skipped, a missing status is not
    a submit, and an unrecognized outcome counts under "none" rather than
    inventing a bucket that would quietly widen the vocabulary."""
    if applications is None:
        applications = config.load_applications()

    def _empty_outcomes() -> dict:
        return {o: 0 for o in APPLICATION_OUTCOMES}

    by_outcome = _empty_outcomes()
    by_company: dict[str, dict] = {}
    total = submitted = responded = 0
    for a in applications or []:
        if not isinstance(a, dict):
            continue
        total += 1
        outcome = str(a.get("outcome") or "none").strip().lower()
        if outcome not in by_outcome:
            outcome = "none"
        by_outcome[outcome] += 1
        company = str(a.get("company") or "").strip() or "(unknown)"
        row = by_company.setdefault(company, {"submitted": 0, "responded": 0,
                                              "by_outcome": _empty_outcomes()})
        row["by_outcome"][outcome] += 1
        if str(a.get("status") or "") in SUBMITTED_STATUSES:
            submitted += 1
            row["submitted"] += 1
        if outcome in _RESPONDED_OUTCOMES:
            responded += 1
            row["responded"] += 1

    return {
        "total": total,
        "submitted": submitted,
        "responded": responded,
        "response_rate": round(responded / submitted, 4) if submitted else 0.0,
        "by_outcome": by_outcome,
        "by_company": by_company,
    }
