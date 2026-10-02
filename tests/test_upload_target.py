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


def cand(i, text="", name="", id="", aria="", label="", file="", single=True):
    return {"i": i, "text": text, "name": name, "id": id, "aria": aria,
            "label": label, "file": file, "single": single}


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


def test_container_text_alone_needs_two_inputs_and_a_single_box():
    # Fix 2: container-only cover detection is the guarded exception.
    box = dict(text="Cover Letter Attach")
    assert choose([cand(0, **box), cand(1, text="Resume Attach")], "cover") == 0
    # lone input near a cover-letter label: never a cover slot
    assert choose([cand(0, **box)], "cover") is None
    # box spans more than one file input: not trustworthy
    assert choose([cand(0, single=False, **box), cand(1, text="Resume")],
                  "cover") is None
    # container mentions resume too: not trustworthy
    assert choose([cand(0, text="Resume Cover Letter"), cand(1)], "cover") is None


def test_own_attribute_match_beats_container_only_match():
    cs = [cand(0, text="Cover Letter Attach"), cand(1, id="cover_letter"),
          cand(2, text="Resume")]
    assert choose(cs, "cover") == 1


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
                "single": True,
                "name": self.kw.get("name", ""), "id": self.kw.get("id", ""),
                "aria": "", "label": self.kw.get("label", "")}


class FakeLoc:
    def __init__(self, frame, inp=None):
        self.frame = frame
        self.inp = inp
        self.first = self

    def _target(self):
        return self.inp or next(x for x in self.frame.inputs if x.tagged)

    async def set_input_files(self, p):
        inp = self._target()
        self.frame.uploads.append(inp.i)
        if self.frame.redirect_to is not None:       # swallowing widget
            inp = self.frame.inputs[self.frame.redirect_to]
        name = p.replace("\\", "/").rsplit("/", 1)[-1]
        if self.frame.rerender:
            # React dropzone: node replaced -> the new input holds no file,
            # the filename is rendered in the box text instead.
            inp.file = ""
        else:
            inp.file = name
        inp.text = inp.text + " " + name

    async def evaluate(self, js):
        assert js is browser._FILE_DESCRIBE_JS
        d = self._target().descriptor()
        d["total"] = len(self.frame.inputs)
        return d


class FakeFrame:
    def __init__(self, inputs, redirect_to=None, rerender=False):
        self.inputs = inputs
        self.redirect_to = redirect_to
        self.rerender = rerender
        self.uploads = []

    def locator(self, selector):
        if selector.startswith("[data-jaidx="):
            return FakeLoc(self, self.inputs[int(selector.split('"')[1])])
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


def session(inputs, redirect_to=None, rerender=False):
    frame = FakeFrame(inputs, redirect_to, rerender)
    s = browser.BrowserSession()
    s.tabs[0] = {"page": FakePage(frame), "fields": {}, "company": "",
                 "title": "", "url": ""}
    s.active_id = 0
    # read_form indexes: map field index N -> file input N (test shorthand).
    s.tabs[0]["fields"].update({n: {"frame": frame, "kind": "file"}
                                for n in range(len(inputs))})
    return s, frame


def gh_inputs():
    return [FakeInput(0, "Resume/CV Attach", id="resume"),
            FakeInput(1, "Cover Letter Attach", id="cover_letter")]


def up(s, path, target="resume", index=None):
    return asyncio.run(s.upload_resume(index, path, target))


# --------------------------------------------------------------------------
def test_cover_upload_goes_to_cover_and_resume_untouched():
    s, frame = session(gh_inputs())
    res = up(s, "/x/cover_letter.pdf", "cover")
    assert res["status"] == "uploaded", res
    assert res["verified"] is True and res["observed_filename"] == "cover_letter.pdf", res
    assert frame.inputs[1].file == "cover_letter.pdf"
    assert frame.inputs[0].file == "", "resume slot must be untouched"
    assert not any(x.tagged for x in frame.inputs), "tag must be cleaned up"


def test_cover_upload_on_resume_only_page_is_no_cover_slot_and_uploads_nothing():
    s, frame = session([FakeInput(0, "Resume/CV Attach", id="resume")])
    res = up(s, "/x/cover_letter.pdf", "cover")
    assert res["status"] == "no_cover_slot", res
    assert frame.uploads == [], "must NEVER fall back to the resume slot"


def test_resume_upload_default_target_hits_resume_slot():
    s, frame = session(gh_inputs())
    res = up(s, "/x/resume.pdf")
    assert res["status"] == "uploaded" and res["verified"] is True, res
    assert frame.inputs[0].file == "resume.pdf" and frame.inputs[1].file == ""


def test_cover_landing_in_resume_slot_is_flagged_as_error():
    s, frame = session(gh_inputs(), redirect_to=0)
    res = up(s, "/x/cover_letter.pdf", "cover")
    assert res["status"] == "error", res
    assert res["error"] == "cover upload landed in resume slot", res
    assert res["verified"] is False


# Fix 1: explicit index + cover ---------------------------------------------
def test_index_cover_on_resume_input_is_refused_and_uploads_nothing():
    s, frame = session(gh_inputs())
    res = up(s, "/x/cover_letter.pdf", "cover", index=0)
    assert res == {"index": 0, "status": "error", "target": "cover",
                   "error": "index is not a cover slot"}, res
    assert frame.uploads == []


def test_index_cover_on_cover_input_uploads_and_verifies():
    s, frame = session(gh_inputs())
    res = up(s, "/x/cover_letter.pdf", "cover", index=1)
    assert res["status"] == "uploaded" and res["verified"] is True, res
    assert frame.inputs[0].file == ""


def test_index_cover_on_lone_unlabelled_input_is_refused():
    s, frame = session([FakeInput(0, "Attach")])
    res = up(s, "/x/cover_letter.pdf", "cover", index=0)
    assert res["error"] == "index is not a cover slot", res
    assert frame.uploads == []


# Fix 4: verified needs files[0].name ---------------------------------------
def test_rerendered_dropzone_verifies_via_box_text_when_files_empty():
    s, frame = session(gh_inputs(), rerender=True)
    res = up(s, "/x/cover_letter.pdf", "cover")
    assert res["status"] == "uploaded", res
    assert res["verified"] is True and res["observed_filename"] == "", res


def test_files_empty_and_filename_already_in_box_is_not_verified():
    inputs = gh_inputs()
    inputs[1].text += " cover_letter.pdf"     # stale text from an earlier attach
    s, _ = session(inputs, rerender=True)
    res = up(s, "/x/cover_letter.pdf", "cover")
    assert res["verified"] is False, res


def test_wrong_files_name_is_not_verified_even_if_text_matches():
    v = browser._verify_upload("a.pdf", {"text": ""},
                               {"file": "other.pdf", "text": "a.pdf"})
    assert v["verified"] is False, v


def test_nothing_observed_is_not_verified():
    class Silent(FakeLoc):
        async def set_input_files(self, p):
            return None
    s, frame = session(gh_inputs())
    frame.locator = lambda sel: Silent(frame)
    res = up(s, "/x/cover_letter.pdf", "cover")
    assert res["verified"] is False and res["observed_filename"] == "", res


# Fix 5: slot identity ------------------------------------------------------
def test_cover_landed_keys_by_id_not_position():
    before = [{"frame": 0, "i": 0, "id": "resume", "text": "Resume", "file": ""},
              {"frame": 0, "i": 1, "id": "cover", "text": "Cover", "file": ""}]
    # An input was inserted ahead: positions shift. The RESUME slot (id) is
    # now i=1 and shows nothing new; the cover slot holds the file. Position
    # keying would blame the resume slot.
    after = [{"frame": 0, "i": 0, "id": "cover", "text": "Cover c.pdf",
              "file": "c.pdf"},
             {"frame": 0, "i": 1, "id": "resume", "text": "Resume", "file": ""}]
    assert not browser._cover_landed_in_resume(before, after, "c.pdf")
    after[1]["file"] = "c.pdf"
    assert browser._cover_landed_in_resume(before, after, "c.pdf")


def test_slot_key_falls_back_to_position_only_without_id_or_name():
    assert browser._slot_key({"id": "a", "name": "b", "i": 3}) == ("id", "a")
    assert browser._slot_key({"name": "b", "i": 3}) == ("name", "b")
    assert browser._slot_key({"i": 3, "frame": 1}) == ("pos", 1, 3)


def test_cover_landed_helper_ignores_preexisting_filename():
    before = [{"frame": 0, "i": 0, "text": "Resume/CV cover_letter.pdf",
               "file": "", "id": "resume"}]
    assert not browser._cover_landed_in_resume(before, before, "cover_letter.pdf")


# Fix 6: errors are dicts ---------------------------------------------------
def test_errors_come_back_as_dicts_never_raise():
    s, _ = session(gh_inputs())
    r = up(s, "/x/a.pdf", "other")
    assert r["status"] == "error" and "target" in r["error"], r
    r = up(s, None, "cover")
    assert r["status"] == "error" and "path" in r["error"], r
    r = up(s, "/x/a.pdf", "cover", index=99)
    assert r["status"] == "error" and "Unknown field index" in r["error"], r
    s2, _ = session([])
    r = up(s2, "/x/a.pdf", "resume")
    assert r["status"] == "error" and "No file input" in r["error"], r


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
