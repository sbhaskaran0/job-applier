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
        B["~30 watchlist boards<br/>public Greenhouse/Lever/Ashby APIs"] --> N["normalize<br/>(ats, slug, job_id)"]
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
    H -- "no strong match" --> C["3 · Context retrieval<br/>context/*.md·txt·pdf + resume text<br/>top-5 keyword-scored snippets"]

    C --> CR["Claude crafts answer<br/>confidence: craft"]
    HR --> U{"User approves / edits"}
    CR --> U
    U --> FM["fill_many"]
    U -- "save_answer<br/>(question, answer, scope)" --> HIST

    PV --> REV["Review: verify current_value,<br/>user explicitly says submit"]
    HH --> REV
    FM --> REV

    REV --> S{"submit_application<br/>verify from page TEXT"}
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

**Full setup and usage: [USER_GUIDE.md](USER_GUIDE.md).**
