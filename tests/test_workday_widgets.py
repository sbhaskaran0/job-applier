"""Workday widget shapes observed live on Capital One (2026-11-13):

  * segmented dates: role=spinbutton "Month" / "Year" (education: "Year"
    only). read_form never listed them; filled by clicking the part and
    TYPING (focus auto-advances), read back as "4" for "04";
  * School / Field of Study: role=searchbox. type, Enter, results ~3s later,
    click the match, it commits as a chip. Verified only on the chip; the
    first search can return the unfiltered list, so a second one runs;
  * the tenant's own spelling "University of California-Los Angeles";
  * a listbox button whose first click only focuses (Space opens it), and an
    option with an EMPTY accessible name (match by text content).

Pure Python with small fakes (Playwright stubbed); the real DOM is driven
by tests/manual/run_dom_harness.py over workday_widgets_fixture.html.
Standalone -- `python tests/test_workday_widgets.py`.
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

B = browser


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class Widget:
    """One fake widget: `value` is what the shared value JS reads (a
    spinbutton's aria-valuetext, a search box's chip)."""

    def __init__(self, kind="spin", results_by_query=None, commit_unique=False,
                 first_unfiltered=None, expanded=None, click_opens=True,
                 options=()):
        self.kind, self.value, self.typed, self.pressed = kind, "", "", []
        self.results_by_query = results_by_query or {}
        self.commit_unique, self.first_unfiltered = commit_unique, first_unfiltered
        self.results, self.enters, self.clicks = [], 0, 0
        self.expanded, self.click_opens = expanded, click_opens
        self.options, self.picked = list(options), []


class Loc:
    def __init__(self, w, sel=""):
        self.w, self.sel = w, sel

    @property
    def first(self):
        return self

    async def click(self, timeout=None):
        w = self.w
        if "data-jaresult" in self.sel:
            i = int(self.sel.split('"')[1])
            w.value, w.results = w.results[i], []
            return
        if "data-japick" in self.sel:
            w.value = w.picked[-1]
            w.expanded = False
            return
        w.clicks += 1
        if w.expanded is not None and w.click_opens:
            w.expanded = not w.expanded

    async def focus(self):
        return None

    async def fill(self, value):
        raise AssertionError("a spinbutton / searchbox must never be .fill()ed")


class Frame:
    def __init__(self, w):
        self.w = w

    async def evaluate(self, js, arg=None):
        w = self.w
        if js in (B._FIELD_VALUE_JS, B._COMBO_VALUE_JS, B._LISTBOX_VALUE_JS):
            return w.value
        if js is B._RESULTS_JS:
            return list(w.results)
        if js is B._EXPANDED_JS:
            return w.expanded
        if js is B._COMBO_SCOPED_JS:
            return True
        if js is B._COMBO_OPTIONS_JS:
            return [o for o, _ in w.options]
        if js is getattr(B, "_TAG_OPTION_JS", object()):
            hit = [o for o, _ in w.options if o == arg[1]]
            if hit:
                w.picked.append(hit[0])
            return bool(hit)
        return None

    def locator(self, sel):
        return Loc(self.w, sel)

    def get_by_role(self, role, name=None, exact=False):
        w = self.w

        class Opt:
            first = None

            async def click(self, timeout=None):
                # accessible names: an option whose name is empty is never found
                names = {o: (n if n is not None else o) for o, n in w.options}
                if not any(v == name for v in names.values()):
                    raise RuntimeError("no option named %r" % name)
                w.value, w.expanded = name, False
        o = Opt()
        o.first = o
        return o


class Keyboard:
    def __init__(self, w):
        self.w = w

    async def type(self, text, delay=None):
        w = self.w
        w.typed += text
        if w.kind == "spin":
            w.value = str(int(text))            # aria-valuetext drops the 0

    async def press(self, key):
        w = self.w
        w.pressed.append(key)
        if key == "Control+A":
            w.typed = ""
        elif key == "Space" and w.expanded is False:
            w.expanded = True
        elif key == "Enter" and w.kind == "search":
            w.enters += 1
            q = w.typed.strip().lower()
            if w.first_unfiltered is not None and w.enters == 1:
                w.results = list(w.first_unfiltered)
                return
            hits = w.results_by_query.get(q, [])
            if w.commit_unique and len(hits) == 1:
                w.value, w.results = hits[0], []
            else:
                w.results = list(hits)
        elif key == "Escape":
            w.results = []


class Page:
    def __init__(self, w):
        self.keyboard = Keyboard(w)
        self.url = "https://acme.wd5.myworkdayjobs.com/x"

    async def wait_for_timeout(self, ms):
        return None


def session(w, widget, kind="text"):
    s = B.BrowserSession()
    s.tabs[0] = {"page": Page(w), "company": "", "title": "", "url": "",
                 "fields": {0: {"frame": Frame(w), "kind": kind,
                                "native": True, "widget": widget}}}
    s.active_id = 0
    s._SEARCH_WAIT_S = 0.6
    return s


def fill(s, value):
    return asyncio.run(s.fill_field(0, value))


# --------------------------------------------------------------------------
# spinbuttons
# --------------------------------------------------------------------------
def test_scanner_and_verifier_see_spinbuttons_and_searchboxes():
    assert "[role=spinbutton]" in B._SCAN_JS and "[role=searchbox]" in B._SCAN_JS
    # one value-reading code path: the SAME snippet in both
    assert B._WIDGET_VALUE_FN in B._SCAN_JS
    assert B._WIDGET_VALUE_FN in B._FIELD_VALUE_JS
    assert "aria-valuetext" in B._WIDGET_VALUE_FN


def test_spinbutton_is_typed_not_filled_and_reads_back_without_zero():
    w = Widget("spin")
    res = fill(session(w, "spinbutton"), "04")
    assert w.typed == "04" and w.clicks == 1, (w.typed, w.clicks)
    assert res["status"] == "filled" and res["verified"] is True, res


# --------------------------------------------------------------------------
# search boxes
# --------------------------------------------------------------------------
UCLA = "University of California-Los Angeles"


def test_unique_hit_commits_a_chip_in_the_tenant_spelling():
    w = Widget("search", commit_unique=True, results_by_query={
        "university of california, los angeles": [UCLA]})
    res = fill(session(w, "searchbox"), "University of California, Los Angeles")
    assert res["status"] == "filled" and res["verified"] is True, res
    assert res["value"] == UCLA and res["committed_as"] == "pill", res
    assert w.pressed.count("Enter") == 1, w.pressed


def test_pick_from_results_after_an_unfiltered_first_search():
    w = Widget("search", first_unfiltered=["Accounting", "Anthropology", "Art"],
               results_by_query={"economics": ["Agricultural Economics",
                                                "Economics"]})
    res = fill(session(w, "searchbox"), "Economics")
    assert w.pressed.count("Enter") == 2, w.pressed
    assert res["verified"] is True and w.value == "Economics", res


def test_results_with_the_school_in_tenant_spelling_are_picked():
    w = Widget("search", results_by_query={
        "university of california, los angeles": [
            "University of California-Berkeley", UCLA]})
    res = fill(session(w, "searchbox"), "University of California, Los Angeles")
    assert res["verified"] is True and w.value == UCLA, res
    assert res.get("matched_option") == UCLA


def test_typed_text_is_never_verified_without_a_chip():
    # no results and no chip: the typed text alone proves nothing
    w = Widget("search", results_by_query={})
    res = fill(session(w, "searchbox"), "Example U")
    assert res["status"] == "uncommitted" and res["verified"] is False, res
    assert not res.get("options"), res


def test_no_confident_result_is_unmatched_with_the_options():
    w = Widget("search", results_by_query={
        "basket weaving": ["Basketball Studies", "Weaving Arts"]})
    res = fill(session(w, "searchbox"), "Basket Weaving")
    assert res["status"] == "unmatched" and res["verified"] is False, res
    assert res["options"] == ["Basketball Studies", "Weaving Arts"], res
    assert "Escape" in w.pressed and w.typed == "", (w.pressed, w.typed)


def test_match_option_ignores_punctuation_but_not_words():
    m = B.BrowserSession._match_option
    opts = ["University of California-Berkeley", UCLA]
    assert m("University of California, Los Angeles", opts) == UCLA
    # same words in another order are NOT the same name
    assert m("Los Angeles University of California", opts) is None


# --------------------------------------------------------------------------
# listbox quirks
# --------------------------------------------------------------------------
def test_listbox_that_only_focuses_on_click_opens_on_space():
    w = Widget("listbox", expanded=False, click_opens=False,
               options=[("Yes", None), ("No, I have never worked here", None)])
    res = fill(session(w, "listbox", kind="combobox"),
               "No, I have never worked here")
    assert "Space" in w.pressed, w.pressed
    assert res["verified"] is True and w.value.startswith("No, I have"), res


def test_option_with_an_empty_accessible_name_is_picked_by_text():
    w = Widget("listbox", expanded=False,
               options=[("Yes", None), ("No", "")])     # "No" has name ""
    res = fill(session(w, "listbox", kind="combobox"), "No")
    assert res["verified"] is True and w.value == "No", res


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
        except Exception as exc:
            failed += 1
            print("ERROR %s: %s: %s" % (name, type(exc).__name__, exc))
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
