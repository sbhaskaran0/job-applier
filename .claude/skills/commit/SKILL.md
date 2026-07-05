---
name: commit
description: Create a git commit for the current changes with the project's mandatory hygiene — ALWAYS update README.md, USER_GUIDE.md, and SESSION_HANDOFF.md to reflect the change; add/refresh a Mermaid diagram whenever a new process or workflow is introduced; then commit and notify the user with a summary. Use whenever you are about to commit work in this repo.
---

# Commit (with mandatory docs + diagram hygiene)

You are committing changes to the **job-applier** repo. A commit here is not
just `git commit` — it always carries the project's documentation and
diagramming hygiene so the three living docs never drift from the code. Follow
every step; do not skip the doc updates because a change "looks small."

## Workflow

1. **Review what changed.** `git status` and `git diff` (staged + unstaged).
   Identify exactly which files you touched and what behavior changed. If a
   concurrent session has unrelated modified files (e.g. `data/*.json`,
   `watchlist.yaml`), **do not sweep them in** — stage only the paths belonging
   to this change.

2. **Update the three living docs — ALWAYS, every commit:**
   - **[README.md](../../../README.md)** — the top-level overview + the
     answer-resolution Mermaid diagram. Update if the change touches
     architecture, the tool set, the field-answer/submission flow, or anything a
     first-time reader should see.
   - **[USER_GUIDE.md](../../../USER_GUIDE.md)** — the end-user manual (skills,
     tools, safety, known limits). Update the affected section(s) so behavior
     described matches behavior shipped.
   - **[SESSION_HANDOFF.md](../../../SESSION_HANDOFF.md)** — the context-restore
     doc. Bump the `Last updated` line, add/extend the current session's
     "What happened THIS session" entry, and reconcile OPEN ITEMS / gotchas.
     Convert relative dates to absolute.

   Even a pure bug fix warrants at least a SESSION_HANDOFF entry and a check of
   the other two. If, after genuinely checking, a given doc needs no change,
   say so explicitly in your notify summary (don't silently skip it).

3. **Add / refresh a Mermaid diagram when a new process or workflow is
   introduced** (or an existing one materially changes its control flow):
   - New skill, new multi-step pipeline, a new decision/branch in a flow (e.g. a
     submit-outcome classification, a queue/stage machine, a retry/gate path).
   - Put the diagram where a reader will find it — usually README.md (overview
     flows) or SESSION_HANDOFF.md (session-specific mechanics). Keep the existing
     README cascade diagram authoritative; extend or add rather than duplicate.
   - Prefer `flowchart TD`; label edges with the real condition. Keep it legible
     (a handful of nodes), and make sure it matches the code you just wrote.
   - A change that only tweaks values/copy with no new control flow does **not**
     need a diagram — don't invent one.

4. **Stage precisely.** `git add <the paths for this change>` — the code files,
   the new/edited skill, and the docs you updated. Re-check `git status` to
   confirm nothing unrelated is staged.

5. **Commit.** If on the default branch, create a topic branch first (the repo
   works on a feature branch). Write a clear message: a concise subject line,
   then a body explaining what changed and why, and reference the Linear issue
   (`JOB-*`) when there is one. In the **Bash** tool use a heredoc
   (`git commit -F - <<'EOF' … EOF`), NOT PowerShell `@'…'@`. End the message
   with the required trailer:

   ```
   Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
   ```

   Do not push unless the user asks. Never use `--no-verify` / skip hooks unless
   the user explicitly asks; if a hook fails, fix the cause.

6. **Notify the user at the end.** Always end with a short summary containing:
   the commit SHA + subject, the files committed, which of the three docs were
   updated (and which were checked-but-unchanged and why), whether a Mermaid
   diagram was added/updated, and anything intentionally left unstaged (e.g.
   another session's files) or still pending (push, MCP restart, live test).

## Rules
- The three doc updates are **mandatory hygiene**, not optional — the whole
  point of this skill is that docs never lag the code.
- Report faithfully: if a doc was left unchanged, say why; if tests weren't run
  or a live check is still pending, say so — don't imply more was verified than
  was.
- Keep diagrams truthful to the code; a wrong diagram is worse than none.
