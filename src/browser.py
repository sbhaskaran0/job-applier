"""ATS-agnostic browser layer (async Playwright).

Holds one live browser session for the MCP server. The key primitive is
`read_form()`, which reads the *live* page (and all iframes) and returns a
generic, structured list of every fillable field — derived purely from the DOM
and accessibility attributes, with NO per-site selectors. Claude reasons over
that list and fills fields by index. This is what makes it work on any ATS
(Greenhouse, Lever, Ashby, Workday, ...) without predefined rules.
"""

import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from . import config, data
from . import repeating as rep

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Native <select> lists longer than this are summarized in read_form's payload
# (count + a few sample labels) instead of enumerated inline — the full list is
# one get_field_options(index) call away. Big lists (country, year, school
# pickers) are the dominant source of read_form token bloat.
_INLINE_OPTS_MAX = 25

# Shared JS: read the value a *listbox button* (a <button aria-haspopup=
# "listbox">, the Workday / Oracle / SuccessFactors / Radix / MUI dropdown
# shape) has actually COMMITTED. Defined once and embedded verbatim into both
# _SCAN_JS (read_form's current_value) and _LISTBOX_VALUE_JS (the fill-time
# verifier) so the two can never disagree about what a listbox "contains" —
# one source of truth beats two tidy copies.
#
# The blacklist is the load-bearing part. An UNSET Workday listbox renders the
# literal text "Select One", so a naive read reports a committed value for a
# field nobody answered: verification would return verified=true against
# nothing, and capture_submission would persist "Select One" into the user's
# answer history as if it were a real answer. A filtered value reads back as
# "" — indistinguishable from an untouched field, which is exactly right.
_LISTBOX_VALUE_FN = r"""
  const listboxValue = (el) => {
    if (!el) return '';
    const lbClean = (t) => (t || '').replace(/\s+/g, ' ').trim();
    const aria = lbClean(el.getAttribute && el.getAttribute('aria-label'));
    // Cascade, most explicit first: Workday's own selected-item node, then the
    // button's direct <span> child, then the button text, then the aria-label
    // suffix ("Country, United States" / "Country: United States").
    let v = '';
    const si = el.querySelector && el.querySelector('[data-automation-id="selectedItem"]');
    if (si) v = lbClean(si.innerText || si.textContent);
    if (!v && el.querySelector) {
      const sp = el.querySelector(':scope > span');
      if (sp) v = lbClean(sp.innerText || sp.textContent);
    }
    if (!v) v = lbClean(el.innerText || el.textContent);
    if (!v && aria) {
      const m = aria.split(/\s*(?:,|:|\bselected\b)\s*/i).filter(Boolean);
      v = lbClean(m.length ? m[m.length - 1] : aria);
    }
    const low = v.toLowerCase();
    const PLACEHOLDERS = ['select one', 'choose one', '--', 'none selected', 'search'];
    if (!v) return '';
    if (PLACEHOLDERS.indexOf(low) !== -1) return '';
    if (aria && low === aria.toLowerCase()) return '';   // echoes its own label
    return v;
  };
"""


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
  // Clear tags from any PREVIOUS scan of this document. read_form must be
  // idempotent: the button-group pass below skips elements that already carry
  // data-jaidx, so without this sweep a second read of the same page silently
  // drops every button-group option (and Claude then "sees" an answered
  // question as missing). Within a single pass the hasAttribute guard still
  // prevents double-capture, because everything tagged during this pass was
  // tagged by this pass.
  document.querySelectorAll('[data-jaidx]').forEach(e => e.removeAttribute('data-jaidx'));

  __LISTBOX_VALUE_FN__

  // button[aria-haspopup=listbox] is the Workday dropdown (step 2 of a Snap
  // Workday application is nothing but these) and also Oracle/SuccessFactors/
  // Radix/MUI menus. Without it the scanner reported those pages as having no
  // dropdowns at all. It is an ARIA contract, not a per-site selector, so the
  // module's no-per-site-selectors invariant holds.
  const sel = 'input, textarea, select, [role=textbox], [role=combobox], [contenteditable=true], button[aria-haspopup="listbox"]';
  const els = Array.from(document.querySelectorAll(sel));
  const out = [];
  let idx = startIndex;
  for (const el of els) {
    const tag = el.tagName.toLowerCase();
    const itype = (el.getAttribute('type') || '').toLowerCase();
    // NOTE: this type-skip is gated on tag === 'input', so a <button> (whose
    // type attribute is usually the literal "button") is NOT eaten by it.
    if (tag === 'input' && ['hidden', 'submit', 'button', 'reset', 'image'].includes(itype)) continue;
    if (el.disabled || el.readOnly) continue;
    if (!isVisible(el)) continue;

    // A listbox button is reported as kind 'combobox' ON PURPOSE: every
    // closed-choice code path downstream (mcp_server's _CLOSED_KINDS routing,
    // get_field_options, the skills' "pick an option" branch) keys off that
    // kind, so the whole pipeline works with no edit anywhere else. `widget`
    // carries the physical shape so fill_field can dispatch on the WIDGET.
    const isListbox = (tag === 'button' && el.getAttribute('aria-haspopup') === 'listbox');

    let kind;
    if (isListbox) kind = 'combobox';
    else if (tag === 'select') kind = 'select';
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
    if (isListbox) value = listboxValue(el);
    else if (kind === 'checkbox' || kind === 'radio') value = el.checked ? 'checked' : '';
    else value = el.value || '';
    // Passwords are NEVER echoed: the field stays fillable (fill_credential),
    // but its value is masked here and flagged `sensitive` for every consumer.
    // Sticky: a show-password toggle flips type to text, so also key off
    // autocomplete, the label, and a marker set on first sight / on fill.
    const acAttr = (el.getAttribute('autocomplete') || '').toLowerCase();
    const sensitive = (tag === 'input' && (itype === 'password' ||
      /(new|current)-password/.test(acAttr) ||
      (el.dataset && el.dataset.jaSensitive === '1') ||
      /pass(word|code)/i.test(labelFor(el)))) || undefined;
    if (sensitive) {
      el.dataset.jaSensitive = '1';
      value = value ? '********' : '';
    }
    if (kind === 'combobox' && !isListbox && !value) {
      // React-select renders the committed choice in a sibling "single-value"
      // element, not in the input itself — surface it so verification and
      // submit-time capture can see combobox answers.
      const ctl = el.closest('[class*="control"]') ||
                  (el.parentElement && el.parentElement.parentElement);
      const sv = ctl && ctl.querySelector('[class*="single-value"], [class*="singleValue"]');
      if (sv) value = clean(sv.innerText);
    }

    // Multi-line / free-text signal: a <textarea>, contenteditable, or ARIA
    // textbox is almost always an open-ended answer (essay / cover letter /
    // "why us"), whereas a plain <input type=text> is usually a profile fact.
    // Snapshot surfaces this as freetext_count so batch prep can route craft
    // jobs to a crafting agent and resolve all-profile jobs inline — without
    // ever reading the field payload into the main context.
    const multiline = (tag === 'textarea' ||
                       el.getAttribute('contenteditable') === 'true' ||
                       el.getAttribute('role') === 'textbox') || undefined;

    el.setAttribute('data-jaidx', String(idx));
    out.push({
      index: idx, kind, widget: isListbox ? 'listbox' : undefined,
      label: labelFor(el), multiline,
      group: (kind === 'radio' || kind === 'checkbox') ? clean(el.getAttribute('name')) : undefined,
      option_value: (kind === 'radio' || kind === 'checkbox') ? (el.value || '') : undefined,
      options, required, current_value: value, sensitive,
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
  // Listbox buttons are safe from this pass: the first pass above already ran
  // and tagged them with data-jaidx, and the `b.hasAttribute('data-jaidx')`
  // guard on the next line skips anything tagged. The ordering is what makes
  // that true, so it must stay: first pass tags, second pass skips tagged.
  // The hasAttribute guard only protects the element being *considered*, not
  // the siblings collected around it, so the sibling filter below excludes
  // listbox buttons explicitly too.
  const optSel = 'button, [role=radio]';
  const seenParents = new Set();
  for (const b of Array.from(document.querySelectorAll(optSel))) {
    if (b.hasAttribute('data-jaidx')) continue;
    if (b.getAttribute('aria-haspopup') === 'listbox') continue;
    const parent = b.parentElement;
    if (!parent || seenParents.has(parent)) continue;
    const sibs = Array.from(parent.children).filter(c =>
      (c.tagName === 'BUTTON' || c.getAttribute('role') === 'radio') &&
      c.getAttribute('aria-haspopup') !== 'listbox' &&
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
""".replace("__LISTBOX_VALUE_FN__", _LISTBOX_VALUE_FN)


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

# JS: did aria-controls/aria-owns resolve to a REAL popup element for the
# combobox at data-jaidx=idx? _COMBO_OPTIONS_JS silently falls back to a loose
# on-screen [role=option] sweep, which can scrape a completely different
# widget's menu (the phone country-code list is the classic one). The listbox
# fill path needs to know which of the two happened: enumerate-and-click is
# only trustworthy when the list was ARIA-scoped; otherwise it types instead.
_COMBO_SCOPED_JS = r"""
(idx) => {
  const el = document.querySelector('[data-jaidx="' + idx + '"]');
  const lbId = el && (el.getAttribute('aria-controls') || el.getAttribute('aria-owns'));
  return !!(lbId && document.getElementById(lbId));
}
"""

# JS: is the listbox at data-jaidx=idx currently OPEN? true/false from
# aria-expanded, or null when the widget doesn't publish the state. A listbox
# button is a TOGGLE, so "click it to open" is only correct when it is closed.
_EXPANDED_JS = r"""
(idx) => {
  const el = document.querySelector('[data-jaidx="' + idx + '"]');
  if (!el) return null;
  const v = el.getAttribute('aria-expanded');
  return v === null ? null : v === 'true';
}
"""

# JS: the committed value of a listbox BUTTON at data-jaidx=idx (same cascade,
# same placeholder blacklist as read_form uses — see _LISTBOX_VALUE_FN).
_LISTBOX_VALUE_JS = r"""
(idx) => {
  __LISTBOX_VALUE_FN__
  const el = document.querySelector('[data-jaidx="' + idx + '"]');
  if (el && (el.type === 'password' || (el.dataset && el.dataset.jaSensitive === '1'))) return el.value ? '********' : '';
  return listboxValue(el);
}
""".replace("__LISTBOX_VALUE_FN__", _LISTBOX_VALUE_FN)

# JS: the value the combobox at data-jaidx=idx has actually COMMITTED (the
# react-select "single-value" element), as opposed to transient typed text.
# A <button> combobox is a listbox widget, so it branches to the shared
# listbox cascade — that keeps read_form and the fill-time verifier on ONE
# code path, which is why they can never disagree about a Workday dropdown.
_COMBO_VALUE_JS = r"""
(idx) => {
  const el = document.querySelector('[data-jaidx="' + idx + '"]');
  if (!el) return '';
  if ((el.type === 'password' || (el.dataset && el.dataset.jaSensitive === '1'))) return el.value ? '********' : '';
  __LISTBOX_VALUE_FN__
  if (el.tagName.toLowerCase() === 'button') return listboxValue(el);
  const ctl = el.closest('[class*="control"]') ||
              (el.parentElement && el.parentElement.parentElement);
  const sv = ctl && ctl.querySelector('[class*="single-value"], [class*="singleValue"]');
  if (sv) return (sv.innerText || '').replace(/\s+/g, ' ').trim();
  // plain comboboxes (no react-select shell) keep the value in the input
  return (el.value || '').replace(/\s+/g, ' ').trim();
}
""".replace("__LISTBOX_VALUE_FN__", _LISTBOX_VALUE_FN)


# JS: read back whatever value the NON-combobox field at data-jaidx=idx is
# actually holding, so a fill can be verified instead of assumed. Returns null
# (not "") when this DOM exposes no readable value for the element — the caller
# must report that as verified=null, never as a failure.
_FIELD_VALUE_JS = r"""
(idx) => {
  const el = document.querySelector('[data-jaidx="' + idx + '"]');
  if (!el) return null;
  if ((el.type === 'password' || (el.dataset && el.dataset.jaSensitive === '1'))) return el.value ? '********' : '';
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const tag = el.tagName.toLowerCase();
  const itype = (el.getAttribute('type') || '').toLowerCase();
  if (tag === 'select') {
    const o = el.selectedOptions && el.selectedOptions[0];
    return o ? clean(o.textContent) : clean(el.value);
  }
  if (itype === 'radio' || itype === 'checkbox') return el.checked ? 'checked' : '';
  if (tag === 'button' || el.getAttribute('role') === 'radio') {
    // Non-native segmented option (Ashby Yes/No): "checked" lives in ARIA or
    // a class, never in a .value.
    const on = el.getAttribute('aria-checked') === 'true' ||
               el.getAttribute('aria-pressed') === 'true' ||
               /(selected|active|checked)/i.test(el.className || '');
    return on ? 'checked' : '';
  }
  if (el.isContentEditable) return clean(el.innerText);
  if (typeof el.value === 'string') return clean(el.value);
  return null;
}
"""

# JS: blur the element at data-jaidx=idx when it (or something inside it) has
# focus. Returns whether a blur happened.
_BLUR_JS = r"""
(idx) => {
  const el = document.querySelector('[data-jaidx="' + idx + '"]');
  const a = document.activeElement;
  if (!el || !a || !(a === el || el.contains(a))) return false;
  a.blur();
  return true;
}
"""

# JS: LIVE check of the element at data-jaidx=idx, run immediately before a
# fill (cached scan metadata goes stale on a type flip or re-render). Returns
# {password: a real <input type=password>, sensitive: password-like by type,
# autocomplete, marker or label}. With mark=true it stamps the sticky marker.
_SENSITIVE_PROBE_JS = r"""
(args) => {
  const el = document.querySelector('[data-jaidx="' + args[0] + '"]');
  if (!el) return null;
  const tag = el.tagName.toLowerCase();
  const t = (el.getAttribute('type') || '').toLowerCase();
  const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
  const lab = (el.getAttribute('aria-label') || '') + ' ' +
    (el.labels && el.labels[0] ? el.labels[0].innerText : '');
  const password = tag === 'input' && el.type === 'password';
  const sensitive = password || (el.dataset && el.dataset.jaSensitive === '1') ||
    ac.indexOf('password') !== -1 || /pass(word|code)/i.test(lab);
  if (args[1] && password) el.dataset.jaSensitive = '1';
  return {password: password, sensitive: !!sensitive};
}
"""

# JS: tag a clickable PROXY for the toggle at data-jaidx=idx with data-japroxy.
# Workday (and any React control that paints its own radio) overlays the real
# <input> so Playwright's .check() raises "element intercepts pointer events"
# — or clicks through without firing the framework's handler. The proxy chain
# is label[for=id] -> closest('label') -> the parent wrapper: the first one
# that exists is the element a human actually clicks.
_PROXY_JS = r"""
(idx) => {
  const el = document.querySelector('[data-jaidx="' + idx + '"]');
  document.querySelectorAll('[data-japroxy]').forEach(e => e.removeAttribute('data-japroxy'));
  if (!el) return false;
  let p = null;
  if (el.id) {
    try { p = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); } catch (e) {}
  }
  if (!p) p = el.closest('label');
  if (!p) p = el.parentElement;
  if (!p || p === el) return false;
  p.setAttribute('data-japroxy', '1');
  return true;
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


# --- file-upload slot selection (upload_resume) ---------------------------
# Greenhouse hides BOTH its Resume/CV and Cover Letter <input type=file>, so
# read_form never lists them and callers cannot pass an index. upload_resume
# therefore locates the input itself. JS only DESCRIBES every file input; the
# choice is made in pure Python (_choose_file_input) so it is unit-testable.
#
# The "container" text is the load-bearing detail. The old code used
# closest('div,section,fieldset,li'), which on a form that nests both uploads
# in one wrapper yields text containing "resume" AND "cover" for each input, so
# every input looked like a cover letter (score 0) and the resume pick became
# arbitrary. Here we climb ancestors only while the ancestor holds exactly ONE
# file input (so it can never span both uploads), max 4 levels, and never take
# an ancestor whose text mixes resume/cv with cover. `label` is the explicit
# label (label[for], aria-labelledby) which beats container text.
_FILE_HELPERS_JS = r"""
const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
const MIX = (t) => /resume|\bcv\b/i.test(t) && /cover/i.test(t);
const nFiles = (n) => n.querySelectorAll('input[type=file]').length;
const tightBox = (el) => {
  let best = el.parentElement, cur = el.parentElement;
  for (let n = 0; cur && n < 4; n++, cur = cur.parentElement) {
    if (nFiles(cur) !== 1) break;
    if (MIX(clean(cur.innerText))) break;
    best = cur;
  }
  return best;
};
const labelOf = (el) => {
  const parts = [];
  if (el.id) {
    const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    if (l) parts.push(l.innerText);
  }
  const lb = el.getAttribute('aria-labelledby');
  if (lb) lb.split(/\s+/).forEach(id => {
    const n = document.getElementById(id); if (n) parts.push(n.innerText);
  });
  const wrap = el.closest('label');
  if (wrap) parts.push(wrap.innerText);
  return clean(parts.join(' '));
};
const describe = (el, i) => {
  const box = tightBox(el);
  return {i: i, text: clean(box && box.innerText).slice(0, 200),
          single: !!box && nFiles(box) === 1,
          name: el.name || '', id: el.id || '',
          aria: el.getAttribute('aria-label') || '',
          label: labelOf(el),
          file: (el.files && el.files[0] && el.files[0].name) || ''};
};
"""

_FILE_CANDIDATES_JS = "() => {" + _FILE_HELPERS_JS + r"""
  return Array.from(document.querySelectorAll('input[type=file]'))
    .map((el, i) => describe(el, i));
}"""

# Describe ONE element (an explicit-index upload target); null if it is not a
# file input. `total` = file inputs on the page/frame.
_FILE_DESCRIBE_JS = "(el) => {" + _FILE_HELPERS_JS + r"""
  if (!el || el.tagName !== 'INPUT' || el.type !== 'file') return null;
  const all = Array.from(document.querySelectorAll('input[type=file]'));
  const d = describe(el, all.indexOf(el));
  d.total = all.length;
  return d;
}"""

_TAG_FILE_JS = r"""
(i) => {
  const inputs = Array.from(document.querySelectorAll('input[type=file]'));
  inputs.forEach(el => el.removeAttribute('data-jaidx-file'));
  if (inputs[i]) inputs[i].setAttribute('data-jaidx-file', '1');
}"""

_RESUME_RE = re.compile(r"resume|\bcv\b|curriculum", re.I)
_COVER_RE = re.compile(r"cover", re.I)


def _own(c: dict) -> str:
    return " ".join(str(c.get(k) or "") for k in ("name", "id", "aria", "label"))


def _is_cover_candidate(c: dict, total: int) -> bool:
    """May this input receive a cover letter? Own name/id/aria/label must say
    "cover" and must not say resume/cv. One guarded exception: an input with
    no signal of its own qualifies via its container only if that container
    holds exactly this one file input (`single`), its text says cover and not
    resume/cv, and the page has >= 2 file inputs (so it is not merely the
    lone resume input sitting near a cover-letter textarea)."""
    own = _own(c)
    if _RESUME_RE.search(own):
        return False
    if _COVER_RE.search(own):
        return True
    text = str(c.get("text") or "")
    return bool(total >= 2 and c.get("single") and _COVER_RE.search(text)
                and not _RESUME_RE.search(text))


def _choose_file_input(candidates: list[dict], target: str = "resume"):
    """Pick which file input to use; returns its `i` or None.

    target="cover": only `_is_cover_candidate` inputs; an own-attribute match
    beats a container-only match. Resume/CV-labelled inputs are never
    eligible and there is no fallback, so a page with no cover slot yields
    None (the caller reports no_cover_slot).

    target="resume": the historical scoring -- cover-labelled 0, resume/cv-
    labelled 2, otherwise 1; highest wins, first on ties."""
    total = len(candidates)
    if target == "cover":
        weak = None
        for c in candidates:
            if not _is_cover_candidate(c, total):
                continue
            if _COVER_RE.search(_own(c)):
                return c["i"]
            if weak is None:
                weak = c["i"]
        return weak
    best, best_score = None, -1
    for c in candidates:
        t = _own(c) + " " + str(c.get("text") or "")
        score = 0 if _COVER_RE.search(t) else 2 if _RESUME_RE.search(t) else 1
        if score > best_score:
            best, best_score = c["i"], score
    return best


def _is_resume_slot(c: dict) -> bool:
    if _COVER_RE.search(_own(c)):
        return False
    return bool(_RESUME_RE.search(_own(c) + " " + str(c.get("text") or "")))


def _slot_key(c: dict):
    """Stable identity of a file slot across re-renders: id, else name, and
    position only when both are absent (React dropzones replace the node, so
    position alone would mis-pair slots if inputs are added or removed)."""
    if c.get("id"):
        return ("id", c["id"])
    if c.get("name"):
        return ("name", c["name"])
    return ("pos", c.get("frame", 0), c.get("i"))


def _match_slot(cands: list[dict], ref: dict):
    """Re-find `ref` (a pre-upload descriptor) among freshly read candidates:
    by id, else name, else position."""
    want = _slot_key(ref)
    for c in cands:
        if _slot_key(c) == want:
            return c
    return None


def _shows(c: dict, low: str) -> bool:
    return bool(c) and (low in str(c.get("text") or "").lower()
                        or str(c.get("file") or "").lower() == low)


def _cover_landed_in_resume(before: list[dict], after: list[dict],
                            fname: str) -> bool:
    """True if a resume-labelled slot newly shows the cover filename."""
    if not fname:
        return False
    prev = {_slot_key(c): c for c in before}
    low = fname.lower()
    return any(_is_resume_slot(c) and _shows(c, low)
               and not _shows(prev.get(_slot_key(c)) or {}, low)
               for c in after)


def _verify_upload(fname: str, before: dict | None, after: dict | None) -> dict:
    """Decide `verified`. The input's own files[0].name must equal the
    filename. Container text is accepted only when the input reads back EMPTY
    (a dropzone that swapped the node / cleared the input) AND the filename
    was not already in the box before the upload."""
    after = after or {}
    observed = str(after.get("file") or "")
    if observed:
        ok = observed == fname
    else:
        low = fname.lower()
        ok = bool(low) and _shows(after, low) and not _shows(before or {}, low)
    return {"verified": bool(ok), "observed_filename": observed,
            "container_text": str(after.get("text") or "")[:160]}


# JS: the validation errors a page is currently showing, mapped back to the
# fields that caused them. Generic on purpose: an error node with an `id` is
# linked to its input by `aria-describedby` — an ARIA contract, not a Workday
# detail — and the fallback walks up at most 4 ancestors looking for a scanned
# field. Containers that merely WRAP another error node are skipped so one
# error isn't reported three times.
#
# An error that cannot be mapped is still RETURNED, with index null: a
# visually-hidden (sr-only) input is skipped by _SCAN_JS's visibility test, so
# the field most likely to fail validation may carry no data-jaidx at all.
# Dropping it is how a run parks with no explanation.
_ERRORS_JS = r"""
() => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const isVisible = (el) => {
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const labelOf = (el) => {
    if (!el) return '';
    if (el.getAttribute && el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    if (el.id) {
      try { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (l) return clean(l.innerText); } catch (e) {}
    }
    const w = el.closest && el.closest('label');
    if (w) return clean(w.innerText);
    const c = el.closest && el.closest('div,section,fieldset,li,td');
    const lab = c && c.querySelector('label');
    return lab ? clean(lab.innerText) : '';
  };
  const ownerOf = (node) => {
    if (node.id) {
      try {
        const o = document.querySelector(
          '[aria-describedby~="' + node.id.replace(/"/g, '\\"') + '"]');
        if (o) return o;
      } catch (e) {}
    }
    // Fallback: climb, but accept an ancestor ONLY when it contains exactly
    // ONE scanned field. `querySelector` here searches the ancestor's whole
    // subtree in document order, so "first tagged descendant" is not an
    // association — on a flat form the walk reaches <body> in two hops and
    // would hand back field 0 for an error that belongs to nothing. A real
    // DOM run did exactly that: an orphan "Section could not be validated"
    // came back as index 0 / "Legal Name". A confidently WRONG index is worse
    // than none: the caller refills a field that was never the problem and
    // unmapped_error_count reads 0, so nothing flags the guess. The moment a
    // level holds more than one field the association is ambiguous — stop and
    // report the error unmapped instead of guessing.
    let p = node.parentElement, hops = 0;
    while (p && hops < 4) {
      const tag = p.tagName;
      if (tag === 'BODY' || tag === 'FORM' || tag === 'MAIN' || tag === 'HTML') return null;
      const fs = p.querySelectorAll('[data-jaidx]');
      if (fs.length === 1) return fs[0];
      if (fs.length > 1) return null;              // ambiguous — never guess
      p = p.parentElement; hops++;
    }
    return null;
  };
  const sel = '[role=alert], [aria-live=assertive], [data-automation-id*="rror"],'
            + ' [class*="error"], [class*="Error"], [id*="error"]';
  const out = [], seen = new Set();
  for (const node of Array.from(document.querySelectorAll(sel))) {
    if (!isVisible(node)) continue;
    if (node.querySelector(sel)) continue;           // a wrapper, not the error
    const msg = clean(node.innerText || node.textContent);
    if (!msg || msg.length > 300) continue;
    const owner = ownerOf(node);
    let idx = null;
    if (owner) {
      const tagged = owner.hasAttribute('data-jaidx')
        ? owner : (owner.closest ? owner.closest('[data-jaidx]') : null);
      if (tagged) {
        const n = parseInt(tagged.getAttribute('data-jaidx'), 10);
        if (!isNaN(n)) idx = n;
      }
    }
    const key = (idx === null ? 'x' : idx) + '|' + msg;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ index: idx,
               field_label: labelOf(owner) || labelOf(node.parentElement),
               message: msg });
  }
  return out;
}
"""


# Labels that are NOT a submit control even when the element is type=submit.
# Workday's "Save and Continue" is a type=submit button, so without this the
# submit path could click NEXT and advance the wizard. The bare step words are
# ANCHORED (whole label only) on purpose: "Submit and Continue" is a real
# submit and must NOT be excluded by the word "continue".
_NOT_SUBMIT_RE = re.compile(
    r"^(save\s*(and|&)\s*continue|save\s*(and|&)\s*next|save\s+for\s+later"
    r"|next(\s+step)?|continue|back|previous|save|review)"
    r"\s*(→|›|»|>)?$", re.I)

# Labels a wizard's NEXT control may carry (whole label; same anchoring logic).
_ADVANCE_RE = re.compile(
    r"^(save\s*(and|&)\s*continue|save\s*(and|&)\s*next|next(\s+step)?"
    r"|continue|proceed|review)\s*(→|›|»|>)?$", re.I)

# Anything that reads like an actual submit — never clicked by advance_step.
_SUBMITTY_RE = re.compile(r"submit|apply|finish|send\s+application", re.I)

# --- Entry and auth controls (Workday "Apply" -> chooser -> sign-in) -------
# Classified by VISIBLE TEXT only, anchored to the whole label like the step
# regexes above. Neither class is ever a submit: "Apply Manually" used to be
# picked by has-text('Apply') and "Create Account" (type=submit) by the
# generic selector, so submit_application could click them.
_ARROW = r"\s*(→|›|»|>)?$"
_ENTRY_RE = re.compile(
    r"^(apply|apply now|apply manually|start( your)? application)" + _ARROW, re.I)
_AUTH_RE = re.compile(
    r"^(sign in|log in|create account|create an account|verify|verify account"
    r"|resend( verification)?( email| code)?)" + _ARROW, re.I)
# Reveals an email sign-in form behind an SSO chooser (Snap's tenant shows
# Google / LinkedIn / email). Not a credential submit.
_EMAIL_SIGNIN_RE = re.compile(
    r"^(sign in|log in|continue|apply) with (email|e-mail)( address)?" + _ARROW,
    re.I)
# Never clicked by ANY tool: resume autofill and last-application reuse
# overwrite answers the agent cannot audit, and SSO buttons sign in as a
# third-party identity the user never configured.
_NEVER_CLICK_RE = re.compile(
    r"^(autofill with resume|use my last application)"
    r"|^(sign in|log in|sign up|continue|apply) with (google|linkedin|apple"
    r"|facebook|microsoft|indeed|github|seek|x|twitter)\b"
    r"|quick apply", re.I)
_SIGN_IN_LABEL_RE = re.compile(r"^(sign in|log in)" + _ARROW, re.I)
_CREATE_LABEL_RE = re.compile(r"^(create account|create an account)" + _ARROW, re.I)
_ENTRY_PRIORITY = ("apply manually", "start your application",
                   "start application", "apply now", "apply")


def _norm_label(label: str) -> str:
    return " ".join((label or "").split()).lower()


def _entry_rank(label: str) -> int:
    """"Apply Manually" beats a plain "Apply" when both are visible."""
    low = re.sub(_ARROW, "", _norm_label(label)).strip()
    return (_ENTRY_PRIORITY.index(low) if low in _ENTRY_PRIORITY
            else len(_ENTRY_PRIORITY))


def classify_control(label: str) -> str:
    """Class of a button-ish control from its visible text: "never",
    "auth_reveal", "entry", "auth", "advance", "submit" or "other". Pure; the
    single source both the finders and start_application use."""
    low = _norm_label(label)
    if not low:
        return "other"
    if _NEVER_CLICK_RE.search(low):
        return "never"
    if _EMAIL_SIGNIN_RE.match(low):
        return "auth_reveal"
    if _ENTRY_RE.match(low):
        return "entry"
    if _AUTH_RE.match(low):
        return "auth"
    if _ADVANCE_RE.match(low):
        return "advance"
    if _NOT_SUBMIT_RE.match(low):
        return "other"
    if _SUBMITTY_RE.search(low):
        return "submit"
    return "other"


_NOT_A_SUBMIT_CLASSES = ("never", "auth_reveal", "entry", "auth")

# A bare "Apply" / "Apply Now" opens an application on a job page, but on a
# filled application's last page it IS the submit. Which one it is depends on
# whether the tab has been filled (BrowserSession tracks that per tab).
_FINAL_APPLY_RE = re.compile(r"^(apply|apply now)" + _ARROW, re.I)
_BACK_RE = re.compile(r"^(back|previous)( step)?" + _ARROW, re.I)
_STEP_TEXT_RE = re.compile(r"\bstep \d+ of \d+\b", re.I)

# Workday domains, matched on a dot boundary. Credentials are only ever
# typed on these hosts; open_verification_link only opens these hosts.
_WORKDAY_DOMAINS = ("myworkdayjobs.com", "myworkday.com", "workday.com")


def workday_domain(host: str) -> str | None:
    """The Workday domain `host` belongs to (dot-boundary suffix), else
    None. ASCII only, so a homoglyph host never matches."""
    host = (host or "").lower().rstrip(".")
    if not host or not host.isascii():
        return None
    for d in _WORKDAY_DOMAINS:
        if host == d or host.endswith("." + d):
            return d
    return None


def is_workday_host(host: str) -> bool:
    return workday_domain(host) is not None


def _host_of(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""

# JS: every visible button-ish control in the frame, tagged data-jactl=n, with
# its visible label (innerText, else value, else aria-label: Workday's
# click_filter overlay carries only an aria-label), and whether it follows the
# first visible password input in DOM order (separates the in-form "Sign In"
# toggle from the page header's "Sign In"). Also returns visible headings and
# short notice/error texts for auth classification. Text only; never values.
#
# Tags are STABLE: an element keeps the number it was first given and only
# untagged elements get a new one, from a per-document counter that never
# reuses a number. Renumbering on every scan made a control dict from an
# earlier scan point at a DIFFERENT element after any re-scan (live Capital
# One run: a password show/hide toggle shifted the numbers between the scan
# and the Create Account click). A tag two elements share (a cloned node) is
# dropped from both and re-issued. A re-rendered node loses its tag, so a
# stale reference then resolves to nothing rather than to a neighbour.
# `in_dialog` / `has_dialog`: an open modal ([role=dialog], [aria-modal=true],
# <dialog open>), whose controls win over the page visible behind it.
_CONTROLS_JS = r"""
(emailIdx) => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const vis = (el) => {
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const FOLLOWS = Node.DOCUMENT_POSITION_FOLLOWING;
  const tagged = Array.from(document.querySelectorAll('[data-jactl]'));
  const count = new Map();
  tagged.forEach(e => {
    const t = e.getAttribute('data-jactl');
    count.set(t, (count.get(t) || 0) + 1);
  });
  let maxTag = -1;
  tagged.forEach(e => {
    const t = e.getAttribute('data-jactl');
    if (count.get(t) > 1) { e.removeAttribute('data-jactl'); return; }
    const v = parseInt(t, 10);
    if (v > maxTag) maxTag = v;
  });
  if (typeof window.__jactlNext !== 'number' || window.__jactlNext <= maxTag)
    window.__jactlNext = maxTag + 1;
  const dsel = '[role=dialog], [role=alertdialog], [aria-modal=true], dialog[open]';
  const has_dialog = Array.from(document.querySelectorAll(dsel)).some(vis);
  const pw = Array.from(document.querySelectorAll('input[type=password]')).find(vis) || null;
  const email = (emailIdx === null || emailIdx === undefined || emailIdx < 0) ? null
    : document.querySelector('[data-jaidx="' + emailIdx + '"]');
  const emailForm = email ? email.closest('form') : null;
  // Document order of every element, so Python can ask "is this checkbox
  // between the email field and the submit?" without another round-trip.
  const order = new Map();
  Array.from(document.querySelectorAll('*')).forEach((e, i) => order.set(e, i));
  const chrome = 'header, nav, footer, [role=banner], [role=navigation], [role=contentinfo]';
  const sel = 'button, [role=button], [role=link], a, input[type=submit], input[type=button]';
  const controls = [];
  for (const el of Array.from(document.querySelectorAll(sel))) {
    if (controls.length >= 300) break;
    if (el.disabled || !vis(el)) continue;
    let label = clean(el.innerText || el.value || '');
    if (!label) label = clean(el.getAttribute('aria-label'));
    if (!label || label.length > 60) continue;
    let tag = el.getAttribute('data-jactl');
    if (tag === null) {
      tag = String(window.__jactlNext++);
      el.setAttribute('data-jactl', tag);
    }
    const dlg = el.closest(dsel);
    controls.push({i: parseInt(tag, 10), label: label, tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase(),
      role: el.getAttribute('role') || '',
      pos: order.get(el),
      in_chrome: !!el.closest(chrome),
      same_form_email: !!(emailForm && el.closest('form') === emailForm),
      after_email: !!(email && (email.compareDocumentPosition(el) & FOLLOWS)),
      after_password: !!(pw && (pw.compareDocumentPosition(el) & FOLLOWS)),
      in_dialog: !!(dlg && vis(dlg))});
  }
  const field_pos = {};
  document.querySelectorAll('[data-jaidx]').forEach(e => {
    field_pos[e.getAttribute('data-jaidx')] = order.get(e);
  });
  const has_step = !!document.querySelector('[aria-current="step"]');
  const headings = Array.from(document.querySelectorAll('h1, h2, h3, [role=heading]'))
    .filter(vis).map(e => clean(e.innerText)).filter(t => t && t.length <= 80);
  const nsel = '[role=alert], [aria-live], [class*="error"], [class*="Error"], [class*="notice"], [class*="message"]';
  const notices = [];
  for (const el of Array.from(document.querySelectorAll(nsel))) {
    if (!vis(el) || el.querySelector(nsel)) continue;
    const t = clean(el.innerText);
    if (t && t.length <= 300 && notices.indexOf(t) === -1) notices.push(t);
  }
  return {controls: controls, headings: headings, notices: notices.slice(0, 10),
          field_pos: field_pos, has_step: has_step, has_dialog: has_dialog};
}
"""

# JS: the label of ONE located control, computed exactly as _CONTROLS_JS does,
# so _click_control can confirm a tag still sits on the intended control.
_CONTROL_LABEL_JS = r"""
(el) => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  let label = clean(el.innerText || el.value || '');
  if (!label) label = clean(el.getAttribute('aria-label'));
  return label;
}
"""

_EMAIL_LABEL_RE = re.compile(r"e-?mail", re.I)
_CONFIRM_LABEL_RE = re.compile(r"verify|confirm|re-?enter|repeat|again", re.I)
_CONSENT_LABEL_RE = re.compile(r"terms|privacy|consent|agree|acknowledg", re.I)
# Opt-ins that look like consent but are not needed to create an account.
_MARKETING_LABEL_RE = re.compile(
    r"marketing|newsletter|\bsms\b|text message|job alert|talent community"
    r"|future opportunit|linkedin|resume|promotional", re.I)
# Page chrome the scanner legitimately sees but that does not make a page a
# form: a header job-search box, and the header language picker (a live
# Salesforce/Snap Workday job page exposes a listbox button whose only label
# is its name, "languageSelectorButton").
_CHROME_LABEL_RE = re.compile(r"\bsearch\b|^language|languageselector", re.I)

# Auth outcome texts, checked against notices + body text after an auth
# submit. Specific on purpose: Workday's create-account page itself reads
# "Verify New Password" and "By activating your account", which must not
# count as a verification notice.
_VERIFY_TEXT_RE = re.compile(
    r"verification (email|link|code)|verify your (email|account|e-?mail address)"
    r"|check your (email|inbox)|(we('ve| have)?|has been) sent (you )?(an? )?"
    r"(email|verification|link|code)|confirm your (email|account)"
    r"|activation (email|link)", re.I)
_EXISTS_TEXT_RE = re.compile(
    r"already (exists|in use|registered|been registered|associated)"
    r"|account (with|for) this e-?mail( address)? (already )?exists", re.I)
_NO_ACCOUNT_TEXT_RE = re.compile(
    r"\bno (existing )?account|account (does not|doesn't) exist"
    r"|(user|e-?mail|account) (was )?not found|(isn't|is not|not) registered"
    r"|could(n't| not) find (an|your|that) account|unknown (user|e-?mail)", re.I)
# "Wrong email address or password": cannot tell a missing account from a bad
# password, so in auto mode it earns one create-account attempt.
_COMBINED_TEXT_RE = re.compile(
    r"(wrong|invalid|incorrect) (e-?mail( address)?|user ?name|login"
    r"|credentials)( (and|or) password)?", re.I)
_BAD_PW_TEXT_RE = re.compile(
    r"(wrong|invalid|incorrect) password|password (is |was )?(wrong|invalid"
    r"|incorrect)|account (is |has been )?locked|too many (failed |unsuccessful )?"
    r"(attempts|sign[- ]in)", re.I)


def form_fields(fields: list[dict]) -> list[dict]:
    """Fields that make a page a form (header search / language picker don't)."""
    return [f for f in fields or []
            if not _CHROME_LABEL_RE.search(f.get("label") or "")]


def _email_index(fields: list[dict]):
    return next((f["index"] for f in fields or []
                 if f.get("kind") == "text" and not f.get("sensitive")
                 and _EMAIL_LABEL_RE.search(f.get("label") or "")), None)


def _in_auth_form(c: dict) -> bool:
    """Is this control part of the auth form rather than page navigation?
    Same <form> as the email field, or after it in DOM order and outside
    header/nav/footer. A control missing the relation data never qualifies."""
    if c.get("same_form_email"):
        return True
    return bool(c.get("after_email")) and not c.get("in_chrome")


def _consent_indexes(fields: list[dict], field_pos: dict, email_index,
                     submit: dict | None) -> list[int]:
    """Checkboxes to tick on CREATE ACCOUNT only: label reads as terms /
    privacy / consent and NOT as a marketing opt-in, and the box sits between
    the email field and the auth submit. Of those, required ones (required /
    aria-required / a "*" in the label) are ticked; when none is marked
    required, a SINGLE such box is ticked too, because live Workday tenants
    (Salesforce, Zillow) render the mandatory privacy acknowledgement with no
    required marker at all. Positions unknown -> nothing is ticked."""
    if email_index is None or not submit or submit.get("pos") is None:
        return []
    pos = field_pos or {}
    lo = pos.get(str(email_index), pos.get(email_index))
    hi = submit["pos"]
    if lo is None:
        return []
    cands = []
    for f in fields or []:
        if f.get("kind") != "checkbox":
            continue
        lab = f.get("label") or ""
        if not _CONSENT_LABEL_RE.search(lab) or _MARKETING_LABEL_RE.search(lab):
            continue
        p = pos.get(str(f["index"]), pos.get(f["index"]))
        if p is None or not (lo < p < hi):
            continue
        cands.append(f)
    req = [f["index"] for f in cands if f.get("required") or "*" in (f.get("label") or "")]
    if req:
        return req
    return [cands[0]["index"]] if len(cands) == 1 else []


def _auth_layout(fields: list[dict], controls: list[dict],
                 headings: list[str], field_pos: dict | None = None) -> dict:
    """Pure: where the auth form's parts are, and which mode it is in.
    mode is "create_account", "sign_in" or "unknown"; `available` lists the
    modes whose buttons are visible (Workday shows Sign In alongside Create
    Account and toggles between them)."""
    pws = [f for f in fields or [] if f.get("sensitive")]
    password_index = confirm_index = None
    if pws:
        conf = [f for f in pws[1:] if _CONFIRM_LABEL_RE.search(f.get("label") or "")]
        confirm = conf[0] if conf else (pws[1] if len(pws) > 1 else None)
        main = [f for f in pws if f is not confirm]
        password_index = main[0]["index"] if main else None
        confirm_index = confirm["index"] if confirm else None
    email_index = _email_index(fields)
    heads = [_norm_label(h) for h in headings or []]
    labels = [_norm_label(c.get("label")) for c in controls or []]
    available = []
    if any(_SIGN_IN_LABEL_RE.match(lab) for lab in labels):
        available.append("sign_in")
    if any(_CREATE_LABEL_RE.match(lab) for lab in labels):
        available.append("create_account")
    if confirm_index is not None or any(_CREATE_LABEL_RE.match(h) for h in heads):
        mode = "create_account"
    elif password_index is not None and (
            any(_SIGN_IN_LABEL_RE.match(h) for h in heads)
            or any(_SIGN_IN_LABEL_RE.match(_norm_label(c.get("label")))
                   and c.get("type") == "submit" for c in controls or [])):
        mode = "sign_in"
    else:
        mode = "unknown"
    consent = []
    if mode == "create_account":
        sub = _pick_auth_controls(controls, "create_account", submit=True)
        consent = _consent_indexes(fields, field_pos or {}, email_index,
                                   sub[0] if sub else None)
    return {"mode": mode, "available": available, "email_index": email_index,
            "password_index": password_index, "confirm_index": confirm_index,
            "consent_indexes": consent}


def _is_auth_page(fields: list[dict], controls: list[dict]) -> bool:
    """A password field, or an email field plus an auth control that belongs
    to the email field's form (a header "Log in" link does not)."""
    if any(f.get("sensitive") for f in fields or []):
        return True
    if _email_index(fields) is None:
        return False
    return any(classify_control(c.get("label")) == "auth" and _in_auth_form(c)
               for c in controls or [])


def _entry_targets(state: dict, kind: str) -> list[dict]:
    """Ranked `kind` ("entry" / "auth_reveal") controls to click. When an
    open modal holds such controls, ONLY those: the job page's "Apply" stays
    visible behind Workday's "Start Your Application" modal. A modal without
    any (a cookie banner) does not hide the page's controls."""
    ctls = [c for c in state.get("controls") or []
            if (c.get("cls") or classify_control(c.get("label"))) == kind]
    in_dialog = [c for c in ctls if c.get("in_dialog")]
    if in_dialog:
        ctls = in_dialog
    if kind == "entry":
        ctls = sorted(ctls, key=lambda c: _entry_rank(c.get("label")))
    return ctls


def _entry_landed(state: dict) -> bool:
    """start_application's terminal pages: blocked, sign-in gate, or form."""
    if state.get("blocked"):
        return True
    fields = state.get("fields") or []
    return _is_auth_page(fields, state.get("controls")) or bool(form_fields(fields))


def _entry_progress(state: dict, before: dict) -> bool:
    """Did an entry click get somewhere? Landed, or an entry / email-reveal
    control appeared that was not there before (the chooser rendered), or a
    modal opened holding one. A bare URL change is NOT progress: Workday's
    /apply route renders its form seconds after the URL flips."""
    if _entry_landed(state):
        return True

    def labels(st):
        return {(_norm_label(c.get("label")), bool(c.get("in_dialog")))
                for c in st.get("controls") or []
                if (c.get("cls") or classify_control(c.get("label")))
                in ("entry", "auth_reveal")}
    return bool(labels(state) - labels(before))


def _pick_auth_controls(controls: list[dict], mode: str, submit: bool,
                        current: str | None = None) -> list[dict]:
    """Ranked candidates for the `mode` button. submit=True: the button that
    sends the credentials (type=submit first, then DOM order), restricted to
    controls after the password field when one exists. submit=False: the
    in-form toggle from the KNOWN mode `current`: never type=submit, only
    controls inside the auth form, and never one whose label equals the
    current mode's submit label (Workday's click_filter overlay)."""
    rx = _SIGN_IN_LABEL_RE if mode == "sign_in" else _CREATE_LABEL_RE
    cands = [c for c in controls or [] if rx.match(_norm_label(c.get("label")))]
    if submit:
        if any(c.get("after_password") for c in controls or []):
            # a password field is on the page: the header's "Sign In" (before
            # it) is navigation, not the form's submit
            cands = [c for c in cands if c.get("after_password")]
        return sorted(cands, key=lambda c: 0 if c.get("type") == "submit" else 1)
    if current not in ("sign_in", "create_account") or current == mode:
        return []
    if any(c.get("type") == "submit" for c in cands):
        # The wanted mode's own submit is already showing: a same-label
        # non-submit next to it is its overlay, and clicking it would SUBMIT.
        return []
    cur_sub = _pick_auth_controls(controls, current, submit=True)
    cur_label = _norm_label(cur_sub[0]["label"]) if cur_sub else None
    cands = [c for c in cands if c.get("type") != "submit"
             and (c.get("after_password") or _in_auth_form(c))
             and not c.get("in_chrome")
             and _norm_label(c.get("label")) != cur_label]
    return sorted(cands, key=lambda c: 0 if c.get("after_password") else 1)


def _classify_auth_outcome(state: dict) -> str:
    """Pure: what an auth submit led to. One of blocked, verification,
    exists, no_account, combined, bad_password, authenticated, still_auth,
    unknown."""
    if state.get("blocked"):
        return "blocked"
    text = " ".join(state.get("notices") or []) + " " + (state.get("body") or "")
    if _VERIFY_TEXT_RE.search(text):
        return "verification"
    if _EXISTS_TEXT_RE.search(text):
        return "exists"
    if _NO_ACCOUNT_TEXT_RE.search(text):
        return "no_account"
    if _COMBINED_TEXT_RE.search(text):
        return "combined"
    if _BAD_PW_TEXT_RE.search(text):
        return "bad_password"
    fields = state.get("fields") or []
    if any(f.get("sensitive") for f in fields):
        return "still_auth"
    if form_fields(fields):
        return "authenticated"
    return "unknown"


def _auth_settled(state: dict, before_notices: list) -> bool:
    """Has an auth submit produced a result worth classifying? The password
    fields are gone, a new notice/error appeared, a verification message
    shows, or a CAPTCHA appeared. Mere DOM stability is not enough: a
    spinner holds still too, and classifying it as "unconfirmed" would waste
    one of the two allowed submits."""
    if state.get("blocked"):
        return True
    if not any(f.get("sensitive") for f in state.get("fields") or []):
        return True
    notices = list(state.get("notices") or [])
    if notices and notices != list(before_notices or []):
        return True
    return bool(_VERIFY_TEXT_RE.search(state.get("body") or ""))


def _redact(text: str, secrets) -> str:
    for s in secrets:
        if s:
            text = re.sub(re.escape(s), "<redacted>", text, flags=re.I)
    return text


def _message_excerpt(state: dict, secrets=()) -> str:
    """Visible notice/error text near the form (page text, never input
    values), up to 200 chars; falls back to the verification sentence in the
    body. Credential strings are redacted in case the page echoes them."""
    text = " | ".join(state.get("notices") or [])
    if not text:
        m = _VERIFY_TEXT_RE.search(state.get("body") or "")
        if m:
            body = state["body"]
            text = body[max(0, m.start() - 80):m.end() + 100]
    return _redact(text, secrets)[:200]


def verification_link_allowed(url: str, tab_host: str = "") -> bool:
    """Only https links on a Workday domain (dot-boundary suffix), with an
    ASCII host, no userinfo and no explicit port. When the active tab is on a
    Workday host, the link must be on the SAME Workday domain."""
    try:
        p = urlparse(url or "")
        port = p.port
    except Exception:
        return False
    if p.scheme != "https" or port is not None:
        return False
    netloc = p.netloc or ""
    if "@" in netloc or p.username or p.password:
        return False
    host = (p.hostname or "")
    if not host.isascii() or not netloc.isascii():
        return False
    dom = workday_domain(host)
    if dom is None:
        return False
    tab_dom = workday_domain(tab_host)
    if tab_dom is not None and tab_dom != dom:
        return False
    return True


# Words that carry no initial in an abbreviation: "University of California,
# Santa Barbara" abbreviates to UCSB, not UOCSB. Without this the live Snap
# run could not match "UCSB" to its own school list.
_INITIALS_STOPWORDS = {"of", "the", "and", "at", "in", "for", "a", "an", "de", "la"}

# Pick-from-a-list kinds. A mismatch here is a PROVEN failure (the widget
# either holds the option or it doesn't), so it is reported as "uncommitted"
# rather than as a filled-but-unverified value.
_CLOSED_FILL_KINDS = {"select", "radio", "checkbox", "combobox"}


def _values_match(expected: str, observed: str) -> bool:
    """Did the field end up holding `expected`, allowing for site formatting?

    Sites normalize as they commit: a phone typed as 4155551212 reads back as
    "(415) 555-1212", a currency field gains "$", a select shows the option
    label with trailing whitespace. A strict comparison turns every one of
    those into a bogus retry. So: casefold, drop non-alphanumerics, and accept
    when either string contains the other. Two empty strings never match — an
    empty read-back is the signal that the fill did not land.
    """
    a = re.sub(r"[^a-z0-9]+", "", (expected or "").casefold())
    b = re.sub(r"[^a-z0-9]+", "", (observed or "").casefold())
    if not a or not b:
        return False
    return a in b or b in a


SENSITIVE_MASK = "********"
_SECRET_LABEL_RE = re.compile(r"pass(word|code)", re.I)


def _fill_result(index: int, kind: str, value: str, observed,
                 expected: str | None = None, sensitive: bool = False) -> dict:
    """Turn a post-fill read-back into an HONEST result.

    Why a proven failure returns status "uncommitted" and not just
    verified=false: the skills act on `status`. Returning
    {"status": "filled", "verified": false} would mean an un-updated skill
    reads a known failure as a success — we would build the detector and then
    throw the detection away. "uncommitted" is already in both skills'
    vocabulary with correct handling (one corrective re-fill, then flag), so
    proven failures are safe with no SKILL.md edit. Keep that property.

    `observed_value` is REQUIRED on every non-verified result: a bare boolean
    cannot tell "empty, refill" from "wrong option, refill differently".
    """
    if sensitive:
        # Passwords: verify on PRESENCE (the read-back is already masked, so
        # equality is meaningless) and never echo the sent or observed value.
        res = {"index": index, "kind": kind, "status": "filled",
               "value": SENSITIVE_MASK, "sensitive": True}
        if observed is None:
            res["verified"] = None
            res["note"] = "fill was issued but could not be verified"
        elif str(observed).strip():
            res["verified"] = True
        else:
            res.update(verified=False, status="uncommitted",
                       observed_value="")
        return res
    res = {"index": index, "kind": kind, "status": "filled", "value": value}
    if observed is None:
        # The widget exposes no readable value in this DOM. That is not
        # evidence of failure, so don't claim one — say we can't tell.
        res["verified"] = None
        res["note"] = ("fill was issued but this widget exposes no readable "
                       "value — could not verify; screenshot-audit if the "
                       "field matters")
        return res
    exp = value if expected is None else expected
    if _values_match(exp, observed):
        res["verified"] = True
        return res
    res["verified"] = False
    res["observed_value"] = observed
    if kind in _CLOSED_FILL_KINDS or not str(observed).strip():
        res["status"] = "uncommitted"
    return res


class BrowserSession:
    def __init__(self) -> None:
        self._pw = None
        self.context = None
        # Multi-tab: each queued job can get its own tab so a filled form is
        # never destroyed by opening the next job. A tab is
        # {"page": Page, "fields": {index -> {"frame","kind","native"}},
        #  "company","title","url"}. All the field/submit methods act on the
        # ACTIVE tab through the `page` / `fields` properties, so they needed no
        # per-tab plumbing.
        self.tabs: dict[int, dict] = {}
        self.active_id: int | None = None
        self._next_id = 0
        # Auth-attempt ledger (tenant host -> [{t, kind, outcome}]): timestamps
        # and outcomes only, never credentials. Loaded lazily from the active
        # profile's runtime dir; `_ledger_path = False` keeps it in memory.
        self._ledger: dict | None = None
        self._ledger_path = None

    @property
    def filled(self) -> bool:
        """Has anything been typed/picked/uploaded on the ACTIVE tab since it
        was opened? Decides whether a bare "Apply" is an entry or a submit."""
        t = self.tabs.get(self.active_id)
        return bool(t and t.get("filled"))

    def _mark_filled(self) -> None:
        t = self.tabs.get(self.active_id)
        if t is not None:
            t["filled"] = True

    @property
    def page(self):
        t = self.tabs.get(self.active_id)
        return t["page"] if t else None

    @property
    def fields(self) -> dict:
        # index -> {"frame": Frame, "kind": str} for the ACTIVE tab
        t = self.tabs.get(self.active_id)
        return t["fields"] if t else {}

    async def _ensure(self, headless: bool = False) -> None:
        # Default is a VISIBLE, maximized window so the user can watch and step
        # in (e.g. to solve a CAPTCHA or log in). headless=True is only used by
        # automated tests.
        if self._pw is None:
            self._pw = await async_playwright().start()
        if self.context is None:
            # Persistent profile: cookies/logins survive across sessions.
            config.BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            self.context = await self._pw.chromium.launch_persistent_context(
                user_data_dir=str(config.BROWSER_PROFILE_DIR),
                headless=headless, args=["--start-maximized"],
                user_agent=_UA, no_viewport=True)

    async def _open_tab(self) -> int:
        """Create a fresh tab, make it active, and return its id."""
        page = await self.context.new_page()
        tab_id = self._next_id
        self._next_id += 1
        self.tabs[tab_id] = {"page": page, "fields": {}, "filled": False,
                             "company": "", "title": "", "url": ""}
        self.active_id = tab_id
        return tab_id

    async def open_job(self, url: str, new_tab: bool = False,
                       company: str = "") -> dict:
        # Start of an apply session: refresh resume.txt from resume.pdf so the
        # reasoning text matches the document that gets uploaded.
        resume_synced = config.sync_resume_text_from_pdf()
        await self._ensure()
        # Open in a NEW tab when asked (batch mode keeps every filled form
        # alive) or when there is no usable active tab; otherwise reuse the
        # active tab (single-job apply, retries, re-opening the same URL).
        if new_tab or self.active_id not in self.tabs:
            await self._open_tab()
        page = self.page
        await page.goto(url, timeout=45000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)  # let JS-rendered forms settle
        self.fields.clear()
        host = urlparse(url).hostname or ""
        ats = "greenhouse" if "greenhouse" in host else \
              "lever" if "lever" in host else \
              "ashby" if "ashby" in host else \
              "workday" if "workday" in host else "generic"
        title = await page.title()
        tab = self.tabs[self.active_id]
        tab.update(url=url, title=title, filled=False,
                   company=company or tab.get("company", ""))
        # One-shot: also return the intervention check and the parsed form so the
        # common open path is a single round-trip instead of three. read_form()
        # populates self.fields as a side effect. If the page later changes,
        # re-call read_form / check_for_intervention.
        intervention = await self.detect_blockers()
        fields = await self.read_form()
        return {"url": url, "title": title, "detected_ats": ats,
                "tab_id": self.active_id,
                "resume_synced": resume_synced,
                "intervention": intervention, "fields": fields,
                "note": ("intervention + fields are included — no need to call "
                         "check_for_intervention or read_form again unless the "
                         "page navigates or changes. Opened in "
                         + ("a new tab" if new_tab else "the active tab")
                         + "; manage tabs with list_tabs / switch_tab / "
                         "close_tab.")}

    async def list_tabs(self) -> list[dict]:
        """List open tabs — in batch mode each is one job's filled/loaded form."""
        out = []
        for tid, t in self.tabs.items():
            page = t["page"]
            try:
                closed = page.is_closed()
            except Exception:
                closed = True
            out.append({"tab_id": tid, "company": t.get("company", ""),
                        "title": t.get("title", ""),
                        "url": (page.url if not closed else t.get("url", "")),
                        "active": tid == self.active_id, "closed": closed})
        return out

    async def switch_tab(self, tab_id: int) -> dict:
        """Make `tab_id` the active tab, bring it to the front, and re-read its
        form (field indexes are per-tab and only valid after a read on that
        page)."""
        t = self.tabs.get(tab_id)
        if t is None:
            raise ValueError(f"Unknown tab_id {tab_id}. Call list_tabs() first.")
        self.active_id = tab_id
        try:
            await t["page"].bring_to_front()
        except Exception:
            pass
        fields = await self.read_form()
        return {"tab_id": tab_id, "url": t["page"].url,
                "title": t.get("title", ""), "company": t.get("company", ""),
                "fields": fields}

    async def close_tab(self, tab_id: int) -> dict:
        """Close a tab (e.g. after a job is verified submitted)."""
        t = self.tabs.pop(tab_id, None)
        if t is None:
            return {"status": "unknown_tab", "tab_id": tab_id}
        try:
            await t["page"].close()
        except Exception:
            pass
        if self.active_id == tab_id:
            self.active_id = next(reversed(self.tabs), None) if self.tabs else None
        return {"status": "closed", "tab_id": tab_id, "active_id": self.active_id}

    async def read_form(self, values_only: bool = False) -> list[dict]:
        """Read the live page + all iframes; return a generic field list.

        Side effect: tags elements with data-jaidx so fill_field/upload_resume
        can act on them. Re-call after any navigation or page reload.

        `values_only=True` returns a lean payload — only
        `{index, kind, label, current_value}` per field — for cheap
        verification re-reads (confirming a fill took) without re-sending
        option lists and field metadata that a full first read already
        supplied. Regardless of the flag, long native ``<select>`` option
        lists (> `_INLINE_OPTS_MAX`) are collapsed to
        `options_count`/`options_sample`/`options_note`; fetch the full list
        with `get_field_options(index)`. `self.fields` is fully populated
        either way, so `fill_field`/verification work on every index.
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
                # `widget` is the physical shape ("listbox" for a Workday-style
                # button dropdown); fill_field dispatches on it, never on the
                # site/ATS.
                self.fields[d["index"]] = {"frame": frame, "kind": d["kind"],
                                           "native": d.get("native", True),
                                           "widget": d.get("widget"),
                                           "sensitive": bool(d.get("sensitive")),
                                           "label": d.get("label")}
                counter = max(counter, d["index"] + 1)
                if values_only:
                    lean = {"index": d["index"], "kind": d["kind"],
                            "label": d.get("label"),
                            "current_value": d.get("current_value", "")}
                    if d.get("sensitive"):
                        lean["sensitive"] = True
                    all_fields.append({k: v for k, v in lean.items() if v is not None})
                    continue
                # Collapse long native <select> option lists — country/year/
                # school pickers otherwise dominate the payload; the full list
                # is a get_field_options(index) call away.
                opts = d.get("options")
                if opts and len(opts) > _INLINE_OPTS_MAX:
                    labels = [o["label"] if isinstance(o, dict) else o for o in opts]
                    d["options"] = None
                    d["options_count"] = len(opts)
                    d["options_sample"] = labels[:3] + ["…"] + labels[-1:]
                    d["options_note"] = (
                        f"long list summarized — call get_field_options({d['index']}) "
                        f"for all {len(opts)} options")
                # drop None/undefined keys for a clean payload
                all_fields.append({k: v for k, v in d.items() if v is not None})
        return all_fields

    def _locator(self, index: int):
        meta = self.fields.get(index)
        if not meta:
            raise ValueError(f"Unknown field index {index}. Call read_form() first.")
        return meta["frame"].locator(f'[data-jaidx="{index}"]'), meta["kind"]

    async def _read_back(self, index: int):
        """Read the value the field at `index` is ACTUALLY holding right now.

        Returns "" for "holds nothing", a string for what it holds, and None
        for "this DOM exposes no readable value" (element gone, or a widget
        with no value-bearing surface) — a distinction fill_field turns into
        verified=false vs verified=null. Comboboxes go through _COMBO_VALUE_JS,
        which itself branches to the shared listbox cascade for <button>
        widgets, so fill-time verification and read_form's current_value are
        computed by the same code.
        """
        meta = self.fields.get(index) or {}
        frame = meta.get("frame")
        if frame is None:
            return None
        js = _COMBO_VALUE_JS if meta.get("kind") == "combobox" else _FIELD_VALUE_JS
        try:
            obs = await frame.evaluate(js, index)
        except Exception:
            return None
        if meta.get("sensitive") and obs:
            return "********"       # belt-and-braces: never surface a secret
        return obs

    async def _probe(self, index: int, mark: bool = False):
        """Live sensitivity probe of the element (None = unknown)."""
        frame = (self.fields.get(index) or {}).get("frame")
        if frame is None:
            return None
        try:
            return await frame.evaluate(_SENSITIVE_PROBE_JS, [index, mark])
        except Exception:
            return None

    async def fill_field(self, index: int, value: str) -> dict:
        """Fill any field by index, then READ THE VALUE BACK and report what
        actually happened. Dispatches on the field kind and — for dropdowns —
        on the `widget` shape: text -> type; select -> choose option;
        radio/checkbox -> check (with a label/wrapper proxy click when the
        native check is intercepted); combobox -> react-select typeahead, or
        the listbox open/pick flow when `widget` is "listbox".

        For radio/checkbox, pass "true"/"yes" to check or "false"/"no" to
        uncheck.

        Every result carries `verified`: true (read back and matches), false
        (read back and does NOT match — `observed_value` says what is there
        instead), or null (nothing readable to verify against). A verified=false
        result on a pick-from-a-list field comes back with status "uncommitted"
        rather than "filled" — see _fill_result for why that distinction is
        load-bearing.
        """
        loc, kind = self._locator(index)
        meta = self.fields.get(index, {})
        probe = await self._probe(index) if kind == "text" else None
        if (meta.get("sensitive") or _SECRET_LABEL_RE.search(meta.get("label") or "")
                or (probe or {}).get("sensitive")):
            # The model must never pass a password literal through the generic
            # fill path (it would sit in the transcript). Credentials are
            # filled server-side by fill_credential.
            return {"index": index, "status": "use_fill_credential",
                    "sensitive": True,
                    "error": "password field: use fill_credential, never a "
                             "literal value"}
        self._mark_filled()
        if kind == "combobox":
            # Dispatch on the WIDGET, never on the site: aria-haspopup=listbox
            # is an ARIA contract honored by Workday, Oracle, SuccessFactors,
            # Radix and MUI alike, so one widget branch fixes all of them at
            # once — and it is strictly less code than an ATS gate, which would
            # also break this module's no-per-site-selectors invariant.
            if meta.get("widget") == "listbox":
                return await self._fill_listbox(index, loc, value)
            return await self._fill_combobox(index, loc, value)
        if kind in ("radio", "checkbox"):
            return await self._fill_toggle(index, loc, kind, value)
        if kind == "select":
            try:
                await loc.select_option(label=value)
            except Exception:
                await loc.select_option(value=value)
        else:  # text / textarea / contenteditable
            await loc.fill(value)
            # Verify what SURVIVES leaving the field, not the keystrokes: a
            # search/prompt typeahead that renders as a plain <input> holds
            # the typed text until blur and then drops it unless a result
            # was picked. Reading before blur is how live Workday School /
            # Field of Study came back verified:true over empty boxes.
            await self._blur(index)
        observed = await self._read_back(index)
        res = _fill_result(index, kind, value, observed)
        if kind == "text" and observed == "" and value:
            res["note"] = ("the typed value did not survive leaving the field: "
                           "a search/prompt widget that needs a pick from its "
                           "results, or a validator rejected it")
        return res

    async def _blur(self, index: int) -> None:
        """Blur the field at `index` if it holds focus (focusout fires, so
        framework commit/validate-on-blur handlers run), then let it settle.
        Uses el.blur(), not Tab: Tab would focus, and possibly open, the NEXT
        widget."""
        frame = (self.fields.get(index) or {}).get("frame")
        if frame is None:
            return
        try:
            await frame.evaluate(_BLUR_JS, index)
            await self.page.wait_for_timeout(150)
        except Exception:
            pass

    async def fill_sensitive(self, index: int, value: str,
                             require_password: bool = False,
                             mark: bool = True) -> dict:
        """Fill a field with a server-side secret; the value never leaves
        here. Result is masked and verified on presence only. The target is
        re-checked LIVE immediately before typing (a type flip or re-render
        invalidates the scan): with `require_password` it must be a real
        <input type=password>; without it (email), it must NOT be sensitive.
        Every failure returns only the exception TYPE: Playwright messages
        embed the typed value in their call log."""
        meta = self.fields.get(index, {})
        out = {"index": index, "sensitive": True}
        try:
            loc, kind = self._locator(index)
            if kind != "text":
                return {**out, "status": "unsupported_field"}
            probe = await self._probe(index, mark=require_password)
            if require_password:
                if not (probe and probe.get("password")):
                    return {**out, "status": "not_a_password_field"}
            elif meta.get("sensitive") or probe is None or probe.get("sensitive"):
                return {**out, "status": "sensitive_target"}
            if mark:
                self._mark_filled()
            await loc.fill(value)
            observed = await self._read_back(index)
            return _fill_result(index, kind, value, observed,
                                sensitive=require_password)
        except Exception as e:
            return {**out, "status": "error", "error": type(e).__name__}

    async def _click_toggle_proxy(self, index: int) -> bool:
        """Click the element a HUMAN would click for the toggle at `index`
        (label[for] -> enclosing label -> parent wrapper). Returns whether a
        proxy was found and clicked. Exactly ONE click: on a toggle control a
        second click un-sets what the first one set, so never blind-double-
        click — verify instead."""
        frame = (self.fields.get(index) or {}).get("frame")
        if frame is None:
            return False
        try:
            if not await frame.evaluate(_PROXY_JS, index):
                return False
            await frame.locator('[data-japroxy="1"]').first.click(timeout=2000)
            return True
        except Exception:
            return False
        finally:
            try:
                await frame.evaluate(
                    "() => document.querySelectorAll('[data-japroxy]')"
                    ".forEach(e => e.removeAttribute('data-japroxy'))")
            except Exception:
                pass

    async def _fill_toggle(self, index: int, loc, kind: str, value: str) -> dict:
        """Check/uncheck a radio or checkbox and verify it stuck.

        Workday paints its own radio over the real <input>, so Playwright's
        .check() either RAISES ("element intercepts pointer events") or clicks
        through without firing the React handler — which is how a live run
        reported every radio on step 2 as "filled" while none of them were.
        Hence: try the native action, tolerate the raise, then fall back to a
        single proxy click, then read the state back."""
        native = self.fields[index].get("native", True)
        check = str(value).strip().lower() in ("true", "yes", "1", "checked", "on")
        if not native:
            # Segmented button option (Ashby Yes/No): the index already points
            # at the specific option, so selecting it is just a click —
            # .check() would fail on a <button>. This branch used to return
            # "filled" unconditionally; it verifies now too.
            await loc.click()
            check = True
        else:
            try:
                if check:
                    await loc.check()
                else:
                    await loc.uncheck()
            except Exception:
                pass  # overlaid control — the proxy click below is the real fix
            try:
                state = await loc.is_checked()
            except Exception:
                state = None
            if state is None or state != check:
                await self._click_toggle_proxy(index)
        observed = await self._read_back(index)
        if observed is None:
            return _fill_result(index, kind, value, None)
        ok = (observed == "checked") if check else (observed != "checked")
        if ok:
            return {"index": index, "kind": kind, "status": "filled",
                    "value": value, "verified": True}
        return {"index": index, "kind": kind, "status": "uncommitted",
                "value": value, "verified": False, "observed_value": observed,
                "note": ("the control did not change state — neither the native "
                         "check nor a label/wrapper proxy click committed it; "
                         "re-fill once, then flag for the user")}

    @staticmethod
    def _match_option(value: "str | list[str]", options: list[str]) -> str | None:
        """Pick the option that actually MEANS `value`: exact (case-insensitive)
        first, then prefix, then abbreviation (initials — 'US' ↔ 'United
        States', 'UCSB' ↔ 'University of California, Santa Barbara'). Returns
        None when nothing matches; never falls back to 'first option in the
        filtered list' (that is how 'US' once selected 'AUStralia').

        `value` may be a LIST of candidate spellings. Tiers are tried
        OUTER, candidates INNER: every candidate gets an exact match attempt
        before any candidate is allowed a prefix match, and so on. Candidate-
        outer ordering would let an earlier candidate's prefix hit beat a later
        candidate's exact hit, which destroys exactly the tiering that stopped
        'US' from selecting 'AUStralia'.

        The initials tier tries two spellings of each option's initials: the
        plain one and one with linking stopwords dropped ('of', 'the', ...),
        because 'University of California, Santa Barbara' initials to UOCSB
        plainly but UCSB the way humans actually abbreviate it. An initials
        string shorter than 2 chars is ignored so one-letter noise can't match.
        """
        cands = [value] if isinstance(value, str) else list(value or [])
        pairs = [(c, (c or "").strip().lower()) for c in cands]
        pairs = [(raw, low) for raw, low in pairs if low]
        if not pairs:
            return None

        def initials(s: str, drop_stopwords: bool = False) -> str:
            words = re.findall(r"[A-Za-z]+", s or "")
            if drop_stopwords:
                words = [w for w in words if w.lower() not in _INITIALS_STOPWORDS]
            return "".join(w[0] for w in words).lower()

        for _, low in pairs:                                   # tier 1: exact
            for o in options:
                if o.strip().lower() == low:
                    return o
        for _, low in pairs:                                   # tier 2: prefix
            for o in options:
                if o.strip().lower().startswith(low):
                    return o
        for raw, low in pairs:                                 # tier 3: initials
            cand_initials = {s for s in (initials(raw), initials(raw, True))
                             if len(s) >= 2}
            for o in options:
                ol = o.strip().lower()
                opt_initials = {s for s in (initials(o), initials(o, True))
                                if len(s) >= 2}
                if low in opt_initials or ol in cand_initials:
                    return o
        if len(options) == 1 and any(low in options[0].lower() for _, low in pairs):
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
            observed = await self._read_back(index)
            return {"index": index, "kind": "combobox", "status": "unmatched",
                    "value": value, "options": full[:80], "verified": False,
                    "observed_value": "" if observed is None else observed,
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
        committed = await self._read_back(index)
        res = _fill_result(index, "combobox", value, committed, expected=target)
        if res.get("verified") is True:
            res["value"] = committed
            return res
        res["matched_option"] = target
        if res.get("verified") is False:
            res["note"] = ("clicked an option but the widget did not commit it "
                           "(observed_value shows what it holds) — re-fill once, "
                           "then flag for the user")
        return res

    async def _fill_listbox(self, index: int, loc, value: str) -> dict:
        """Fill a listbox-button dropdown (`widget` == "listbox": Workday,
        Oracle, SuccessFactors, Radix, MUI — anything honoring
        aria-haspopup=listbox).

        Enumerate FIRST: open the widget, and if aria-controls/aria-owns
        resolved a real popup (so the option list is genuinely this widget's,
        not a stray on-screen [role=option] sweep), match an option by meaning
        and click it. Only if the list could not be scoped, or nothing matched,
        fall back to typing. The typeahead uses delay=100 deliberately — this
        widget family drops characters typed faster, and a dropped character
        silently filters to the wrong option; do not lower it.
        """
        frame = self.fields[index]["frame"]
        opened = await self._open_listbox(index, loc)
        options: list[str] = []
        try:
            scoped = bool(await frame.evaluate(_COMBO_SCOPED_JS, index))
        except Exception:
            scoped = False
        if scoped and opened is not False:
            try:
                options = await frame.evaluate(_COMBO_OPTIONS_JS, index) or []
            except Exception:
                options = []
            target = self._match_option(value, options)
            if target is not None:
                clicked = False
                for exact in (True, False):
                    try:
                        await frame.get_by_role("option", name=target,
                                                exact=exact).first \
                            .click(timeout=2000 if exact else 1500)
                        clicked = True
                        break
                    except Exception:
                        continue
                if clicked:
                    await self.page.wait_for_timeout(300)
                    observed = await self._read_back(index)
                    res = _fill_result(index, "combobox", value, observed,
                                       expected=target)
                    if res.get("verified") is not False:
                        res["widget"] = "listbox"
                        if res.get("verified") is True:
                            res["value"] = observed or value
                        return res
        # Fallback: type into the open widget and commit with Enter. This is
        # also the ONLY path when ARIA scoping failed — an unscoped option list
        # can belong to a different widget entirely, so clicking from it would
        # answer the wrong question.
        await self.page.keyboard.type(value, delay=100)
        await self.page.wait_for_timeout(600)
        await self.page.keyboard.press("Enter")
        await self.page.wait_for_timeout(300)
        observed = await self._read_back(index)
        res = _fill_result(index, "combobox", value, observed)
        res["widget"] = "listbox"
        if res.get("verified") is True:
            res["value"] = observed or value
            return res
        if res.get("verified") is False:
            # Leave the widget closed so the next field isn't typed into this
            # one's open popup.
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            if scoped and options:
                res["options"] = options[:80]
            # "didn't open" and "didn't commit" need different corrective
            # actions, so never report one as the other.
            res["note"] = (
                "the dropdown did not OPEN (aria-expanded stayed false), so no "
                "option could be picked — something is covering the control or "
                "another popup is in the way; screenshot before retrying"
                if opened is False else
                "the dropdown did not commit a value — re-fill once with one of "
                "`options` verbatim when they are listed, then flag for the user")
        return res

    async def _open_listbox(self, index: int, loc):
        """Make sure a listbox popup is OPEN, and report whether it is:
        True (open), False (still closed), None (the widget doesn't publish
        aria-expanded, so proceed blind).

        A listbox button is a TOGGLE. Clicking it unconditionally CLOSES a
        popup that is already open — and the path that leaves one open is our
        own failure path (a widget that ignores the Escape we press on
        failure). That made a perfectly valid fill of the NEXT field fail for
        reasons that had nothing to do with its value: a cross-field failure
        that is near-impossible to diagnose from a live run. aria-expanded is
        the same ARIA contract this widget branch already dispatches on, so
        reading it first costs nothing and stays site-agnostic.
        """
        frame = self.fields[index]["frame"]

        async def state():
            try:
                return await frame.evaluate(_EXPANDED_JS, index)
            except Exception:
                return None

        if await state() is True:
            return True                      # already open — clicking closes it
        await loc.click()
        await self.page.wait_for_timeout(400)
        after = await state()
        if after is None:
            return None
        if after is False:
            # The click may have closed a popup whose open state we couldn't
            # see. One bounded retry: a menu toggle holds no answer, so
            # re-clicking it is safe in a way re-clicking a radio is not.
            await loc.click()
            await self.page.wait_for_timeout(400)
            after = await state()
        return bool(after)

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

    # ------------------------------------------------------------------ #
    # Repeating sections ("My Experience": Work Experience / Education).
    # Located by HEADING TEXT, the heading's own container and document
    # order (see repeating.SECTION_JS); every value goes through fill_field,
    # so listbox / combobox / toggle dispatch and read-back verification all
    # apply unchanged. Fill-blanks everywhere: a field that already holds a
    # value is never overwritten.
    # ------------------------------------------------------------------ #
    _RS_OK_STATUSES = ("filled", "already_set", "skipped", "not_found")

    async def _section_layout(self, section: str) -> dict | None:
        """Fresh read_form, then the section's groups (one list of field
        descriptors per entry, each with `key` / `part` / `eid`) and Add
        control. None when no heading on any frame names the section.
        Sensitive fields are dropped here, so this path can never touch one.
        A typeahead whose <input> is empty but shows a committed pill reads
        as the pill text."""
        spec = rep.SECTIONS[section]
        by_idx = {f["index"]: f for f in await self.read_form()}
        arg = {"heading": spec["heading"], "subhead": spec["subhead"],
               "names": spec["names"]}
        for frame in self.page.frames:
            try:
                res = await frame.evaluate(rep.SECTION_JS, arg)
            except Exception:
                continue
            if not res:
                continue
            rows = []
            for d in res.get("fields") or []:
                if d.get("index") is None:
                    if d.get("rcb") is None or \
                            _SECRET_LABEL_RE.search(d.get("label") or ""):
                        continue
                    rows.append({**d, "kind": "aria_checkbox", "frame": frame,
                                 "current_value": "checked" if d.get("checked")
                                 else "", "required": bool(d.get("req"))})
                    continue
                f = by_idx.get(d["index"])
                if f is None or f.get("sensitive") or \
                        _SECRET_LABEL_RE.search(f.get("label") or ""):
                    continue
                cv = f.get("current_value", "") or ""
                row = {**d, "kind": f.get("kind"), "widget": f.get("widget"),
                       "label": f.get("label") or "",
                       "current_value": cv or (d.get("pill") or ""),
                       "required": bool(f.get("required") or d.get("req"))}
                rows.append(row)
            picker_keys = set()
            for p in res.get("pickers") or []:
                k = (rep.label_key(p.get("ctx"), section)
                     or rep.label_key(p.get("label"), section))
                if k in rep.DATE_KEYS:
                    picker_keys.add(k)
            return {"frame": frame, "add": bool(res.get("add")),
                    "ambiguous_add": bool(res.get("ambiguous_add")),
                    "add_label": res.get("add_label") or "",
                    "groups": rep.group_fields(rows, section),
                    "picker_keys": picker_keys}
        return None

    @staticmethod
    def _new_blank_groups(lay: dict, known: set):
        """Groups none of whose elements existed before the Add (diffed by
        stable element id, so a group inserted ANYWHERE is found). Returns
        None when there is no new group yet, else the new BLANK groups'
        anchors (possibly [])."""
        new = [g for g in lay["groups"]
               if not any(f.get("eid") in known for f in g)]
        if not new:
            return None
        return [rep.anchor(g) for g in new if rep.group_is_blank(g)]

    async def _add_group(self, section: str, known: set):
        """Click the section's Add control until a new BLANK group appears:
        at most MAX_ADD_CLICKS clicks, ADD_WAIT_S polling after each. Before
        a second click the layout is re-read, so a slow first click is never
        doubled. Returns (status, clicks, [new blank anchors]) with status
        ok / no_add_button / ambiguous_add / add_failed /
        new_group_not_blank."""
        loop = asyncio.get_event_loop()
        clicks = 0
        for _ in range(rep.MAX_ADD_CLICKS):
            lay = await self._section_layout(section)
            if lay is None or not lay["add"]:
                return ("ambiguous_add" if lay is not None and lay["ambiguous_add"]
                        else "no_add_button"), clicks, []
            new = self._new_blank_groups(lay, known)
            if new is not None:
                return ("ok" if new else "new_group_not_blank"), clicks, new
            self._mark_filled()
            try:
                await lay["frame"].locator('[data-jaadd="1"]').first.click(
                    timeout=3000)
                clicks += 1
            except Exception:
                continue
            deadline = loop.time() + rep.ADD_WAIT_S
            while loop.time() < deadline:
                await self.page.wait_for_timeout(300)
                lay = await self._section_layout(section)
                new = None if lay is None else self._new_blank_groups(lay, known)
                if new is not None:
                    return ("ok" if new else "new_group_not_blank"), clicks, new
        return "add_failed", clicks, []

    async def _pill(self, index: int) -> str:
        frame = (self.fields.get(index) or {}).get("frame")
        if frame is None:
            return ""
        try:
            return await frame.evaluate(rep.PILL_JS, index) or ""
        except Exception:
            return ""

    async def _toggle_aria_checkbox(self, f: dict, value: str) -> dict:
        """Click a non-native [role=checkbox] (tagged data-jarcb by the
        layout scan) and verify on aria-checked. One click only."""
        want = rep.truthy(value)
        loc = f["frame"].locator('[data-jarcb="%s"]' % f["rcb"]).first
        self._mark_filled()
        try:
            await loc.click(timeout=3000)
        except Exception as e:
            return {"status": "error", "verified": False, "error": type(e).__name__}
        await self.page.wait_for_timeout(200)
        try:
            state = await loc.get_attribute("aria-checked")
        except Exception:
            state = None
        if state is None:
            return {"status": "filled", "verified": None, "value": value}
        ok = (state == "true") == want
        res = {"status": "filled" if ok else "uncommitted", "verified": ok,
               "value": value}
        if not ok:
            res["observed_value"] = "checked" if state == "true" else ""
        return res

    async def _rs_fill(self, key: str, f: dict, value: str) -> dict:
        """Fill one section field; shape a compact, truncated result. A
        typeahead that cleared its input on commit is verified on its pill."""
        if f.get("rcb") is not None:
            r = await self._toggle_aria_checkbox(f, value)
        else:
            try:
                r = await self.fill_field(f["index"], value)
            except Exception as e:
                r = {"status": "error", "verified": False,
                     "error": type(e).__name__}
            if (r.get("verified") is not True and r.get("status") != "unmatched"
                    and f.get("kind") in ("text", "combobox")
                    and f.get("widget") != "listbox"):
                pill = await self._pill(f["index"])
                if pill and _values_match(value, pill):
                    r = {k: v for k, v in r.items()
                         if k not in ("observed_value", "note")}
                    r.update(status="filled", verified=True, value=pill,
                             committed_as="pill")
        out = {"key": key, "label": rep.clip(f.get("label")),
               "index": f.get("index"), "status": r.get("status"),
               "verified": r.get("verified"),
               "_eid": f.get("eid")}       # internal; popped before returning
        for k in ("part", "rcb"):
            if f.get(k) is not None:
                out[k] = f[k]
        if r.get("value") is not None:
            out["value"] = rep.clip(r.get("value"))
        if r.get("observed_value") is not None:
            out["observed_value"] = rep.clip(r.get("observed_value"))
        if r.get("options"):
            out["options"] = [rep.clip(o) for o in r["options"][:20]]
        if r.get("committed_as"):
            out["committed_as"] = r["committed_as"]
        if r.get("error"):
            out["error"] = rep.clip(r["error"], rep.ERROR_ECHO)
        return out

    async def _fill_date(self, key: str, fs: list, value: str) -> list:
        """Shapes: (b) separate Month / Year boxes -> each part; (a) one box
        -> the format its placeholder / input type asks for (MM/YYYY by
        default; a native type=date gets day 01 and says so). A missing month
        is never invented. After typing, focus is moved away (Tab) and the
        value that SURVIVED the blur is what gets verified: React-style
        spinbuttons can accept a keystroke and revert on blur."""
        out, sent = [], []
        parts = [f for f in fs if f.get("part")]
        for f in (parts or fs[:1]):
            shape = rep.date_shape(f)
            v = rep.format_date(value, shape)
            if v is None:
                out.append({"key": key, "label": rep.clip(f.get("label")),
                            "index": f.get("index"), "status": "skipped",
                            "reason": "incomplete_date"})
                continue
            row = await self._rs_fill(key, f, v)
            if shape == "date":
                row["day_assumed"] = True
            out.append(row)
            sent.append((row, f, v))
        if not sent:
            return out
        # fill_field already blurs a text part; this covers any part that
        # kept focus. A targeted blur, not Tab: with focus already gone, Tab
        # would land on the page's first focusable element.
        for _, f, _ in sent:
            if f.get("index") is not None:
                await self._blur(f["index"])
        await self.page.wait_for_timeout(100)
        for row, f, v in sent:
            if f.get("index") is None or row.get("status") == "error":
                continue
            obs = await self._read_back(f["index"])
            if obs is None:
                continue
            if _values_match(v, obs):
                row.pop("observed_value", None)
                row.update(status="filled", verified=True)
            else:
                row.update(status="uncommitted", verified=False,
                           observed_value=rep.clip(obs),
                           note="value did not survive leaving the field")
        return out

    async def _locate(self, section: str, entry: dict, track: dict):
        """Fresh layout + this entry's group. By its anchor element first;
        when that element is gone (a re-render replaced the group's DOM),
        re-found by content / last position (repeating.refind_group) and
        `track` re-anchored. One short wait and re-read before giving up,
        since a re-render can be caught mid-flight."""
        lay = None
        for attempt in range(2):
            lay = await self._section_layout(section)
            if lay is not None:
                grp, pos, refound = rep.locate_group(
                    lay["groups"], track["anchor"], entry, section,
                    track.get("pos"))
                if grp is not None:
                    track["pos"] = pos
                    if refound:
                        track["anchor"] = rep.anchor(grp)
                        track["regrouped"] = track.get("regrouped", 0) + 1
                    return lay, grp
            if attempt == 0:
                await self.page.wait_for_timeout(600)
        return lay, None

    async def _fill_group(self, section: str, track: dict, entry: dict,
                          gpa_policy: str) -> list:
        """Fill-blanks for one group from one profile entry, key by key in
        SECTIONS order. The layout is re-read before EVERY key (toggling "I
        currently work here" removes To; a listbox commit can re-render), and
        the group is re-found by its anchor element, never by position alone
        (see _locate for the re-render case). `track` carries the anchor and
        is updated in place when the group had to be re-found."""
        spec = rep.SECTIONS[section]
        current = section == "experience" and rep.truthy(entry.get("current"))
        has_box = False
        out: list = []
        for key in spec["order"]:
            lay, grp = await self._locate(section, entry, track)
            if grp is None:
                out.append({"key": key, "status": "error",
                            "error": "group_vanished"})
                break
            fs = [f for f in grp if f.get("key") == key]
            if key == "current":
                box = next((f for f in fs if f.get("kind")
                            in ("checkbox", "aria_checkbox")), None)
                if box is None:
                    if current:
                        out.append({"key": key, "status": "not_found",
                                    "note": "no 'currently work here' "
                                            "checkbox; To was not skipped"})
                    continue
                has_box = True
                checked = box.get("current_value") == "checked"
                if current and not checked:
                    ends = [f for f in grp if f.get("key") == "end"]
                    if any(rep.has_value(f) for f in ends):
                        out.append({"key": key, "status": "skipped",
                                    "reason": "to_already_filled"})
                    else:
                        out.append(await self._rs_fill(key, box, "true"))
                continue                       # never untick
            if key == "end" and current and has_box:
                if fs and not any(rep.has_value(f) for f in fs):
                    out.append({"key": key, "status": "skipped",
                                "reason": "current_role"})
                continue
            value = rep.entry_value(entry, key)
            if key == "gpa":
                fs = rep.gpa_fields(grp, value)
            empty = [f for f in fs if not rep.has_value(f)]
            if fs and not empty:
                continue                       # holds a value: never overwrite
            if not value:
                if fs:
                    out.append({"key": key, "status": "skipped",
                                "reason": "no_value"})
                continue
            if key == "gpa" and fs and not rep.gpa_allowed(
                    gpa_policy, any(rep.is_required(f) for f in fs)):
                out.append({"key": key, "status": "skipped",
                            "reason": "gpa_policy"})
                continue
            if not fs:
                if key in rep.DATE_KEYS and key in lay["picker_keys"]:
                    out.append({"key": key, "status": "unsupported_date_widget",
                                "note": "only a date-picker button, no text "
                                        "input; fill this date by hand"})
                else:
                    out.append({"key": key, "status": "not_found"})
                continue
            if key in rep.DATE_KEYS:
                out.extend(await self._fill_date(key, empty, value))
                continue
            f = empty[0]
            if key == "degree" and (f.get("kind") == "select"
                                    or f.get("widget") == "listbox"):
                try:
                    opts = (await self.get_field_options(f["index"])) \
                        .get("options") or []
                except Exception:
                    opts = []
                pick = rep.match_degree(value, opts)
                if pick is None:
                    out.append({"key": key, "label": rep.clip(f.get("label")),
                                "index": f["index"], "status": "unmatched",
                                "verified": False, "value": rep.clip(value),
                                "options": [rep.clip(o) for o in opts[:20]],
                                "note": "no option is this degree; nothing "
                                        "was picked"})
                    continue
                value = pick
            out.append(await self._rs_fill(key, f, value))
        return out

    async def fill_repeating_section(self, section: str, entries=None,
                                     gpa_policy: str | None = None) -> dict:
        """Fill a repeating form section ("experience" / "education") from
        the PROFILE's structured entries, one group per entry. Where each
        entry goes is planned up front (repeating.plan_groups): a group that
        already holds it gets its BLANK fields completed; an orphan group
        (values, no company/title/school) consistent with it is completed;
        a blank group is reused; only then is Add clicked — and never while
        an unresolvable orphan exists. Nothing is ever overwritten.
        `entries` / `gpa_policy` are for tests only; the MCP tool never
        passes them (no model-supplied entry data). Entry numbers in the
        result index the profile's list."""
        section = (section or "").strip().lower()
        out = {"section": section, "status": "ok", "added": 0, "filled": [],
               "skipped": [], "errors": [], "warnings": []}
        if section not in rep.SECTIONS:
            out.update(status="bad_section",
                       error="section must be one of: "
                             + ", ".join(rep.SECTIONS))
            return out
        if self.page is None:
            raise RuntimeError("No page open. Call open_job(url) first.")
        if entries is None:
            items = data.profile_section_items(section)
        else:
            items = [(i, e) for i, e in enumerate(entries) if isinstance(e, dict)]
        if gpa_policy is None:
            try:
                gpa_policy = config.load_user_profile().get("gpa_policy")
            except Exception:
                gpa_policy = None
        policy = str(gpa_policy or "if_required").strip().lower()
        if policy not in rep.GPA_POLICIES:
            out["warnings"].append(rep.clip(
                "unknown gpa_policy %r; using if_required" % policy,
                rep.ERROR_ECHO))
            policy = "if_required"
        out["gpa_policy_used"] = policy
        if not items:
            out["status"] = "no_entries"
            return out
        for pos, _ in items[rep.MAX_ENTRIES:]:
            out["skipped"].append({"entry": pos, "reason": "entry_cap"})
        items = items[:rep.MAX_ENTRIES]

        lay = await self._section_layout(section)
        if lay is None:
            out["status"] = "section_not_found"
            return out
        groups = lay["groups"]
        out["existing_groups"] = len(groups)
        plan = rep.plan_groups(groups, [e for _, e in items], section)
        if plan["unresolved"]:
            out["orphan_groups"] = list(plan["unresolved"])
        anchors = [rep.anchor(g) for g in groups]
        blanks = [anchors[j] for j in plan["blanks"]]
        touched, add_error = [], None
        for k, (pos, entry) in enumerate(items):
            if k in plan["matched"]:
                target, mode = anchors[plan["matched"][k]], "existing"
            elif k in plan["orphans"]:
                target, mode = anchors[plan["orphans"][k]], "completed_orphan"
            elif blanks:
                target, mode = blanks.pop(0), "filled"
            elif add_error or plan["unresolved"]:
                out["skipped"].append({"entry": pos, "reason":
                                       add_error or "orphan_groups"})
                continue
            else:
                now = await self._section_layout(section)
                known = {f.get("eid") for g in (now or {}).get("groups", [])
                         for f in g}
                st, clicks, new = await self._add_group(section, known)
                if st != "ok":
                    add_error = st
                    out["errors"].append({"entry": pos, "error": st,
                                          "clicks": clicks})
                    continue
                out["added"] += 1
                target, mode = new[0], "added"
                blanks.extend(new[1:])
            track = {"anchor": target}
            rows = await self._fill_group(section, track, entry, policy)
            touched.append((pos, entry, track, rows))
            acted = [r for r in rows if r.get("status") not in ("skipped",
                                                                "not_found")]
            if mode == "existing":
                if not acted:
                    out["skipped"].append({"entry": pos,
                                           "reason": "already_present"})
                    continue
                mode = "completed_blanks"
            res = {"entry": pos, "result": mode, "fields": rows}
            if track.get("regrouped"):
                res["regrouped"] = track["regrouped"]
            out["filled"].append(res)

        final = await self._section_layout(section)
        req_empty = []
        keyed = set(rep.SECTIONS[section]["order"]) | {"overall_result"}
        for pos, entry, track, rows in touched:
            grp = None
            if final:
                grp, _, _ = rep.locate_group(final["groups"], track["anchor"],
                                             entry, section, track.get("pos"))
            self._reconcile_rows(rows, grp)
            for f in grp or []:
                if (f.get("key") in keyed and f.get("key") != "current"
                        and rep.is_required(f) and not rep.has_value(f)):
                    req_empty.append({"entry": pos, "key": f["key"],
                                      "label": rep.clip(f.get("label"))})
        for _, _, _, rows in touched:
            for r in rows:
                r.pop("_eid", None)
        if req_empty:
            out["required_empty"] = req_empty
        if final is not None:
            out["leftover_blank_groups"] = sum(
                1 for g in final["groups"] if rep.group_is_blank(g))
        if not out["warnings"]:
            del out["warnings"]
        out["status"] = self._rs_status(out, section)
        return out

    @staticmethod
    def _reconcile_rows(rows: list, grp) -> None:
        """The section-wide re-read at the end is a second, LATER read-back.
        A row reported verified=True whose field reads EMPTY there was never
        committed: live Capital One Workday reported School / Field of Study
        `filled, verified:true` in the same result that listed school under
        required_empty, while the page showed both boxes empty (search/prompt
        widgets drop typed text that was not picked from their results).
        Downgrade such rows so one result can never contradict itself.
        Conversely a row left verified=null because its element was replaced
        mid-verify (a re-render) is settled by this re-read: verified when
        the field now holds the value, uncommitted when it is empty. The
        field is found by its stable element id, else by a unique key/part
        (a re-render drops the ids). Toggles are left alone."""
        if not grp:
            return
        by_eid = {f.get("eid"): f for f in grp if f.get("eid") is not None}
        for r in rows:
            if r.get("verified") is False or r.get("rcb") is not None \
                    or r.get("status") not in ("filled", "uncommitted"):
                continue
            f = by_eid.get(r.get("_eid"))
            if f is None:
                same = [g for g in grp if g.get("index") is not None
                        and g.get("key") == r.get("key")
                        and g.get("part") == r.get("part")]
                if len(same) != 1:
                    continue
                f = same[0]
            if f.get("kind") in ("checkbox", "radio"):
                continue
            if rep.has_value(f):
                if r.get("verified") is None and _values_match(
                        r.get("value") or "", f.get("current_value") or ""):
                    r.pop("note", None)
                    r.update(status="filled", verified=True,
                             verified_by="section_reread")
                continue
            r.pop("committed_as", None)
            r.update(status="uncommitted", verified=False, observed_value="",
                     note="read back EMPTY when the section was re-read after "
                          "filling: the widget dropped the value (a search/"
                          "prompt field needs a pick from its results)")

    @classmethod
    def _rs_status(cls, out: dict, section: str) -> str:
        must = set(rep.SECTIONS[section]["identity"]) | {"start", "current"}

        def bad(f):
            st = f.get("status")
            if st == "not_found":
                return f.get("key") in must
            return st not in cls._RS_OK_STATUSES or f.get("verified") is False

        rows_bad = any(bad(f) for e in out["filled"] for f in e["fields"])
        present = any(s.get("reason") == "already_present"
                      for s in out["skipped"])
        if out["errors"] and not out["filled"] and not present:
            return out["errors"][0]["error"]
        if (rows_bad or out["errors"] or out.get("required_empty")
                or out.get("orphan_groups")):
            return "partial"
        if not out["filled"] and present:
            return "already_present"
        return "ok"

    async def upload_resume(self, index: int | None = None,
                            path: str | None = None,
                            target: str = "resume") -> dict:
        """Attach the resume (target="resume", default) or a cover-letter FILE
        (target="cover"). With an explicit `index`, use that field. Without
        one, auto-locate a file input anywhere on the page -- **even if hidden**
        (Greenhouse/Ashby "Attach" widgets back their dropzone with a hidden
        <input type=file>, which read_form doesn't list).

        target="resume" prefers a resume/CV input over a cover-letter one.
        target="cover" only ever uses an input labelled "cover" -- an explicit
        `index` that is not one is refused -- and NEVER falls back to the
        resume slot: with none it returns {"status": "no_cover_slot"}.
        Results carry `verified` + `observed_filename`; a cover upload that
        changed the resume slot returns status "error". Failures (bad target,
        missing cover path, unknown index, no file input) are returned as
        {"status": "error", "error": ...}, never raised."""
        self._mark_filled()
        try:
            return await self._upload_file(index, path, target)
        except Exception as e:
            return {"status": "error", "target": target,
                    "error": f"{type(e).__name__}: {e}"}

    async def _eval(self, frame, js, arg=None):
        """frame.evaluate with a short timeout: a re-rendering dropzone must
        not hang the tool."""
        coro = frame.evaluate(js) if arg is None else frame.evaluate(js, arg)
        return await asyncio.wait_for(coro, timeout=2)

    async def _upload_file(self, index, path, target) -> dict:
        if target not in ("resume", "cover"):
            raise ValueError("target must be 'resume' or 'cover'")
        cover = target == "cover"
        up_path = path or (None if cover else str(config.resume_upload_path()))
        if not up_path:
            raise ValueError("target='cover' needs a path (the cover_letter_path "
                             "from get_job_artifacts)")
        fname = up_path.replace("\\", "/").rsplit("/", 1)[-1]
        before_all = await self._file_slot_snapshot() if cover else []

        if index is not None and index >= 0:
            loc, _ = self._locator(index)
            frame = self.fields[index]["frame"]
            desc = await asyncio.wait_for(loc.evaluate(_FILE_DESCRIBE_JS), 2)
            if desc is None:
                raise ValueError(f"index {index} is not a file input")
            if cover and not _is_cover_candidate(desc, desc.get("total", 0)):
                return {"index": index, "status": "error", "target": target,
                        "error": "index is not a cover slot"}
            await loc.set_input_files(up_path)
            res = {"index": index, "status": "uploaded", "path": up_path,
                   "target": target}
            res.update(await self._file_readback(frame, desc, fname))
            return await self._guard_cover_landing(res, before_all, fname, cover)

        for frame in self.page.frames:
            try:
                cands = await self._eval(frame, _FILE_CANDIDATES_JS)
            except Exception:
                cands = []
            pick = _choose_file_input(cands or [], target)
            if pick is None:
                continue
            desc = next(c for c in cands if c["i"] == pick)
            await self._eval(frame, _TAG_FILE_JS, pick)
            loc = frame.locator('[data-jaidx-file="1"]').first
            try:
                await loc.set_input_files(up_path)
            finally:
                try:
                    await self._eval(
                        frame,
                        "() => document.querySelectorAll('[data-jaidx-file]')"
                        ".forEach(e => e.removeAttribute('data-jaidx-file'))")
                except Exception:
                    pass
            res = {"index": -1, "status": "uploaded", "path": up_path,
                   "target": target, "note": "auto-located file input"}
            res.update(await self._file_readback(frame, desc, fname))
            return await self._guard_cover_landing(res, before_all, fname, cover)
        if cover:
            return {"index": -1, "status": "no_cover_slot", "target": target,
                    "path": up_path,
                    "note": "no file input labelled 'cover' on the page; the "
                            "resume slot was NOT touched. Use the free-text "
                            "cover letter (cover_letter_text) instead."}
        raise RuntimeError("No file input found on the page to attach the resume.")

    async def _file_slot_snapshot(self) -> list[dict]:
        """Descriptors of every file input in every frame, tagged with the
        frame ordinal so before/after snapshots line up."""
        out: list[dict] = []
        for n, frame in enumerate(self.page.frames):
            try:
                cands = await self._eval(frame, _FILE_CANDIDATES_JS)
            except Exception:
                continue
            for c in cands or []:
                out.append({**c, "frame": n})
        return out

    async def _file_readback(self, frame, before: dict, fname: str) -> dict:
        """Re-read the slot AFTER the upload. React dropzones replace the
        <input> node, so the tagged locator is stale; re-run the candidate
        scan and re-match the slot by id/name (else position)."""
        try:
            cands = await self._eval(frame, _FILE_CANDIDATES_JS)
        except Exception as e:
            return {"verified": False, "observed_filename": "",
                    "readback_error": f"{type(e).__name__}: {e}"}
        return _verify_upload(fname, before, _match_slot(cands or [], before))

    async def _guard_cover_landing(self, res: dict, before: list[dict],
                                   fname: str, cover: bool) -> dict:
        """A cover upload must leave the resume slot alone; if the resume
        slot now shows the cover filename it was overwritten."""
        if not cover:
            return res
        after = await self._file_slot_snapshot()
        if _cover_landed_in_resume(before, after, fname):
            res["status"] = "error"
            res["error"] = "cover upload landed in resume slot"
            res["verified"] = False
        return res

    async def get_field_options(self, index: int) -> dict:
        """Return the selectable options for a dropdown. For a native `select`
        the options are read directly; for a `combobox` — react-select or a
        listbox button (Workday and friends; both are reported as kind
        "combobox") — the widget is opened, its `[role=option]` items are read,
        then it is closed. Use this to see the real choices for a custom
        dropdown before fill_field."""
        loc, kind = self._locator(index)
        if kind == "select":
            opts = await loc.evaluate(
                "el => Array.from(el.options).map(o => (o.textContent || '').trim())"
                ".filter(Boolean)")
            return {"index": index, "kind": kind, "options": opts}
        if kind == "combobox":
            frame = self.fields[index]["frame"]
            # Same toggle hazard as _fill_listbox: only click to open when the
            # widget isn't already open, or this call closes it and reads an
            # empty list.
            if self.fields[index].get("widget") == "listbox":
                await self._open_listbox(index, loc)
            else:
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
        out = path or str(config.RUNTIME_DIR / "current_page.png")
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

    @staticmethod
    async def _label_of(cand) -> str:
        """The visible text of a button-ish locator, whitespace-collapsed and
        lowercased. Returns "" when it can't be read."""
        try:
            raw = await cand.evaluate("el => (el.innerText || el.value || '')")
        except Exception:
            return ""
        return " ".join((raw or "").split()).lower()

    async def _find_submit_button(self):
        """Locate the real submit control across all frames. Candidate
        selectors are tried in priority order — EXPLICIT TEXT FIRST, the
        generic [type=submit] only after — and anything whose label reads like
        a wizard step control or a 'Quick Apply' shortcut is skipped.

        Both rules are paid for in incidents. The naive DOM-first
        `button:has-text('Apply')` once clicked Greenhouse's 'Quick Apply with
        MyGreenhouse' instead of 'Submit application'. Worse, Workday's
        "Save and Continue" is a `type=submit` button, so with the generic
        selector first, submit_application on a wizard page clicked NEXT,
        advanced the wizard, and then reported on the wrong page. Use
        advance_step() for that; this method must only ever find a real
        submit."""
        for sel in ("button:has-text('Submit application')",
                    "button:has-text('Submit')",
                    "button[type=submit], input[type=submit]",
                    # Workday renders Apply as <a role=button>; a bare
                    # Apply only qualifies on a filled tab (see below).
                    "button:has-text('Apply'), [role=button]:has-text('Apply')"):
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
                    except Exception:
                        continue
                    label = await self._label_of(cand)
                    if not label:
                        continue
                    if "quick apply" in label:
                        continue
                    if _NOT_SUBMIT_RE.search(label):
                        continue
                    # Entry ("Apply", "Apply Manually"), auth ("Sign In",
                    # "Create Account", type=submit on Workday) and
                    # never-click ("Autofill with Resume") controls are not
                    # submits, whatever selector matched them.
                    # Exception: once this tab has been filled, a bare
                    # "Apply" / "Apply Now" is the application's final
                    # submit, so it must go through this (gated) path.
                    if classify_control(label) in _NOT_A_SUBMIT_CLASSES \
                            and not (self.filled and _FINAL_APPLY_RE.match(label)):
                        continue
                    return cand
        return None

    async def _find_advance_button(self):
        """Locate a wizard's NEXT control across all frames. Returns
        (locator, label) or (None, "").

        Deliberately NOT shared with `_find_submit_button`: the two have
        different priorities and different exclusion lists, and one of them is
        destructive. A shared helper is exactly how the two lists drift apart
        later and a "Next" click ends up submitting an application.

        Acceptance is by LABEL, not by selector, so the broad last-resort
        selector can't drag in a random button: the label must read as a step
        control (`_ADVANCE_RE`) and must not read as a submit (`_SUBMITTY_RE`)
        — a Review page's only button is Submit, and advance_step reports that
        as `is_submit_page` instead of clicking it."""
        for sel in ("button:has-text('Save and Continue')",
                    "button:has-text('Next')",
                    "button:has-text('Continue')",
                    "button[type=submit], input[type=submit]",
                    "button, input[type=button]"):
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
                    except Exception:
                        continue
                    label = await self._label_of(cand)
                    if not label or _SUBMITTY_RE.search(label):
                        continue
                    if not _ADVANCE_RE.match(label):
                        continue
                    return cand, label
        return None, ""

    async def _collect_errors(self) -> list[dict]:
        """Validation errors currently shown, across all frames, as
        [{index, field_label, message}]. `index` is null when the error can't
        be tied to a scanned field — those are REPORTED, never dropped."""
        rows: list[dict] = []
        for frame in self.page.frames:
            try:
                rows.extend(await frame.evaluate(_ERRORS_JS) or [])
            except Exception:
                continue
        return rows

    @staticmethod
    def _label_signature(fields: list[dict]) -> str:
        """A cheap fingerprint of which questions a page is asking. Workday
        sometimes advances a wizard WITHOUT changing the URL, so comparing
        URLs alone reports a real advance as "no_change"; comparing the set of
        labels catches that case."""
        return "|".join("%s~%s" % (f.get("label", ""), f.get("kind", ""))
                        for f in fields)

    async def advance_step(self) -> dict:
        """Click a multi-step application's NEXT control (Workday's "Save and
        Continue", any wizard's "Next"/"Continue") and report what happened.

        NOT a submit and NOT destructive: it moves through a wizard, which is
        why it is ungated while submit_application is not. See the MCP tool
        docstring for the caller-facing contract. Statuses: "advanced",
        "blocked_validation", "no_change", "is_submit_page" (nothing clicked),
        "entry_page" / "auth_page" (only Apply-like or sign-in controls;
        nothing clicked), "no_advance_button".
        """
        if self.page is None:
            raise RuntimeError("No page open.")
        before = await self.read_form()
        before_sig = self._label_signature(before)
        before_url = self.page.url
        loc, label = await self._find_advance_button()
        if loc is None:
            # No step control. Either this IS the submit page (and the caller
            # must go through the gated submit path), or it's a single-page
            # form. _find_submit_button is used here for DETECTION ONLY —
            # nothing is clicked on either branch.
            submit = await self._find_submit_button()
            if submit is not None:
                return {"status": "is_submit_page", "url_changed": False,
                        "current_url": before_url, "errors": [],
                        "error_count": 0, "unmapped_error_count": 0,
                        "fields": before,
                        "button_label": await self._label_of(submit),
                        "note": "this is the submit page — nothing was "
                                "clicked; review, confirm with the user, then "
                                "call submit_application"}
            # Entry / auth pages: report them distinctly, click nothing.
            controls = await self._scan_controls(_email_index(before))
            classes = {classify_control(c.get("label")) for c in controls}
            base = {"url_changed": False, "current_url": before_url,
                    "errors": [], "error_count": 0, "unmapped_error_count": 0,
                    "fields": before, "button_label": ""}
            if "entry" in classes and not form_fields(before):
                return {**base, "status": "entry_page",
                        "note": "a job page or apply chooser — nothing was "
                                "clicked; call start_application"}
            if _is_auth_page(before, controls):
                return {**base, "status": "auth_page",
                        "note": "a sign-in / create-account page — nothing "
                                "was clicked; call authenticate"}
            return {"status": "no_advance_button", "url_changed": False,
                    "current_url": before_url, "errors": [], "error_count": 0,
                    "unmapped_error_count": 0, "fields": before,
                    "button_label": "",
                    "note": "no next/continue control found — this is a "
                            "single-page form"}
        await loc.click()
        # Fixed settle, like everywhere else in this module. NEVER
        # wait_for_load_state("networkidle") here: Workday polls continuously,
        # so networkidle never fires and the call hangs until timeout.
        await self.page.wait_for_timeout(2500)
        # Re-read FIRST: this re-tags the live DOM with data-jaidx, so error
        # rows map to indexes that exist in the `fields` we return.
        after = await self.read_form()
        errors = await self._collect_errors()
        after_url = self.page.url
        url_changed = after_url != before_url
        moved = url_changed or self._label_signature(after) != before_sig
        unmapped = sum(1 for e in errors if e.get("index") is None)
        if errors:
            status = "blocked_validation"
            note = ("the step did not advance — fix the listed fields and call "
                    "advance_step again. Errors with index null could not be "
                    "tied to a scanned field (often a visually-hidden input); "
                    "match them by field_label")
        elif moved:
            status = "advanced"
            note = ("advanced to the next step — ALL field indexes from before "
                    "this call are dead; use the `fields` in this result")
        else:
            status = "no_change"
            note = ("clicked but neither the URL nor the questions changed — a "
                    "swallowed click or a modal; screenshot before retrying")
        return {"status": status, "url_changed": url_changed,
                "current_url": after_url, "errors": errors,
                "error_count": len(errors), "unmapped_error_count": unmapped,
                "fields": after, "button_label": label, "note": note}

    # ------------------------------------------------------------------ #
    # Entry + auth (Workday: job page -> "Start Your Application" chooser
    # -> Create Account / Sign In). Classified by visible text and ARIA only.
    # ------------------------------------------------------------------ #
    _WAIT_S = 10.0
    _ENTRY_BUDGET_S = 30.0
    _LEDGER_WINDOW_S = 24 * 3600
    _RECENT_SUBMIT_S = 60
    _AUTH_OK = ("authenticated", "verification")

    async def _scan_controls(self, email_index=None) -> list[dict]:
        """Visible button-ish controls across frames (see _CONTROLS_JS), each
        with its frame and `cls` from classify_control."""
        return (await self._scan_page_bits(email_index))["controls"]

    async def _scan_page_bits(self, email_index=None) -> dict:
        out = {"controls": [], "headings": [], "notices": [], "field_pos": {},
               "has_step": False, "has_dialog": False}
        arg = -1 if email_index is None else email_index
        for frame in self.page.frames:
            try:
                res = await frame.evaluate(_CONTROLS_JS, arg)
            except Exception:
                continue
            if not res:
                continue
            for c in res.get("controls") or []:
                c["frame"] = frame
                c["cls"] = classify_control(c.get("label"))
                out["controls"].append(c)
            out["headings"].extend(res.get("headings") or [])
            out["notices"].extend(res.get("notices") or [])
            out["field_pos"].update(res.get("field_pos") or {})
            out["has_step"] = out["has_step"] or bool(res.get("has_step"))
            out["has_dialog"] = out["has_dialog"] or bool(res.get("has_dialog"))
        return out

    async def _page_state(self) -> dict:
        """One read of everything entry/auth classification needs."""
        fields = await self.read_form()
        bits = await self._scan_page_bits(_email_index(fields))
        try:
            body = " ".join((await self.page.inner_text("body")).split())[:4000]
        except Exception:
            body = ""
        try:
            blk = await self.detect_blockers()
        except Exception:
            blk = {}
        return {"url": self.page.url, "fields": fields, **bits, "body": body,
                "blocked": bool(blk.get("blocked")),
                "signals": blk.get("signals") or []}

    @staticmethod
    def _state_sig(state: dict) -> str:
        return "|".join([state.get("url") or "",
                         ";".join(c.get("label", "") for c in state.get("controls") or []),
                         ";".join(f.get("label") or "" for f in state.get("fields") or []),
                         ";".join(state.get("headings") or []),
                         ";".join(state.get("notices") or []),
                         "dialog" if state.get("has_dialog") else ""])

    async def _settle(self, before_sig: str, timeout: float | None = None,
                      until=None) -> dict:
        """Poll (bounded, ~10s) until the page differs from `before_sig` and
        either `until(state)` holds or, with no predicate, it has held still
        for one poll. Returns the last state read. Fixed polls, never
        networkidle (Workday polls forever)."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + (self._WAIT_S if timeout is None else timeout)
        prev = None
        while True:
            await self.page.wait_for_timeout(500)
            state = await self._page_state()
            sig = self._state_sig(state)
            if sig != before_sig:
                if until is not None and until(state):
                    return state
                if until is None and sig == prev:
                    return state
            prev = sig
            if loop.time() >= deadline:
                return state

    async def _click_control(self, ctl: dict, timeout: int = 3000) -> bool:
        """Click one tagged control. A trial click first: when an overlay
        (Workday's click_filter) intercepts the element, the trial fails and
        nothing is clicked, so the caller moves to the next candidate.

        Guarded: the tag must resolve to exactly ONE element whose live label
        (and so its class) still equals the scanned control's. A tag that now
        sits on anything else -- or on nothing -- is never clicked."""
        frame = ctl.get("frame")
        if frame is None:
            return False
        want = _norm_label(ctl.get("label"))
        if not want:
            return False
        want_cls = ctl.get("cls") or classify_control(want)
        locs = frame.locator('[data-jactl="%s"]' % ctl["i"])
        try:
            if await locs.count() != 1:
                return False
            loc = locs.first
            live = await loc.evaluate(_CONTROL_LABEL_JS, timeout=1500)
        except Exception:
            return False
        if _norm_label(live) != want or classify_control(live) != want_cls:
            return False
        try:
            await loc.click(trial=True, timeout=1500)
        except Exception:
            return False
        try:
            await loc.click(timeout=timeout)
            return True
        except Exception:
            return False

    @staticmethod
    def _ctl_keys(ctls: list[dict]) -> list[tuple]:
        """(identity, control) for each candidate. Identity is what a control
        IS (label, tag, type, role) plus its rank among same-identity
        candidates, so it survives a re-scan that gives the element a new
        tag or a fresh dict."""
        seen: dict = {}
        out = []
        for c in ctls or []:
            base = (_norm_label(c.get("label")), c.get("tag") or "",
                    c.get("type") or "", c.get("role") or "")
            n = seen.get(base, 0)
            seen[base] = n + 1
            out.append((base + (n,), c))
        return out

    async def _click_first(self, ctls: list[dict], before_sig: str | None = None,
                           pick=None):
        """Click the first candidate that accepts a click. After every
        attempt that did not report success (including one that raised) the
        page is re-read: if its signature changed, that attempt DID click and
        nothing else is tried, so an overlay and its button are never both
        clicked. Otherwise the next candidate is taken from that FRESH read
        when `pick(state) -> ranked controls` is given (never a control dict
        from before the re-read), skipping identities already tried. Returns
        the clicked control or None; re-raises the last exception when
        nothing was clicked."""
        if before_sig is None:
            before_sig = self._state_sig(await self._page_state())
        last_exc = None
        tried: set = set()
        cands = list(ctls or [])
        for _ in range(8):
            nxt = None
            for key, c in self._ctl_keys(cands):
                if key in tried:
                    continue
                if (c.get("cls") == "never"
                        or classify_control(c.get("label")) == "never"):
                    continue
                nxt = (key, c)
                break
            if nxt is None:
                break
            key, c = nxt
            tried.add(key)
            try:
                if await self._click_control(c):
                    return c
            except Exception as e:
                last_exc = e
            state = await self._page_state()
            if self._state_sig(state) != before_sig:
                return c
            if pick is not None:
                cands = list(pick(state) or [])
        if last_exc is not None:
            raise last_exc
        return None

    async def has_entry_control(self) -> bool:
        if self.page is None:
            return False
        return any(c["cls"] == "entry" for c in await self._scan_controls())

    def _layout(self, state: dict) -> dict:
        return _auth_layout(state.get("fields"), state.get("controls"),
                            state.get("headings"), state.get("field_pos"))

    def _entry_result(self, status: str, state: dict, **extra) -> dict:
        res = {"status": status, "url": state.get("url", ""),
               "fields": state.get("fields") or [], "auth": None, **extra}
        if status == "auth_required":
            lay = self._layout(state)
            want = lay["mode"] if lay["mode"] != "unknown" else "sign_in"
            sub = _pick_auth_controls(state.get("controls"), want, submit=True)
            lay["submit_label"] = sub[0]["label"] if sub else ""
            res["auth"] = lay
        if status == "blocked":
            res["signals"] = state.get("signals") or []
        return res

    def _inside_application(self, state: dict) -> bool:
        """Is this tab already inside an application (so a bare "Apply" may
        be its final submit)? Filled tab, a Back/Previous control, or a step
        indicator ("step N of M" / aria-current="step")."""
        if self.filled or state.get("has_step"):
            return True
        if _STEP_TEXT_RE.search(state.get("body") or ""):
            return True
        return any(_BACK_RE.match(_norm_label(c.get("label")))
                   for c in state.get("controls") or [])

    async def _adopt_popup(self, pages_before) -> int | None:
        """If the last click opened a NEW browser tab, register it as a tab
        and make it active. Called at most once per start_application."""
        if self.context is None:
            return None
        try:
            await self.page.wait_for_timeout(700)
            pages = list(self.context.pages)
        except Exception:
            return None
        known = {id(t["page"]) for t in self.tabs.values()}
        new = [p for p in pages if p not in pages_before and id(p) not in known]
        if not new:
            return None
        page = new[-1]
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        tab_id = self._next_id
        self._next_id += 1
        self.tabs[tab_id] = {"page": page, "fields": {}, "filled": False,
                             "company": self.tabs.get(self.active_id, {}).get("company", ""),
                             "title": "", "url": page.url}
        self.active_id = tab_id
        return tab_id

    async def start_application(self) -> dict:
        """From the current tab, get from a job page to the application form
        or its sign-in gate WITHOUT submitting anything: click an entry
        control ("Apply", then "Apply Manually" on the chooser; never
        "Autofill with Resume" / "Use My Last Application"), reveal an email
        sign-in behind an SSO chooser, then classify. Idempotent: on a form or
        auth page it only classifies. Refuses to click an entry control once
        the tab is inside an application (status already_in_application),
        because there a bare "Apply" is the final submit. Statuses: form,
        auth_required, blocked, no_entry, already_in_application. Controls in
        an open modal win over the page behind it. After each click it waits
        for real progress (a new entry control, the form, the sign-in fields),
        not just any change, so a modal or SPA route that renders seconds
        later is not misread as no_entry. Bounded: at most 4 clicks and
        _ENTRY_BUDGET_S (~30s) per call; stops when a click changes nothing."""
        if self.page is None:
            raise RuntimeError("No page open. Call open_job(url) first.")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._ENTRY_BUDGET_S

        def left() -> float:
            return max(0.0, deadline - loop.time())

        def actionable(st):
            return (_entry_landed(st) or bool(_entry_targets(st, "entry"))
                    or bool(_entry_targets(st, "auth_reveal")))

        state = await self._page_state()
        if not actionable(state):
            # The job page's Apply renders late (live Capital One probe: none
            # at open_job's 2.5s mark, there a moment later).
            state = await self._settle("", timeout=min(self._WAIT_S, left()),
                                       until=actionable)
        start_path = urlparse(state.get("url") or "").path
        popup_done = False
        for _ in range(4):
            if state["blocked"]:
                return self._entry_result("blocked", state)
            fields = state["fields"]
            if _is_auth_page(fields, state["controls"]):
                return self._entry_result("auth_required", state)
            if form_fields(fields):
                return self._entry_result("form", state)
            entries = _entry_targets(state, "entry")
            reveal = _entry_targets(state, "auth_reveal")
            if entries:
                if self._inside_application(state):
                    return self._entry_result(
                        "already_in_application", state,
                        note="this tab is inside an application; an Apply "
                             "control here may be the final submit — nothing "
                             "was clicked (use advance_step / submit_application)")
                kind, target = "entry", entries
            elif reveal:
                # "Sign in with email": never a submit
                kind, target = "auth_reveal", reveal
            else:
                break
            if left() <= 0:
                break
            sig = self._state_sig(state)
            try:
                pages_before = list(self.context.pages) if self.context else []
            except Exception:
                pages_before = []
            if await self._click_first(
                    target, sig,
                    pick=lambda st, k=kind: _entry_targets(st, k)) is None:
                break
            if not popup_done:
                popup_done = await self._adopt_popup(pages_before) is not None
            before = state

            def progressed(st, before=before):
                return _entry_progress(st, before)
            state = await self._settle(sig, timeout=min(self._WAIT_S, left()),
                                       until=progressed)
            if not progressed(state) and left() > 0:
                # Slow render: Workday opens the "Start Your Application"
                # chooser as a modal on the SAME URL, sometimes seconds after
                # the click. One more bounded wait before judging the click.
                state = await self._settle(sig, timeout=min(self._WAIT_S, left()),
                                           until=progressed)
            if self._state_sig(state) == sig:
                break                    # the click changed nothing: stop
        if (not _entry_landed(state) and left() > 0
                and (state.get("has_dialog")
                     or urlparse(state.get("url") or "").path != start_path)):
            # An entry click moved us (/apply, /applyManually, a modal) but
            # the form / sign-in fields have not rendered yet.
            state = await self._settle("", timeout=min(self._WAIT_S, left()),
                                       until=_entry_landed)
        if state["blocked"]:
            return self._entry_result("blocked", state)
        if _is_auth_page(state["fields"], state["controls"]):
            return self._entry_result("auth_required", state)
        if form_fields(state["fields"]):
            return self._entry_result("form", state)
        return self._entry_result("no_entry", state)

    # --- auth-attempt ledger (lockout budget) -------------------------------
    def _ledger_file(self):
        if self._ledger_path is False:
            return None
        if self._ledger_path:
            return Path(self._ledger_path)
        try:
            return Path(config.RUNTIME_DIR) / "auth_attempts.json"
        except Exception:
            return None

    def _ledger_load(self) -> dict:
        if self._ledger is None:
            self._ledger = {}
            path = self._ledger_file()
            if path is not None and path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        self._ledger = {h: [e for e in v if isinstance(e, dict)]
                                        for h, v in raw.items() if isinstance(v, list)}
                except Exception:
                    self._ledger = {}
        cutoff = time.time() - self._LEDGER_WINDOW_S
        for h in list(self._ledger):
            self._ledger[h] = [e for e in self._ledger[h]
                               if float(e.get("t", 0)) >= cutoff]
            if not self._ledger[h]:
                del self._ledger[h]
        return self._ledger

    def _ledger_save(self) -> None:
        path = self._ledger_file()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._ledger or {}, indent=1),
                            encoding="utf-8")
        except Exception:
            pass

    def _ledger_add(self, host: str, kind: str, outcome: str) -> None:
        led = self._ledger_load()
        led.setdefault(host, []).append(
            {"t": time.time(), "kind": kind, "outcome": outcome})
        self._ledger_save()

    def _ledger_entries(self, host: str) -> list[dict]:
        return list(self._ledger_load().get(host, []))

    def _ledger_blocked(self, host: str) -> bool:
        """bad_credentials recorded for this tenant in the last 24h."""
        return any(e.get("outcome") == "bad_credentials"
                   for e in self._ledger_entries(host))

    def _ledger_recent_submit(self, host: str) -> bool:
        now = time.time()
        return any(e.get("kind") in ("sign_in", "create_account")
                   and now - float(e.get("t", 0)) < self._RECENT_SUBMIT_S
                   for e in self._ledger_entries(host))

    def _ledger_failed_sign_in(self, host: str) -> bool:
        """A failed sign-in in the last 24h that no later success explains
        (a sign-in that failed because the account did not exist yet is
        explained by the create-account that succeeded after it)."""
        entries = self._ledger_entries(host)
        last_ok = max((float(e["t"]) for e in entries
                       if e.get("outcome") in self._AUTH_OK), default=-1.0)
        return any(e.get("kind") == "sign_in"
                   and e.get("outcome") not in self._AUTH_OK
                   and float(e.get("t", 0)) > last_ok for e in entries)

    def clear_auth_ledger(self, tenant: str = "") -> dict:
        led = self._ledger_load()
        host = (_host_of(tenant) or tenant or "").strip().lower()
        if host:
            cleared = [host] if led.pop(host, None) is not None else []
        else:
            cleared = sorted(led)
            led.clear()
        self._ledger_save()
        return {"status": "cleared", "tenants": cleared}

    # --- authenticate -------------------------------------------------------
    def _on_workday(self) -> bool:
        try:
            return is_workday_host(_host_of(self.page.url))
        except Exception:
            return False

    async def authenticate(self, mode: str = "auto") -> dict:
        """Sign in to / create the tenant account with the user's local
        credentials (config.credential; never generated, never returned).
        Workday hosts only. At most 2 auth submits per call, gated by the
        per-tenant attempt ledger. See the MCP tool for statuses."""
        def out(status, state=None, secrets=(), **extra):
            url = (state or {}).get("url")
            if url is None:
                try:
                    url = self.page.url if self.page is not None else ""
                except Exception:
                    url = ""
            return {"status": status, "url": url,
                    "message_excerpt": _message_excerpt(state or {}, secrets),
                    **extra}

        if mode not in ("auto", "sign_in", "create_account"):
            return out("bad_mode")
        email, err = config.credential("workday", "email")
        if not err:
            password, err = config.credential("workday", "password")
        if err:
            return out(err["status"], hint=err.get("hint", ""))
        if self.page is None:
            return out("error", error="NoPage")
        if not self._on_workday():
            return out("unsupported_auth_flow",
                       note="credentials are only ever used on a Workday host")
        host = _host_of(self.page.url)
        if self._ledger_blocked(host):
            return out("bad_credentials",
                       note="a bad password was recorded for this tenant in the "
                            "last 24h; not retrying. After the user fixes "
                            "credentials.yaml they may ask to clear_auth_ledger")
        try:
            return await self._authenticate(mode, email, password, host, out)
        except Exception as e:  # never str(e): Playwright logs embed values
            return out("error", error=type(e).__name__)

    async def _authenticate(self, mode, email, password, host, out) -> dict:
        secrets = (email, password)
        state = await self._page_state()
        if self._ledger_recent_submit(host):
            # Another auth submit hit this tenant under a minute ago: look,
            # don't touch.
            o = _classify_auth_outcome(state)
            status = {"authenticated": "authenticated",
                      "verification": "email_verification_required",
                      "blocked": "blocked"}.get(o, "recently_submitted")
            return out(status, state, secrets,
                       note="an auth submit happened on this tenant in the "
                            "last 60s; classified only, nothing submitted")
        if not any(f.get("sensitive") for f in state["fields"]):
            entry = await self.start_application()
            if entry["status"] == "form":
                return out("authenticated", await self._page_state(), secrets,
                           note="no sign-in was needed")
            if entry["status"] != "auth_required":
                return out(entry["status"], await self._page_state(), secrets)
            state = await self._page_state()
            if not self._on_workday():
                return out("unsupported_auth_flow", state, secrets)
        if state["blocked"]:
            return out("blocked", state, secrets)
        lay = self._layout(state)
        known = ("sign_in", "create_account")
        if mode != "auto":
            want = mode
        elif lay["mode"] in known:
            want = lay["mode"]
        else:
            want = ("create_account" if _pick_auth_controls(
                state["controls"], "create_account", submit=True)
                and not _pick_auth_controls(state["controls"], "sign_in", True)
                else "sign_in")
        submits, switched, ambiguous = 0, False, False
        while submits < 2:
            if lay["mode"] != want:
                if lay["mode"] not in known:
                    # Never toggle from an unrecognised layout: a "Log in"
                    # here may be page navigation, not the form's toggle.
                    if not _pick_auth_controls(state["controls"], want, True):
                        return out("toggle_failed", state, secrets, wanted=want)
                else:
                    cur = lay["mode"]

                    def pick_toggle(st, want=want, cur=cur):
                        return _pick_auth_controls(st["controls"], want,
                                                   submit=False, current=cur)
                    toggles = pick_toggle(state)
                    sig = self._state_sig(state)
                    origin = urlparse(state["url"])[:2]
                    if await self._click_first(toggles, sig,
                                               pick=pick_toggle) is None:
                        return out("toggle_failed", state, secrets, wanted=want)
                    state = await self._settle(sig)
                    if urlparse(state["url"])[:2] != origin:
                        return out("toggle_failed", state, secrets, wanted=want,
                                   note="the toggle navigated away")
                    lay = self._layout(state)
                    if lay["mode"] != want:
                        return out("toggle_failed", state, secrets, wanted=want)
            if lay["password_index"] is None:
                return out("unsupported_auth_flow", state, secrets)
            if want == "sign_in" and self._ledger_failed_sign_in(host):
                return out("bad_credentials", state, secrets,
                           note="a sign-in already failed on this tenant in "
                                "the last 24h; not risking a lockout")
            fills = [(lay["email_index"], email, False),
                     (lay["password_index"], password, True)]
            if want == "create_account":
                fills.append((lay["confirm_index"], password, True))
            for idx, val, is_pw in fills:
                if idx is None:
                    continue
                if not self._on_workday():
                    return out("unsupported_auth_flow", state, secrets)
                r = await self.fill_sensitive(idx, val, require_password=is_pw,
                                              mark=False)
                if r.get("status") != "filled":
                    return out("error", state, secrets,
                               error=("password_fill:" if is_pw else "email_fill:")
                               + str(r.get("status")))
            if want == "create_account" and lay["consent_indexes"]:
                was_filled = self.filled
                for idx in lay["consent_indexes"]:
                    try:
                        await self.fill_field(idx, "true")
                    except Exception:
                        pass
                t = self.tabs.get(self.active_id)
                if t is not None:
                    t["filled"] = was_filled   # a consent tick is not an answer
            # Re-scan AFTER the fills: typing makes Workday add controls
            # (password show/hide toggles) and validation text, so neither the
            # control dicts nor the signature from before the fills may be
            # used to pick or judge the submit click.
            state = await self._page_state()

            def pick_submit(st, want=want):
                return _pick_auth_controls(st["controls"], want, submit=True)
            subs = pick_submit(state)
            if not subs:
                return out("no_auth_submit", state, secrets)
            if not self._on_workday():
                return out("unsupported_auth_flow", state, secrets)
            sig = self._state_sig(state)
            before_notices = list(state.get("notices") or [])
            if await self._click_first(subs, sig, pick=pick_submit) is None:
                return out("no_auth_submit", state, secrets)
            submits += 1
            state = await self._settle(
                sig, until=lambda st: _auth_settled(st, before_notices))
            outcome = _classify_auth_outcome(state)
            self._ledger_add(host, want, outcome)
            if outcome == "blocked":
                return out("blocked", state, secrets)
            if outcome == "verification":
                return out("email_verification_required", state, secrets)
            if outcome == "authenticated":
                return out("authenticated", state, secrets)
            if outcome == "exists":
                if want == "create_account" and not switched and submits < 2:
                    want, switched = "sign_in", True
                    lay = self._layout(state)
                    continue
                if ambiguous:
                    self._ledger_add(host, "verdict", "bad_credentials")
                    return out("bad_credentials", state, secrets)
                return out("account_exists", state, secrets)
            if outcome in ("no_account", "combined"):
                if (want == "sign_in" and mode == "auto" and not switched
                        and submits < 2):
                    want, switched = "create_account", True
                    ambiguous = outcome == "combined"
                    lay = self._layout(state)
                    continue
                if outcome == "no_account":
                    return out("no_account", state, secrets)
                self._ledger_add(host, "verdict", "bad_credentials")
                return out("bad_credentials", state, secrets)
            if outcome == "bad_password":
                self._ledger_add(host, "verdict", "bad_credentials")
                return out("bad_credentials", state, secrets)
            return out("unconfirmed", state, secrets)
        return out("unconfirmed", state, secrets)

    async def open_verification_link(self, url: str) -> dict:
        """Open an emailed Workday verification link in a NEW tab of this
        session, read its text, close it, and reload the original tab. Only
        https links on a Workday domain (dot-boundary), no userinfo/port, and
        on the same Workday domain as the active tab."""
        tab_host = ""
        try:
            tab_host = _host_of(self.page.url) if self.page is not None else ""
        except Exception:
            pass
        if not verification_link_allowed(url, tab_host):
            return {"status": "not_allowed",
                    "note": "only https links on the active tab's Workday "
                            "domain may be opened"}
        if self.context is None or self.page is None:
            raise RuntimeError("No page open. Call open_job(url) first.")
        orig = self.page
        tab = await self.context.new_page()
        text = ""
        try:
            await tab.goto(url, timeout=30000, wait_until="domcontentloaded")
            await tab.wait_for_timeout(3000)
            try:
                text = " ".join((await tab.inner_text("body")).split())[:300]
            except Exception:
                text = ""
        finally:
            try:
                await tab.close()
            except Exception:
                pass
            try:
                await orig.bring_to_front()
            except Exception:
                pass
        await orig.reload(timeout=30000, wait_until="domcontentloaded")
        await orig.wait_for_timeout(2500)
        self.fields.clear()
        return {"status": "opened", "text_excerpt": text,
                "note": "the original tab was reloaded — call start_application "
                        "or authenticate to continue"}

    # Explicit post-submit signals. Success is only ever concluded from
    # positive success text — a spam rejection (Ashby, invisible reCAPTCHA v3)
    # transiently removes the form too, so field-count disappearance ALONE is
    # NOT a reliable "submitted" signal (JOB-24).
    _SUCCESS_RE = re.compile(
        r"thank you for (applying|submitting)"
        r"|application (has been |was )?(received|submitted)"
        r"|we('ve| have) received your application"
        r"|your application (was|has been) successfully submitted"
        r"|application was successfully submitted"
        r"|successfully submitted your application", re.I)
    _FAILURE_RE = re.compile(
        r"we (couldn't|could not|were unable to) submit your application"
        r"|(flagged|detected) as (possible )?spam"
        r"|submission was flagged"
        r"|please submit your application again", re.I)

    async def _submission_confirmed(self, fields_before: int) -> tuple[str, str]:
        """Classify the page after a submit click as one of:
          - "submitted"     — explicit success text is present.
          - "rejected_spam" — explicit failure/spam-rejection text is present
            (Ashby: "flagged as possible spam"). The caller must NOT log this.
          - "attempted"     — neither; the click may have hit validation, a
            verification gate, a spam flag mid-re-render, or the wrong control.
        Field-count disappearance is reported as evidence but never on its own
        upgrades to "submitted" — the rejection flow removes the form too, then
        re-renders it with an error banner, so we re-read a couple of times to
        let that banner settle before concluding."""
        body = ""
        for attempt in range(2):
            try:
                body = " ".join((await self.page.inner_text("body")).split())
            except Exception:
                body = ""
            fm = self._FAILURE_RE.search(body)
            if fm:
                return "rejected_spam", f'rejection text: "{fm.group(0)}"'
            sm = self._SUCCESS_RE.search(body)
            if sm:
                return "submitted", f'confirmation text: "{sm.group(0)}"'
            if attempt == 0:
                # let a spam-rejection banner re-render before the final read
                await self.page.wait_for_timeout(1500)
        try:
            after = len(await self.read_form())
        except Exception:
            after = fields_before
        if fields_before >= 4 and after <= fields_before // 4:
            return "attempted", (
                f"form fields went from {fields_before} to {after} but no "
                f"success/failure text found — screenshot-audit before trusting")
        return "attempted", (f"form still present ({after} fields, was "
                             f"{fields_before}); no confirmation text found")

    async def submit_application(self, index: int | None = None) -> dict:
        """Click the submit control. DESTRUCTIVE — the caller (skill) must
        confirm with the user first. If `index` is given, that field is
        clicked; otherwise the submit button is located across frames
        (excluding 'Quick Apply' shortcuts and wizard step controls — see
        _find_submit_button).

        On a multi-step wizard page there is no submit control, so this
        returns "no_submit_button" with `form_snapshot` intact and clicks
        NOTHING. That is the intended outcome: failing honestly beats clicking
        "Save and Continue", advancing the wizard, and reporting on a page the
        caller never saw. Use advance_step() to move through a wizard.

        An explicit `index` BYPASSES that guard entirely — it clicks whatever
        the caller points at, so only pass one when you have verified the
        element is the real submit.

        Snapshots the form (labels + current values) just before clicking and
        returns it as `form_snapshot` so the caller can persist what was
        actually submitted, then verifies the submission landed: `status` is
        "submitted" only when the page shows explicit success text;
        "rejected_spam" when it shows a spam/submission-failure banner (do NOT
        log); "attempted" when neither is seen. Evidence is in `confirmation`."""
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
        status, evidence = await self._submission_confirmed(len(form_snapshot))
        return {"status": status,
                "confirmation": evidence, "current_url": self.page.url,
                "form_snapshot": form_snapshot}

    async def close(self) -> None:
        if self.context is not None:
            # Closing the persistent context closes the browser it launched.
            await self.context.close()
            self.context = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None
        self.tabs.clear()
        self.active_id = None


# module-level singleton used by the MCP server
session = BrowserSession()
