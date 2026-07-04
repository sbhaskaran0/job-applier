# Session handoff — job-applier

Paste this into a fresh Claude Code session to restore context. Last updated 2026-07-04 (session 2).

## What this project is
An AI job-application agent that runs **inside Claude Code**. Claude is the
reasoner; a local **MCP server** (`job-applier`, Python, stdio, launched via
`.mcp.json` → `python -m src.mcp_server`) gives it a live Playwright browser plus
the user's data. **No LLM API key** in the core flow. Two skills:
- **`/find-jobs <query>`** — pulls live roles from a curated 20-company watchlist
  via public ATS board APIs (Greenhouse/Lever/Ashby), ranks semantically, strict-
  filters by `job_criteria.yaml`.
- **`/apply-to-job <url>`** — opens an application, fills it from
  profile→history→context with confidence-gated approval, **never auto-submits**.

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
- Skills: `.claude/skills/{find-jobs,apply-to-job}/SKILL.md`.
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

## Git state
- Branch: **`fix/ashby-button-group-and-linkedin`** (off `main`). NOT merged, NOT pushed.
- Commits on it (newest first): `0a4fa2b` README diagram · `e185486` context files
  · `334a0de` browser/EEO fixes · `1850991` history capture · `b3c3593` Audare
  timeline + batch wiring · `7867f6c` batch tools + compact corpus · `378cb93`
  button-group + LinkedIn. (7 unpushed commits total; nothing on `main`/GitHub yet.)
- `.env` untracked/never committed; git identity set locally.

## OPEN ITEMS / next steps
1. **Restart Claude Code** — the running MCP server caches old code; the
   browser-layer + EEO + capture fixes are NOT live until a restart.
2. **Merge/push** the branch when ready (nothing is on `main`/GitHub yet).
3. **User action: update `resume.pdf`** — still says "Audare AI … Ongoing".
   `resume.txt` is regenerated from it on every `open_job`, so that stale line
   still out-ranks the corrected `background.md` in retrieval. Apply skill style
   rule #4 mitigates meanwhile.

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
- **Greenhouse anti-bot**: reCAPTCHA badge is usually passive (non-blocking).
  Real submits may trigger an **8-char email verification code** — human-only step.
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
