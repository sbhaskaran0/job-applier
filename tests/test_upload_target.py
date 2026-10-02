"""upload_resume(target="cover") must never touch the resume slot.

Greenhouse hides BOTH its Resume/CV and Cover Letter <input type=file>, so
read_form never lists them and a caller cannot pass an index. The old
auto-locator scored every file input and picked the "resume-ish" one -- so
uploading a cover letter with no index OVERWROTE the resume (seen live
2026-09-16), with no read-back to notice.

Pinned here: the pure chooser (`_choose_file_input`), the no_cover_slot result,
the read-back (`verified` / `observed_filename`), and the guard that flags a
cover upload which landed in the resume slot anyway.

Standalone, like the rest of tests/ -- `python tests/test_upload_target.py`.
"""

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_pw = types.ModuleType("playwright")
_pw_async = types.ModuleType("playwright.async_api")
_pw_async.async_playwright = lambda: None
sys.modules.setdefault("playwright", _pw)
sys.modules.setdefault("playwright.async_api", _pw_async)

from src import browser                 # noqa: E402

choose = browser._choose_file_input


def cand(i, text="", name="", id="", aria="", label="", file=""):
    return {"i": i, "text": text, "name": name, "id": id, "aria": aria,
            "label": label, "file": file}


RESUME = dict(text="Resume/CV Attach Enter manually", id="resume")
COVER = dict(text="Cover Letter Attach Enter manually", id="cover_letter")


# --------------------------------------------------------------------------
# Pure chooser
# --------------------------------------------------------------------------
def test_resume_only_page_cover_has_no_slot():
    assert choose([cand(0, **RESUME)], "cover") is None


def test_resume_only_page_resume_found():
    assert choose([cand(0, **RESUME)], "resume") == 0


def test_cover_only_page():
    assert choose([cand(0, **COVER)], "cover") == 0


def test_both_inputs_each_target_gets_its_own():
    cs = [cand(0, **RESUME), cand(1, **COVER)]
    assert choose(cs, "resume") == 0
    assert choose(cs, "cover") == 1
    assert choose(list(reversed([cand(1, **COVER), cand(0, **RESUME)])),
                  "cover") == 1


def test_resume_target_unchanged_prefers_resume_over_cover_in_any_order():
    cs = [cand(0, **COVER), cand(1, **RESUME)]
    assert choose(cs, "resume") == 1


def test_unlabelled_input_is_never_a_cover_slot():
    assert choose([cand(0), cand(1, text="Attach")], "cover") is None


def test_own_attributes_beat_a_mixed_container():
    # Both uploads in ONE container: every input's text mentions both words.
    mixed = "Resume/CV Cover Letter Attach Attach"
    cs = [cand(0, text=mixed, id="resume_file", label="Resume/CV"),
          cand(1, text=mixed, id="cover_file", label="Cover Letter")]
    assert choose(cs, "cover") == 1
    assert choose(cs, "resume") == 0


def test_mixed_container_without_own_signal_is_not_cover():
    # Text mentions resume too, so it cannot prove the input is the cover slot.
    mixed = "Resume/CV Cover Letter Attach Attach"
    assert choose([cand(0, text=mixed), cand(1, text=mixed)], "cover") is None


def test_resume_labelled_input_never_chosen_for_cover():
    cs = [cand(0, aria="Upload your resume", text="Cover Letter"),
          cand(1, name="cv")]
    assert choose(cs, "cover") is None


def test_cover_via_aria_or_name_or_label():
    assert choose([cand(0, aria="Attach cover letter")], "cover") == 0
    assert choose([cand(0, name="cover_letter")], "cover") == 0
    assert choose([cand(0, label="Cover letter")], "cover") == 0


def test_no_inputs_at_all():
    assert choose([], "cover") is None
    assert choose([], "resume") is None


# --------------------------------------------------------------------------
# Fakes: a page of file inputs; a frame answers the module's JS by identity.
# --------------------------------------------------------------------------
class FakeInput:
    def __init__(self, i, text, **kw):
        self.i, self.text, self.kw = i, text, kw
        self.file = ""
        self.tagged = False

    def descriptor(self):
        return {"i": self.i, "text": self.text, "file": self.file,
                "name": self.kw.get("name", ""), "id": self.kw.get("id", ""),
                "aria": "", "label": self.kw.get("label", "")}


class FakeLoc:
    def __init__(self, frame):
        self.frame = frame
        self.first = self

    def _target(self):
        return next(x for x in self.frame.inputs if x.tagged)

    async def set_input_files(self, p):
        inp = self._target()
        # Misbehaving widget: redirect the file into another slot.
        if self.frame.redirect_to is not None:
            inp = self.frame.inputs[self.frame.redirect_to]
        inp.file = p.replace("\\", "/").rsplit("/", 1)[-1]
        inp.text = inp.text + " " + inp.file

    async def evaluate(self, js):
        assert js is browser._FILE_READBACK_JS
        inp = self._target()
        return {"name": inp.file, "text": inp.text}


class FakeFrame:
    def __init__(self, inputs, redirect_to=None):
        self.inputs = inputs
        self.redirect_to = redirect_to

    def locator(self, selector):
        return FakeLoc(self)

    async def evaluate(self, js, arg=None):
        if js is browser._FILE_CANDIDATES_JS:
            return [x.descriptor() for x in self.inputs]
        if js is browser._TAG_FILE_JS:
            for x in self.inputs:
                x.tagged = x.i == arg
            return None
        for x in self.inputs:           # the untag snippet
            x.tagged = False
        return None


class FakePage:
    def __init__(self, frame):
        self.frames = [frame]


def session(inputs, redirect_to=None):
    frame = FakeFrame(inputs, redirect_to)
    s = browser.BrowserSession()
    s.tabs[0] = {"page": FakePage(frame), "fields": {}, "company": "",
                 "title": "", "url": ""}
    s.active_id = 0
    return s, frame


def gh_inputs():
    return [FakeInput(0, "Resume/CV Attach", id="resume"),
            FakeInput(1, "Cover Letter Attach", id="cover_letter")]


# --------------------------------------------------------------------------
def test_cover_upload_goes_to_cover_and_resume_untouched():
    s, frame = session(gh_inputs())
    res = asyncio.run(s.upload_resume(None, "/x/cover_letter.pdf", "cover"))
    assert res["status"] == "uploaded", res
    assert res["verified"] is True and res["observed_filename"] == "cover_letter.pdf", res
    assert frame.inputs[1].file == "cover_letter.pdf"
    assert frame.inputs[0].file == "", "resume slot must be untouched"
    assert not any(x.tagged for x in frame.inputs), "tag must be cleaned up"


def test_cover_upload_on_resume_only_page_is_no_cover_slot_and_uploads_nothing():
    s, frame = session([FakeInput(0, "Resume/CV Attach", id="resume")])
    res = asyncio.run(s.upload_resume(None, "/x/cover_letter.pdf", "cover"))
    assert res["status"] == "no_cover_slot", res
    assert frame.inputs[0].file == "", "must NEVER fall back to the resume slot"


def test_cover_upload_with_no_path_is_refused():
    s, _ = session(gh_inputs())
    try:
        asyncio.run(s.upload_resume(None, None, "cover"))
    except ValueError:
        return
    raise AssertionError("target=cover without a path must raise")


def test_resume_upload_default_target_hits_resume_slot():
    s, frame = session(gh_inputs())
    res = asyncio.run(s.upload_resume(None, "/x/resume.pdf"))
    assert res["status"] == "uploaded" and res["verified"] is True, res
    assert frame.inputs[0].file == "resume.pdf"
    assert frame.inputs[1].file == ""


def test_cover_landing_in_resume_slot_is_flagged_as_error():
    # The widget swallows the file into slot 0 (the resume) regardless.
    s, frame = session(gh_inputs(), redirect_to=0)
    res = asyncio.run(s.upload_resume(None, "/x/cover_letter.pdf", "cover"))
    assert res["status"] == "error", res
    assert res["error"] == "cover upload landed in resume slot", res
    assert res["verified"] is False


def test_unverified_when_filename_not_observed():
    class Silent(FakeLoc):
        async def set_input_files(self, p):
            return None
    inputs = gh_inputs()
    s, frame = session(inputs)
    frame.locator = lambda sel: Silent(frame)
    res = asyncio.run(s.upload_resume(None, "/x/cover_letter.pdf", "cover"))
    assert res["verified"] is False and res["observed_filename"] == "", res


def test_bad_target_rejected():
    s, _ = session(gh_inputs())
    try:
        asyncio.run(s.upload_resume(None, "/x/a.pdf", "other"))
    except ValueError:
        return
    raise AssertionError("unknown target must raise")


def test_cover_landed_helper_ignores_preexisting_filename():
    before = [{"frame": 0, "i": 0, "text": "Resume/CV cover_letter.pdf",
               "file": "", "id": "resume"}]
    assert not browser._cover_landed_in_resume(before, before, "cover_letter.pdf")


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
