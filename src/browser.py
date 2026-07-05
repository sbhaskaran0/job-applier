"""ATS-agnostic browser layer (async Playwright).

Holds one live browser session for the MCP server. The key primitive is
`read_form()`, which reads the *live* page (and all iframes) and returns a
generic, structured list of every fillable field — derived purely from the DOM
and accessibility attributes, with NO per-site selectors. Claude reasons over
that list and fills fields by index. This is what makes it work on any ATS
(Greenhouse, Lever, Ashby, Workday, ...) without predefined rules.
"""

import re
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
    if (kind === 'combobox' && !value) {
      // React-select renders the committed choice in a sibling "single-value"
      // element, not in the input itself — surface it so verification and
      // submit-time capture can see combobox answers.
      const ctl = el.closest('[class*="control"]') ||
                  (el.parentElement && el.parentElement.parentElement);
      const sv = ctl && ctl.querySelector('[class*="single-value"], [class*="singleValue"]');
      if (sv) value = clean(sv.innerText);
    }

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


# JS: enumerate the options of the listbox controlled by the combobox at
# data-jaidx=idx. Scoped via aria-controls/aria-owns so options from OTHER
# widgets (e.g. the phone country-code dropdown) don't leak in; falls back to
# on-screen [role=option] elements only.
_COMBO_OPTIONS_JS = r"""
(idx) => {
  const el = document.querySelector('[data-jaidx="' + idx + '"]');
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const lbId = el && (el.getAttribute('aria-controls') || el.getAttribute('aria-owns'));
  const root = lbId ? document.getElementById(lbId) : null;
  let opts;
  if (root) {
    opts = Array.from(root.querySelectorAll('[role=option]'));
    if (!opts.length && root.getAttribute('role') === 'option') opts = [root];
  } else {
    opts = Array.from(document.querySelectorAll('[role=option]')).filter(o => {
      const r = o.getBoundingClientRect();
      return r.width > 1 && r.height > 1;
    });
  }
  return opts.map(o => clean(o.textContent)).filter(Boolean);
}
"""

# JS: the value the combobox at data-jaidx=idx has actually COMMITTED (the
# react-select "single-value" element), as opposed to transient typed text.
_COMBO_VALUE_JS = r"""
(idx) => {
  const el = document.querySelector('[data-jaidx="' + idx + '"]');
  if (!el) return '';
  const ctl = el.closest('[class*="control"]') ||
              (el.parentElement && el.parentElement.parentElement);
  const sv = ctl && ctl.querySelector('[class*="single-value"], [class*="singleValue"]');
  if (sv) return (sv.innerText || '').replace(/\s+/g, ' ').trim();
  // plain comboboxes (no react-select shell) keep the value in the input
  return (el.value || '').replace(/\s+/g, ' ').trim();
}
"""


# JS run in each frame to spot human-verification walls. Crucially it separates
# a VISIBLE, interactable challenge (a reCAPTCHA v2 checkbox, an open image
# challenge, a Turnstile/hCaptcha widget) — which only a human can clear — from
# BACKGROUND anti-bot that needs no interaction (reCAPTCHA v3, invisible v2, the
# collapsed "protected by reCAPTCHA" badge). Only the former should stop the
# flow; flagging the latter told the user to "solve a captcha" that wasn't there
# (Greenhouse loads invisible v3 on every page). Returns {blocking, warnings}.
_BLOCKER_JS = r"""
() => {
  const blocking = [], warnings = [];
  const vis = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden'
        || parseFloat(s.opacity || '1') === 0) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 24 || r.height < 24) return false;
    const vw = window.innerWidth || document.documentElement.clientWidth || 0;
    const vh = window.innerHeight || document.documentElement.clientHeight || 0;
    if (r.right <= 4 || r.bottom <= 4) return false;      // parked off-screen
    if (vw && r.left >= vw - 4) return false;             // (the collapsed v3 badge)
    if (vh && r.top >= vh + 600) return false;
    return true;
  };

  // --- reCAPTCHA -------------------------------------------------------------
  const badge = document.querySelector('.grecaptcha-badge');
  const anchors = Array.from(document.querySelectorAll(
    'iframe[src*="recaptcha/api2/anchor"], iframe[title="reCAPTCHA"]'));
  const challengeFrames = Array.from(document.querySelectorAll(
    'iframe[src*="recaptcha"][src*="bframe"]'));
  const gDiv = document.querySelector('.g-recaptcha');
  // A v2 checkbox anchor is visible and NOT tucked inside the invisible badge;
  // an open image challenge (bframe) is a visible popup.
  const anchorVisible = anchors.some(a => !(badge && badge.contains(a)) && vis(a));
  const challengeVisible = challengeFrames.some(vis);
  const gDivVisible = !!gDiv && vis(gDiv)
    && (gDiv.getAttribute('data-size') || '') !== 'invisible';
  const anyRecaptcha = badge || anchors.length || challengeFrames.length || gDiv;
  if (anchorVisible || challengeVisible || gDivVisible) {
    blocking.push('reCAPTCHA (visible challenge)');
  } else if (anyRecaptcha) {
    warnings.push('reCAPTCHA v3/invisible (background scoring; no challenge to solve)');
  }

  // --- hCaptcha --------------------------------------------------------------
  const hFrames = Array.from(document.querySelectorAll('iframe[src*="hcaptcha"]'));
  const hDiv = document.querySelector('.h-captcha');
  if (hFrames.some(vis) || (hDiv && vis(hDiv))) blocking.push('hCaptcha');
  else if (hFrames.length || hDiv) warnings.push('hCaptcha (invisible)');

  // --- Cloudflare Turnstile --------------------------------------------------
  const tFrames = Array.from(
    document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]'));
  const tDiv = document.querySelector('.cf-turnstile');
  if (tFrames.some(vis) || (tDiv && vis(tDiv))) blocking.push('Cloudflare Turnstile');
  else if (tFrames.length || tDiv) warnings.push('Cloudflare Turnstile (managed/invisible)');

  // --- Explicit "verify you are human" wall text (a real visible wall) -------
  const t = ((document.body && document.body.innerText) || '').toLowerCase();
  const phrases = ['verify you are human', 'verifying you are human',
    "i'm not a robot", 'are you a robot', 'checking your browser',
    'complete the captcha', 'press and hold', 'security check'];
  for (const p of phrases) {
    if (t.includes(p)) { blocking.push('verification text: ' + p); break; }
  }
  return { blocking, warnings };
}
"""


# JS run in each frame to find an email/OTP verification-code entry gate — the
# 8-box code challenge Greenhouse throws on submit. Tags the code inputs with
# data-jacode="0..n" (DOM order) so fill_verification_code can drive them, and
# returns {count, mode}. `segmented` = N single-char boxes; `single` = one code
# field. This is NOT a captcha — the agent can fetch the code from the inbox.
_CODE_INPUTS_JS = r"""
() => {
  const inputs = Array.from(document.querySelectorAll('input')).filter(el => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    return !['hidden','submit','button','checkbox','radio','file','image','reset']
      .includes(type);
  });
  const isSingle = (el) => {
    const ml = el.getAttribute('maxlength');
    return ml && parseInt(ml, 10) === 1;
  };
  const named = (el) => {
    const s = ((el.name||'') + ' ' + (el.id||'') + ' '
      + (el.getAttribute('aria-label')||'') + ' '
      + (el.getAttribute('placeholder')||'') + ' '
      + (el.getAttribute('autocomplete')||'')).toLowerCase();
    return /(verif|one[- ]?time|otp|passcode|\bcode\b|security code)/.test(s);
  };
  let picked = [], mode = 'single';
  const singles = inputs.filter(isSingle);
  if (singles.length >= 4) { picked = singles; mode = 'segmented'; }
  else {
    const n = inputs.filter(named);
    if (n.length) { picked = n.slice(0, 1); mode = 'single'; }
  }
  picked.forEach((el, i) => el.setAttribute('data-jacode', String(i)));
  return { count: picked.length, mode };
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
        # One-shot: also return the intervention check and the parsed form so the
        # common open path is a single round-trip instead of three. read_form()
        # populates self.fields as a side effect. If the page later changes,
        # re-call read_form / check_for_intervention.
        intervention = await self.detect_blockers()
        fields = await self.read_form()
        return {"url": url, "title": title, "detected_ats": ats,
                "resume_synced": resume_synced,
                "intervention": intervention, "fields": fields,
                "note": ("intervention + fields are included — no need to call "
                         "check_for_intervention or read_form again unless the "
                         "page navigates or changes.")}

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
            return await self._fill_combobox(index, loc, value)
        else:  # text / textarea / contenteditable
            await loc.fill(value)
        return {"index": index, "kind": kind, "status": "filled", "value": value}

    @staticmethod
    def _match_option(value: str, options: list[str]) -> str | None:
        """Pick the option that actually MEANS `value`: exact (case-insensitive)
        first, then prefix, then abbreviation (initials — 'US' ↔ 'United
        States'). Returns None when nothing matches; never falls back to 'first
        option in the filtered list' (that is how 'US' once selected
        'AUStralia')."""
        v = (value or "").strip().lower()
        if not v:
            return None

        def initials(s: str) -> str:
            return "".join(w[0] for w in re.findall(r"[A-Za-z]+", s)).lower()

        for o in options:
            if o.strip().lower() == v:
                return o
        for o in options:
            if o.strip().lower().startswith(v):
                return o
        for o in options:
            if initials(o) == v or initials(value) == o.strip().lower():
                return o
        if len(options) == 1 and v in options[0].lower():
            return options[0]
        return None

    async def _fill_combobox(self, index: int, loc, value: str) -> dict:
        """Open the widget, type to filter, then select the option that best
        matches `value` and VERIFY the widget committed it. If no option
        matches, the typed text is cleared and the real options are returned
        (status "unmatched") so the caller can refill with the right label."""
        frame = self.fields[index]["frame"]
        await loc.click()
        await self.page.wait_for_timeout(200)
        await self.page.keyboard.type(value, delay=20)
        await self.page.wait_for_timeout(600)
        options = await frame.evaluate(_COMBO_OPTIONS_JS, index)
        target = self._match_option(value, options)
        if target is None:
            # Clear the typed filter and re-read the unfiltered list so the
            # caller sees the widget's actual choices.
            for _ in range(len(value) + 2):
                await self.page.keyboard.press("Backspace")
            await self.page.wait_for_timeout(400)
            full = await frame.evaluate(_COMBO_OPTIONS_JS, index)
            await self.page.keyboard.press("Escape")
            return {"index": index, "kind": "combobox", "status": "unmatched",
                    "value": value, "options": full[:80],
                    "note": "no option matched the value; call fill_field "
                            "again with one of `options` verbatim"}
        try:
            await frame.get_by_role("option", name=target, exact=True).first \
                .click(timeout=2000)
        except Exception:
            try:
                await frame.get_by_role("option", name=target).first \
                    .click(timeout=1500)
            except Exception:
                await self.page.keyboard.press("Enter")
        await self.page.wait_for_timeout(200)
        committed = await frame.evaluate(_COMBO_VALUE_JS, index)
        if committed:
            return {"index": index, "kind": "combobox", "status": "filled",
                    "value": committed}
        return {"index": index, "kind": "combobox", "status": "uncommitted",
                "value": value, "matched_option": target,
                "note": "clicked an option but the widget shows no committed "
                        "value — verify visually or retry"}

    async def fill_many(self, items: list[dict]) -> list[dict]:
        """Fill several fields in one call. `items` is [{index, value}, ...],
        applied in order. A per-item failure is captured (not fatal) so one bad
        field doesn't abort the batch."""
        results: list[dict] = []
        for it in items:
            idx = it.get("index")
            val = it.get("value", "")
            try:
                results.append(await self.fill_field(idx, val))
            except Exception as e:
                results.append({"index": idx, "status": "error",
                                "error": f"{type(e).__name__}: {e}"})
        return results

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
            opts = await frame.evaluate(_COMBO_OPTIONS_JS, index)
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
        Returns {blocked, signals, warnings, message}. Only a VISIBLE, human-
        solvable challenge sets blocked=true; background anti-bot (reCAPTCHA v3,
        invisible v2, the collapsed badge) is reported in `warnings` and does NOT
        stop the flow. Use `blocked` to decide when to hand the browser over."""
        if self.page is None:
            raise RuntimeError("No page open.")
        blocking: list[str] = []
        warnings: list[str] = []
        for frame in self.page.frames:
            try:
                res = await frame.evaluate(_BLOCKER_JS)
            except Exception:
                continue
            blocking.extend(res.get("blocking", []))
            warnings.extend(res.get("warnings", []))
        blocking = sorted(set(blocking))
        warnings = sorted(set(warnings))
        blocked = bool(blocking)
        if blocked:
            message = ("Human verification appears required (%s). Ask the user "
                       "to complete it in the visible browser window, then "
                       "continue." % ", ".join(blocking))
        elif warnings:
            message = ("Background anti-bot present but no visible challenge "
                       "(%s) — safe to proceed; do NOT ask the user to solve a "
                       "captcha." % ", ".join(warnings))
        else:
            message = "No CAPTCHA / verification wall detected."
        return {"blocked": blocked, "signals": blocking, "warnings": warnings,
                "message": message}

    async def detect_verification_gate(self) -> dict:
        """Detect an email/OTP verification-code gate (e.g. Greenhouse's 8-box
        code challenge on submit). Returns {present, count, mode, text_hint,
        message}. Unlike a CAPTCHA this is recoverable by the agent: fetch the
        code from the applicant's inbox, then fill_verification_code + submit."""
        if self.page is None:
            raise RuntimeError("No page open.")
        count, mode = 0, None
        for frame in self.page.frames:
            try:
                info = await frame.evaluate(_CODE_INPUTS_JS)
            except Exception:
                continue
            if info and info.get("count", 0) > count:
                count, mode = info["count"], info.get("mode")
        try:
            body = " ".join((await self.page.inner_text("body")).split()).lower()
        except Exception:
            body = ""
        phrases = ["verification code", "verify your email", "confirm your email",
                   "we sent", "enter the code", "one-time code", "sent you a code",
                   "check your email", "6-digit", "8-character", "enter the 8"]
        hint = next((p for p in phrases if p in body), "")
        present = count > 0 or bool(hint)
        return {"present": present, "count": count, "mode": mode,
                "text_hint": hint,
                "message": ("Email verification-code gate detected — fetch the "
                            "code from the applicant's inbox, then call "
                            "fill_verification_code(code) and submit_application "
                            "again." if present
                            else "No verification-code gate detected.")}

    async def fill_verification_code(self, code: str) -> dict:
        """Fill a detected verification-code gate with `code`. Focuses the first
        code box and types the whole code (segmented OTP components auto-advance;
        a single field takes it directly), then leaves submission to the caller
        (submit_application). Returns {status, mode, count}."""
        if self.page is None:
            raise RuntimeError("No page open.")
        code = (code or "").strip()
        if not code:
            return {"status": "error", "note": "empty code"}
        for frame in self.page.frames:
            try:
                info = await frame.evaluate(_CODE_INPUTS_JS)
            except Exception:
                continue
            if not info or info.get("count", 0) <= 0:
                continue
            mode = info.get("mode")
            if mode == "segmented":
                # One char per box, in the tagged DOM order — deterministic and
                # independent of whether the OTP widget auto-advances focus.
                boxes = frame.locator("[data-jacode]")
                cnt = await boxes.count()
                for i in range(min(cnt, len(code))):
                    box = boxes.nth(i)
                    try:
                        await box.fill(code[i])
                    except Exception:
                        await box.click()
                        await self.page.keyboard.type(code[i])
                    await self.page.wait_for_timeout(40)
            else:
                await frame.locator('[data-jacode="0"]').first.fill(code)
            await self.page.wait_for_timeout(200)
            return {"status": "filled", "mode": mode,
                    "count": info["count"], "code_len": len(code)}
        return {"status": "no_code_input",
                "note": "no verification-code input detected on the page"}

    async def get_job_text(self) -> str:
        """Return the visible page text (for reading the job description)."""
        if self.page is None:
            raise RuntimeError("No page open.")
        text = await self.page.inner_text("body")
        return " ".join(text.split())[:8000]

    async def _find_submit_button(self):
        """Locate the real submit control across all frames. Candidate
        selectors are tried in priority order, and anything reading like
        'Quick Apply' is skipped — the naive DOM-first `button:has-text
        ('Apply')` once clicked Greenhouse's 'Quick Apply with MyGreenhouse'
        button instead of 'Submit application'."""
        for sel in ("button[type=submit], input[type=submit]",
                    "button:has-text('Submit application')",
                    "button:has-text('Submit')",
                    "button:has-text('Apply')"):
            for frame in self.page.frames:
                try:
                    cands = frame.locator(sel)
                    n = await cands.count()
                except Exception:
                    continue
                for i in range(n):
                    cand = cands.nth(i)
                    try:
                        if not await cand.is_visible():
                            continue
                        label = (await cand.evaluate(
                            "el => (el.innerText || el.value || '')")).lower()
                    except Exception:
                        continue
                    if "quick apply" in label:
                        continue
                    return cand
        return None

    async def _submission_confirmed(self, fields_before: int) -> tuple[bool, str]:
        """Decide whether the submit actually went through: confirmation text
        on the page, or the form largely disappearing. Anything else is only
        an *attempt* — the click may have hit validation, a verification gate,
        or the wrong control."""
        try:
            body = " ".join((await self.page.inner_text("body")).split()).lower()
        except Exception:
            return False, "could not read the page after the click"
        m = re.search(
            r"thank you for (applying|submitting)"
            r"|application (has been |was )?(received|submitted)"
            r"|we('ve| have) received your application", body)
        if m:
            return True, f'confirmation text: "{m.group(0)}"'
        try:
            after = len(await self.read_form())
        except Exception:
            after = fields_before
        if fields_before >= 4 and after <= fields_before // 4:
            return True, f"form fields went from {fields_before} to {after}"
        return False, (f"form still present ({after} fields, was "
                       f"{fields_before}); no confirmation text found")

    async def submit_application(self, index: int | None = None) -> dict:
        """Click the submit control. DESTRUCTIVE — the caller (skill) must
        confirm with the user first. If `index` is given, that field is
        clicked; otherwise the submit button is located across frames
        (excluding 'Quick Apply' style buttons).

        Snapshots the form (labels + current values) just before clicking and
        returns it as `form_snapshot` so the caller can persist what was
        actually submitted, then verifies the submission landed: `status` is
        "submitted" only when the page shows confirmation (or the form is
        gone); otherwise "attempted" with the evidence in `confirmation`."""
        if self.page is None:
            raise RuntimeError("No page open.")
        try:
            form_snapshot = await self.read_form()
        except Exception:
            form_snapshot = []
        if index is not None:
            loc, _ = self._locator(index)
        else:
            loc = await self._find_submit_button()
            if loc is None:
                return {"status": "no_submit_button",
                        "current_url": self.page.url,
                        "form_snapshot": form_snapshot,
                        "note": "no visible submit control found; ask the "
                                "user to submit manually"}
        await loc.click()
        await self.page.wait_for_timeout(3000)
        confirmed, evidence = await self._submission_confirmed(len(form_snapshot))
        return {"status": "submitted" if confirmed else "attempted",
                "confirmation": evidence, "current_url": self.page.url,
                "form_snapshot": form_snapshot}

    async def close(self) -> None:
        if self.browser is not None:
            await self.browser.close()
            self.browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None


# module-level singleton used by the MCP server
session = BrowserSession()
