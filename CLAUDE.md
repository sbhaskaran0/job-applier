# CLAUDE.md

Project-level guidance for Claude Code. See `SESSION_HANDOFF.md` for full architecture and current state.

## Task tracking — keep the tracker in sync

Work on this project is tracked in **taskman** (the local REST/graph backbone
at `C:/Users/siddh/Task Management buildout`), project **`JOB`**. The taskman
MCP is a local HTTP server reached via this repo's `.mcp.json`
(`${TASKMAN_URL:-http://localhost:8000}/mcp`, bearer `${TASKMAN_API_KEY}` — the
token is an environment variable, never committed). This replaced Linear on
2026-09-10; there is no Linear MCP any more.

> **`JOB-*` numbers in this repo are NOT taskman keys.** taskman's `JOB`
> project reuses the `JOB-` prefix but renumbered from 1, so the `JOB-*` IDs
> written throughout `SESSION_HANDOFF.md`, `docs/backlog-multiuser.md`, and
> `src/` code comments are **Linear-era historical citations** that point at
> different issues (or none). **Always resolve a live issue by title with
> `mcp__taskman__search_nodes`** — never by assuming a number carried over,
> and never cite a number you read in a doc as if it were a tracker key.

**When you complete a task, update the tracker before considering the work
done:**

1. **Find the matching issue** — `mcp__taskman__search_nodes` (filter
   `under: "JOB"`, `status_category: "todo,in_progress"`) against the work's
   *title*. If none matches, create one first with
   `mcp__taskman__create_story` so the board stays the source of truth, then
   proceed. A story needs exactly one primary parent **epic**
   (`epic:contains:story`, `min_primary: 1`), so `create_story` takes an
   `epic_key`: list candidates with `mcp__taskman__list_children(key="JOB",
   type="epic")` and file under the epic for hand-filed session work, creating
   one if none exists. Keep it **distinct from dev-loop's "Loop backlog" epic**
   so hand-filed and loop-filed work stay separable.
2. **Only mark work done once it's verified** — tests pass / change is
   committed / behavior confirmed. Mirror the project's "trust only a verified
   result" principle; do not close an issue optimistically.
3. **Move the issue to `done`** — `mcp__taskman__update_status(key, "done")`.
   There is no separate close tool. Story statuses are `todo` / `in_progress` /
   `in_review` / `done` / `canceled`.
4. **Add a brief closing comment** — `mcp__taskman__add_comment` summarizing
   what changed and referencing the commit SHA(s) or files touched.
5. If a task is only **partially** complete, leave it open and instead post a
   progress comment and/or move it to `in_progress` — don't close it.

Issues labeled **`loop`** were filed by the dev-loop agent, and **`loop-deferred`**
were planned but deferred on budget. Don't hand-close those without checking the
corresponding `c:/Users/siddh/dev-loop/runs/<date>/` artifacts.

**Scope:** this applies to substantive units of work (a feature, fix, or
discrete backlog item), not trivial intermediate steps. When in doubt about
whether a completed change warrants a tracker update, err toward updating it.

If the taskman MCP tools are unavailable in the current session (server not
running, or `TASKMAN_API_KEY` unset), note that the tracker update is
**pending** rather than silently skipping it.

## Knowledge base — fold new context material into the curated stores

When the user adds new material to the active profile's `context/` dir (pasted application answers, cover letters, writing samples, notes) or shares durable new facts in conversation, **proactively fold it into the curated knowledge stores** — this does **not** happen automatically. All of these live under the active profile (resolve paths with `mcp__job-applier__get_profile_paths`):

- **`context/background.md`** — the canonical career narrative (roles, dates, metrics, tense). Add any new concrete facts/metrics (e.g. a new adoption number, a project detail). Keep the career-timeline/tense rules intact.
- **`context/stories.md`** — reusable situation→action→result talking points and positioning. Enrich existing stories or add new ones for open-ended questions ("hardest thing", "describe a product", operating style).
- **the profile's `data/history.json`** — reusable Q&A. Add via `mcp__job-applier__save_answer` (keeps normalized dedupe/date). Scope honestly: `evergreen` (short stable facts), `company` (essays/tailored — use empty company for a generic essay so it surfaces but stays gated for adaptation, or the real company name to auto-reuse there), `conditional` (situation-dependent).

**What IS automatic (no fold-in needed):** `search_context` and the tailoring voice corpus (`get_cover_letter_examples`) both scan the profile's `context/` **live** on every call, so a newly-added file is immediately searchable for crafting answers and available as cover-letter voice. The fold-in above is specifically to keep the *curated* narrative (`background.md`/`stories.md`) and the *reusable-answer* store (`history.json`) current — those are hand-maintained, not regenerated.

**Ground truth, never fabricate.** Only fold in what the material or the user actually states; get role recency/tense right per the active profile's `context/background.md` (its career timeline decides which role is current and which have ended; its dates override resume text). Lightly fix typos/grammar when adapting the user's prose into a reusable answer; don't invent metrics or details.
