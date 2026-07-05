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
