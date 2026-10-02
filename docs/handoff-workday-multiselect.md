# Handoff: Workday multi-select misclassification (+ password capture leak)

**Written:** 2026-09-17, by a terminating SDK session.
**For:** a persistent interactive session with a live browser.
**Status:** analysis complete and verified against the source; **no code written yet**.

---

## 0. Why this needs a persistent session

The whole patch hinges on one DOM fact that cannot be checked from here:

> Does Workday's **multi-select** widget expose `aria-haspopup="listbox"` on its
> `<input>`, or does it instead expose `aria-autocomplete="list"` + `aria-controls`?

- The browser tab from the original run is **closed** (`list_tabs` returns empty).
- The URL was never captured — the run parked before submit, so there is no
  `applications.json` row for it. Last logged entries are the 2026-09-16
  Greenhouse batch (Alpaca, Splice, Stripe, Coinbase).
- It cannot be answered offline: `tests/manual/workday_fixture.html` models
  **only** the single-select `button[aria-haspopup=listbox]` shape (lines 65, 76,
  90). It encodes the same assumption that caused the bug, which is precisely
  why `tests/manual/run_dom_harness.py` never caught it.

**First action for the persistent session:** get the Workday posting URL from the
user, `open_job` it, navigate to the application form, and run the probe in §3.
Everything downstream branches on that result.

---

## 1. The bug

`src/browser.py:136`:

```js
const isListbox = (tag === 'button' && el.getAttribute('aria-haspopup') === 'listbox');
```

`tag === 'button'` is the defect. Workday ships two dropdown shapes:

| Shape | DOM | Classified as | Outcome |
|---|---|---|---|
| Single-select (Country, State, Phone Device Type) | `<button aria-haspopup="listbox">` | `kind:"combobox"`, `widget:"listbox"` | works — this is why `State = California` committed via the typeahead |
| Multi-select (How Did You Hear About Us, Country Phone Code) | `<input>`, no `role="combobox"` | falls through to `kind:'text'` at line 146, `widget` undefined | **all three observed failures** |

The typeahead fallback in `_fill_listbox` is **not** broken — it is simply
unreachable for these fields.

### Observed consequences, each traced to a line

- `fill_field` (`src/browser.py:919`) dispatches on `meta["widget"] == "listbox"`.
  Never matches → plain-text path → `loc.fill(value)` types without opening or
  picking. Matches the symptom: `verified: true` on the raw text, then Workday
  cleared it and raised "required".
- `get_field_options` (`src/browser.py:1289`, final line 1317) returns
  `{"options": []}` for any kind that is not `select`/`combobox`. Matches the
  empty `[]` returned twice.
- `_fill_listbox` (`src/browser.py:1105`) is never entered, so the `delay=100`
  typeahead never runs.

### The quieter, more dangerous half

`Country Phone Code*` read back `current_value: ""` while the screenshot showed a
committed chip `× United States of America (+1)`. So the scanner **mis-reads**
this widget family, not just mis-fills it. A correct field that reads as empty
means a verification pass calls it a mismatch and a fill pass may clobber a good
value. Fix #2 below is the one that matters most.

---

## 2. Second, independent issue: password capture leak

Confirmed real, **latent, nothing leaked yet**:

- Scanner type-skip list (`src/browser.py:127`) is
  `['hidden','submit','button','reset','image']`. `password` is **absent**, so a
  Workday account-creation password scans as `kind:'text'` with a readable
  `el.value`.
- `capture_submission` (`src/data.py:315`) filters file fields, empty values, EEO
  self-ID, and profile-answered fields. There is **no** password filter anywhere
  in the chain, so the value would be written to **both** `history.json` (via
  `_upsert_answer`) and the `fields` blob in `applications.json`.

**Audit performed 2026-09-17:** scanned both stores for password-shaped records —
**0 hits across 192 history rows and 93 applications.** This is only clean
because no Workday run has reached a confirmed submit yet. Fix before the next
Workday attempt, since that run will create an account.

---

## 3. Do this first: the live probe

With the form open, in the page console (or via an `evaluate`), on the
"How Did You Hear About Us" / "Country Phone Code" input:

```js
const el = document.activeElement;  // or querySelector the multiselect input
JSON.stringify({
  tag: el.tagName,
  type: el.type,
  haspopup: el.getAttribute('aria-haspopup'),
  role: el.getAttribute('role'),
  autocomplete: el.getAttribute('aria-autocomplete'),
  controls: el.getAttribute('aria-controls'),
  expanded: el.getAttribute('aria-expanded'),
  value: el.value,
  wrapperLabel: (el.closest('[aria-label]') || {}).ariaLabel,
  wrapperText: (el.closest('div[data-automation-id]') || el.parentElement).innerText.slice(0, 200),
});
```

Record the raw output verbatim in the commit message — it is the ground truth the
fixture will be built from.

**Branch on the result:**

- `aria-haspopup="listbox"` present → fix #1 as written below.
- `aria-autocomplete="list"` + `aria-controls` instead → fix #1 must test for
  *that* contract. Still ARIA, so the no-per-site-selectors invariant holds.
- Neither → stop and re-plan. Do not reach for `data-automation-id` as a
  classifier; it is Workday-specific and breaks the invariant. It is acceptable
  only inside `_LISTBOX_VALUE_FN`'s value cascade, where it already appears.

---

## 4. The patch — four parts

### #1 Classification (`src/browser.py:136`)

Drop the tag restriction:

```js
const isListbox = el.getAttribute('aria-haspopup') === 'listbox';
```

(or the `aria-autocomplete="list"` variant, per §3).

Keep the existing comment block at lines 131-135 accurate — it explains *why*
listbox buttons are reported as `kind:'combobox'`, and that reasoning still holds.

**Regression to watch:** line 211 (`if (b.getAttribute('aria-haspopup') === 'listbox') continue;`)
and the sibling filter just below already exclude listbox elements from the
button-group second pass, and the `data-jaidx` guard protects anything tagged in
pass one. Broadening `isListbox` should not disturb that, but re-run the harness
to confirm the button-group pass still finds Ashby Yes/No groups.

### #2 Value readback (`_LISTBOX_VALUE_FN`, `src/browser.py:40`)

`_COMBO_VALUE_JS` (line ~326) branches on `tagName === 'button'` to reach
`listboxValue()`, else returns `el.value`. For a multi-select input `el.value` is
the **typed search text**, not the selection.

Add a chip/selection branch to the `listboxValue` cascade. The generic hook is the
accessible name on the widget wrapper — Workday renders literally
`"1 item selected, United States of America (+1)"` (this string was observed in
`get_job_text` output). Match on the ARIA pattern:

```js
/(\d+)\s+items?\s+selected/i
```

and take the remainder as the value. **Do not** key off `data-automation-id` for
this — the "N items selected" accessible name is the portable contract.

Also update `_COMBO_VALUE_JS` so an `<input>` multi-select routes to
`listboxValue()` rather than falling through to `el.value`. Keeping `read_form`
and the fill-time verifier on one code path is load-bearing (see the comment at
lines 320-324) — preserve that property.

Placeholder guard: `'search'` is already in the `PLACEHOLDERS` list and
`'select one'` is covered; check whether Workday's *empty* multi-select renders
something else and extend the list if the probe shows a new one.

### #3 Commit semantics (`_fill_listbox`, `src/browser.py:1105`)

A multi-select keeps its popup **open** after a pick — that is the point of it.
The existing flow presses `Enter` then `Escape` only on the `verified is False`
branch (lines ~1168-1172). Picking a single option should still work, but the
"leave the widget closed so the next field isn't typed into this one's popup"
cleanup likely needs an explicit `Escape` after a **successful** pick too, not
only after a failure.

Verify empirically: fill the multi-select, then fill the next field, and confirm
the next field's text did not land in the first widget's popup.

### #4 Password leak — three small edits, not one

Plugging only `capture_submission` fixes the disk leak but leaves the value
exposed in the transcript, because `_FIELD_VALUE_JS` (`src/browser.py:341`) reads
it back raw at fill time.

1. **Scanner** (`src/browser.py`, the `out.push({...})` at ~line 178): emit
   `sensitive: itype === 'password' || undefined`, and mask the emitted value —
   `current_value: (itype === 'password' ? (value ? '********' : '') : value)`.
   Do **not** add `password` to the type-skip list at line 127: the field must
   stay fillable, since Workday requires account creation.
2. **Verification** (`_fill_result`, `src/browser.py:640`): for a sensitive field,
   verify on **presence** (observed non-empty) rather than equality — otherwise
   masking makes every password fill report `verified: false`, and the
   `_CLOSED_FILL_KINDS` routing would mark it `uncommitted`. Mask `observed_value`
   too; it is emitted on every non-verified result.
3. **Capture** (`capture_submission`, `src/data.py:315`): skip sensitive fields in
   the same guard family as `kind == "file"` and `pf.get("eeo")`. Note that
   `submitted.append(...)` (line ~350) happens **before** the profile-value
   `continue`, so the skip must come earlier or the password still reaches
   `applications.json`.

---

## 5. Test coverage — required, not optional

The fixture is why this shipped. Extend it:

- Add the real multi-select shape (exact attributes from the §3 probe) to
  `tests/manual/workday_fixture.html`, including a committed-chip state so
  readback is exercised, and an empty state so the placeholder guard is.
- Add a `type="password"` input to the fixture and assert the scanner emits
  `sensitive: true` with a masked `current_value`.
- Extend `tests/manual/run_dom_harness.py` assertions for both.
- CI-side: `tests/test_fill_verification.py` stubs Playwright, so add a pure-Python
  case there for the `_fill_result` sensitive-field presence check, and one in
  `tests/test_answer_hygiene.py` (or a new file) proving `capture_submission` drops
  a password field from both history and the `submitted` blob.
- Run `python tests/manual/run_dom_harness.py` before the live retry.

---

## 6. Live validation

Re-run the parked Workday application end to end. Success criteria:

1. `read_form` reports the multi-selects as `kind:"combobox"`, `widget:"listbox"`.
2. `get_field_options` returns a non-empty list for both.
3. `fill_field` commits, and readback shows the chip text, not the typed text.
4. An already-set `Country Phone Code` reads back its committed value, not `""`.
5. The next field after a multi-select fills into itself, not the open popup.
6. The run proceeds to the Review page.

Keep a Stage-D blanket re-read before submit regardless — it would have caught the
`Country Phone Code` mis-read independently.

---

## 7. Housekeeping

- **Tracker update is PENDING.** taskman was unreachable from the authoring
  session (`search_nodes` and `list_children` both errored — server down or
  `TASKMAN_API_KEY` unset). Per `CLAUDE.md`, resolve the issue **by title**, never
  by number: `mcp__taskman__search_nodes` with `under: "JOB"`,
  `status_category: "todo,in_progress"`. If nothing matches, `create_story` under a
  hand-filed epic (not the dev-loop "Loop backlog" epic). Close only after the live
  validation in §6 passes.
- The working tree already has ~20 modified files (`src/browser.py` among them)
  from prior work. **Check what is uncommitted before editing** — do not fold
  unrelated changes into this commit.
- Use the `/commit` skill: it requires updating `README.md`, `USER_GUIDE.md`, and
  `SESSION_HANDOFF.md`, and adding a Mermaid diagram for any new workflow.
- Suggested split: one commit for the dropdown fixes (#1-#3 + fixture), one for the
  password fix (#4 + tests) — the second is a security fix and reads better alone.
