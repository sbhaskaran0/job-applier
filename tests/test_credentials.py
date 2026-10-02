"""Workday security foundations: passwords never reach the transcript or disk.

The user supplies ONE shared tenant password in a local-only credentials.yaml;
the agent never sees or generates it. These tests pin:

  * a sensitive field verifies on PRESENCE and never echoes sent/observed values;
  * the generic fill tools refuse a sensitive target (use_fill_credential);
  * capture_submission drops password fields from history AND the applications log;
  * save_answer refuses a password label;
  * fill_credential: no file -> no_credentials; with a temp fake file -> fills via
    a stubbed locator and the result never contains the password string.

Playwright (and the MCP SDK, when absent) are stubbed; only fake passwords are used.
Standalone -- `python tests/test_credentials.py`.
"""

import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_pw = types.ModuleType("playwright")
_pw_async = types.ModuleType("playwright.async_api")
_pw_async.async_playwright = lambda: None
sys.modules.setdefault("playwright", _pw)
sys.modules.setdefault("playwright.async_api", _pw_async)

try:
    import mcp.server.mcpserver  # noqa: F401
except Exception:  # CI installs only pyyaml/httpx
    _m = types.ModuleType("mcp")
    _ms = types.ModuleType("mcp.server")
    _mm = types.ModuleType("mcp.server.mcpserver")

    class _FakeMCP:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            return lambda fn: fn

    _mm.MCPServer = _FakeMCP
    sys.modules["mcp"], sys.modules["mcp.server"] = _m, _ms
    sys.modules["mcp.server.mcpserver"] = _mm

from src import browser, config, data, mcp_server  # noqa: E402

FAKE_PW = "fake-pw-123"


class FakeLocator:
    def __init__(self, el):
        self.el = el

    async def fill(self, value):
        self.el["value"] = value


class FakeFrame:
    """Mimics the JS readers: a password element reads back masked."""

    def __init__(self, el):
        self.el = el

    async def evaluate(self, js, arg=None):
        if js in (browser._FIELD_VALUE_JS, browser._COMBO_VALUE_JS):
            if self.el["password"]:
                return "********" if self.el["value"] else ""
            return self.el["value"]
        return None

    def locator(self, selector):
        return FakeLocator(self.el)


def make_session(*, password, el=None):
    el = el or {"value": "", "password": password}
    s = browser.BrowserSession()
    s.tabs[0] = {"page": None, "company": "", "title": "", "url": "",
                 "fields": {0: {"frame": FakeFrame(el), "kind": "text",
                                "native": True, "widget": None,
                                "sensitive": password}}}
    s.active_id = 0
    return s, el


# --- result masking / verification ---------------------------------------
def test_sensitive_result_is_presence_verified_and_masked():
    res = browser._fill_result(0, "text", FAKE_PW, "********", sensitive=True)
    assert res["verified"] is True and res["sensitive"] is True, res
    assert FAKE_PW not in json.dumps(res), res


def test_sensitive_empty_readback_is_uncommitted_without_leak():
    res = browser._fill_result(0, "text", FAKE_PW, "", sensitive=True)
    assert res["verified"] is False and res["status"] == "uncommitted", res
    assert FAKE_PW not in json.dumps(res), res


def test_js_value_readers_mask_passwords():
    for js in (browser._FIELD_VALUE_JS, browser._COMBO_VALUE_JS,
               browser._LISTBOX_VALUE_JS):
        assert "type === 'password'" in js and "********" in js
    assert "sensitive" in browser._SCAN_JS and "********" in browser._SCAN_JS


# --- generic fill refuses ---------------------------------------------------
def test_fill_field_refuses_sensitive_target():
    s, el = make_session(password=True)
    res = asyncio.run(s.fill_field(0, FAKE_PW))
    assert res["status"] == "use_fill_credential", res
    assert el["value"] == "", "nothing may be typed"
    assert FAKE_PW not in json.dumps(res)


def test_fill_many_refuses_sensitive_target():
    s, el = make_session(password=True)
    out = asyncio.run(s.fill_many([{"index": 0, "value": FAKE_PW}]))
    assert out[0]["status"] == "use_fill_credential", out
    assert FAKE_PW not in json.dumps(out)


# --- capture / save_answer --------------------------------------------------
def test_capture_submission_drops_password_from_both_stores():
    history: list = []
    logged: list = []
    patches = {
        (config, "load_history"): lambda: history,
        (config, "save_history"): lambda h: None,
        (config, "load_user_profile"): lambda: {},
        (data, "log_application_record"):
            lambda **kw: (logged.append(kw), {"applications_count": 1})[1],
    }
    orig = {k: getattr(*k) for k in patches}
    for (obj, name), fn in patches.items():
        setattr(obj, name, fn)
    try:
        fields = [
            {"label": "Password", "kind": "text", "current_value": FAKE_PW,
             "sensitive": True},
            {"label": "Verify new password", "kind": "text",
             "current_value": FAKE_PW},          # label-only belt-and-braces
            {"label": "Favourite colour", "kind": "text",
             "current_value": "green"},
        ]
        data.capture_submission(fields, company="Acme", job_title="PM",
                                url="https://x.test/1")
    finally:
        for (obj, name), fn in orig.items():
            setattr(obj, name, fn)
    blob = json.dumps([history, logged])
    assert FAKE_PW not in blob, blob
    assert "green" in blob, "non-secret answers must still be captured"


def test_save_answer_refuses_password_label():
    called = []
    real_load, real_save = config.load_history, config.save_history
    config.load_history = lambda: called.append("load") or []
    config.save_history = lambda h: called.append("save")
    try:
        res = data.save_answer("Create Password", FAKE_PW)
    finally:
        config.load_history, config.save_history = real_load, real_save
    assert res["status"] == "error" and not called, (res, called)
    assert FAKE_PW not in json.dumps(res)


# --- fill_credential --------------------------------------------------------
class _FakeProfile:
    def __init__(self, root: Path):
        self.credentials_yaml = root / "secrets" / "credentials.yaml"

    def __getattr__(self, name):
        raise FileNotFoundError(name)


def _with_profile(root: Path, fn):
    import src.profiles as profiles
    real = profiles.active
    prof = _FakeProfile(root)
    prof.profile_yaml = root / "profile.yaml"   # absent -> email fallback skipped
    profiles.active = lambda: prof
    try:
        return fn()
    finally:
        profiles.active = real


def test_load_credentials_missing_file_is_empty():
    with tempfile.TemporaryDirectory() as d:
        assert _with_profile(Path(d), config.load_credentials) == {}


def test_fill_credential_no_credentials():
    s, el = make_session(password=True)
    real = browser.session
    browser.session = s
    try:
        with tempfile.TemporaryDirectory() as d:
            res = _with_profile(Path(d), lambda: asyncio.run(
                mcp_server.fill_credential(0, "password")))
    finally:
        browser.session = real
    assert res["status"] == "no_credentials" and "hint" in res, res
    assert el["value"] == ""


def _run_fill_credential(password_field, field="password"):
    s, el = make_session(password=password_field)
    real = browser.session
    browser.session = s
    try:
        with tempfile.TemporaryDirectory() as d:
            sec = Path(d) / "secrets"
            sec.mkdir()
            (sec / "credentials.yaml").write_text(
                'workday:\n  email: "me@example.test"\n  password: "%s"\n' % FAKE_PW,
                encoding="utf-8")
            res = _with_profile(Path(d), lambda: asyncio.run(
                mcp_server.fill_credential(0, field)))
    finally:
        browser.session = real
    return res, el


def test_fill_credential_fills_server_side_without_echo():
    res, el = _run_fill_credential(True)
    assert el["value"] == FAKE_PW, "the password must actually be typed"
    assert res == {"index": 0, "status": "filled", "verified": True,
                   "sensitive": True}, res
    assert FAKE_PW not in json.dumps(res), res


def test_fill_credential_refuses_password_into_visible_text_box():
    res, el = _run_fill_credential(False)
    assert res["status"] == "not_a_password_field", res
    assert el["value"] == "", "password must not be typed into a text box"


def test_fill_credential_email_may_be_filled_into_text_field():
    res, el = _run_fill_credential(False, field="email")
    assert el["value"] == "me@example.test" and res["status"] == "filled", res


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
