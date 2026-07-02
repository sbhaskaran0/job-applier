"""Structured data layer: exact profile lookup and past-answer search.

These are deterministic tools. They report *what* they found and *how well*
it matched; Claude decides whether a match is good enough to use.
"""

import re
from difflib import SequenceMatcher

from . import config

# Maps a canonical profile key -> phrases that commonly appear in the matching
# form question/label. Matching is substring-based on a normalized question.
# Order matters: more specific keys should come before generic ones.
ALIASES: dict[str, list[str]] = {
    "requires_sponsorship": [
        "require sponsorship", "need sponsorship", "visa sponsorship",
        "sponsorship now or in the future", "require visa",
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
    "veteran_status": ["veteran"],
    "disability_status": ["disability"],
}


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def _tokens(text: str) -> set[str]:
    return set(_normalize(text).split())


def get_profile_field(question_or_key: str) -> dict:
    """Resolve a form label (or a raw key) to an exact profile value.

    Returns {"matched_key", "value", "confidence"}. `value` is None when no
    field maps to the question. `confidence` is "exact" when a value is found.
    """
    profile = config.load_user_profile()
    norm = _normalize(question_or_key)

    # 1) direct key hit (e.g. Claude passes "email" straight through)
    if question_or_key in profile:
        val = profile.get(question_or_key)
        return {"matched_key": question_or_key, "value": val or None,
                "confidence": "exact" if val else "empty"}

    # 2) alias phrase match against the normalized question
    for key, phrases in ALIASES.items():
        if key not in profile:
            continue
        for phrase in phrases:
            if phrase in norm:
                val = profile.get(key)
                return {"matched_key": key, "value": val or None,
                        "confidence": "exact" if val else "empty"}

    return {"matched_key": None, "value": None, "confidence": "none"}


def _similarity(a: str, b: str) -> float:
    na, nb = _normalize(a), _normalize(b)
    ratio = SequenceMatcher(None, na, nb).ratio()
    ta, tb = _tokens(a), _tokens(b)
    overlap = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    # blend sequence ratio with token-set overlap
    return round(0.5 * ratio + 0.5 * overlap, 3)


def search_history(question: str, top_k: int = 5) -> list[dict]:
    """Return the closest past {question, answer} pairs, ranked by similarity."""
    history = config.load_history()
    scored = [
        {"question": e.get("question", ""), "answer": e.get("answer", ""),
         "score": _similarity(question, e.get("question", ""))}
        for e in history
    ]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def save_answer(question: str, answer: str) -> dict:
    """Persist an approved answer so it can be reused. Updates in place if the
    exact question already exists, otherwise appends."""
    history = config.load_history()
    for entry in history:
        if entry.get("question") == question:
            entry["answer"] = answer
            config.save_history(history)
            return {"status": "updated", "count": len(history)}
    history.append({"question": question, "answer": answer})
    config.save_history(history)
    return {"status": "added", "count": len(history)}
