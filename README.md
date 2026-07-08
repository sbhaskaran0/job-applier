# Job Applier

An AI job-application agent that runs inside **Claude Code**. It finds high-quality
product/strategy roles across a curated company watchlist and fills out their
application forms on any ATS (Greenhouse, Lever, Ashby, Workday…) — intelligently,
and **never submitting without your say-so**.

Claude Code is the reasoner; a local **MCP server** gives it a real browser
(Playwright), your profile/history/knowledge base, and live jobs from company
career boards. **No LLM API key or cost** in the core flow.

## Quick start

```bash
pip install -r requirements.txt
playwright install chromium
```

Open this project in Claude Code and reload it (loads `.mcp.json`), then:

- `/find-jobs fintech product strategy` — search your watchlist, ranked & filtered.
- `/apply-to-job <url>` — open an application and fill it from your data.
- `/apply-batch <url> <url> …` — queue several applications: answers are
  prepared in parallel, you approve everything (including per-job submit
  consent) in **one** upfront review, then the queue fills and submits
  serially with zero further prompts — anything unexpected is parked with a
  reason instead of interrupting you.
- `/tailor-application <url>` — **on demand**, generate a bespoke resume +
  cover letter for one posting: it re-emphasizes/reorders bullets in your
  `resume.docx` (formatting preserved) and drafts a cover letter in **your own
  writing voice**, saved for the apply flow to pick up automatically.
- **`autonomous` prefix** — prefix the argument of `/find-jobs`,
  `/apply-to-job`, or `/apply-batch` with `autonomous` (e.g.
  `/apply-batch autonomous <url> <url>`) to run **without approval gates** and
  **auto-submit where possible**. Jobs that still need a manual submit
  (spam-reject, visible CAPTCHA, parked fields) are left filled for you. Opt-in
  per run — nothing is ever autonomous unless you say so. See
  [the autonomous flow](#autonomous-mode-hands-off-per-run) below.

Edit `user_profile.yaml`, `job_criteria.yaml`, `watchlist.yaml`, `resume.txt`
(+ optional `resume.pdf`), and `context/` to make it yours.

Optionally, keep the job corpus warm without a Claude session:

```bash
python -m src.refresh    # fetch all boards → data/postings.db + data/digest-latest.md
```

> **⚠️ EEO / self-identification data:** `user_profile.yaml` may contain
> voluntary EEO self-identification values (gender, race/ethnicity,
> Hispanic/Latino status, veteran status, disability status), each marked with
> `eeo: true`. This is sensitive demographic data: it lives in plain text in
> this repo, and when present the agent will auto-answer the corresponding
> *voluntary* self-ID sections on applications. Providing it is always
> optional — delete the values to have those sections left blank instead. EEO
> answers are never written to the answer history or the application log.

## How jobs are discovered

Discovery is split into a **deterministic, LLM-free ingest** (schedulable — it
needs no Claude session) and a **semantic ranking layer** that Claude runs at
`/find-jobs` time over the local store. Salary, years-of-experience, and
seniority are extracted **once per posting at ingest**, so the strict baseline
in `job_criteria.yaml` is enforced as a local query instead of per-search JD
deep-reads.

```mermaid
flowchart TD
    subgraph ING["python -m src.refresh — pure Python, no LLM, scheduler-friendly"]
        B["~34 watchlist boards<br/>public Greenhouse/Lever/Ashby APIs"] --> N["normalize<br/>(ats, slug, job_id)"]
        N --> X["extract once per posting:<br/>salary from JD text · min-years (advisory)<br/>· excluded-seniority flag"]
        X --> DB[("data/postings.db<br/>first_seen · last_seen · removed_at<br/>(removals only from boards that fetched OK)")]
        DB --> DG["data/digest-latest.md<br/>new baseline-passing roles ·<br/>board health · yield per company"]
    end
    DB --> Q["list_watchlist_postings<br/>deterministic baseline filter<br/>+ already_applied · is_new<br/>(live-fetch fallback if store > 36h old)"]
    Q --> R["/find-jobs: Claude ranks semantically,<br/>deep-reads finalists, returns apply URLs"]
```

The store is a cache of public data — delete `data/postings.db` and the next
refresh rebuilds it. Schedule the refresh daily (Windows Task Scheduler via
`scripts/refresh.cmd`, or cron/launchd) to get a standing digest of new
matching roles; see the USER_GUIDE.

## How a field gets answered

Every form field runs through a strict source cascade — **profile → history →
context** — where the first hit wins and precision decreases (and gating
increases) down the stack. Approved and submitted answers flow back into
history, so the system compounds with every application.

```mermaid
flowchart TD
    F["Form field label"] --> P{"1 · Profile<br/>user_profile.yaml<br/>alias match?"}

    P -- "exact value<br/>(EEO entries flagged)" --> PV["Fill verbatim<br/>+ style rules"]
    P -- "no match" --> H{"2 · History<br/>history.json<br/>similarity ≥ 0.7?"}

    H -- "evergreen or<br/>same-company scope" --> HH["confidence: high<br/>adapt & fill"]
    H -- "other-company or<br/>conditional scope" --> HR["confidence: review<br/>adapt, then GATE"]
    H -- "no match · closed-choice<br/>(combobox/select/radio/checkbox)" --> CH["source: choice<br/>pick from options<br/>(no essay corpus)"]
    H -- "no match · open free-text" --> C["3 · Context retrieval<br/>context/*.md·txt·pdf + resume text<br/>clipped keyword-scored snippets"]

    CH --> FM
    C --> CR["Claude crafts answer<br/>confidence: craft"]
    HR --> U{"User approves / edits"}
    CR --> U
    U --> FM["fill_many"]
    U -- "save_answer<br/>(question, answer, scope)" --> HIST

    PV --> REV["Review: verify current_value<br/>(1 corrective pass, else flag)"]
    HH --> REV
    FM --> REV

    REV -- "field won't commit/verify<br/>after one fix" --> RF["Flag for manual intervention<br/>left set in the open browser"]
    REV -- "all fields verified,<br/>user explicitly says submit" --> S{"submit_application<br/>verify from page TEXT"}
    S -- "explicit success text" --> SS["submitted"]
    S -- "'flagged as possible spam'" --> SR["rejected_spam<br/>leave form for manual click"]
    S -- "neither (vanished form ≠ success)" --> SA["attempted<br/>screenshot-audit"]
    SS -- "form snapshot<br/>(EEO never persisted)" --> HIST[("history.json<br/>normalized dedupe,<br/>scope + company + date")]
    SS -- "confirmed submits only<br/>(deduped on company+role)" --> APPS[("applications.json<br/>tracker")]

    HIST -. "reused on the<br/>next application" .-> H
```

- **Profile** (deterministic): ~30 curated facts matched by alias phrases;
  filled verbatim, never re-stored. EEO self-ID values are flagged and only
  used in voluntary self-ID sections.
- **History** (probabilistic, vetted): your own past answers, fuzzy-matched
  and scope-gated — only evergreen or same-company matches auto-fill;
  everything else needs approval.
- **Context + resume** (generative, always gated): paragraph chunks scored by
  keyword overlap; Claude writes from the snippets and pauses for approval.
  The resume text is just another retrieval source here — it has no special
  priority over `context/` files.

## Tailoring a resume + cover letter (on demand)

Response rate — not just apply speed — is the highest-leverage lever, so
`/tailor-application <url>` produces a **bespoke resume and cover letter for one
posting**. It is explicitly invoked, never part of the normal apply flow, and
follows the same "Claude reasons, tools are hands" split: Python reads the base
`.docx`, applies the edits Claude decides, exports the PDF, and gathers your own
past cover letters as voice exemplars — **no LLM API key**.

```mermaid
flowchart TD
    JD["Job posting<br/>get_posting / paste"] --> PLAN
    DOCX["resume.docx<br/>(your base template)"] --> RT["read_resume_template<br/>indexed paragraphs"]
    RT --> PLAN{"Claude plans edits:<br/>reorder · re-emphasize ·<br/>trim bullets · sharpen summary<br/>(truthful, grounded in background.md)"}
    PLAN --> TR["tailor_resume(edits)<br/>edit .docx COPY in place<br/>→ export PDF (MS Word)"]
    TR --> STORE[("resumes/&lt;job-slug&gt;/<br/>resume.docx + resume.pdf")]

    EX["context/ cover letters<br/>+ writing samples"] --> GCE["get_cover_letter_examples<br/>full-text voice exemplars"]
    GCE --> DRAFT{"Claude drafts cover letter:<br/>YOUR voice + JD substance<br/>(gated for your approval)"}
    JD --> DRAFT
    DRAFT --> SCL["save_cover_letter(text)"]
    SCL --> STORE2[("resumes/&lt;job-slug&gt;/<br/>cover_letter.txt + .pdf")]

    STORE -.-> GJA["get_job_artifacts<br/>(at apply time)"]
    STORE2 -.-> GJA
    GJA -->|"tailored exists"| USE["upload tailored resume<br/>+ cover letter"]
    GJA -->|"none"| DEF["fall back to<br/>default resume"]
```

The job folder is keyed off the **same `(company, role)` identity**
`applications.json` dedupes on, so `/apply-to-job` and `/apply-batch`
automatically use the tailored artifacts when they exist and the default resume
otherwise. Drop a `resume.docx` in the project root to enable resume tailoring
(the cover-letter half works from `context/` alone). PDF export is
cross-platform: it uses Microsoft Word when present (Windows or macOS) and
falls back to LibreOffice (`soffice`, any OS, no Word needed); if neither is
installed the tailored `.docx` is still saved to export manually.

## Autonomous mode (hands-off, per run)

By default the agent gates its answers and **never submits without your say-so**.
Prefix any of the three commands' arguments with **`autonomous`** to opt that one
run into full hands-off execution: it resolves and fills every answer without
pausing, and **auto-submits where it safely can**. In autonomous `/find-jobs` it
also **auto-selects the top finalists** (default 5, skipping already-applied) and
chains straight into the batch apply — search to submit, end to end.

Autonomous mode removes the **approval gates**, not the **guardrails**. It still
won't fight a stubborn widget (one corrective pass, then park), won't
force-submit past a spam-reject or a visible CAPTCHA, and never fabricates an
answer — anything it can't complete cleanly is left **fully filled** in its tab
for a one-click manual submit, exactly as in gated batch mode.

```mermaid
flowchart TD
    KW["/find-jobs · /apply-to-job · /apply-batch<br/>with <b>autonomous</b> prefix"] --> FIND
    subgraph FIND["find-jobs (autonomous)"]
        RANK["rank + strict-filter"] --> PICK["auto-select top N finalists<br/>(default 5, skip already-applied)"]
    end
    PICK --> QUEUE["apply-batch (autonomous)"]
    KWURL["autonomous URL(s) given directly"] --> QUEUE
    QUEUE --> NOGATE["Stage C approval <b>skipped</b><br/>(one-line plan logged)"]
    NOGATE --> FILL["fill every field<br/>profile → history → context<br/>(no pause; still save_answer)"]
    FILL --> VERIFY{"verify + one<br/>corrective pass"}
    VERIFY -->|"clean"| SUBMIT["auto-submit"]
    VERIFY -->|"stubborn field / no honest answer"| PARK["park — leave filled"]
    SUBMIT --> CHECK{"get_job_text<br/>success?"}
    CHECK -->|"verified"| DONE["log submitted"]
    CHECK -->|"spam-reject / unverified"| MANUAL["manual_submission<br/>leave tab filled"]
    VISIBLE["visible CAPTCHA / login"] --> PARK
    PARK --> REPORT["Report: N submitted ·<br/>K awaiting your manual submit · M parked"]
    MANUAL --> REPORT
    DONE --> REPORT
```

**Full setup and usage: [USER_GUIDE.md](USER_GUIDE.md).**
