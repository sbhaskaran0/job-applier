# CLAUDE.md

Project-level guidance for Claude Code. See `SESSION_HANDOFF.md` for full architecture and current state.

## Task tracking — keep Linear in sync

Pending work for this project is tracked in **Linear**, team **Job Applier Task Management** (issue prefix `JOB-*`). The Linear MCP server is configured in local dev config (`~/.claude.json`), not committed to the repo.

**When you complete a task, update Linear before considering the work done:**

1. **Find the matching issue.** If the work maps to an existing `JOB-*` issue, use that. If no issue exists for it, create one first (via `mcp__linear__save_issue`) so the board stays the source of truth, then proceed.
2. **Only mark work done once it's verified** — tests pass / change is committed / behavior confirmed. Mirror the project's "trust only a verified result" principle; do not close an issue optimistically.
3. **Move the issue to `Done`** (`mcp__linear__save_issue` with `id` + `state: "Done"`).
4. **Add a brief closing comment** (`mcp__linear__save_comment`) summarizing what changed and referencing the commit SHA(s) or files touched.
5. If a task is only **partially** complete, leave it open and instead post a progress comment and/or move it to `In Progress` — don't close it.

**Scope:** this applies to substantive units of work (a feature, fix, or discrete backlog item), not trivial intermediate steps. When in doubt about whether a completed change warrants a Linear update, err toward updating it.

If the Linear MCP tools are unavailable (server not connected in the current session), note that the Linear update is pending rather than silently skipping it.

## Knowledge base — fold new context material into the curated stores

When the user adds new material to `context/` (pasted application answers, cover letters, writing samples, notes) or shares durable new facts in conversation, **proactively fold it into the curated knowledge stores** — this does **not** happen automatically:

- **`context/background.md`** — the canonical career narrative (roles, dates, metrics, tense). Add any new concrete facts/metrics (e.g. a new adoption number, a project detail). Keep the career-timeline/tense rules intact.
- **`context/stories.md`** — reusable situation→action→result talking points and positioning. Enrich existing stories or add new ones for open-ended questions ("hardest thing", "describe a product", operating style).
- **`data/history.json`** — reusable Q&A. Add via `mcp__job-applier__save_answer` (keeps normalized dedupe/date). Scope honestly: `evergreen` (short stable facts), `company` (essays/tailored — use empty company for a generic essay so it surfaces but stays gated for adaptation, or the real company name to auto-reuse there), `conditional` (situation-dependent).

**What IS automatic (no fold-in needed):** `search_context` and the tailoring voice corpus (`get_cover_letter_examples`) both scan `context/` **live** on every call, so a newly-added file is immediately searchable for crafting answers and available as cover-letter voice. The fold-in above is specifically to keep the *curated* narrative (`background.md`/`stories.md`) and the *reusable-answer* store (`history.json`) current — those are hand-maintained, not regenerated.

**Ground truth, never fabricate.** Only fold in what the material or the user actually states; get role recency/tense right per `background.md` (M Science is current; Audare AI ended Nov 2025). Lightly fix typos/grammar when adapting the user's prose into a reusable answer; don't invent metrics or details.
