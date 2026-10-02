"""Cover-letter lint + letterhead rendering (src/tailor.py).

Fixtures follow the B1 lint spec (scratchpad/b1/lint_spec.md): one synthetic
pass letter in the user's canonical shape, then one fail case per HARD rule made
by changing a single line. Placeholder profile values only. Everything runs
against a monkeypatched profile dict and temp dirs; no real profile is read.

Standalone: `python tests/test_cover_letter_lint.py`.
"""

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, tailor  # noqa: E402

PROFILE = {"full_name": "Jane Q Doe", "email": "jane@example.com",
           "phone": "310-555-0100", "address": "123 Main St", "city": "Los Angeles",
           "state": "California", "zip": "90001"}

PAD = "The team shipped the work on schedule and finance closed the books faster."

OPENER = ("I recently came across the posting for the Senior Product Manager role. "
          "I am certain that my background in payments and operations makes me a perfect fit "
          "for your team. I have built my career on turning messy workflows into reliable "
          "products, leveraging a blend of analytics and stakeholder management.")
P2 = ("The role involves owning a risk roadmap, a challenge that requires clear judgment. "
      "At Example Corp, I demonstrated this capability by rebuilding the review queue. "
      "The result was a 30% drop in manual reviews. " + PAD + " " + PAD + " " + PAD + " " + PAD)
P3 = ("I understand that scaling a platform requires strong operating habits. I have a proven "
      "track record of aligning engineering and finance. For example, I led a billing "
      "migration across four teams. " + PAD + " " + PAD + " " + PAD + " " + PAD)
P4 = ("Additionally, my technical proficiency in SQL allows me to be self-sufficient in "
      "analysis. At Example Corp, I built the dashboards the team now uses weekly. " + PAD
      + " " + PAD)
CLOSER = ("I excel at connecting product, data and operations. I am eager to bring my uniquely "
          "suited skillset as a Swiss Army Knife to help the Acme team reduce fraud losses.")
SIGN = "Sincerely,\nJane Q Doe"


def letter(opener=OPENER, p2=P2, p3=P3, p4=P4, closer=CLOSER, sal="To the Acme Hiring Team,",
           sign=SIGN):
    return "\n\n".join([sal, opener, p2, p3, p4, closer, sign])


GOOD = letter()


def _ids(r, sev=None):
    return sorted({f["id"] for f in r["findings"] if sev is None or f["severity"] == sev})


def test_good_letter_passes_clean():
    r = tailor.lint_cover_letter(GOOD, PROFILE)
    assert r["ok"] and not r["soft"], r
    assert 295 <= r["word_count"] <= 425 and r["paragraphs"] == 5, r


def test_crlf_and_cr_normalized():
    for nl in ("\r\n", "\r"):
        r = tailor.lint_cover_letter(GOOD.replace("\n", nl), PROFILE)
        assert r["ok"], (nl, r["hard"])
    # Pass PROFILE explicitly: without it lint_text reads the ACTIVE profile,
    # whose full_name won't match the fixture's signature on a real checkout.
    assert tailor.lint_text(GOOD.replace("\n", "\r\n"), kind="cover_letter",
                            profile=PROFILE)["ok"]
    typed = "Jane Q Doe\r\n\r\n" + GOOD.replace("\n", "\r\n")
    assert tailor.strip_letterhead(typed, PROFILE).startswith("To the Acme")


def test_h05_em_dash_en_dash_double_hyphen():
    for bad in ("—", "–", " -- "):
        r = tailor.lint_cover_letter(letter(p2=P2 + " It worked" + bad + "really."), PROFILE)
        assert "CL-H05" in _ids(r, "HARD"), (bad, r["hard"])


def test_h01_h02_letterhead_above_salutation():
    r = tailor.lint_cover_letter("Jane Q Doe\n\n" + GOOD, PROFILE)
    assert "CL-H02" in _ids(r, "HARD"), r
    r = tailor.lint_cover_letter(GOOD.replace("To the Acme Hiring Team,", "Hello team"), PROFILE)
    msg = " ".join(r["hard"])
    assert "CL-H01" in msg and "Hiring Team" in msg and "Dear" in msg, r["hard"]


def test_dear_form_accepts_comma_or_colon():
    for end in (",", ":"):
        r = tailor.lint_cover_letter(letter(sal="Dear Ms. Smith" + end), PROFILE)
        assert r["ok"], (end, r["hard"])


def test_typed_letterhead_stripped_before_lint():
    typed = ("Jane Q Doe\n123 Main St, Los Angeles CA 90001 | 310-555-0100 | jane@example.com\n"
             "10/1/2026\n\n" + GOOD)
    assert not tailor.lint_cover_letter(typed, PROFILE)["ok"]
    stripped = tailor.strip_letterhead(typed, PROFILE)
    assert stripped.startswith("To the Acme Hiring Team,"), stripped[:60]
    assert tailor.lint_cover_letter(stripped, PROFILE)["ok"]


def test_h03_contact_details_blocked():
    r = tailor.lint_cover_letter(letter(p2=P2 + " Reach me at 415-555-0199."), PROFILE)
    assert "CL-H03" in _ids(r, "HARD"), r
    r = tailor.lint_cover_letter(letter(p2=P2 + " Mail me at someone@else.com."), PROFILE)
    assert "CL-H03" in _ids(r, "HARD"), r


def test_phone_blocked_via_monkeypatched_profile():
    real = config.load_user_profile
    config.load_user_profile = lambda: dict(PROFILE, phone="212-555-0000")
    try:
        r = tailor.lint_cover_letter(letter(p2=P2 + " Call 310-555-0100."))
    finally:
        config.load_user_profile = real
    assert "CL-H03" in _ids(r, "HARD"), r


def test_h04_signoff_name_tokens():
    assert tailor.lint_cover_letter(letter(sign="Sincerely,\nJane Doe"), PROFILE)["ok"]
    assert tailor.lint_cover_letter(letter(sign="Sincerely,\njane q. doe"), PROFILE)["ok"]
    r = tailor.lint_cover_letter(letter(sign="Sincerely,\nJohn Smith"), PROFILE)
    assert "CL-H04" in _ids(r, "HARD"), r
    r = tailor.lint_cover_letter(letter(sign="Best,\nJane Q Doe"), PROFILE)
    assert "CL-H04" in _ids(r, "HARD"), r


def test_writing_to_express_is_soft_only():
    op = "I'm writing to express my strong interest in the role. " + OPENER.split(". ", 1)[1]
    r = tailor.lint_cover_letter(letter(opener=op), PROFILE)
    assert r["ok"], r["hard"]
    assert {"CL-S04", "CL-S06"} <= set(_ids(r, "SOFT")), r["soft"]


def test_closer_requirements_soft():
    r = tailor.lint_cover_letter(letter(closer="I am eager to help the Acme team."), PROFILE)
    assert r["ok"] and "CL-S05" in _ids(r, "SOFT"), r
    assert sum("CL-S05" in s for s in r["soft"]) == 2, r["soft"]


def test_h09_parenthetical_blocked_but_opener_exempt():
    r = tailor.lint_cover_letter(letter(p2=P2 + " (see below)"), PROFILE)
    assert "CL-H09" in _ids(r, "HARD"), r
    op = OPENER.replace("Senior Product Manager role", "Senior Product Manager (Payments) role")
    assert tailor.lint_cover_letter(letter(opener=op), PROFILE)["ok"]


def test_length_soft_and_hard_bands():
    longer = letter(p2=P2 + (" " + PAD) * 10)      # past 425, under 460
    r = tailor.lint_cover_letter(longer, PROFILE)
    assert r["ok"] and "CL-S01" in _ids(r, "SOFT"), (r["word_count"], r)
    huge = letter(p2=P2 + (" " + PAD) * 16)
    r = tailor.lint_cover_letter(huge, PROFILE)
    assert "CL-H10" in _ids(r, "HARD") and r["word_count"] > 460, r["word_count"]
    tiny = letter(p2="Short.", p3="Short.", p4="Short.")
    assert "CL-H10" in _ids(tailor.lint_cover_letter(tiny, PROFILE), "HARD")


def test_h11_placeholder_blocked():
    r = tailor.lint_cover_letter(letter(p2=P2 + " Welcome to <Company>."), PROFILE)
    assert "CL-H11" in _ids(r, "HARD"), r


def test_h12_single_paragraph_blocked():
    body = " ".join([OPENER, P2, P3, P4, CLOSER])
    r = tailor.lint_cover_letter("To the Acme Hiring Team,\n\n" + body + "\n\n" + SIGN, PROFILE)
    assert "CL-H12" in _ids(r, "HARD"), r


def test_address_in_body_is_soft():
    r = tailor.lint_cover_letter(letter(p2=P2 + " I live at 9 Oak Street."), PROFILE)
    assert r["ok"] and "CL-S12" in _ids(r, "SOFT"), r


def test_lint_text_answer():
    assert not tailor.lint_text("Great fit — truly.")["ok"]
    assert tailor.lint_text("A plain answer.")["ok"]
    runs = tailor.lint_text("I built it. I shipped it. I ran it. Then we grew.")
    assert runs["ok"] and any("in a row" in w for w in runs["soft"]), runs


def test_letterhead_fields():
    f = tailor._letterhead_fields(PROFILE)
    assert re.fullmatch(r"[1-9]\d?/[1-9]\d?/\d{4}", f["date"]), f["date"]
    assert f["address"] == "123 Main St, Los Angeles CA 90001", f["address"]
    assert not f["warnings"]
    assert tailor.state_abbrev("New York") == "NY" and tailor.state_abbrev("tx") == "TX"
    nz = tailor._letterhead_fields({k: v for k, v in PROFILE.items() if k != "zip"})
    assert nz["address"] == "123 Main St, Los Angeles CA" and nz["warnings"], nz


def test_template_not_in_voice_corpus():
    real = config.CONTEXT_DIR
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "cover_letter_template.md").write_text("TEMPLATE RULES", encoding="utf-8")
        (d / "cover_letter_acme.txt").write_text("my own letter", encoding="utf-8")
        config.CONTEXT_DIR = d
        try:
            ex = tailor.cover_letter_examples()
        finally:
            config.CONTEXT_DIR = real
    names = [i["name"] for i in ex["cover_letters"] + ex["responses"] + ex["writing_samples"]]
    assert names == ["cover_letter_acme.txt"], names


def _docx():
    try:
        import docx  # noqa: F401
        return True
    except ImportError:
        return False


def test_docx_generated_letterhead():
    if not _docx():
        print("SKIP  test_docx_generated_letterhead (python-docx not installed)")
        return
    from docx import Document
    real = config.cover_letter_template_docx
    config.cover_letter_template_docx = lambda: None
    try:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "cl.docx"
            mode, warns = tailor._render_cover_docx(GOOD, tailor._letterhead_fields(PROFILE), out)
            doc = Document(str(out))
    finally:
        config.cover_letter_template_docx = real
    paras = doc.paragraphs
    texts = [p.text for p in paras]
    assert mode == "generated" and not warns
    assert texts[0] == "Jane Q Doe" and paras[0].runs[0].font.size.pt == 26, texts[:2]
    assert not paras[0].runs[0].font.bold
    assert paras[0].runs[0].font.name == "Mongolian Baiti"
    assert texts[1] == "123 Main St, Los Angeles CA 90001 | 310-555-0100 | jane@example.com", texts[1]
    assert paras[1].runs[0].font.size.pt == 11
    assert re.fullmatch(r"\d+/\d+/\d{4}", texts[2]), texts[2]
    assert texts[3].startswith("To the Acme Hiring Team"), texts[3]
    assert texts[-3:] == ["Sincerely,", "", "Jane Q Doe"], texts[-3:]


def _make_template(path, with_body=True, extra=""):
    from docx import Document
    d = Document()
    d.add_paragraph("{{full_name}}")
    d.add_paragraph("{{address}} | {{phone}} | {{email}}")
    d.add_paragraph("{{date}}")
    if with_body:
        d.add_paragraph("{{body}}")
    if extra:
        d.add_paragraph(extra)
    d.sections[0].footer.paragraphs[0].text = "{{full_name}} footer"
    d.save(str(path))


def test_docx_template_fill_and_fallback():
    if not _docx():
        print("SKIP  test_docx_template_fill_and_fallback (python-docx not installed)")
        return
    from docx import Document
    real = config.cover_letter_template_docx
    fields = tailor._letterhead_fields(PROFILE)
    try:
        with tempfile.TemporaryDirectory() as td:
            tpl, out = Path(td) / "t.docx", Path(td) / "o.docx"
            _make_template(tpl)
            config.cover_letter_template_docx = lambda: tpl
            mode, warns = tailor._render_cover_docx(GOOD, fields, out)
            doc = Document(str(out))
            texts = [p.text for p in doc.paragraphs]
            assert mode == "template" and texts[0] == "Jane Q Doe", (mode, warns)
            assert texts[-3:] == ["Sincerely,", "", "Jane Q Doe"], texts[-3:]
            assert doc.sections[0].footer.paragraphs[0].text == "Jane Q Doe footer"
            _make_template(tpl, with_body=False)
            mode, warns = tailor._render_cover_docx(GOOD, fields, out)
            assert mode == "generated" and any("{{body}}" in w for w in warns), (mode, warns)
            _make_template(tpl, extra="{{unknown}}")
            mode, warns = tailor._render_cover_docx(GOOD, fields, out)
            assert mode == "generated" and any("unfilled" in w for w in warns), (mode, warns)
    finally:
        config.cover_letter_template_docx = real


def test_save_blocked_writes_nothing_and_saved_shape():
    real = (config.load_user_profile, config.RESUMES_DIR, tailor._export_pdf,
            config.cover_letter_template_docx)
    with tempfile.TemporaryDirectory() as td:
        config.load_user_profile = lambda: dict(PROFILE)
        config.RESUMES_DIR = Path(td)
        config.cover_letter_template_docx = lambda: None
        tailor._export_pdf = lambda d, p: (False, "no word")
        try:
            bad = tailor.save_cover_letter("Acme", "PM", "http://x/1", "Hi — there")
            assert bad["saved"] is False and bad["lint"]["hard"] and not list(Path(td).iterdir())
            ok = tailor.save_cover_letter("Acme", "PM", "http://x/1", "Jane Q Doe\n\n" + GOOD)
            assert ok["saved"] is True and ok["lint"]["ok"], ok
            txt = Path(ok["txt_path"]).read_text(encoding="utf-8")
            assert txt.startswith("To the Acme Hiring Team,") and "123 Main" not in txt
            if _docx():
                assert (Path(ok["dir"]) / "cover_letter.docx").exists()
        finally:
            (config.load_user_profile, config.RESUMES_DIR, tailor._export_pdf,
             config.cover_letter_template_docx) = real


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
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
