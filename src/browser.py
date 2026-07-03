"""ATS-agnostic browser layer (async Playwright).

Holds one live browser session for the MCP server. The key primitive is
`read_form()`, which reads the *live* page (and all iframes) and returns a
generic, structured list of every fillable field — derived purely from the DOM
and accessibility attributes, with NO per-site selectors. Claude reasons over
that list and fills fields by index. This is what makes it work on any ATS
(Greenhouse, Lever, Ashby, Workday, ...) without predefined rules.
"""

from urllib.parse import urlparse

from playwright.async_api import async_playwright

from . import config

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# JS run inside each frame: tags every fillable element with data-jaidx and
# returns a descriptor for each. Label resolution walks the standard
# accessibility fallbacks. Radios/checkboxes are reported individually with
# their group `name` so Claude can pick the right option.
_SCAN_JS = r"""
(startIndex) => {
  const isVisible = (el) => {
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const labelFor = (el) => {
    if (el.id) {
      try { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (l) return clean(l.innerText); } catch (e) {}
    }
    const wrap = el.closest('label');
    if (wrap) return clean(wrap.innerText);
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map(id => {
        const e = document.getElementById(id); return e ? e.innerText : ''; }).join(' ');
      if (clean(t)) return clean(t);
    }
    if (el.getAttribute('placeholder')) return clean(el.getAttribute('placeholder'));
    const cont = el.closest('div,section,fieldset,li,td');
    if (cont) { const lab = cont.querySelector('label'); if (lab) return clean(lab.innerText); }
    return clean((el.getAttribute('name') || el.id || '').replace(/[_\-]+/g, ' '));
  };
  const sel = 'input, textarea, select, [role=textbox], [role=combobox], [contenteditable=true]';
  const els = Array.from(document.querySelectorAll(sel));
  const out = [];
  let idx = startIndex;
  for (const el of els) {
    const tag = el.tagName.toLowerCase();
    const itype = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && ['hidden', 'submit', 'button', 'reset', 'image'].includes(itype)) continue;
    if (el.disabled || el.readOnly) continue;
    if (!isVisible(el)) continue;

    let kind;
    if (tag === 'select') kind = 'select';
    else if (tag === 'textarea') kind = 'text';
    else if (itype === 'file') kind = 'file';
    else if (itype === 'radio') kind = 'radio';
    else if (itype === 'checkbox') kind = 'checkbox';
    else if (el.getAttribute('role') === 'combobox') kind = 'combobox';
    else kind = 'text';

    let options = null;
    if (kind === 'select') {
      options = Array.from(el.options).map(o => ({ label: clean(o.textContent), value: o.value }));
    }
    const required = el.required || el.getAttribute('aria-required') === 'true' || undefined;
    let value = '';
    if (kind === 'checkbox' || kind === 'radio') value = el.checked ? 'checked' : '';
    else value = el.value || '';

    el.setAttribute('data-jaidx', String(idx));
    out.push({
      index: idx, kind, label: labelFor(el),
      group: (kind === 'radio' || kind === 'checkbox') ? clean(el.getAttribute('name')) : undefined,
      option_value: (kind === 'radio' || kind === 'checkbox') ? (el.value || '') : undefined,
      options, required, current_value: value,
    });
    idx++;
  }

  // --- Second pass: button-group choice controls (e.g. Ashby Yes/No booleans
  // and segmented pickers). These render as a group of sibling <button> (or
  // [role=radio]) elements backed by a HIDDEN <input>, so the native-input
  // pass above skips them entirely. We only treat a button group as a field
  // when it looks like a real form control — it has a backing hidden
  // input[type=checkbox|radio] or sits inside a [data-field-path]/[role=radiogroup]
  // wrapper — so page tabs, the submit button, and the resume dropzone are
  // never captured.
  const GBAD = new Set(['submit', 'submit application', 'apply', 'upload file',
    'upload', 'replace', 'browse', 'remove', 'delete', 'next', 'back',
    'previous', 'continue', 'save', 'cancel', 'add', 'edit', '+', '×', 'x']);
  const optSel = 'button, [role=radio]';
  const seenParents = new Set();
  for (const b of Array.from(document.querySelectorAll(optSel))) {
    if (b.hasAttribute('data-jaidx')) continue;
    const parent = b.parentElement;
    if (!parent || seenParents.has(parent)) continue;
    const sibs = Array.from(parent.children).filter(c =>
      (c.tagName === 'BUTTON' || c.getAttribute('role') === 'radio') &&
      clean(c.innerText).length > 0 && clean(c.innerText).length <= 40 &&
      (c.getAttribute('type') || '').toLowerCase() !== 'submit');
    if (sibs.length < 2 || sibs.length > 6) continue;
    if (sibs.some(s => GBAD.has(clean(s.innerText).toLowerCase()))) continue;
    if (parent.querySelector('input[type=file]')) continue;   // resume dropzone
    // Must look like a real form field, not a tab strip / button toolbar:
    const backing = parent.querySelector('input[type=checkbox], input[type=radio]');
    const wrapper = parent.closest('[data-field-path], [role=radiogroup]');
    if (!backing && !wrapper) continue;
    seenParents.add(parent);

    const entry = parent.closest('[data-field-path]') ||
                  parent.closest('div, section, fieldset, li');
    let glabel = '';
    if (entry) { const lab = entry.querySelector('label'); if (lab) glabel = clean(lab.innerText); }
    if (!glabel) glabel = labelFor(parent);
    const req = entry
      ? /required/i.test((entry.className || '') + ' ' +
          ((entry.querySelector('label') || {}).className || '')) || undefined
      : undefined;
    const gname = 'btngroup_' + idx;
    for (const opt of sibs) {
      const t = clean(opt.innerText);
      const selected = opt.getAttribute('aria-checked') === 'true' ||
        opt.getAttribute('aria-pressed') === 'true' ||
        /(selected|active|checked)/i.test(opt.className);
      opt.setAttribute('data-jaidx', String(idx));
      out.push({
        index: idx, kind: 'radio', label: glabel || t,
        group: gname, option_value: t, native: false,
        options: null, required: req,
        current_value: selected ? 'checked' : '',
      });
      idx++;
    }
  }
  return out;
}
"""


# JS run in the main document to spot human-verification walls.
_BLOCKER_JS = r"""
() => {
  const found = [];
  const has = (s) => !!document.querySelector(s);
  if (has('.g-recaptcha') || has('iframe[src*="recaptcha"]')) found.push('reCAPTCHA');
  if (has('.h-captcha') || has('iframe[src*="hcaptcha"]')) found.push('hCaptcha');
  if (has('.cf-turnstile') || has('iframe[src*="challenges.cloudflare.com"]'))
    found.push('Cloudflare Turnstile');
  const t = ((document.body && document.body.innerText) || '').toLowerCase();
  const phrases = ['verify you are human', 'verifying you are human',
    "i'm not a robot", 'are you a robot', 'checking your browser',
    'complete the captcha', 'press and hold', 'security check'];
  for (const p of phrases) { if (t.includes(p)) { found.push('verification text: ' + p); break; } }
  return found;
}
"""


class BrowserSession:
    def __init__(self) -> None:
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        # index -> {"frame": Frame, "kind": str}
        self.fields: dict[int, dict] = {}

    async def _ensure(self, headless: bool = False) -> None:
        # Default is a VISIBLE, maximized window so the user can watch and step
        # in (e.g. to solve a CAPTCHA or log in). headless=True is only used by
        # automated tests.
        if self._pw is None:
            self._pw = await async_playwright().start()
        if self.browser is None:
            self.browser = await self._pw.chromium.launch(
                headless=headless, args=["--start-maximized"])
            self.context = await self.browser.new_context(
                user_agent=_UA, no_viewport=True)
            self.page = await self.context.new_page()

    async def open_job(self, url: str) -> dict:
        # Start of an apply session: refresh resume.txt from resume.pdf so the
        # reasoning text matches the document that gets uploaded.
        resume_synced = config.sync_resume_text_from_pdf()
        await self._ensure()
        await self.page.goto(url, timeout=45000, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2500)  # let JS-rendered forms settle
        self.fields.clear()
        host = urlparse(url).hostname or ""
        ats = "greenhouse" if "greenhouse" in host else \
              "lever" if "lever" in host else \
              "ashby" if "ashby" in host else \
              "workday" if "workday" in host else "generic"
        title = await self.page.title()
        return {"url": url, "title": title, "detected_ats": ats,
                "resume_synced": resume_synced,
                "note": "Call read_form() to list the form fields."}

    async def read_form(self) -> list[dict]:
        """Read the live page + all iframes; return a generic field list.

        Side effect: tags elements with data-jaidx so fill_field/upload_resume
        can act on them. Re-call after any navigation or page reload.
        """
        if self.page is None:
            raise RuntimeError("No page open. Call open_job(url) first.")
        self.fields.clear()
        all_fields: list[dict] = []
        counter = 0
        for frame in self.page.frames:
            try:
                descriptors = await frame.evaluate(_SCAN_JS, counter)
            except Exception:
                continue
            for d in descriptors:
                self.fields[d["index"]] = {"frame": frame, "kind": d["kind"],
                                           "native": d.get("native", True)}
                # drop None/undefined keys for a clean payload
                all_fields.append({k: v for k, v in d.items() if v is not None})
                counter = max(counter, d["index"] + 1)
        return all_fields

    def _locator(self, index: int):
        meta = self.fields.get(index)
        if not meta:
            raise ValueError(f"Unknown field index {index}. Call read_form() first.")
        return meta["frame"].locator(f'[data-jaidx="{index}"]'), meta["kind"]

    async def fill_field(self, index: int, value: str) -> dict:
        """Fill any field by index. Dispatches on the field kind:
        text/combobox -> type; select -> choose option; radio/checkbox -> check.
        For radio/checkbox, pass "true"/"yes" to check or "false"/"no" to uncheck.
        """
        loc, kind = self._locator(index)
        if kind == "select":
            try:
                await loc.select_option(label=value)
            except Exception:
                await loc.select_option(value=value)
        elif kind in ("radio", "checkbox"):
            # Non-native option (a <button>/[role=radio] in a segmented control,
            # e.g. Ashby Yes/No): the index already points at the specific option,
            # so selecting it is just a click — .check() would fail on a <button>.
            if not self.fields[index].get("native", True):
                await loc.click()
            else:
                check = str(value).strip().lower() in ("true", "yes", "1", "checked", "on")
                if check:
                    await loc.check()
                else:
                    await loc.uncheck()
        elif kind == "combobox":
            # React-select style widget (common on Greenhouse/Ashby/Lever):
            # open it, type to filter, then click the matching option (fall
            # back to Enter). Typing goes to whichever input the widget focuses.
            frame = self.fields[index]["frame"]
            await loc.click()
            await self.page.wait_for_timeout(200)
            await self.page.keyboard.type(value, delay=20)
            await self.page.wait_for_timeout(500)
            option = frame.get_by_role("option", name=value, exact=False).first
            try:
                await option.click(timeout=1500)
            except Exception:
                await self.page.keyboard.press("Enter")
        else:  # text / textarea / contenteditable
            await loc.fill(value)
        return {"index": index, "kind": kind, "status": "filled", "value": value}

    async def upload_resume(self, index: int | None = None,
                            path: str | None = None) -> dict:
        """Attach the resume. With an explicit `index`, use that field. Without
        one, auto-locate a file input anywhere on the page — **even if hidden**
        (Greenhouse/Ashby "Attach" widgets back their dropzone with a hidden
        <input type=file>, which read_form doesn't list) — preferring a
        resume/CV input over a cover-letter one."""
        resume_path = path or str(config.resume_upload_path())
        if index is not None and index >= 0:
            loc, _ = self._locator(index)
            await loc.set_input_files(resume_path)
            return {"index": index, "status": "uploaded", "path": resume_path}

        pick_js = r"""
        (hint) => {
          const inputs = Array.from(document.querySelectorAll('input[type=file]'));
          if (!inputs.length) return -1;
          const h = (hint || '').toLowerCase();
          const score = (el) => {
            const c = el.closest('div,section,fieldset,li') || el.parentElement;
            const t = (((c && c.innerText) || '') + ' ' + (el.name || '') + ' ' + (el.id || '')).toLowerCase();
            if (t.includes('cover')) return 0;          // deprioritize cover letter
            if (t.includes(h) || t.includes('cv')) return 2;
            return 1;
          };
          inputs.forEach(el => el.removeAttribute('data-jaidx-file'));
          let bestIdx = 0, best = -1;
          inputs.forEach((el, i) => { const s = score(el); if (s > best) { best = s; bestIdx = i; } });
          inputs[bestIdx].setAttribute('data-jaidx-file', '1');
          return bestIdx;
        }
        """
        for frame in self.page.frames:
            try:
                picked = await frame.evaluate(pick_js, "resume")
            except Exception:
                picked = -1
            if picked is not None and picked >= 0:
                loc = frame.locator('[data-jaidx-file="1"]').first
                await loc.set_input_files(resume_path)
                try:
                    await frame.evaluate(
                        "() => { const e = document.querySelector('[data-jaidx-file]');"
                        " if (e) e.removeAttribute('data-jaidx-file'); }")
                except Exception:
                    pass
                return {"index": -1, "status": "uploaded", "path": resume_path,
                        "note": "auto-located file input"}
        raise RuntimeError("No file input found on the page to attach the resume.")

    async def get_field_options(self, index: int) -> dict:
        """Return the selectable options for a dropdown. For a native `select`
        the options are read directly; for a react-select `combobox` the widget
        is opened, its `[role=option]` items are read, then it is closed. Use
        this to see the real choices for a custom combobox before fill_field."""
        loc, kind = self._locator(index)
        if kind == "select":
            opts = await loc.evaluate(
                "el => Array.from(el.options).map(o => (o.textContent || '').trim())"
                ".filter(Boolean)")
            return {"index": index, "kind": kind, "options": opts}
        if kind == "combobox":
            frame = self.fields[index]["frame"]
            await loc.click()
            await self.page.wait_for_timeout(500)
            opts = await frame.evaluate(
                "() => Array.from(document.querySelectorAll('[role=option]'))"
                ".map(o => (o.textContent || '').trim()).filter(Boolean)")
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return {"index": index, "kind": kind, "options": opts}
        return {"index": index, "kind": kind, "options": []}

    async def screenshot(self, path: str | None = None) -> dict:
        if self.page is None:
            raise RuntimeError("No page open.")
        out = path or str(config.BASE_DIR / "current_page.png")
        await self.page.screenshot(path=out, full_page=True)
        return {"path": out}

    async def detect_blockers(self) -> dict:
        """Scan the page + iframes for CAPTCHAs / verification / challenge walls.
        Returns {blocked, signals, message}. Use it to decide when to hand the
        browser to the user."""
        if self.page is None:
            raise RuntimeError("No page open.")
        signals: list[str] = []
        # Cross-origin CAPTCHA iframes: inspect frame URLs directly.
        for frame in self.page.frames:
            u = (frame.url or "").lower()
            if "recaptcha" in u:
                signals.append("reCAPTCHA")
            elif "hcaptcha" in u:
                signals.append("hCaptcha")
            elif "challenges.cloudflare.com" in u:
                signals.append("Cloudflare Turnstile")
        # DOM markers + body text in the main document.
        try:
            signals.extend(await self.page.main_frame.evaluate(_BLOCKER_JS))
        except Exception:
            pass
        signals = sorted(set(signals))
        blocked = bool(signals)
        message = (
            "Human verification appears required (%s). Ask the user to complete "
            "it in the visible browser window, then continue." % ", ".join(signals)
            if blocked else "No CAPTCHA / verification wall detected."
        )
        return {"blocked": blocked, "signals": signals, "message": message}

    async def get_job_text(self) -> str:
        """Return the visible page text (for reading the job description)."""
        if self.page is None:
            raise RuntimeError("No page open.")
        text = await self.page.inner_text("body")
        return " ".join(text.split())[:8000]

    async def submit_application(self, index: int | None = None) -> dict:
        """Click the submit control. DESTRUCTIVE — the caller (skill) must
        confirm with the user first. If `index` is given, that field is clicked;
        otherwise a best-effort submit button is located."""
        if self.page is None:
            raise RuntimeError("No page open.")
        if index is not None:
            loc, _ = self._locator(index)
            await loc.click()
        else:
            frame = self.page.main_frame
            btn = frame.locator(
                "button[type=submit], input[type=submit], "
                "button:has-text('Submit'), button:has-text('Apply')"
            ).first
            await btn.click()
        await self.page.wait_for_timeout(3000)
        return {"status": "submitted", "current_url": self.page.url}

    async def close(self) -> None:
        if self.browser is not None:
            await self.browser.close()
            self.browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None


# module-level singleton used by the MCP server
session = BrowserSession()
