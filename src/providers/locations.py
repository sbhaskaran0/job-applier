"""Location normalization: raw ATS location strings → canonical tokens (JOB-55).

Boards publish location as free text, and it is genuinely many-to-many:
"San Francisco, CA; New York, NY", "SF, NYC, remote", "US-CA-Menlo Park",
"Remote (US/Canada)". This module turns one raw string into a list of
canonical location tokens plus a work-mode classification, deterministically
(pure regex + a curated alias map — no LLM, no network, same philosophy as
src/providers/extract.py).

Layered strategy:
  1. regex canonicalization — split multi-location strings, strip country/state
     prefixes ("US-CA-", "GB-"), trailing state/country qualifiers (", CA",
     ", United States"), and office noise ("HQ", "Office", "(HQ)").
  2. curated alias map (location_aliases.yaml) — abbreviations, nicknames, and
     typos regex can't infer (SF, NYC, "San Fransisco", "Cananda").
  3. every raw token → canonical mapping this produces is observable: the store
     logs distinct pairs into the location_observations table so mismatches can
     be curated back into the alias file later.

Work mode: 'hybrid' when the string says hybrid; 'remote' when any token is
remote-ish (or the ATS flags it structurally); else 'onsite'. A posting can be
remote AND carry office cities — the cities stay in the token list so a city
filter still matches.
"""

import re
from datetime import datetime, timezone

from .. import config

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------
_US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN",
    "MO", "MS", "MT", "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA",
    "WI", "WV", "WY",
}
_US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
}
_CA_PROVINCE_ABBR = {"ON", "QC", "BC", "AB", "MB", "SK", "NS", "NB", "NL", "PE",
                     "YT", "NT", "NU"}
_CA_PROVINCE_NAMES = {
    "ontario", "quebec", "british columbia", "alberta", "manitoba",
    "saskatchewan", "nova scotia", "new brunswick", "newfoundland",
    "prince edward island", "yukon",
}
# lowercased name → canonical country
_COUNTRIES = {
    "united states": "United States", "usa": "United States",
    "us": "United States", "u.s.": "United States", "u.s": "United States",
    "america": "United States",
    "canada": "Canada", "mexico": "Mexico", "brazil": "Brazil",
    "united kingdom": "United Kingdom", "uk": "United Kingdom",
    "england": "United Kingdom", "scotland": "United Kingdom",
    "ireland": "Ireland", "france": "France", "germany": "Germany",
    "netherlands": "Netherlands", "spain": "Spain", "portugal": "Portugal",
    "italy": "Italy", "poland": "Poland", "switzerland": "Switzerland",
    "sweden": "Sweden", "norway": "Norway", "denmark": "Denmark",
    "finland": "Finland", "belgium": "Belgium", "austria": "Austria",
    "israel": "Israel", "india": "India", "singapore": "Singapore",
    "japan": "Japan", "south korea": "South Korea", "korea": "South Korea",
    "china": "China", "hong kong": "Hong Kong", "taiwan": "Taiwan",
    "australia": "Australia", "new zealand": "New Zealand",
    "philippines": "Philippines", "indonesia": "Indonesia",
    "argentina": "Argentina", "colombia": "Colombia", "chile": "Chile",
    "costa rica": "Costa Rica", "uae": "United Arab Emirates",
    "united arab emirates": "United Arab Emirates",
}

# remote scope, checked in order (first hit wins) against a remote-ish token
_REMOTE_SCOPES = [
    (re.compile(r"canada|\bcan\b|ontario", re.I), "Canada"),
    (re.compile(r"north america", re.I), "North America"),
    (re.compile(r"united states|\busa?\b|u\.s|america|national|\bus[- ]", re.I), "US"),
    (re.compile(r"united kingdom|\buk\b|\bgb\b", re.I), "UK"),
    (re.compile(r"europe|emea", re.I), "Europe"),
    (re.compile(r"apac|asia", re.I), "APAC"),
    (re.compile(r"global|worldwide|international|anywhere", re.I), "Global"),
]

_REMOTE_RE = re.compile(r"\bremote\b|\bwork from home\b|\bdistributed\b|\bwfh\b", re.I)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)

# strong separators between locations: ; | · • / & "or" and newlines
_STRONG_SEP = re.compile(r"\s*(?:[;|·•/&\n]|\s(?:or|OR|Or)\s)\s*")
# leading country/region board prefixes: "US-", "GB-", "MX- Mexico City" ...
_COUNTRY_PREFIX = re.compile(
    r"^(?:us|usa|u\.s\.?a?\.?|gb|uk|can|emea|apac|latam|mx|de|fr|jp|sg|au|br|"
    r"nl|ie|il|es|it|pl|se|dk|ch|kr|nz)[-–—]\s*", re.I)
_PAREN = re.compile(r"\(([^)]*)\)")
_OFFICE_NOISE = re.compile(
    r"\b(?:hq|headquarters|office|only|based)\b\.?", re.I)


def _aliases() -> dict:
    return config.load_location_aliases()


# ---------------------------------------------------------------------------
# posted-date normalization (Lever = epoch millis, Greenhouse/Ashby = ISO)
# ---------------------------------------------------------------------------
def parse_posted(value) -> str | None:
    """Any board 'posted' value → ISO-8601 UTC (seconds), or None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{10,13}", s):
        ts = int(s)
        if ts >= 100_000_000_000:  # epoch milliseconds
            ts /= 1000
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(
                timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# tokenization + canonicalization
# ---------------------------------------------------------------------------
def _remote_canonical(token: str) -> str:
    for pattern, scope in _REMOTE_SCOPES:
        if pattern.search(token):
            return f"Remote · {scope}"
    return "Remote"


def _region_of(segment: str) -> str | None:
    """Canonical region if the segment IS a state/province/country, else None."""
    s = segment.strip().rstrip(".")
    if s.upper() in _US_STATE_ABBR or s.upper() in _CA_PROVINCE_ABBR:
        return s.upper()
    low = s.lower()
    if low in _US_STATE_NAMES or low in _CA_PROVINCE_NAMES:
        return s.title()
    if low in _COUNTRIES:
        return _COUNTRIES[low]
    return None


# state names that are also major-city names: standalone, read them as the city
_CITYLIKE_STATES = {"new york", "washington"}


def _clean_segment(seg: str) -> tuple[str, bool]:
    """→ (cleaned segment, had a country prefix). A "US-"/"GB-" prefix marks
    the segment as a standalone location, never a qualifier of the previous
    one ("US-Chicago, US-New York" is two cities, not Chicago-in-New-York)."""
    stripped = _COUNTRY_PREFIX.sub("", seg.strip())
    prefixed = stripped != seg.strip()
    seg = stripped
    # a second layer: "US-CA-Menlo Park" → after "US-" strip, "CA-Menlo Park"
    m = re.match(r"^([A-Za-z]{2})[-–—](?=\S)", seg)
    if m and (m.group(1).upper() in _US_STATE_ABBR
              or m.group(1).upper() in _CA_PROVINCE_ABBR):
        seg = seg[m.end():]
    seg = _PAREN.sub(" ", seg)
    seg = _OFFICE_NOISE.sub(" ", seg)
    seg = _HYBRID_RE.sub(" ", seg)
    seg = re.sub(r"[-–—,()\s]+$|^[-–—,()\s]+", "", " ".join(seg.split()))
    return seg, prefixed


def _city_canonical(seg: str, region: str | None = None) -> str:
    """Alias-map a cleaned city segment; keep board casing unless it's shouty."""
    canon = _aliases().get(seg.lower())
    if canon is not None:
        return canon  # may be "" → caller drops
    if region and region.upper() == "DC":
        return "Washington, DC"
    if seg.isupper() or seg.islower():
        seg = seg.title()
    return seg


def normalize(raw: str, remote_hint: bool = False) -> dict:
    """One raw board location string → {
        locations: [{name, kind: city|region|remote}],   # deduped, in order
        work_mode: remote|hybrid|onsite,
        observations: [(raw_token, canonical, kind)],    # for curation logging
    }"""
    raw = (raw or "").strip()
    hybrid = bool(_HYBRID_RE.search(raw))
    out: list[dict] = []
    seen: set[str] = set()
    observations: list[tuple] = []
    any_remote = False

    def add(name: str, kind: str, source: str):
        nonlocal any_remote
        if kind == "remote":
            any_remote = True
        if not name or len(name) <= 1:
            return
        if kind == "city" and name in _COUNTRIES.values():
            kind = "region"  # alias map can resolve a token to a country
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append({"name": name, "kind": kind})
        observations.append((source.strip(), name, kind))

    for fragment in _STRONG_SEP.split(raw):
        fragment = fragment.strip(" ,")
        if not fragment:
            continue
        # comma walk: "San Francisco, CA, New York, NY" — a region segment
        # qualifies the pending city; a non-region segment flushes it.
        pending: tuple[str, str] | None = None  # (cleaned city, raw source)
        segments = fragment.split(",")
        for i, raw_segment in enumerate(segments):
            terminal = i == len(segments) - 1
            # remote check runs on the RAW segment: cleaning strips the
            # parenthetical in "United States (Remote)" and the "US-" prefix
            # whose scope "Remote - US"-style tokens need.
            if _REMOTE_RE.search(raw_segment):
                if pending:
                    add(_city_canonical(pending[0]), "city", pending[1])
                    pending = None
                add(_remote_canonical(raw_segment), "remote", raw_segment)
                continue
            segment, prefixed = _clean_segment(raw_segment)
            if len(segment) <= 1:
                continue
            if prefixed and pending:  # standalone location, not a qualifier
                add(_city_canonical(pending[0]), "city", pending[1])
                pending = None
            region = _region_of(segment)
            # a city-like state name ("New York") mid-list is a city in its own
            # right, not a qualifier: "San Francisco, New York, Seattle"
            qualifier = (region is not None and not prefixed and pending
                         and (segment.lower() not in _CITYLIKE_STATES or terminal))
            if qualifier:
                add(_city_canonical(pending[0], region), "city", pending[1])
                pending = None
                if region in _COUNTRIES.values():  # keep country filterable
                    add(region, "region", segment)
                continue
            if region is not None and segment.lower() not in _CITYLIKE_STATES:
                if region in _COUNTRIES.values() or len(region) > 2:
                    add(region, "region", segment)
                elif segment.lower() in _aliases():  # "LA", "DC" as nicknames
                    add(_city_canonical(segment), "city", segment)
                # else: a bare 2-letter code with no pending city is noise
                continue
            if pending:
                add(_city_canonical(pending[0]), "city", pending[1])
            pending = (segment, raw_segment)
        if pending:
            add(_city_canonical(pending[0]), "city", pending[1])

    work_mode = ("hybrid" if hybrid
                 else "remote" if (any_remote or remote_hint)
                 else "onsite")
    return {"locations": out, "work_mode": work_mode,
            "observations": observations}
