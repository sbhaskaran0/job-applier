"""Per-job resume & cover-letter tailoring (JOB-6).

On-demand generation of a bespoke resume + cover letter for a single posting,
stored under `resumes/<job-slug>/`. Consistent with the project's core
principle, this module holds only the **mechanical** operations — read the base
`.docx`, apply the edits Claude decides on, export to PDF, gather the user's own
cover-letter exemplars, and persist/resolve artifacts. The *reasoning* (which
bullets to re-emphasize, writing the cover letter in the user's voice) is done
by Claude Code via the `/tailor-application` skill, which then calls these tools
to persist the result. No LLM API key lives here.

Storage layout, per job:
    resumes/<job-slug>/
        resume.docx        tailored copy of the base template (formatting kept)
        resume.pdf         exported for upload where the ATS wants a PDF
        cover_letter.txt   the cover letter text (for a free-text field)
        cover_letter.pdf   the cover letter as a file (for a file input)

Job identity reuses `data._application_key` (the (company, role[, url]) tuple
applications.json dedupes on) so tailoring, storage, and the JOB-5 tracker all
share ONE identifier.
"""

import hashlib
import re

from . import config, data


# --------------------------------------------------------------------------- #
# Job identity → filesystem-safe slug (shares data._application_key)
# --------------------------------------------------------------------------- #
def job_slug(company: str = "", job_title: str = "", url: str = "") -> str:
    """A filesystem-safe folder name for this job, derived from the SAME identity
    applications.json dedupes on (`data._application_key`). Normalized
    `company-job_title`; if the title/company are missing, a short hash of the
    URL keeps it unique."""
    company_n, title_n, url_part = data._application_key(company, job_title, url)
    base = "-".join(p for p in (company_n, title_n) if p).strip()
    slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    if not slug:
        # No usable company/title — fall back to a stable hash of the url tuple.
        h = hashlib.sha1((url_part or url or "").encode("utf-8")).hexdigest()[:12]
        slug = f"job-{h}" if h else "job-unknown"
    return slug[:120]


def job_dir(company: str = "", job_title: str = "", url: str = ""):
    """Path to resumes/<job-slug>/ for this job (not created)."""
    return config.RESUMES_DIR / job_slug(company, job_title, url)


# --------------------------------------------------------------------------- #
# Base .docx template — read structure so Claude can plan edits
# --------------------------------------------------------------------------- #
def _iter_paragraphs(parent):
    """Yield every paragraph in document order, descending into tables (resume
    templates often lay content out in tables). Order is stable, so the position
    in this stream is a paragraph's addressable index."""
    from docx.document import Document as _Doc
    from docx.oxml.text.paragraph import CT_P
    from docx.oxml.table import CT_Tbl
    from docx.table import _Cell, Table
    from docx.text.paragraph import Paragraph

    if isinstance(parent, _Doc):
        parent_elm = parent.element.body
    elif isinstance(parent, _Cell):
        parent_elm = parent._tc
    else:
        parent_elm = parent._element
    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            table = Table(child, parent)
            for row in table.rows:
                for cell in row.cells:
                    yield from _iter_paragraphs(cell)


def _open_base_document():
    """Open the user's base resume.docx. Raises a clear error if absent."""
    from docx import Document

    base = config.base_resume_docx()
    if base is None:
        raise FileNotFoundError(
            "No resume.docx base template found. Per-job resume tailoring edits a "
            "`resume.docx` you provide (drop it in the project root, alongside "
            "resume.pdf). Until then, applications use the default resume."
        )
    return Document(str(base))


def read_resume_template() -> dict:
    """Return the base resume as an ordered, indexed list of paragraphs so Claude
    can decide which to rewrite/reorder/drop. Each item:
      {index, text, style, is_bullet, is_empty}
    `index` is the stable address to pass back in `tailor_resume` edits."""
    doc = _open_base_document()
    paras = []
    for i, p in enumerate(_iter_paragraphs(doc)):
        style = (p.style.name if p.style is not None else "") or ""
        text = p.text or ""
        is_bullet = ("list" in style.lower()
                     or bool(p._p.find(
                         "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pPr/"
                         "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}numPr")))
        paras.append({"index": i, "text": text, "style": style,
                      "is_bullet": is_bullet, "is_empty": not text.strip()})
    return {"base": str(config.base_resume_docx()), "paragraph_count": len(paras),
            "paragraphs": paras}


# --------------------------------------------------------------------------- #
# Apply edits → tailored .docx (+ PDF)
# --------------------------------------------------------------------------- #
def _set_paragraph_text(paragraph, text: str) -> None:
    """Replace a paragraph's text while KEEPING its formatting: write into the
    first run (inheriting its font/bold/size) and blank the remaining runs. If
    the paragraph has no runs, add one (inherits the paragraph/style default)."""
    runs = paragraph.runs
    if runs:
        runs[0].text = text
        for r in runs[1:]:
            r.text = ""
    else:
        paragraph.add_run(text)


def _delete_paragraph(paragraph) -> None:
    el = paragraph._element
    el.getparent().remove(el)
    el._p = el._element = None


def tailor_resume(company: str = "", job_title: str = "", url: str = "",
                  edits: list | None = None) -> dict:
    """Apply `edits` to a COPY of the base resume.docx and save it under
    resumes/<job-slug>/resume.docx, then export a resume.pdf for upload.

    `edits` is a list of ops addressing paragraph `index` from
    read_resume_template (the base is re-read here, so indices line up):
      {"op": "replace", "index": N, "text": "..."}  — rewrite a paragraph's text,
                                                       preserving its formatting.
      {"op": "delete",  "index": N}                 — remove a paragraph.
    Reorder/re-emphasize bullets by `replace`-ing their text in the desired
    order and `delete`-ing the least JD-relevant ones. Formatting/layout of the
    template is preserved (no text-to-PDF regeneration).

    Returns {slug, dir, docx_path, pdf_path, pdf_exported, edits_applied, note}.
    """
    edits = edits or []
    doc = _open_base_document()
    paras = list(_iter_paragraphs(doc))
    n = len(paras)

    applied, errors = 0, []
    # Apply against original indices; deletes don't reindex the others because we
    # hold the Paragraph objects directly.
    for e in edits:
        op = (e.get("op") or "").lower()
        idx = e.get("index")
        if not isinstance(idx, int) or not (0 <= idx < n):
            errors.append(f"index {idx} out of range 0..{n - 1}")
            continue
        p = paras[idx]
        if p._element is None:
            errors.append(f"index {idx} already deleted")
            continue
        if op == "replace":
            _set_paragraph_text(p, e.get("text", ""))
            applied += 1
        elif op == "delete":
            _delete_paragraph(p)
            applied += 1
        else:
            errors.append(f"index {idx}: unknown op {op!r}")

    out_dir = job_dir(company, job_title, url)
    out_dir.mkdir(parents=True, exist_ok=True)
    docx_path = out_dir / "resume.docx"
    doc.save(str(docx_path))

    pdf_path = out_dir / "resume.pdf"
    pdf_ok, pdf_note = _export_pdf(docx_path, pdf_path)

    return {"slug": out_dir.name, "dir": str(out_dir),
            "docx_path": str(docx_path),
            "pdf_path": str(pdf_path) if pdf_ok else None,
            "pdf_exported": pdf_ok, "edits_applied": applied,
            "edit_errors": errors,
            "note": pdf_note if not pdf_ok else "tailored resume saved (docx + pdf)"}


# --------------------------------------------------------------------------- #
# Cover letter — exemplars in, artifact out
# --------------------------------------------------------------------------- #
_COVER_PAT = re.compile(r"cover", re.I)
_SAMPLE_PAT = re.compile(r"writing[_ ]sample|personal statement|statement of purpose|essay", re.I)
# Reference material and structured knowledge files that are NOT the user's own
# free-form prose — kept out of the voice corpus. background/stories/preferences
# still feed the cover letter as SUBSTANCE via search_context, separately.
_NON_VOICE_PAT = re.compile(r"transcript|resume|template|^background\.|^stories\.|^preferences\.", re.I)


def cover_letter_examples() -> dict:
    """Return the FULL text of the user's own writing from context/, as few-shot
    exemplars for matching their VOICE — sentence rhythm, structure, tone — when
    drafting a cover letter. Three buckets, all the user's authentic prose:
      - cover_letters: past cover letters (primary style source).
      - writing_samples: essays / personal statements / writing samples.
      - responses: past answers to application questions (e.g. "Describe a
        product you managed…", "the hardest you've worked…") — the user's own
        first-person responses, useful as BOTH voice and substance.
    The catch-all `responses` bucket also picks up any other free-form .txt/.md
    the user drops into context/, so newly pasted writing is used automatically.
    Structured knowledge (background/stories/preferences) and reference files
    (transcript, resume) are excluded — those feed substance via search_context.
    Each item: {name, type, text}. Empty/no-text files are dropped."""
    covers, samples, responses = [], [], []
    for path in sorted(config.CONTEXT_DIR.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if _NON_VOICE_PAT.search(name):
            continue
        is_cover = bool(_COVER_PAT.search(name))
        is_sample = bool(_SAMPLE_PAT.search(name))
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            # Only pull named cover letters / writing samples from PDFs — avoid
            # slurping arbitrary reference PDFs as if they were the user's voice.
            if not (is_cover or is_sample):
                continue
            text = config.extract_pdf_text(path)
        elif suffix in (".txt", ".md"):
            text = path.read_text(encoding="utf-8", errors="ignore")
        else:
            continue
        if not text.strip():
            continue
        item = {"name": name, "text": text.strip()}
        if is_cover:
            item["type"] = "cover_letter"
            covers.append(item)
        elif is_sample:
            item["type"] = "writing_sample"
            samples.append(item)
        else:
            item["type"] = "response"   # application-question answers + other prose
            responses.append(item)
    return {"cover_letters": covers, "writing_samples": samples,
            "responses": responses,
            "count": len(covers) + len(samples) + len(responses)}


# --------------------------------------------------------------------------- #
# Cover-letter lint (data-driven: tune COVER_LINT_RULES, not the code)
# Rule ids follow the B1 lint spec: CL-Hnn = HARD (blocks save), CL-Snn = SOFT.
# --------------------------------------------------------------------------- #
COVER_LINT_RULES = {
    "signoff": "Sincerely,",                     # must be followed by the full name
    "allowed_salutations": 'To the <Company> Hiring Team,  |  Dear <Name>,',
    "salutation_re": r"To the (?P<company>\S.*?) Hiring Team,|Dear \S.*?[,:]",
    # HARD (CL-H05): em/en dash, figure dash, horizontal bar, double hyphen.
    "dash_re": r"[‒–—―]|(?<!-)--(?!-)",
    # HARD (CL-H10): body words between salutation and sign-off.
    "hard_words": (270, 460),
    # SOFT (CL-S01): target band.
    "soft_words": (295, 425),
    # HARD (CL-H12) / SOFT (CL-S02): body paragraph counts.
    "hard_paragraphs": (3, 6),
    "soft_paragraphs": (4, 5),
    # HARD (CL-H11): unfilled placeholders.
    "placeholder_re": (r"<[^<>\n]{1,60}>|\{[A-Za-z_]+\}|\[(?:TODO|TBD|Company|Role|Team)[^\]]*\]"
                       r"|\bTODO\b|\bTBD\b|\bXX+\b"),
    # HARD (CL-H09): no parentheses outside the opener's first sentence.
    "paren_re": r"[()]",
    # SOFT: canonical opener / closer.
    "opener_re": r"^I recently came across (?:the|your) posting for (?:the )?.+? role\b",
    "closer_required": [r"\bI excel at\b", r"Swiss Army Knife"],
    # SOFT: banned phrasing.
    "soft_phrases": [r"\bI(?:'|’)m writing to express\b", r"\bI am writing to express\b",
                     r"\bWhat draws me to\b"],
    "zero_to_one_re": r"(?<![\"“'])\b0[- ]to[- ]1\b(?![\"”'])|\bzero[- ]to[- ]one\b|0\s*→\s*1",
    # SOFT (CL-S06): AI-tell lexicon absent from the user's own letters.
    "lexicon": [r"\bgenuinely\b", r"\brather than\b", r"\broughly \d", r"\b(?:nearly|near) zero\b",
                r"\bI would rather\b", r"\bThat is (?:my|the same|a pattern|exactly)\b",
                r"\bmuscle\b", r"\bthe lesson I took\b", r"\bpoint of view\b",
                r"\bwelcome the chance\b", r"\bThank you for your consideration\b",
                r"\bstrong fit\b"],
    "contraction_re": (r"\b(?:I|you|we|they|it|that|there|isn|aren|don|doesn|didn|can|won|wouldn|"
                       r"couldn|shouldn)(?:'|’)(?:m|d|ve|ll|s|re|t)\b"),
    "max_colon_reveals": 1,
    # Free-text ANSWER rules (lint_text kind="answer").
    "answer_paren_aside_re": r"\([^)]{15,}\)",
    "max_consecutive_i_sentences": 2,
}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_DATE_LINE_RE = re.compile(r"\d{1,2}/\d{1,2}/(?:\d{2}|\d{4})")
_STREET_RE = re.compile(
    r"\b\d{1,6}\s+(?:[A-Z][\w.'-]*\s+){1,4}(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|"
    r"Dr|Drive|Ln|Lane|Way|Ct|Court|Pl|Place|Pkwy|Parkway)\b\.?")
_SENT_SPLIT_RE = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"”]))\s+(?=[A-Z\"“])")

_STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


def state_abbrev(state: str) -> str:
    """2-letter US state abbreviation; a value already 2 letters (or unknown) passes through."""
    s = (state or "").strip()
    if len(s) == 2:
        return s.upper()
    return _STATE_ABBR.get(s.lower(), s)


def _norm_newlines(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _digits(s: str) -> str:
    d = re.sub(r"\D", "", s or "")
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def _profile_or_empty(profile) -> dict:
    if profile is not None:
        return profile
    try:
        return config.load_user_profile() or {}
    except Exception:  # noqa: BLE001 — lint must work without a profile
        return {}


def _pval(profile: dict, key: str) -> str:
    v = profile.get(key, "")
    if isinstance(v, dict):
        v = v.get("value", "")
    return str(v or "").strip()


def _name_tokens(name: str) -> list:
    return [t for t in re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split() if t]


def _name_matches(line: str, full_name: str) -> bool:
    """First and last name tokens both present (case/punctuation-insensitive),
    so a middle initial or suffix is optional."""
    want = _name_tokens(full_name)
    if not want:
        return bool(line.strip())
    got = set(_name_tokens(line))
    return want[0] in got and want[-1] in got


def _excerpt(text: str, m) -> str:
    if m is None:
        return ""
    lo = max(0, m.start() - 30)
    return _ws(text[lo:m.end() + 30])[:80]


def _style_warnings(text: str) -> list:
    """Soft style rules for free-text ANSWERS (not cover letters)."""
    r = COVER_LINT_RULES
    out = []
    for ph in r["soft_phrases"]:
        if re.search(ph, text, re.I):
            out.append("cliche opener or phrase: " + re.search(ph, text, re.I).group(0))
            break
    if re.search(r["answer_paren_aside_re"], text):
        out.append("parenthetical aside (15+ chars); fold it into the sentence")
    if re.search(r["zero_to_one_re"], text, re.I):
        out.append('"0-to-1" used unquoted')
    run = best = 0
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if sent.startswith("I "):
            run += 1
            best = max(best, run)
        else:
            run = 0
    if best > r["max_consecutive_i_sentences"]:
        out.append(f'{best} sentences in a row start with "I "')
    return out


def _hard_dash(text: str) -> list:
    m = re.search(COVER_LINT_RULES["dash_re"], text)
    return [f"no em dashes, en dashes or double hyphens (found {m.group(0)!r})"] if m else []


def _letterhead_line(line: str, profile: dict) -> bool:
    name = _pval(profile, "full_name")
    if name and _ws(line).casefold() == _ws(name).casefold():
        return True
    if _EMAIL_RE.search(line) or _PHONE_RE.search(line) or _DATE_LINE_RE.fullmatch(line.strip()):
        return True
    if re.search(r"\s\|\s", line):
        return True
    addr = _pval(profile, "address")
    return bool(addr and addr.lower() in line.lower())


def strip_letterhead(text: str, profile=None) -> str:
    """Remove a model-typed letterhead: leading lines BEFORE the salutation that
    contain the full name, an email, a phone number, a pipe-joined contact line,
    the street address or a date. The code renders the letterhead, so any typed
    copy is a duplicate (or stale)."""
    profile = _profile_or_empty(profile)
    lines = _norm_newlines(text).strip().splitlines()
    sal = re.compile(COVER_LINT_RULES["salutation_re"])
    idx = next((i for i, ln in enumerate(lines) if sal.fullmatch(ln.strip())), None)
    if not idx:
        return "\n".join(lines).strip()
    head = [ln for ln in lines[:idx] if ln.strip() and not _letterhead_line(ln, profile)]
    return "\n".join(head + lines[idx:]).strip()


def lint_cover_letter(text: str, profile=None, posting_company: str = "") -> dict:
    """Lint a cover-letter body (rules: COVER_LINT_RULES, ids per the B1 spec).
    Returns {ok, hard, soft, word_count, paragraphs, findings}; `ok` is False
    when any HARD rule fails. `hard`/`soft` are "CL-xxx: message" strings."""
    r = COVER_LINT_RULES
    profile = _profile_or_empty(profile)
    name = _pval(profile, "full_name")
    text = _norm_newlines(text).strip()
    lines = [ln.strip() for ln in text.split("\n")]
    ne = [ln for ln in lines if ln]
    findings = []

    def add(fid, msg, m=None):
        findings.append({"id": fid, "severity": "HARD" if "-H" in fid else "SOFT",
                         "message": msg, "excerpt": _excerpt(text, m)})

    sal = re.compile(r["salutation_re"])
    sal_idx = next((i for i, ln in enumerate(lines) if sal.fullmatch(ln)), -1)
    so_idx = max((i for i, ln in enumerate(lines) if ln == r["signoff"]), default=len(lines))
    if sal_idx < 0:
        add("CL-H01", "missing salutation; allowed forms: " + r["allowed_salutations"])
    elif ne[0] != lines[sal_idx]:
        add("CL-H02", "text above the salutation (never type the letterhead, code "
                      "renders it); start at the salutation. Allowed forms: "
                      + r["allowed_salutations"])
    span = "\n".join(lines[sal_idx + 1:so_idx]).strip() if so_idx > sal_idx else ""
    paras = [_ws(p) for p in re.split(r"\n\s*\n", span) if p.strip()]
    body = " ".join(paras)
    n_words = len(re.findall(r"\S+", body))
    first = _SENT_SPLIT_RE.split(paras[0])[0] if paras else ""
    body_wo_s1 = body[len(first):]

    m = _EMAIL_RE.search(text)
    if m:
        add("CL-H03", "email address in the letter text (contact details are rendered by code)", m)
    for m in _PHONE_RE.finditer(text):
        add("CL-H03", "phone number in the letter text (contact details are rendered by code)", m)
        break
    if len(ne) < 2 or ne[-2] != r["signoff"] or not _name_matches(ne[-1], name):
        add("CL-H04", f'sign-off must end the text: "{r["signoff"]}" then the full name'
                      + (f' ("{name}")' if name else ""))
    m = re.search(r["dash_re"], text)
    if m:
        add("CL-H05", "no em dashes, en dashes or double hyphens", m)
    m = re.search(r["paren_re"], body_wo_s1)
    if m:
        add("CL-H09", "no parenthetical asides (the opener's first sentence is exempt)", m)
    lo, hi = r["hard_words"]
    if not lo <= n_words <= hi:
        add("CL-H10", f"body is {n_words} words; hard limits are {lo}-{hi}")
    m = re.search(r["placeholder_re"], text)
    if m:
        add("CL-H11", "unfilled placeholder", m)
    lo, hi = r["hard_paragraphs"]
    if not lo <= len(paras) <= hi:
        add("CL-H12", f"{len(paras)} body paragraphs; must be {lo}-{hi} (blank-line separated)")

    # ---- SOFT ----
    lo, hi = r["soft_words"]
    if not lo <= n_words <= hi:
        add("CL-S01", f"word count {n_words} outside the {lo}-{hi} target")
    lo, hi = r["soft_paragraphs"]
    if not lo <= len(paras) <= hi:
        add("CL-S02", f"{len(paras)} body paragraphs; aim for {lo}-{hi}")
    sm = sal.fullmatch(lines[sal_idx]) if sal_idx >= 0 else None
    if sm and sm.groupdict().get("company") and posting_company:
        def norm(s):
            return re.sub(r"[^a-z0-9]", "", s.casefold())
        a, b = norm(sm.group("company")), norm(posting_company)
        if not (a.startswith(b) or b.startswith(a)):
            add("CL-S03", f'salutation names "{sm.group("company")}" but the posting is "{posting_company}"')
    if paras and not re.search(r["opener_re"], paras[0]):
        add("CL-S04", 'opener should be "I recently came across the posting for the <Role> role."')
    if paras:
        for pat in r["closer_required"]:
            if not re.search(pat, paras[-1]):
                add("CL-S05", f"closing paragraph should contain {pat.replace(chr(92) + 'b', '')!r}")
    for pat in r["soft_phrases"]:
        m = re.search(pat, body, re.I)
        if m:
            add("CL-S06", "banned phrasing", m)
    m = re.search(r["zero_to_one_re"], body, re.I)
    if m:
        add("CL-S06", '"0-to-1" phrasing (only if quoted from your own letters)', m)
    for pat in r["lexicon"]:
        m = re.search(pat, body, re.I)
        if m:
            add("CL-S07", "AI-tell phrasing absent from your own letters", m)
    m = re.search(r["contraction_re"], body, re.I)
    if m:
        add("CL-S08", "contraction (your letters do not use them)", m)
    if len(re.findall(r"[A-Za-z\"”]:\s+\S", body)) > r["max_colon_reveals"]:
        add("CL-S09", "more than one colon reveal")
    m = re.search(r"(?<=\w) - (?=\w)", body_wo_s1)
    if m:
        add("CL-S10", "spaced hyphen used as a dash", m)
    addr = _pval(profile, "address").lower()
    for m in _STREET_RE.finditer(text):
        if addr and m.group(0).lower().rstrip(".") not in addr and addr not in m.group(0).lower():
            add("CL-S12", "street address in the body that differs from the profile", m)
        elif addr:
            add("CL-S12", "street address in the body (rendered by code)", m)
        break

    hard = [f'{f["id"]}: {f["message"]}' for f in findings if f["severity"] == "HARD"]
    soft = [f'{f["id"]}: {f["message"]}' for f in findings if f["severity"] == "SOFT"]
    return {"ok": not hard, "hard": hard, "soft": soft, "word_count": n_words,
            "paragraphs": len(paras), "findings": findings}


def lint_text(text: str, kind: str = "answer", profile=None) -> dict:
    """Lint any free-text draft. kind="cover_letter" runs the full cover-letter
    rules; anything else gets the dash HARD rule plus the soft style rules."""
    text = _norm_newlines(text)
    if kind == "cover_letter":
        return lint_cover_letter(text, profile)
    hard = _hard_dash(text)
    return {"ok": not hard, "hard": hard, "soft": _style_warnings(text),
            "word_count": len(text.split())}


# --------------------------------------------------------------------------- #
# Letterhead rendering + docx
# --------------------------------------------------------------------------- #
LETTER_FONT = "Mongolian Baiti"   # named only; Word falls back silently if absent


def _today_str() -> str:
    from datetime import date
    d = date.today()
    return f"{d.month}/{d.day}/{d.year}"   # M/D/YYYY, no zero padding (Windows-safe)


def _letterhead_fields(profile: dict) -> dict:
    """{full_name, address, phone, email, date, warnings}. address is
    "{street}, {city} {ST} {zip}" (no comma between city and state); the zip is
    omitted, with a warning, when the profile has none."""
    warnings = []
    zip_ = _pval(profile, "zip")
    if not zip_:
        warnings.append("profile has no `zip`; letterhead address rendered without it")
    city_line = " ".join(v for v in (_pval(profile, "city"),
                                     state_abbrev(_pval(profile, "state")), zip_) if v)
    addr = ", ".join(v for v in (_pval(profile, "address"), city_line) if v)
    return {"full_name": _pval(profile, "full_name"), "address": addr,
            "phone": _pval(profile, "phone"), "email": _pval(profile, "email"),
            "date": _today_str(), "warnings": warnings}


def _body_blocks(body: str) -> list:
    """Body paragraphs for rendering; the sign-off becomes "Sincerely," + blank + name."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", body.strip()) if b.strip()]
    if blocks:
        lines = [ln.strip() for ln in blocks[-1].splitlines() if ln.strip()]
        if len(lines) >= 2 and lines[-2] == COVER_LINT_RULES["signoff"]:
            rest = lines[:-2]
            blocks = blocks[:-1] + (["\n".join(rest)] if rest else []) + [lines[-2], "", lines[-1]]
    return blocks


def _set_font(obj, size=None, bold=None):
    """Name the letter font (run or style); eastAsia too so Word honors it."""
    from docx.oxml.ns import qn
    f = obj.font
    f.name = LETTER_FONT
    el = obj.element if hasattr(obj, "element") else obj._element
    rpr = el.get_or_add_rPr()
    if rpr.rFonts is not None:
        rpr.rFonts.set(qn("w:eastAsia"), LETTER_FONT)
    if size is not None:
        from docx.shared import Pt
        f.size = Pt(size)
    if bold is not None:
        f.bold = bold


def _render_generated(blocks: list, fields: dict, path) -> None:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    _set_font(doc.styles["Normal"], size=11)

    def para(text, size, after=11):
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(after)
        if text:
            _set_font(p.add_run(text), size=size, bold=False)
        return p

    para(fields["full_name"], 26)
    contact = " | ".join(v for v in (fields["address"], fields["phone"], fields["email"]) if v)
    if contact:
        para(contact, 11)
    para(fields["date"], 11)
    for b in blocks:
        para(b, 11, after=11 if b else 0)
    doc.save(str(path))


def _render_template(tmpl, blocks: list, fields: dict, path) -> tuple:
    """Fill the profile's cover_letter_template.docx. Returns (ok, warnings)."""
    import copy
    from docx import Document
    from docx.text.paragraph import Paragraph

    doc = Document(str(tmpl))
    body_found = False
    extra = [p for s in doc.sections for part in (s.header, s.footer) for p in part.paragraphs]
    for p in list(_iter_paragraphs(doc)) + extra:
        if "{{" not in p.text:
            continue
        text = p.text
        if "{{body}}" in text:
            body_found = True
            text = text.replace("{{body}}", blocks[0] if blocks else "")
            prev = p
            for more in blocks[1:]:
                el = copy.deepcopy(p._p)
                prev._p.addnext(el)
                prev = Paragraph(el, p._parent)
                _set_paragraph_text(prev, more)
        for k in ("full_name", "address", "phone", "email", "date"):
            text = text.replace("{{%s}}" % k, fields[k])
        _set_paragraph_text(p, text)
    leftover = [p.text for p in list(_iter_paragraphs(doc)) + extra if "{{" in p.text]
    if not body_found:
        return False, ["cover_letter_template.docx has no {{body}} placeholder"]
    if leftover:
        return False, ["cover_letter_template.docx has unfilled placeholders: " + leftover[0][:60]]
    doc.save(str(path))
    return True, []


def _render_cover_docx(body: str, fields: dict, path) -> tuple:
    """Write the cover-letter docx. Uses the profile's cover_letter_template.docx
    ({{full_name}} {{address}} {{phone}} {{email}} {{date}} {{body}}) when it
    fills cleanly, else generates the letterhead. Returns (mode, warnings) with
    mode "template" or "generated"."""
    blocks = _body_blocks(body)
    warnings = list(fields.get("warnings", []))
    tmpl = config.cover_letter_template_docx()
    if tmpl is not None:
        ok, w = _render_template(tmpl, blocks, fields, path)
        if ok:
            return "template", warnings
        warnings += w + ["fell back to the generated letterhead"]
    _render_generated(blocks, fields, path)
    return "generated", warnings


def save_cover_letter(company: str = "", job_title: str = "", url: str = "",
                      text: str = "", force: bool = False) -> dict:
    """Persist a drafted cover letter under resumes/<job-slug>/: cover_letter.txt
    (body only, for a free-text field) and cover_letter.docx/.pdf with a rendered
    letterhead (for a file input). A model-typed letterhead is stripped first;
    the letter is linted and, if any HARD rule fails, NOTHING is written unless
    force=True. Returns {saved, lint, ...}: when blocked just that plus `note`;
    when saved also slug, dir, txt_path, pdf_path, pdf_exported, render_warnings, note."""
    text = _norm_newlines(text)
    if not text.strip():
        raise ValueError("cover letter text is empty")
    profile = _profile_or_empty(None)
    body = strip_letterhead(text, profile)
    lint = lint_cover_letter(body, profile, posting_company=company)
    if not lint["ok"] and not force:
        return {"saved": False, "lint": lint,
                "note": "not saved: fix the HARD lint failures and call again "
                        "(force=True only with the user's explicit OK)"}
    out_dir = job_dir(company, job_title, url)
    out_dir.mkdir(parents=True, exist_ok=True)
    txt_path = out_dir / "cover_letter.txt"
    txt_path.write_text(body + "\n", encoding="utf-8")

    docx_path = out_dir / "cover_letter.docx"
    pdf_path = out_dir / "cover_letter.pdf"
    render_warnings = []
    try:
        _mode, render_warnings = _render_cover_docx(body, _letterhead_fields(profile), docx_path)
        pdf_ok, pdf_note = _export_pdf(docx_path, pdf_path)
    except Exception as exc:  # noqa: BLE001 — never lose the .txt over a PDF issue
        pdf_ok, pdf_note = False, f"pdf export failed: {type(exc).__name__}: {exc}"

    return {"saved": True, "lint": lint, "slug": out_dir.name, "dir": str(out_dir),
            "txt_path": str(txt_path),
            "pdf_path": str(pdf_path) if pdf_ok else None,
            "pdf_exported": pdf_ok, "render_warnings": render_warnings,
            "note": "cover letter saved (txt + pdf)" if pdf_ok
                    else f"cover letter saved (txt only) — {pdf_note}"}


# --------------------------------------------------------------------------- #
# Apply-time resolution: tailored artifact if present, else default
# --------------------------------------------------------------------------- #
def job_artifacts(company: str = "", job_title: str = "", url: str = "") -> dict:
    """Resolve which resume/cover-letter to use for a job at apply time.

    Checks resumes/<job-slug>/ for tailored artifacts and FALLS BACK to the
    default resume when none exist. Returns:
      {slug, dir, tailored (bool), resume_path, resume_is_tailored,
       cover_letter_path, cover_letter_text, has_cover_letter}
    `resume_path` is always usable (tailored PDF/DOCX if present, else the
    project default from config.resume_upload_path)."""
    out_dir = job_dir(company, job_title, url)
    resume_tailored = None
    for name in ("resume.pdf", "resume.docx"):
        p = out_dir / name
        if p.exists():
            resume_tailored = p
            break
    cover_file = None
    for name in ("cover_letter.pdf", "cover_letter.docx"):
        p = out_dir / name
        if p.exists():
            cover_file = p
            break
    cover_txt = out_dir / "cover_letter.txt"
    cover_text = cover_txt.read_text(encoding="utf-8") if cover_txt.exists() else ""

    resume_path = resume_tailored or config.resume_upload_path()
    return {"slug": out_dir.name, "dir": str(out_dir),
            "tailored": bool(resume_tailored or cover_file or cover_text),
            "resume_path": str(resume_path),
            "resume_is_tailored": resume_tailored is not None,
            "cover_letter_path": str(cover_file) if cover_file else None,
            "cover_letter_text": cover_text,
            "has_cover_letter": bool(cover_file or cover_text)}


# --------------------------------------------------------------------------- #
# DOCX -> PDF export (Word COM on Windows; graceful if unavailable)
# --------------------------------------------------------------------------- #
_WD_FORMAT_PDF = 17  # Word's wdFormatPDF SaveAs code


def _convert_via_word(docx_path, pdf_path) -> tuple[bool, str]:
    """Drive Microsoft Word directly (win32com) in a DEDICATED Word process
    (DispatchEx) — one open→SaveAs→close→quit per call. A fresh process each
    time sidesteps docx2pdf's intermittent `Open.SaveAs` failure when a Word
    instance is reused across conversions."""
    try:
        import pythoncom  # type: ignore
        import win32com.client as win32  # type: ignore
    except Exception as exc:  # noqa: BLE001 — pywin32 absent
        return False, f"win32com unavailable ({type(exc).__name__})"
    word = None
    coinit = False
    try:
        pythoncom.CoInitialize()
        coinit = True
        word = win32.DispatchEx("Word.Application")
        word.Visible = False
        try:
            doc = word.Documents.Open(str(docx_path.resolve()), ReadOnly=1)
            doc.SaveAs(str(pdf_path.resolve()), FileFormat=_WD_FORMAT_PDF)
            doc.Close(False)
        finally:
            word.Quit()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if coinit:
            pythoncom.CoUninitialize()
    return (pdf_path.exists(), "" if pdf_path.exists() else "no file written")


def _convert_via_docx2pdf(docx_path, pdf_path) -> tuple[bool, str]:
    """docx2pdf drives Microsoft Word — Windows via COM, macOS via AppleScript.
    So this covers a Mac that HAS Word (the win32com path above is Windows-only)."""
    try:
        from docx2pdf import convert
    except Exception as exc:  # noqa: BLE001
        return False, f"docx2pdf unavailable ({type(exc).__name__})"
    try:
        convert(str(docx_path), str(pdf_path))
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return (pdf_path.exists(), "" if pdf_path.exists() else "no file written")


def _find_soffice():
    """Locate a LibreOffice/OpenOffice `soffice` binary (cross-platform, no MS
    Word needed). Checks PATH then the usual per-OS install locations."""
    import shutil

    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",  # macOS
        "/usr/bin/soffice", "/usr/local/bin/soffice",            # Linux
        "/opt/libreoffice/program/soffice",
    ]
    for c in candidates:
        from pathlib import Path
        if Path(c).exists():
            return c
    return None


def _convert_via_libreoffice(docx_path, pdf_path) -> tuple[bool, str]:
    """Convert with LibreOffice headless — works on Windows/macOS/Linux with NO
    Microsoft Word. `soffice` writes <stem>.pdf into --outdir; we rename if the
    caller wanted a different name."""
    import subprocess

    soffice = _find_soffice()
    if not soffice:
        return False, "LibreOffice (soffice) not found"
    outdir = pdf_path.parent
    try:
        proc = subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir",
             str(outdir), str(docx_path.resolve())],
            capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        return False, f"soffice failed ({type(exc).__name__}: {exc})"
    produced = outdir / (docx_path.stem + ".pdf")
    if produced != pdf_path and produced.exists():
        produced.replace(pdf_path)
    if pdf_path.exists():
        return True, ""
    return False, (proc.stderr or proc.stdout or "no file written").strip()[:200]


def _export_pdf(docx_path, pdf_path) -> tuple[bool, str]:
    """Export a .docx to .pdf, trying converters in fidelity order and stopping
    at the first that works — cross-platform:
      1. MS Word via win32com COM (Windows + Word; retried once), then
      2. docx2pdf (Windows COM OR macOS AppleScript — a Mac WITH Word), then
      3. LibreOffice headless (`soffice`; Windows/macOS/Linux, NO Word needed).
    Returns (ok, note) and never raises — if none is available the caller still
    has the .docx/.txt and the note explains how to convert manually."""
    if pdf_path.exists():
        try:
            pdf_path.unlink()  # stale output would read as a false success
        except OSError:
            pass
    notes = []
    for attempt in (1, 2):  # Word COM: high fidelity, retry the known flaky call
        ok, note = _convert_via_word(docx_path, pdf_path)
        if ok:
            return True, "pdf exported (Word)" + (" [retry]" if attempt == 2 else "")
        notes.append(note)
    for fn, label in ((_convert_via_docx2pdf, "docx2pdf"),
                      (_convert_via_libreoffice, "LibreOffice")):
        ok, note = fn(docx_path, pdf_path)
        if ok:
            return True, f"pdf exported ({label})"
        notes.append(note)
    detail = "; ".join(n for n in notes if n) or "no converter available"
    return False, (f"PDF export failed ({detail}). docx saved — install MS Word "
                   "or LibreOffice, or open the .docx and Save As PDF.")
