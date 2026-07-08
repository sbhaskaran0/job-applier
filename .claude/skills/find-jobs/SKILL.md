---
name: find-jobs
description: Find high-quality product/strategy roles across a curated company watchlist (their public ATS boards), ranked semantically against a natural-language query and filtered strictly by baseline criteria (titles/seniority, location/remote, salary floor). Returns only suitable listings, each with a direct apply URL. Pass a natural-language query as the argument (e.g. "fintech product strategy", "AI product, remote"). No argument = all product/strategy roles that pass the criteria. Prefix the query with `autonomous` to auto-select the top finalists and apply to them end to end (no manual pick, auto-submit where possible).
---

# Find jobs (curated watchlist + semantic search)

You search a **curated set of ~30 target companies** (in [watchlist.yaml](../../../watchlist.yaml))
via the **local postings store** (`data/postings.db`, refreshed by
`python -m src.refresh` — live ATS fetch only as a staleness fallback), then
rank the survivors **semantically** against the user's query. The strict
baseline bar is already enforced deterministically by the tool. Low volume,
high quality.

## Autonomous mode (opt-in per run)

If the argument **begins with** a bare `autonomous` token (also `auto` /
`--autonomous`), **strip it**, set an "autonomous run" flag, and use the
remainder as the query (empty remainder = rank by overall profile fit, as
usual). An autonomous run does not stop at a ranked list — it **auto-selects the
finalists and applies to them end to end**. See Step 5 for the selection rule;
the actual filling/submitting is delegated to `apply-batch` in *its* autonomous
mode, so all of that skill's guardrails (park-don't-ask, auto-submit-not-force,
manual-submission hand-off) apply. Steps 1–4 are unchanged.

## Workflow

1. **Load the corpus** — call `list_watchlist_postings(query, limit)`, passing
   the user's argument as `query` (e.g. "fintech product strategy") and a `limit`
   (~40) so the payload stays small enough to **rank inline — do not spawn a
   subagent**. Optionally pass `max_years` (e.g. the user's experience + 2) to
   drop roles whose JD asks for more. Store-backed results are ALREADY filtered
   for titles, excluded seniority, location/remote, and disclosed-salary floor
   from `job_criteria.yaml`; each posting is `{company, title, location, remote,
   salary_min, salary_max, salary_listed, salary_source, min_years, first_seen,
   is_new, already_applied, url, snippet}`. Check `source`: `"store"` means
   filtered + enriched; `"live"` means the store was stale (report the `note`,
   suggest running `python -m src.refresh`) and you must strict-filter manually
   per step 4. Skip or clearly mark `already_applied` roles; surface `is_new`
   ones — fresh postings convert best. If `returned` looks too narrow, re-call
   with a broader/empty `query` or a higher `limit`. Also call
   `get_search_criteria` for the bar — issue both calls together in one message.
2. **Semantic rank** — interpret the user's argument as a natural-language intent
   (e.g. "fintech product strategy", "AI/ML product", "payments"). Rank the
   postings by how well each **semantically** matches that intent, using the
   title, company (domain context — e.g. Plaid/Ramp/Mercury/Brex/Robinhood/
   Coinbase = fintech; OpenAI/Anthropic/Scale = AI), and snippet. No query =
   rank by overall fit to the profile/context (use `search_context` if helpful).
3. **Deep-read finalists** — for your top ~5–12 candidates, call
   **`get_postings([url, ...])` once** (batch) to read the full JDs together
   rather than one `get_posting` per URL. Confirm seniority, remote/location,
   and salary — `min_years` and `salary_source: "jd"` are regex-extracted and
   **advisory**: trust them for ranking, confirm them on finalists.
4. **Strict filter** — the store already enforced the baseline; on the deep-read,
   still drop anything the JD reveals as a violation:
   - **Title/seniority** — Director+/Staff/Principal/Associate/Intern, or a
     years ask far above the user's band.
   - **Location/remote** — remote-allowed, or in `locations_allowed` /
     `relocation_targets`.
   - **Salary** — if disclosed, ≥ `salary_floor`; drop if below. If undisclosed,
     keep and label "salary not listed".
5. **Return** the passing listings ranked by (semantic match + fit), each with:
   **title · company · location/remote · salary (or "not listed") · one-line why
   it matches · apply URL** (mark `is_new` roles). If nothing passes, say so and
   name the most common blocker. Offer to run `/apply <url>`.

   **(Autonomous run:** don't stop at the list. From the passing, ranked
   finalists, **auto-select** the top ones to apply to and chain straight into
   the apply flow:
   - **Skip `already_applied`**; prefer `is_new`; drop anything the deep-read in
     Step 4 revealed as a baseline violation. Never auto-select a role you
     couldn't confirm passes the bar.
   - **Safety cap: top 5** by default. Honor an explicit override in the query
     (e.g. "autonomous top 10 fintech pm" → 10). If nothing passes, say so and
     stop — do not widen the bar to hit the cap.
   - **Report the selection first** (the same one-line-per-role list above, with
     apply URLs) so the run is legible, then proceed **without pausing**.
   - **Hand off to autonomous batch apply:** run the `apply-batch` flow in its
     **autonomous** mode over the selected apply URLs (read
     `.claude/skills/apply-batch/SKILL.md`; the `autonomous` flag carries
     through — Stage C is skipped, jobs fill and auto-submit where possible, and
     anything unsubmittable is parked/left for manual submission). A single
     selected URL can go through `apply-to-job` in autonomous mode instead.)

## Digest

`data/digest-latest.md` (regenerated by every refresh) lists new baseline-passing
roles, dark boards, and per-company yield — read it when the user asks "anything
new?" instead of re-ranking the whole corpus.

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
