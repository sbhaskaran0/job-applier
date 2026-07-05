---
name: apply-batch
description: Batch-apply queue mode — apply to N queued jobs with parallel prep, one consolidated upfront approval, then serial fill/submit with zero mid-run prompts. Jobs that hit anything unexpected are parked (with a reason), never prompted on. Pass the queued job URLs (one per line or comma-separated) as the argument. Use apply-to-job for a single application.
---

# Batch-apply queue

Apply to every queued job with **one** approval interaction. The
"never auto-submit" rule still holds: explicit per-job submit consent is
collected — once, upfront, for the whole queue (Stage C). After that gate the
run is autonomous: anything unexpected **parks the job and continues**; you
never prompt mid-run.

This skill orchestrates; the resolution cascade (profile → history → context),
the **Response style rules**, and the **Human intervention** rules all come
from `.claude/skills/apply-to-job/SKILL.md` — read it first and apply it
throughout. Do not duplicate its logic; this file only defines what differs in
queue mode.

**Why serial browser use:** the MCP server holds a single Playwright page
(module-level singleton in `src/browser.py`) with a shared field-index map,
and history/applications JSON writes are non-atomic. So: browser stages (A, D)
are strictly serial; only the read-only reasoning (B) is parallel.

## Stage A — Snapshot pass (serial, browser)

For each queued URL, in order:

1. `open_job(url)` — returns `intervention` + `fields` in one shot. (It also
   re-syncs `resume.txt` from `resume.pdf` — expected, harmless.)
2. `get_job_text()` for the JD.
3. Write a **prep file** `data/prep/<company>-<role-slug>.json`:

```json
{
  "url": "...",
  "company": "...",
  "job_title": "...",
  "ats": "...",
  "snapshot_at": "<ISO date>",
  "fields": [
    {"label": "...", "kind": "text|select|radio|checkbox|file|combobox",
     "options": [...], "required": true, "group": "..."}
  ],
  "jd_text": "..."
}
```

Store **labels, not indexes** (indexes are reassigned on every `read_form`).
If `open_job` reports `intervention.blocked` or `fields` is empty, don't
snapshot — mark the job **parked at snapshot** with the reason and continue to
the next URL. (Exception: a Greenhouse "recaptcha" signal with no visible
challenge is a known false positive until JOB-16 lands — take a `screenshot()`
to confirm nothing visible, note it, and proceed if the form is readable.)

## Stage B — Parallel prep (read-only subagents)

Spawn **one subagent per snapshotted job, all in a single message** so they
run concurrently. Each subagent gets its prep file path and writes a **prep
sheet** to `data/prep/<same-name>.sheet.json`.

Subagent prompt template (fill in the bracketed parts):

> Read the prep file at `[path]` and `.claude/skills/apply-to-job/SKILL.md`
> (resolution cascade + Response style rules — follow both exactly).
> For every fillable field in the prep file, resolve an answer:
> 1. Call `resolve_fields(fields, company="[company]")` once with all
>    `{index, label}` rows (use the array position as `index`; it is only a
>    correlation id here).
> 2. For rows that come back `source:"context"` or `confidence:"review"`,
>    use `search_history` / `search_context` / `get_profile_field` and the
>    prep file's `jd_text` to craft an answer in the preferences.md voice.
> STRICT LIMITS: you are read-only. Use ONLY `resolve_fields`,
> `search_history`, `search_context`, `get_profile_field`, `get_job_text` is
> NOT allowed (browser) — the JD is already in the prep file. Never call
> browser tools (`open_job`, `read_form`, `fill_*`, `screenshot`, ...) and
> never call `save_answer` — other agents are running concurrently.
> Write the prep sheet to `[sheet path]` with this shape, then return a
> one-paragraph summary (counts by source/confidence + anything flagged):
>
> ```json
> {
>   "url": "...", "company": "...", "job_title": "...",
>   "answers": {
>     "<normalized label>": {
>       "raw_label": "...",
>       "answer": "...",
>       "source": "profile|history|context",
>       "confidence": "fill|review",
>       "scope": "evergreen|company|conditional",
>       "notes": "why / provenance / anything odd"
>     }
>   },
>   "flags": ["required field X has no supportable answer", ...]
> }
> ```
>
> Keys are the **normalized label**: lowercase, replace every non-alphanumeric
> run with a single space, trim — i.e. `_normalize` in `src/data.py`
> (`re.sub(r"[^a-z0-9 ]+", " ", label.lower()).strip()` after collapsing
> whitespace). Confidence mapping: `profile` values and `history`-high →
> `"fill"`; everything crafted, adapted from another company, or conditional →
> `"review"`. EEO fields: include only if the profile has an `eeo` value; mark
> `notes: "eeo"`. A required field with no honest answer goes in `flags`, not
> `answers` — never fabricate.

## Stage C — Consolidated approval (THE single gate)

Present one review for the whole queue, then **stop and wait for the user**:

- Per job: every `confidence:"review"` answer (question → draft → source),
  plus all `flags` (unanswerable required fields, odd widgets).
- `confidence:"fill"` rows as a one-line count per job (e.g. "14 profile
  fields auto-fill"), not itemized.
- Ask for: edits/approvals to the review answers, and **explicit submit
  consent per job** — "submit all", "submit 1 and 3", or "fill but don't
  submit N".

Nothing proceeds without this response. A job without submit consent is
filled and left open for manual review, never submitted.

## Stage D — Serial fill + submit (zero prompts)

For each approved job, in order:

1. `open_job(url)` → fresh `fields` with **fresh indexes**.
2. Map prep-sheet answers to indexes by **normalized label** (same
   normalization). Unmatched sheet keys: try token-subset matching; a
   leftover **required** form field not covered by the sheet → **park**.
3. `fill_many` the mapped batch (style rules already applied at prep time).
   Combobox rows returning `status:"unmatched"` with real options: one retry
   with the exact option text if the intended answer clearly matches one;
   otherwise **park**.
4. `upload_resume()` (no index).
5. Verify via `read_form()` — every intended `current_value` set. Mismatch
   after one refill attempt → **park**.
6. `check_for_intervention()` — real visible CAPTCHA / login wall → **park**
   (JOB-16 caveat above applies).
7. `submit_application(company=..., job_title=...)` — only for jobs with
   Stage C submit consent. `status:"attempted"` → **park** (do NOT retry the
   click blindly; note the URL for manual follow-up).
   **Do not trust the returned status alone** (JOB-24: the form-disappearance
   heuristic reads Ashby's spam-rejection page as success). Always confirm
   with `get_job_text()`: success text ("application was successfully
   submitted" / thank-you) → real submit; "We couldn't submit your
   application" / "flagged as possible spam" → NOT submitted.
   **Spam rejection → manual submission (by design).** Automated retries
   against reCAPTCHA v3 are a losing game (and repeated flags may hurt the
   applicant's standing), so an intentional design choice: do NOT auto-retry.
   The rejection page restores the filled form — leave it filled, correct any
   false-success auto-log, record the job with status `"manual_submission"`,
   and continue the queue. These jobs are handed to the user at Stage E for a
   real human click (human input passes the v3 scoring — verified live).
8. On verified `status:"submitted"`: `save_answer` each approved
   crafted/adapted answer with its `scope` (+ `company` when company-scoped).
   Serial stage — writes are safe here.

**Park-don't-ask:** parking = record `{url, company, stage, reason}`, leave
the job unsubmitted, move on. Never prompt, never guess at an answer that
wasn't approved, never submit a parked job. Parked jobs must NOT be logged to
`applications.json` as submitted (only a text-verified submit counts;
spam-rejected jobs are logged as `"manual_submission"` and handed to the
user at Stage E).

## Stage E — Screenshot audit + final report

**Audit every "complete" job before reporting.** For each job the run claims
submitted, re-verify visually: `screenshot()` / `get_job_text()` must show
the explicit success page. Any job without that proof is NOT submitted —
reclassify it as `"manual_submission"`.

**Present the manual-submission queue.** For every unsubmitted job (spam
rejections, unverified submits): its form is left fully filled — give the
user the list (company, role, URL, why) and leave the browser on the first
one so they can click **Submit Application** themselves, navigating through
the rest. After their clicks, verify each (success page via screenshot, or
the confirmation email via the Gmail tools) and log it with status
`"manual_submission"` in `data/applications.json` (keep the note of why).

Then one summary: **"N submitted (verified) · K awaiting your manual submit ·
M parked"** with

- per submitted job: company, role, capture summary + the success proof;
- per manual-submission job: company, role, URL, and the rejection reason;
- per parked job: company, role, **stage + reason**, and its URL;
- which answers were `save_answer`'d.

`applications.json` status vocabulary: `"submitted"` (agent-submitted,
text/screenshot-verified) · `"manual_submission"` (agent filled, human
clicked submit) · `"attempted"` (clicked, never confirmed — needs follow-up).

## Queue-mode rules

- Greenhouse jobs: until JOB-16 and JOB-17 land, expect the invisible-
  reCAPTCHA false positive and the email-code gate (`status:"attempted"`).
  Warn at Stage C if the queue contains Greenhouse jobs; they will likely
  park at submit. Ashby/Lever queues are unaffected.
- Prep files/sheets live in `data/prep/` (gitignored); it's fine to leave
  them for post-run inspection.
- If the queue is a single URL, just use `apply-to-job` instead.
