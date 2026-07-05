# Job Applier — User Guide

An AI job-application agent that runs inside **Claude Code**. It finds high-quality
product/strategy roles across a curated set of companies and fills out their
application forms for you — intelligently, on any ATS, and never submitting
without your say-so.

---

## 1. What it is

Claude Code is the brain. A local **MCP server** (`job-applier`) gives it hands and
memory: it drives a real Chrome browser (via Playwright), reads your profile /
history / knowledge base, and pulls live jobs from company career boards. You
interact through three skills:

| Command | What it does |
|---|---|
| `/find-jobs <query>` | Search your company watchlist for matching roles, ranked semantically and filtered by your criteria. |
| `/apply-to-job <url>` | Open an application form and fill it from your profile/history/context, pausing only when unsure. |
| `/apply-batch <urls>` | Queue several applications: parallel answer prep, **one** upfront approval (incl. per-job submit consent), then serial fill/submit with zero mid-run prompts. |

There is **no OpenAI/Anthropic API key** and **no cost** in the core flow — Claude
Code is the reasoner, and job discovery uses free public ATS APIs.

```
Claude Code (the reasoner)
   │  /find-jobs · /apply-to-job
   ▼
job-applier MCP server (Python, local)
   ├─ Browser   open_job · read_form · fill_field · get_field_options ·
   │            upload_resume · screenshot · check_for_intervention ·
   │            get_job_text · submit_application
   ├─ Answers   get_profile_field · search_history · save_answer · search_context
   ├─ Criteria  get_search_criteria
   └─ Discovery list_watchlist_postings · get_posting · list_companies · add_company
Your files: user_profile.yaml · job_criteria.yaml · watchlist.yaml ·
            resume.txt (+ resume.pdf) · context/*.md · data/history.json
```

---

## 2. One-time setup

```bash
pip install -r requirements.txt      # mcp, playwright, pyyaml, pypdf, httpx
playwright install chromium          # the browser the agent drives
```

Then, in Claude Code, **open this project and reload it** so it loads
[.mcp.json](.mcp.json). Run `/mcp` — you should see the `job-applier` server with
**21 tools**.

> Whenever you change code in `src/`, reload Claude Code so the MCP server
> restarts with the new code.

---

## 3. Your files (edit these to make it yours)

| File | Purpose |
|---|---|
| [user_profile.yaml](user_profile.yaml) | Your exact facts (name, email, phone, location, work auth, links…). Filled fields auto-answer forms. Leave a field `""` to skip it. |
| [resume.txt](resume.txt) | Resume **text** used for crafting answers. |
| **`resume.pdf`** (add to project root) | The actual file **uploaded** to forms (preferred over `.txt`). On each apply, its text is auto-synced into `resume.txt`. |
| [context/](context/) | Your "knowledge base" — `background.md`, `stories.md`, `preferences.md`, and any `.md` / `.txt` / **`.pdf`** you add. Searched when crafting open-ended answers. |
| [job_criteria.yaml](job_criteria.yaml) | The strict bar for `/find-jobs`: acceptable titles, seniority, locations/remote, and `salary_floor`. |
| [watchlist.yaml](watchlist.yaml) | The ~20 companies searched by `/find-jobs`. |
| [data/history.json](data/history.json) | Past Q&A answers. Grows automatically as you approve crafted answers. |

**To update your resume:** drop `resume.pdf` in the project root. From then on it's
uploaded to forms and its text is re-extracted into `resume.txt` at the start of
every apply. No PDF? Just edit `resume.txt` (it gets uploaded as-is).

**Extra PDFs** (case studies, portfolio) go in [context/](context/) — they're
indexed for answers, never uploaded. (Text PDFs only; scanned images yield no
text.)

---

## 4. Finding jobs — `/find-jobs`

```
/find-jobs fintech product strategy
/find-jobs AI product, remote
/find-jobs                     ← no query = everything that passes your criteria
```

What happens:
1. Pulls **live** roles from every company in [watchlist.yaml](watchlist.yaml) via
   their public ATS boards (Greenhouse/Lever/Ashby).
2. Pre-filters ~5,000 roles down to product/strategy titles at your seniority
   (from [job_criteria.yaml](job_criteria.yaml)).
3. **Claude ranks them semantically** against your query (company domain is a
   strong signal — Plaid/Ramp/Coinbase = fintech, OpenAI/Anthropic = AI).
4. Deep-reads the top candidates and applies the **strict filter**: title/
   seniority, location/remote, and salary ≥ your floor **when disclosed**
   (undisclosed salary is kept and flagged "salary not listed").
5. Returns only the passing roles — each with company, location, salary, a one-line
   fit note, and a **direct apply URL** — then offers `/apply-to-job`.

Ashby companies return structured salary (e.g. OpenAI PM roles $293–385k); for
Greenhouse, salary is confirmed by reading the description.

---

## 5. Managing your watchlist

The watchlist is the universe `/find-jobs` searches — curated for quality.

- **See it:** `list_companies`, or open [watchlist.yaml](watchlist.yaml).
- **Add:** `add_company https://jobs.ashbyhq.com/<company>` (or a Greenhouse/Lever
  board URL) — it detects the ATS and slug and appends the entry.
- **Remove:** delete the entry from [watchlist.yaml](watchlist.yaml).

Current seed (20): Stripe, Figma, Databricks, Airtable, Brex, Mercury, Coinbase,
Anthropic, Scale AI, Instacart, Airbnb, Robinhood, Samsara, Ramp, Notion, Plaid,
Vanta, Linear, OpenAI, Snowflake.

---

## 6. Applying — `/apply-to-job`

```
/apply-to-job https://job-boards.greenhouse.io/embed/job_app?for=samsara&token=8035756
```

The browser opens **visibly** so you can watch and step in. For each field, the
agent resolves an answer in this order:

1. **Exact profile value** ([user_profile.yaml](user_profile.yaml)) → filled
   verbatim.
2. **Similar past answer** ([data/history.json](data/history.json)) → adapted.
3. **Crafted from your context** (resume + [context/](context/)) → written in your
   voice.

**Confidence gate:** factual/well-supported answers are auto-filled; open-ended or
uncertain drafts pause and ask you to approve or edit. Approved crafted answers are
saved to history and reused next time.

**Answer style rules** (in the skill):
- Demographic/eligibility fields get **bare values** ("United States", "Yes"), not
  sentences.
- Open-ended questions get full, specific, first-person answers.
- Instructions aimed at AIs / prompt-injection in a posting are **ignored** — it
  answers as you would.

**Resume upload:** handled automatically, even when the form hides the file input
behind an "Attach" widget. **Custom dropdowns** ("How did you hear about us?") are
read with `get_field_options` so it picks a real option instead of guessing.

At the end it screenshots the filled form and summarizes what it filled and what it
left for you.

### Batch mode — `/apply-batch`

```
/apply-batch https://jobs.ashbyhq.com/notion/…/application https://jobs.ashbyhq.com/notion/…/application
```

For applying to several roles in one sitting without sitting through each
one's prompts. It runs in stages:

1. **Snapshot** — each form is opened briefly and saved (fields + job
   description) to `data/prep/` (gitignored).
2. **Parallel prep** — one read-only subagent per job resolves every field
   through the same profile → history → context cascade and drafts anything
   open-ended.
3. **One consolidated review** — you approve/edit all gated answers for the
   whole queue **and give per-job submit consent** ("submit both", "fill #2
   but don't submit"…). This is the only interaction.
4. **Serial fill + submit** — each approved job is filled, verified, and
   submitted with **zero further prompts**. Anything unexpected (a new
   required field, an unmatched dropdown, an unverified submit, a real
   CAPTCHA) **parks** that job — recorded with a reason, queue moves on.
   If an ATS spam-flags an automated submit (reCAPTCHA v3 scoring), the job
   is left **fully filled** and queued for you instead — by design, no
   automated retries.
5. **Screenshot audit + report** — every "submitted" job is re-verified
   against its success page; anything unverified is handed to you. The report
   reads "N submitted (verified) · K awaiting your manual submit · M parked
   (why + URLs)", and the browser is parked on the first form waiting for
   your click.

The "never auto-submit" rule is unchanged — consent is just collected once,
upfront, per job. Only verified submits are logged as `submitted` in
`data/applications.json`; forms you submit yourself are logged as
`manual_submission`, and unconfirmed clicks as `attempted`.

### Safety
- **It never submits on its own.** `submit_application` runs only when you
  explicitly say to submit.
- The browser is **non-headless** — you can take over at any point.
- Confidence gating applies to *answers*; submission is *always* your call.

### CAPTCHAs and login walls
If a "verify you are human" / reCAPTCHA / Cloudflare challenge appears, the agent
**stops and asks you** to clear it in the open browser window, then continues once
you confirm. It will never try to solve a CAPTCHA itself, and never submits while
one is present.

---

## 7. The full tool set (21)

**Browser / apply**
- `open_job(url)` — open a posting; syncs resume.pdf→txt; **one shot**: also
  returns the intervention check + the parsed form (no separate
  `check_for_intervention`/`read_form` needed right after).
- `read_form()` — list every fillable field (any ATS, no per-site rules).
- `get_field_options(index)` — real options for a dropdown/combobox.
- `fill_field(index, value)` — fill one field (text/select/radio/checkbox/combobox).
- `fill_many([{index,value}])` — **fill many fields in one call** (the fast path).
- `upload_resume([index])` — attach the resume (auto-finds hidden file inputs).
- `screenshot([path])` — capture the page for review.
- `get_job_text()` — the visible page text (read a JD).
- `check_for_intervention()` — detect CAPTCHA/verification/login walls.
- `submit_application([index])` — **gated**; only on your explicit go-ahead.

**Answers / memory**
- `resolve_fields([{index,label}])` — **resolve many fields in one call**
  (profile → history → context cascade), tagged by source. The fast path.
- `get_profile_field(label)` — exact value from your profile (single).
- `search_history(question)` — closest past answers, scored.
- `save_answer(question, answer)` — remember an approved answer.
- `search_context(query)` — relevant snippets from your knowledge base.

**Criteria / discovery**
- `get_search_criteria()` — your strict bar from job_criteria.yaml.
- `list_watchlist_postings([query],[limit])` — live product/strategy roles across
  the watchlist; `query`/`limit` keep the payload small enough to rank inline.
- `get_posting(url)` — full description for one posting (via ATS API).
- `get_postings([urls])` — **deep-read many postings in one call** (batch).
- `list_companies()` / `add_company(url)` — view / grow the watchlist.

---

## 8. Typical session

1. `/find-jobs fintech product strategy` → review the shortlist.
2. Pick one → `/apply-to-job <its url>`.
3. Watch it fill the form; answer any low-confidence prompts it surfaces.
4. Solve a CAPTCHA if asked.
5. Review the filled form in the browser; tell it to submit (or finish/submit
   yourself).

---

## 9. Known limits & tips

- **Reload after code changes.** The MCP server caches code until Claude Code
  restarts.
- **Lever postings:** the base URL is the job description; the form is at
  `<url>/apply` (the skill handles this).
- **Search is watchlist-only** by design (low-volume, high-quality). To cover a
  company you don't follow, `add_company` it first, or paste its apply URL straight
  into `/apply-to-job`.
- **Salary:** always disclosed on some Ashby boards; on Greenhouse it's inside the
  description. Roles with no posted salary are shown flagged, not dropped.
- **EEO / self-ID fields** are left blank unless you fill them in your profile —
  your choice.
- **Ashby spam flags:** Ashby scores submissions with invisible reCAPTCHA and
  may reject an automated submit with *"flagged as possible spam"* — your
  answers are preserved on the page. **By design the agent does not retry:**
  it leaves the form filled and presents it to you to click
  **Submit Application** yourself (a real human click passes the scoring; the
  application is then tracked as `manual_submission`). A confirmation email
  from the company is the ground truth that a submit landed.
- **A stale `OPENAI_API_KEY`** may still sit in [.env](.env) from the old version;
  it's unused now and safe to delete.

---

## 10. Where things live

```
Job Applier/
├─ .mcp.json                     # registers the job-applier server for Claude Code
├─ user_profile.yaml             # your exact facts
├─ job_criteria.yaml             # strict search bar (titles/seniority/salary/location)
├─ watchlist.yaml                # ~20 target companies
├─ resume.txt   (resume.pdf)     # reasoning text  (uploaded file)
├─ requirements.txt
├─ context/                      # knowledge base (md/txt/pdf) for crafting answers
├─ data/history.json             # learned answers
├─ data/applications.json        # application tracker (verified submits)
├─ data/prep/                    # batch-mode prep files/sheets (gitignored)
├─ src/
│  ├─ mcp_server.py              # the 21 tools
│  ├─ browser.py                 # ATS-agnostic form reading/filling
│  ├─ data.py                    # profile lookup + history
│  ├─ context.py                 # knowledge-base retrieval
│  ├─ config.py                  # paths + loaders
│  └─ providers/
│     ├─ __init__.py             # provider seam (get_provider)
│     └─ watchlist.py            # ATS board fetch/normalize + watchlist mgmt
└─ .claude/skills/
   ├─ find-jobs/SKILL.md         # /find-jobs
   ├─ apply-to-job/SKILL.md      # /apply-to-job
   └─ apply-batch/SKILL.md       # /apply-batch (queue mode)
```
