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

# Read-back polls the slot until it verifies. The fakes advance one state
# per SCAN, not per unit of time, so the settle window only has to outlast
# one poll; it is kept far above Windows' ~15ms sleep granularity so the
# gap between "progressbar gone" and "filename shown" can never time out.
browser.BrowserSession._READBACK_POLL_S = 0.001
browser.BrowserSession._READBACK_SETTLE_S = 0.25
browser.BrowserSession._READBACK_MAX_S = 0.6


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
                x.tagged = x.i == arg["i"]
            return 1
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


# --------------------------------------------------------------------------
# Unmounting widget (live Greenhouse, 2026-11-13). On change the widget
# UNMOUNTS the <input type=file>, shows a [role=progressbar] while it POSTs
# the file to storage (~1s), then renders "<filename> [Remove file]" in the
# slot's role=group box. Probe evidence, Stripe embed form:
#   before: group "Resume/CV* Attach Attach Dropbox Enter manually ..." with
#           <input id=resume>; label[for=resume] = "Attach"
#   +100ms: input gone; group = "Resume/CV*" + role=progressbar
#   +1000ms: group = "Resume/CV* resume.pdf" + button "Remove file"
# The old read-back re-matched the slot by the input's id, found nothing and
# returned verified:false, container_text:"" for every upload.
# --------------------------------------------------------------------------
class Slot:
    def __init__(self, n, label, id):
        self.n, self.label, self.id = n, label, id
        self.mounted, self.file, self.busy_left = True, "", 0
        self.box = label + " Attach Attach Dropbox Enter manually"


class UnmountFrame:
    """A frame whose upload widget unmounts the input, like Greenhouse.
    `busy_polls` = how many candidate scans show the progressbar first."""
    def __init__(self, slots, busy_polls=0, redirect_to=None, sticky=None,
                 name_while_busy=False, fail=False):
        self.slots, self.busy_polls = slots, busy_polls
        self.redirect_to, self.sticky = redirect_to, sticky
        self.name_while_busy, self.fail = name_while_busy, fail
        self.uploads, self.tag = [], None

    def _present(self):
        return [s for s in self.slots if s.mounted]

    def locator(self, selector):
        frame = self

        class Loc:
            first = None

            async def set_input_files(self_, p):
                s = frame._present()[frame.tag]
                frame.uploads.append(s.n)
                if frame.redirect_to is not None:
                    s = frame.slots[frame.redirect_to]
                name = p.replace("\\", "/").rsplit("/", 1)[-1]
                s.mounted, s.busy_left = False, frame.busy_polls
                s.box = s.label
                s.pending_name = name
        loc = Loc()
        loc.first = loc
        return loc

    async def evaluate(self, js, arg=None):
        if js is browser._FILE_CANDIDATES_JS:
            out = []
            for i, s in enumerate(self._present()):
                out.append({"i": i, "present": True, "text": "Attach Attach",
                            "single": True, "name": "", "id": s.id,
                            "aria": "", "label": "Attach " + s.label,
                            "slot": str(s.n), "slot_text": s.box,
                            "busy": False, "file": ""})
            for s in self.slots:
                if s.mounted:
                    continue
                name = getattr(s, "pending_name", None)
                err = ""
                if s.busy_left > 0:
                    s.busy_left -= 1
                    if self.name_while_busy:
                        s.box = s.label + " " + name + " 40%"
                elif name and self.sticky is not False:
                    s.box = s.label + " " + name
                    err = "Upload failed" if self.fail else ""
                out.append({"i": -1, "present": False, "text": s.box,
                            "single": True, "name": "", "id": "",
                            "aria": "", "label": s.label, "slot": str(s.n),
                            "slot_text": s.box, "busy": s.busy_left > 0,
                            "slot_error": err, "removable": False,
                            "file": ""})
            return out
        if js is browser._TAG_FILE_JS:
            # resolve by SLOT, like the real JS (position would be stale)
            live = self._present()
            hits = [k for k, s in enumerate(live) if str(s.n) == arg["slot"]]
            self.tag = hits[0] if len(hits) == 1 else None
            return len(hits)
        return None


def gh_session(**kw):
    frame = UnmountFrame([Slot(1, "Resume/CV*", "resume"),
                          Slot(2, "Cover Letter", "cover_letter")], **kw)
    s = browser.BrowserSession()
    s.tabs[0] = {"page": FakePage(frame), "fields": {}, "company": "",
                 "title": "", "url": ""}
    s.active_id = 0
    return s, frame


def test_unmounting_widget_resume_then_cover_both_verify():
    s, frame = gh_session(busy_polls=2)
    r = up(s, "/x/resume.pdf", "resume")
    assert r["status"] == "uploaded" and r["verified"] is True, r
    assert r["readback"] == "container" and r["observed_filename"] == "", r
    assert r["container_text"] == "Resume/CV* resume.pdf", r
    c = up(s, "/x/cover_letter.pdf", "cover")
    assert c["status"] == "uploaded" and c["verified"] is True, c
    assert c["container_text"] == "Cover Letter cover_letter.pdf", c
    assert frame.uploads == [1, 2], frame.uploads
    assert frame.slots[0].box == "Resume/CV* resume.pdf", "resume untouched"


def test_unmounting_widget_still_uploading_is_pending_not_verified():
    s, frame = gh_session(busy_polls=10 ** 6)
    r = up(s, "/x/resume.pdf", "resume")
    assert r["verified"] is False and r.get("pending") is True, r
    assert r["readback"] == "none", r


def test_unmounting_widget_that_never_shows_the_file_is_not_verified():
    s, frame = gh_session(sticky=False)
    r = up(s, "/x/resume.pdf", "resume")
    assert r["verified"] is False and not r.get("pending"), r


def test_resume_never_goes_into_cover_slot_once_resume_slot_is_filled():
    # After the resume upload only the cover <input> is still mounted. The
    # old chooser picked it (score 0 was still the best score).
    s, frame = gh_session()
    up(s, "/x/resume.pdf", "resume")
    r = up(s, "/x/resume_v2.pdf", "resume")
    assert r["status"] == "error" and "already holds a file" in r["error"], r
    assert frame.uploads == [1], "the cover slot must not receive the resume"
    assert frame.slots[1].mounted


def test_second_cover_upload_on_filled_cover_slot_is_error_not_no_cover_slot():
    s, frame = gh_session()
    up(s, "/x/cover_letter.pdf", "cover")
    r = up(s, "/x/cover_letter.pdf", "cover")
    assert r["status"] == "error" and "already holds a file" in r["error"], r
    assert frame.uploads == [2]


def test_cover_landing_in_unmounted_resume_slot_is_flagged():
    s, frame = gh_session(redirect_to=0)
    r = up(s, "/x/cover_letter.pdf", "cover")
    assert r["status"] == "error", r
    assert r["error"] == "cover upload landed in resume slot", r
    assert r["verified"] is False


def test_resume_not_put_in_other_attachment_slot_when_resume_slot_filled():
    s, frame = gh_session()
    frame.slots.append(Slot(3, "Additional documents", "other"))
    up(s, "/x/resume.pdf", "resume")
    r = up(s, "/x/resume_v2.pdf", "resume")
    assert r["status"] == "error", r
    assert frame.uploads == [1]


def test_chooser_skips_unmounted_slots_and_counts_them_in_total():
    gone = dict(cand(-1, text="Resume/CV* resume.pdf", label="Resume/CV*"),
                present=False)
    box_only = cand(0, text="Cover Letter Attach")
    # container-only cover detection needs >= 2 slots; the filled one counts
    assert choose([gone, box_only], "cover") == 0
    assert choose([gone], "resume") is None
    assert choose([gone], "cover") is None


def test_resume_target_never_picks_a_cover_labelled_input():
    assert choose([cand(0, **COVER)], "resume") is None
    assert choose([cand(0, label="Cover Letter")], "resume") is None
    # a lone input whose box merely mentions cover letters is still usable
    assert choose([cand(0, text="Upload resume or cover letter")], "resume") == 0


def test_slot_tag_is_the_primary_key_and_id_the_fallback():
    assert browser._slot_key({"slot": "2", "id": "a", "frame": 1}) == \
        ("slot", 1, "2")
    ref = {"slot": "9", "id": "resume", "i": 0}
    fresh = [{"slot": "4", "id": "resume", "i": 0, "file": "r.pdf"}]
    assert browser._match_slot(fresh, ref) is fresh[0]
    assert browser._match_slot([{"slot": "4", "id": "", "i": -1}], ref) is None


def test_filename_must_appear_as_a_whole_token():
    assert browser._shows({"slot_text": "Resume/CV* resume.pdf"}, "resume.pdf")
    assert not browser._shows({"slot_text": "Resume/CV* my_resume.pdf"},
                              "resume.pdf")
    assert not browser._shows({"slot_text": "resume.pdf.bak"}, "resume.pdf")


# Review follow-ups --------------------------------------------------------
def test_gap_between_progressbar_and_filename_is_waited_out_every_time():
    # busy for 2 scans, then ONE scan with neither progressbar nor filename,
    # then the filename. The settle window restarts on each busy scan.
    for _ in range(25):
        s, frame = gh_session(busy_polls=2)
        assert up(s, "/x/resume.pdf", "resume")["verified"] is True
        assert up(s, "/x/cover_letter.pdf", "cover")["verified"] is True


def test_filename_shown_while_still_busy_is_not_accepted_early():
    s, frame = gh_session(busy_polls=10 ** 6, name_while_busy=True)
    r = up(s, "/x/resume.pdf", "resume")
    assert frame.slots[0].box == "Resume/CV* resume.pdf 40%"
    assert r["verified"] is False and r.get("pending") is True, r
    s, frame = gh_session(busy_polls=3, name_while_busy=True)
    r = up(s, "/x/resume.pdf", "resume")
    assert r["verified"] is True and frame.slots[0].busy_left == 0, \
        "verified only once the progressbar is gone: %r" % r


def test_filename_next_to_an_upload_error_is_not_verified():
    s, frame = gh_session(fail=True)
    r = up(s, "/x/resume.pdf", "resume")
    assert r["verified"] is False and r["slot_error"] == "Upload failed", r
    assert browser._verify_upload(
        "a.pdf", {}, {"slot_text": "a.pdf", "slot_error": "aria-invalid"}
    )["verified"] is False


def test_index_resume_into_a_cover_input_is_refused():
    s, frame = session(gh_inputs())
    r = up(s, "/x/resume.pdf", "resume", index=1)
    assert r["status"] == "error" and "cover-letter slot" in r["error"], r
    assert frame.uploads == []
    r = up(s, "/x/resume.pdf", "resume", index=0)
    assert r["status"] == "uploaded" and r["verified"] is True, r


def test_explicit_index_cover_counts_unmounted_slots_in_total():
    # container-only cover signal, resume slot already unmounted: the
    # describe JS reports total = slots (2), not mounted inputs (1)
    d = dict(cand(0, text="Cover Letter Attach"), total=2)
    assert browser._is_cover_candidate(d, d["total"])


def test_prefilled_resume_slot_blocks_resume_going_to_portfolio():
    # The resume slot was filled before the tool's first scan: the JS now
    # emits it (untagged role=group named Resume/CV, no file input).
    s, frame = gh_session()
    frame.slots[0].mounted = False
    frame.slots[0].box = "Resume/CV* old_resume.pdf"
    frame.slots.append(Slot(3, "Portfolio", "portfolio"))
    r = up(s, "/x/resume.pdf", "resume")
    assert r["status"] == "error" and "already holds a file" in r["error"], r
    assert frame.uploads == []


def test_resume_slot_without_input_and_without_file_still_blocks_other_slots():
    s, frame = gh_session()
    frame.slots[0].mounted = False
    frame.slots[0].box = "Resume/CV* Paste your resume"
    frame.slots[1] = Slot(3, "Portfolio", "portfolio")
    r = up(s, "/x/resume.pdf", "resume")
    assert r["status"] == "error" and "no other attachment slot" in r["error"], r
    assert frame.uploads == []


def test_container_only_cover_input_never_gets_the_resume_even_when_alone():
    assert choose([cand(0, text="Cover Letter Attach")], "resume") is None
    s, frame = session([FakeInput(0, "Cover Letter Attach")])
    r = up(s, "/x/resume.pdf", "resume")
    assert r["status"] == "error" and "cover-letter slots" in r["error"], r
    assert frame.uploads == []


def test_enter_manually_cover_slot_is_no_cover_slot_not_filled():
    s, frame = gh_session()
    frame.slots[1].mounted = False          # input swapped for a textarea
    frame.slots[1].box = "Cover Letter"
    r = up(s, "/x/cover_letter.pdf", "cover")
    assert r["status"] == "no_cover_slot", r
    assert frame.uploads == []


def test_slot_that_rerendered_between_scan_and_upload_aborts():
    s, frame = gh_session()
    orig = frame.evaluate

    async def ev(js, arg=None):
        if js is browser._TAG_FILE_JS:
            return 0
        return await orig(js, arg)
    frame.evaluate = ev
    r = up(s, "/x/resume.pdf", "resume")
    assert r["status"] == "error" and "re-rendered" in r["error"], r
    assert frame.uploads == []


def test_a_failing_scan_is_retried_not_fatal():
    s, frame = gh_session(busy_polls=1)
    orig, calls = frame.evaluate, []

    async def ev(js, arg=None):
        if js is browser._FILE_CANDIDATES_JS and frame.uploads:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("Execution context was destroyed")
        return await orig(js, arg)
    frame.evaluate = ev
    r = up(s, "/x/resume.pdf", "resume")
    assert r["verified"] is True and "readback_error" not in r, r


def test_resume_slot_text_mentioning_resume_inside_a_cover_box():
    assert not browser._is_resume_slot(
        {"text": "Cover letter (in addition to your resume)"})
    assert browser._is_resume_slot({"text": "Resume/CV* r.pdf"})


def test_filename_whitespace_is_collapsed_like_the_box_text():
    assert browser._shows({"slot_text": "Resume/CV* my resume.pdf"},
                          "my  resume.pdf")


def test_looks_filled_needs_a_filename_or_remove_button():
    assert browser._looks_filled({"slot_text": "Resume/CV* resume.pdf"})
    assert browser._looks_filled({"slot_text": "Resume/CV*", "removable": True})
    assert not browser._looks_filled(
        {"slot_text": "Cover Letter Accepted file types: pdf, doc, docx"})


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
