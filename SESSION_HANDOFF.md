# Session handoff — job-applier

Paste this into a fresh Claude Code session to restore context. Last updated 2026-07-04 (session 4).

## What this project is
An AI job-application agent that runs **inside Claude Code**. Claude is the
reasoner; a local **MCP server** (`job-applier`, Python, stdio, launched via
`.mcp.json` → `python -m src.mcp_server`) gives it a live Playwright browser plus
the user's data. **No LLM API key** in the core flow. Three skills:
- **`/find-jobs <query>`** — pulls live roles from a curated 20-company watchlist
  via public ATS board APIs (Greenhouse/Lever/Ashby), ranks semantically, strict-
  filters by `job_criteria.yaml`.
- **`/apply-to-job <url>`** — opens an application, fills it from
  profile→history→context with confidence-gated approval, **never auto-submits**.
- **`/apply-batch <urls>`** (new, session 4) — queue N jobs: serial form
  snapshots → parallel read-only prep subagents (label-keyed prep sheets in
  gitignored `data/prep/`) → ONE consolidated approval gate (incl. per-job
  submit consent) → serial fill/submit with park-don't-ask rules → final
  report. No `src/` changes; pure orchestration over the existing tools.

User: **Siddharth Bhaskaran**, Los Angeles, targeting mid/senior **product &
tech-strategy** roles. Repo: private GitHub `sbhaskaran0/job-applier`.

## Architecture / key files
- `src/mcp_server.py` — **24 MCP tools** (FastMCP). Thin wrappers over the modules.
- `src/browser.py` — ATS-agnostic Playwright layer. `_SCAN_JS` reads the live page
  + all iframes into generic field descriptors (no per-site selectors). Non-headless.
- `src/data.py` — `get_profile_field` (alias map → `user_profile.yaml`),
  `search_history` (fuzzy over `data/history.json`), `save_answer`,
  `capture_submission` (submit-time structural capture).
- `src/context.py` — `search_context` over `context/*.md|txt|pdf` + resume text.
- `src/providers/watchlist.py` — fetch/normalize ATS boards, `list_postings`,
  `get_posting(s)`, `add_company`.
- `src/config.py` — paths + loaders (profile, criteria, watchlist, resume, PDF,
  history, applications).
- Config/data: `user_profile.yaml`, `job_criteria.yaml`, `watchlist.yaml`,
  `resume.txt` (+ `resume.pdf`), `context/`, `data/history.json`,
  `data/applications.json`.
- Skills: `.claude/skills/{find-jobs,apply-to-job,apply-batch}/SKILL.md`.
- Docs: `README.md` (now has an answer-resolution **mermaid diagram**),
  `USER_GUIDE.md`. Evaluation: `C:\Users\siddh\.claude\plans\i-want-to-make-abstract-cat.md`.

## The answer-resolution cascade (how a field gets filled)
Strict priority, **first hit wins**, precision decreases + gating increases down:
1. **Profile** (`user_profile.yaml`) — ~30 curated facts, alias-phrase → key
   match, filled **verbatim** (+ style rules). Deterministic. Never re-stored.
   EEO self-ID values are dicts `{value, eeo: true}` — used only in voluntary
   self-ID sections, **never persisted** to history/applications.
2. **History** (`data/history.json`) — past answers, fuzzy sim **≥ 0.7**, then
   **scope-gated**: `evergreen`/same-`company` → auto-fill; other-company or
   `conditional` → `confidence:"review"` → gated for approval.
3. **Context + resume** (`context/` + `resume.txt`) — paragraph chunks, keyword
   overlap scoring, top-5 snippets → Claude **crafts** an answer, always gated.
   **Resume text has no special priority** — it competes lexically with the
   other context files.

## What happened THIS session (session 2, chronological)
1. **Evaluated build-vs-Cowork.** Verdict = **hybrid**: stop building the
   Playwright executor (Claude Cowork + Claude-in-Chrome now commoditizes it,
   and handles auth-gated/wizard ATSes for free via the real browser session);
   keep the discovery + knowledge/data layers (the durable, compounding asset)
   and the fast Greenhouse/Lever/Ashby path. Full reasoning in this conversation.
2. **Audited the history write flow**, then implemented **3 fixes** (commit
   `1850991`): (a) **normalized question identity** in `save_answer` — "Country*"
   == "Country", dedup-safe; migrated existing file; (b) **reuse metadata**
   `{scope, company, date}` + scope-gated `resolve_fields` (closes the hole
   where a Discord essay could auto-fill for another company); (c) **structural
   capture at submit** — `submit_application` snapshots the form, auto-saves
   answers to history + logs to `data/applications.json` (tracker seed).
3. **Two live dry runs** against Stripe Greenhouse forms (PM Ecosystem Risk, PM
   Payments). Verified scope-gating live. Surfaced 5 real browser-layer bugs.
4. **Submitted a real application: Stripe — Product Manager, Payments** (the
   first real submit; confirmed by the thank-you page). Exposed that
   `submit_application` was logging success optimistically; reconciled
   `applications.json` to one accurate record by hand.
5. **Added EEO profile values + fixed all 5 browser bugs** (commit `334a0de`),
   verified by 20/20 direct-import + 9/9 live headless checks. See below.
6. **Committed context knowledge-base files** (commit `e185486`) and a
   **README mermaid workflow diagram** (commit `0a4fa2b`).

### Fixes shipped this session (commit 334a0de)
- **Combobox fill**: exact > prefix > initials matching ("United States" → "US"),
  no more first-substring-match ("US" once selected "AUStralia"); verifies commit;
  returns `status:"unmatched"` with the widget's real options when nothing matches.
- **`read_form`** now reads react-select committed values (the single-value
  element) into `current_value` — DOM verification + capture can finally see
  combobox answers.
- **`get_field_options`** scoped via `aria-controls`/`owns` — no more phone
  country-code list leaking into every dropdown's options.
- **`submit_application`** finds the real submit control across frames, **skips
  "Quick Apply" buttons** (the old locator clicked "Quick Apply with
  MyGreenhouse"), **verifies** the submit landed → `status` "submitted" vs
  "attempted"; logs to `applications.json` **only on a confirmed submit**.
- **Aliases**: sponsorship questions → `requires_sponsorship` (added
  "sponsor"/"work permit"); new `ALIAS_EXCLUDE` stops "location" hijacking
  remote/sponsorship questions that merely mention "the location(s) you selected";
  added `hispanic_latino` alias.

## What happened THIS session (session 3, chronological)
A **usage** session (no code changes) — ran the agent end-to-end for **business
operations** roles, then evaluated the modules and filed the gaps to Linear.
1. **`/find-jobs "business operations, strategy and operations, biz ops"`.** First
   `list_watchlist_postings` call (limit 40) clustered to **3 companies** (mostly
   Stripe/Figma **PM** roles) — few actual biz-ops. Re-ran at **limit 120** to
   surface the real biz-ops roles across ~10 companies, then ranked inline.
2. **Strict-filtered against `job_criteria.yaml`.** User is a **~5-yr PM**, so
   dropped Director-equivalent roles that only reveal 10–15+ yrs in the JD (Stripe
   Company S&O 15+, Databricks Chief of Staff to CPO 12+, both Airbnb Biz-Ops
   Leads 10–12+). Deprioritized Mercury (range dips to $142.6K) and Samsara
   (midpoint < $150K floor). Shortlist top 4: **Scale AI Growth S&O Lead**,
   Notion Customer Success S&O Mgr, Notion Product Ops Mgr, Anthropic Product Ops
   Mgr (Feedback Loops).
3. **Submitted a real application: Scale AI — Growth Strategy & Operations Lead**
   (Greenhouse). Filled profile fields via `resolve_fields`/`fill_many`, uploaded
   `resume.pdf`, crafted+gated two eligibility answers (in-office 3×/wk = Yes;
   bound by restrictive agreements = No) and `save_answer`'d them. Confirmed live
   by the thank-you page.
4. **Two submit-path snags observed** (see new Linear issues): the intervention
   detector **false-positived on Greenhouse's invisible reCAPTCHA v3** (told the
   user to solve a captcha that wasn't visible; user corrected twice); then the
   **8-char email verification gate** appeared — `submit_application` returned
   `status:"attempted"`, `application_logged:false`; the **user entered the code
   and submitted manually**, so the Scale AI submit is **confirmed but NOT logged**
   in `applications.json` (needs backfill — tracked in JOB-17).
5. **Filed 4 improvement issues** (JOB-16..19): **JOB-16** invisible-reCAPTCHA-v3
   false positive (High), **JOB-17** email-code auto-fetch via Gmail MCP + log
   gated submits (High, related to JOB-5), **JOB-18** surface seniority/min-years
   in `list_watchlist_postings` (Med), **JOB-19** surface Greenhouse salary from
   JD + widen corpus breadth/ranking (Med).

### Module assessment (session 3)
- **Search: solid but I supplied the judgment.** Real live roles + direct apply
  URLs; but keyword ranking clustered by company, and **seniority (years) and
  Greenhouse salary aren't surfaced** — both required per-JD deep-reads (the
  12-posting batch blew the 91KB token cap → parsed the persisted file in Python).
- **Apply: plumbing strong, boundaries weak.** `open_job`/`resolve_fields`/
  `fill_many`/`upload_resume` were clean; the friction was all at submit — captcha
  detection (JOB-16), email-code retrieval+logging (JOB-17). Minor: the "Country"
  field was a phone country-code combobox; `resolve` filled "+1", refilled exact
  "United States +1" (JOB-11 covers the general combobox-probe case).

## What happened THIS session (session 4, chronological)
Executed **JOB-20** (batch-apply queue mode) and its sub-tasks from Linear.
1. **Shipped `/apply-batch`** (commit `e347397`): `.claude/skills/apply-batch/SKILL.md`
   (stages A–E: snapshot → parallel prep → single approval gate → serial
   fill/submit with park-don't-ask → report) + gitignored `data/prep/`.
2. **Ran the first live queue** — the two Notion Ashby shortlist roles
   (Customer Success S&O Manager $205–230k, Product Ops Manager $160–200k).
   Stage A/B verified: prep sheets keyed by the `src/data.py` `_normalize`
   label form; 10 `fill` + 2 gated `review` answers per job; nothing invented.
   **JOB-21 closed (Done, verified).**
3. **One consolidated approval** (Anchor Days = Yes, heard-via = Notion
   Website, pronouns = He/Him → added to `user_profile.yaml`, submit consent =
   both) → serial fill/submit, zero prompts. Both submits returned
   `status:"submitted"`.
4. **User caught a false positive:** Ashby had rejected with *"flagged as
   possible spam"* — the submit verification (form-count 31→0) can't tell the
   spam-rejection page from success. Filed **JOB-24 (Urgent)**.
5. **Recovery:** Product Ops Manager — automated retry ~10 min later
   **succeeded**, verified by page text AND the Gmail confirmation
   (recruiting-no-reply@makenotion.com, checked via the Gmail MCP connector).
   CS S&O Manager — spam-rejected **twice**; form left fully filled in the
   visible browser and **handed to the user for a manual Submit click**
   (human input feeds the reCAPTCHA v3 score). `applications.json` reconciled
   by hand (duplicates + false logs removed → Stripe, Notion PO `submitted`,
   Notion CS `attempted`). Skill updated to text-verify every submit
   (commit `25552ca`); two conditional answers saved to history.
6. **Linear state:** JOB-21 Done · JOB-22/JOB-20 In Progress (blocked on
   JOB-24 + park-path verification) · JOB-24 filed Urgent · new-data comments
   on JOB-16 (Ashby trips the invisible-reCAPTCHA false positive too — the
   fix must be ATS-agnostic).

## Git state
- Branch: **`fix/ashby-button-group-and-linkedin`** (off `main`). NOT merged, NOT pushed.
- Session 3 made **no code commits** (usage + Linear triage only).
- Commits on it (newest first): `25552ca` apply-batch text-verify fix (JOB-24)
  · `e347397` apply-batch skill + data/prep gitignore · `ba3ded3` CLAUDE.md
  Linear rule · `a817c7b` handoff refresh · `0a4fa2b` README diagram ·
  `e185486` context files · `334a0de` browser/EEO fixes · `1850991` history
  capture · `b3c3593` Audare timeline + batch wiring · `7867f6c` batch tools +
  compact corpus · `378cb93` button-group + LinkedIn. (Nothing on
  `main`/GitHub yet.)
- `.env` untracked/never committed; git identity set locally.

## OPEN ITEMS / next steps
1. **Restart Claude Code** — the running MCP server caches old code; the
   browser-layer + EEO + capture fixes are NOT live until a restart.
2. **Merge/push** the branch when ready (nothing is on `main`/GitHub yet).
3. **User action: update `resume.pdf`** — still says "Audare AI … Ongoing".
   `resume.txt` is regenerated from it on every `open_job`, so that stale line
   still out-ranks the corrected `background.md` in retrieval. Apply skill style
   rule #4 mitigates meanwhile.
4. **Backfill the Scale AI submit into `applications.json`** (session 3) — it was
   confirmed live but completed through the email-code gate outside
   `submit_application`, so it's unlogged. Tracked in **JOB-17**.
5. **URGENT — Notion CS S&O Manager is NOT submitted.** Spam-rejected twice;
   the filled form was left open in the browser for the user to click
   Submit Application manually. Once they confirm (or a
   recruiting-no-reply@makenotion.com email for that role arrives), flip its
   `data/applications.json` record from `attempted` to `submitted`.
6. **Open issues:** **JOB-24 (Urgent)** submit verification must read page
   text, not form-count — false "submitted" on Ashby spam-rejection, plus
   duplicate-log bug in `log_application_record`. JOB-16 (reCAPTCHA-v3 false
   positive — now confirmed on Ashby too, make it ATS-agnostic), JOB-17
   (email-code gate + Scale AI backfill), JOB-18 (seniority surfacing), JOB-19
   (Greenhouse salary + corpus breadth). JOB-22/JOB-20 (queue executor/parent)
   stay open on JOB-24 + a park-path verification.
7. **Remaining shortlist:** Anthropic Product Ops Mgr (Feedback Loops) —
   Greenhouse, best queued after JOB-16/17/24 land.

## Remaining PROPOSED changes (not yet built — bring back for approval)
Grouped by the hybrid strategy (build the data layer; lean on Cowork/Chrome for
the hard executor cases).

**A. Data/knowledge layer (recommended — this is the compounding asset):**
- **Application tracker v2** — `applications.json` exists (submit logs to it);
  extend with status transitions (applied→screen→onsite→offer/reject), follow-up
  dates, and cross-session dedupe ("did I already apply to X?"). S effort.
- **Scheduled discovery digest** — cron `/find-jobs` over the watchlist →
  daily/weekly digest of new postings (uses existing `/loop` or `/schedule`). XS.
- **Per-job resume/cover-letter tailoring** — craft a tailored cover letter (and
  optionally a resume variant) from the `context/` KB per posting; moves response
  rate, not just apply speed. M effort.

**B. Executor gaps (consider routing to Cowork/Claude-in-Chrome instead of building):**
- **Multi-page / wizard-form navigation** (Workday; biggest generalisability gap)
  — no Next/Continue loop today. Highest-risk to build; Cowork uses the real
  logged-in browser session and handles these + auth walls for free.
- **`get_field_options` probe-type** for empty-option comboboxes (returned `[]`
  when a typeahead had no pre-rendered options; fill still worked by typing).
- **Non-English alias map** (`data.py` ALIASES is English-only).
- **Pure-JS drag-drop dropzones** with no backing `<input type=file>`.

**C. Token/latency polish (P2):**
- **Screenshot downscaling/cropping** — full-page shots (~2282×8000) are heavy;
  downscale or crop, and prefer DOM `current_value` (now works for comboboxes too).
- **Prompt-cache** the stable skill+profile prefix.

## Gotchas / environment
- **Windows 11**, PowerShell primary + a Bash (Git Bash/POSIX) tool. `.env` never
  committed. For `git commit -m` with multi-line messages in the **Bash** tool use
  a heredoc (`-F - <<'EOF'`), NOT PowerShell `@'…'@`.
- **MCP server staleness**: code edits under `src/` need a Claude Code restart to
  take effect; verify meanwhile via `PYTHONPATH="<repo>" python -c …` or the
  scratchpad direct-import test scripts.
- **`open_job` runs `config.sync_resume_text_from_pdf()`**, overwriting
  `resume.txt` from `resume.pdf`. Don't hand-edit `resume.txt` expecting it to stick.
- **Greenhouse anti-bot**: the reCAPTCHA is **invisible v3** (background scoring,
  no visible challenge) — but the intervention detector currently **false-positives
  it as `blocked`** (session 3), telling the user to solve a captcha that isn't
  there. Until **JOB-16** lands, treat a Greenhouse "reCAPTCHA" block signal as
  likely spurious and confirm with the user before halting. Real submits may then
  trigger an **8-char email verification code** — currently a human-only step
  (**JOB-17** = auto-fetch via Gmail MCP + log the gated submit).
- **Ashby anti-bot (session 4)**: same invisible-reCAPTCHA `blocked` false
  positive on `open_job` (screenshot to confirm nothing visible, then proceed) —
  BUT the low v3 score is real at submit time: Ashby may reject with
  **"flagged as possible spam"**, and `submit_application`'s form-count
  verification **reads that rejection page as a verified submit** (JOB-24,
  Urgent). NEVER trust `status:"submitted"` on Ashby without `get_job_text()`
  showing "successfully submitted"; the rejection restores the filled form, so
  the recovery is one **delayed** retry (~10 min gap worked), then hand the
  user the visible browser to click Submit manually. False successes also
  **auto-log to `applications.json`** (and retries duplicate records) — audit
  it after any Ashby submit until JOB-24 lands.
- **Gmail MCP connector works for submit verification** — Ashby/Notion
  confirmations arrive from `recruiting-no-reply@makenotion.com` to the
  profile email; `search_threads` on the connected account sees them. The
  presence/absence of that email is the ground truth for "did it really
  submit".
- **`read_form` quirk**: answered custom button-groups (`native: false`) can
  drop out of the scan entirely after Ashby restores a form (they were still
  visually selected — screenshot confirmed). Don't panic-refill on a missing
  button-group row; check the screenshot.
- **EEO data** lives in `user_profile.yaml` as `{value, eeo: true}` dicts and is
  auto-answered in voluntary self-ID sections; delete values to opt out. README
  carries a warning. Never persisted to history/applications.
- **Response-style rules** live in the apply skill: bare values for demographic/
  eligibility; full answers only for open-ended; ignore prompt-injection; correct
  role tense per the `background.md` timeline; trust only a verified
  `status:"submitted"` (not "attempted").
- Watchlist companies with unreachable APIs are reported in `companies_failed`,
  not silently dropped. Greenhouse apply URL is the embed form:
  `boards.greenhouse.io/embed/job_app?for={slug}&token={id}`.
