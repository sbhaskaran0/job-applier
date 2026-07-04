---
name: find-jobs
description: Find high-quality product/strategy roles across a curated company watchlist (their public ATS boards), ranked semantically against a natural-language query and filtered strictly by baseline criteria (titles/seniority, location/remote, salary floor). Returns only suitable listings, each with a direct apply URL. Pass a natural-language query as the argument (e.g. "fintech product strategy", "AI product, remote"). No argument = all product/strategy roles that pass the criteria.
---

# Find jobs (curated watchlist + semantic search)

You search a **curated set of ~20 target companies** (in [watchlist.yaml](../../../watchlist.yaml)),
pulling their live roles directly from public ATS boards, then rank them
**semantically** against the user's query and keep only those that clear the
strict baseline bar. Low volume, high quality.

## Workflow

1. **Load the corpus** — call `list_watchlist_postings(query, limit)`, passing
   the user's argument as `query` (e.g. "fintech product strategy") and a `limit`
   (~40) so the payload stays small enough to **rank inline — do not spawn a
   subagent**. It returns product/strategy roles across all watchlist companies,
   pre-filtered to the acceptable titles/seniority from `job_criteria.yaml` and
   deduped, each `{company, title, location, remote, salary_min, salary_max, url,
   snippet}`, plus `matched` (all that passed the strict filter), `returned`
   (after query/limit), and `companies_failed` (report any). If `returned` looks
   too narrow, re-call with a broader/empty `query` or a higher `limit`. Also
   call `get_search_criteria` for the strict bar (salary_floor, locations, remote
   policy) — issue these two independent calls together in one message.
2. **Semantic rank** — interpret the user's argument as a natural-language intent
   (e.g. "fintech product strategy", "AI/ML product", "payments"). Rank the
   postings by how well each **semantically** matches that intent, using the
   title, company (domain context — e.g. Plaid/Ramp/Mercury/Brex/Robinhood/
   Coinbase = fintech; OpenAI/Anthropic/Scale = AI), and snippet. No query =
   rank by overall fit to the profile/context (use `search_context` if helpful).
3. **Deep-read finalists** — for your top ~5–12 candidates, call
   **`get_postings([url, ...])` once** (batch) to read the full JDs together
   rather than one `get_posting` per URL. Confirm seniority, remote/location, and
   salary — especially for Greenhouse roles where salary isn't structured and
   lives in the description.
4. **Strict filter** — keep a listing ONLY if it passes ALL of:
   - **Title/seniority** — a product/strategy role at mid/senior level (the
     corpus is pre-filtered, but drop anything the JD reveals as Director+/Staff/
     Principal/Associate/Intern).
   - **Location/remote** — remote-allowed, or in `locations_allowed` /
     `relocation_targets`.
   - **Salary** — if disclosed, ≥ `salary_floor`; drop if below. If undisclosed,
     keep and label "salary not listed".
5. **Return** the passing listings ranked by (semantic match + fit), each with:
   **title · company · location/remote · salary (or "not listed") · one-line why
   it matches · apply URL**. If nothing passes, say so and name the most common
   blocker. Offer to run `/apply <url>`.

## Managing the watchlist
- `list_companies` shows the current watchlist.
- `add_company <board-or-careers-url>` adds one (detects ATS + slug); e.g.
  `add_company https://jobs.ashbyhq.com/rula`. To remove, edit
  [watchlist.yaml](../../../watchlist.yaml).

## Rules
- Only return roles from the watchlist that pass every criterion — never pad
  results. Quality over quantity is the whole point.
- Every result must have a **direct ATS apply URL** (the corpus provides these).
- Be honest about salary: Greenhouse roles usually need a JD read; if you didn't
  confirm it, say "salary not listed" rather than assuming it clears the floor.
- If the user pastes an off-watchlist URL, just hand it to `/apply` (or
  `add_company` it first). WebSearch is only a last-resort fallback.
