"""Cover-letter lint + letterhead rendering (src/tailor.py).

Everything runs against a monkeypatched profile dict and a temp dir; no real
profile is read. Standalone: `python tests/test_cover_letter_lint.py`.
"""

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, tailor  # noqa: E402

PROFILE = {"full_name": "Jane Q Doe", "email": "jane@example.com",
           "phone": "310-555-0100", "address": "123 Main St", "city": "Los Angeles",
           "state": "California"}

FILLER = ("I led the migration of a billing platform across four teams and shipped it "
          "on time. The work cut support tickets and gave finance a cleaner close. ")


def _body(extra="", words=380):
    para = FILLER * 20
    para = " ".join(para.split()[:words])
    return ("To the Acme Hiring Team,\n\n" + para + " " + extra + "\n\n"
            "Sincerely,\nJane Q Doe")


GOOD = _body()


def test_good_letter_passes():
    r = tailor.lint_cover_letter(GOOD, PROFILE)
    assert r["ok"], r
    assert not r["soft"], r


def test_em_dash_blocks():
    r = tailor.lint_cover_letter(_body("It worked — really."), PROFILE)
    assert not r["ok"] and any("—" in h for h in r["hard"]), r
    r = tailor.lint_cover_letter(_body("It worked -- really."), PROFILE)
    assert not r["ok"], r


def test_missing_salutation_and_signoff():
    r = tailor.lint_cover_letter("Hello there.\n\nThanks", PROFILE)
    assert not r["ok"] and len(r["hard"]) >= 2, r


def test_typed_letterhead_stripped_before_lint():
    typed = "Jane Q Doe\njane@example.com | 310-555-0100\n10/1/2026\n\n" + GOOD
    assert not tailor.lint_cover_letter(typed, PROFILE)["ok"]  # raw: letterhead present
    stripped = tailor.strip_letterhead(typed, PROFILE)
    assert stripped.startswith("To the Acme Hiring Team,"), stripped[:60]
    assert tailor.lint_cover_letter(stripped, PROFILE)["ok"]


def test_stale_phone_blocked():
    r = tailor.lint_cover_letter(_body("Reach me at 415-555-0199."), PROFILE)
    assert not r["ok"] and any("phone" in h for h in r["hard"]), r
    ok = tailor.lint_cover_letter(_body("Reach me at 310-555-0100."), PROFILE)
    assert ok["ok"], ok


def test_stale_phone_via_monkeypatched_profile():
    real = config.load_user_profile
    config.load_user_profile = lambda: dict(PROFILE, phone="212-555-0000")
    try:
        r = tailor.lint_cover_letter(_body("Call 310-555-0100."))
    finally:
        config.load_user_profile = real
    assert not r["ok"] and any("phone" in h for h in r["hard"]), r


def test_word_band_soft_warning():
    short = tailor.lint_cover_letter(_body(words=60), PROFILE)
    assert short["ok"] and any("word count" in w for w in short["soft"]), short


def test_soft_style_rules():
    r = tailor.lint_cover_letter(
        _body("I'm writing to express interest (which is a long aside here). "
              "It was a 0-to-1 launch."), PROFILE)
    joined = " ".join(r["soft"])
    assert "express" in joined and "parenthetical" in joined and "0-to-1" in joined, r
    runs = tailor.lint_text("I built it. I shipped it. I ran it. Then we grew.")
    assert runs["ok"] and any("in a row" in w for w in runs["soft"]), runs


def test_lint_text_answer_blocks_em_dash():
    assert not tailor.lint_text("Great fit — truly.")["ok"]
    assert tailor.lint_text("A plain answer.")["ok"]


def test_date_format_no_padding():
    f = tailor._letterhead_fields(PROFILE)
    assert re.fullmatch(r"[1-9]\d?/[1-9]\d?/\d{4}", f["date"]), f["date"]
    assert f["address"] == "123 Main St, Los Angeles, California", f


def test_docx_letterhead_first():
    try:
        from docx import Document
    except ImportError:
        print("SKIP  test_docx_letterhead_first (python-docx not installed)")
        return
    real = config.cover_letter_template_docx
    config.cover_letter_template_docx = lambda: None
    try:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "cl.docx"
            mode = tailor._render_cover_docx(GOOD, tailor._letterhead_fields(PROFILE), out)
            paras = [p.text for p in Document(str(out)).paragraphs]
    finally:
        config.cover_letter_template_docx = real
    assert mode == "generated"
    assert paras[0] == "Jane Q Doe", paras[:4]
    assert "310-555-0100" in paras[1] and "jane@example.com" in paras[1], paras[1]
    assert re.fullmatch(r"\d+/\d+/\d{4}", paras[2]), paras[2]
    assert paras[3] == "" and paras[4].startswith("To the Acme Hiring Team"), paras[:5]


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
