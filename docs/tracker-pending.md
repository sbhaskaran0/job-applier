# Tracker updates pending (taskman MCP unavailable in the 2026-10-01 session)

The taskman server answered `/health` 200, but the MCP connection failed at
session start (ECONNREFUSED), so these weren't filed. After restarting Claude
Code, file each one with `mcp__taskman__create_story` under a **hand-filed** epic
(`list_children(key="JOB", type="epic")`, not dev-loop's "Loop backlog").
Resolve existing issues **by title** first (`search_nodes`, `under: "JOB"`).
Then delete this file.

Plan: `~/.claude/plans/create-a-multi-step-glittery-thacker.md`.
Integration branch: `integrate/fixes-2026-10`.

| Story | Status at filing time | Notes |
|---|---|---|
| Phase 0: commit Workday M0 / wizard / storage-seam WIP as baseline; delete credential-bearing probe scripts | done | commits 37e814c..3f5e435 on feat/workday-m0-sourcing |
| A: Greenhouse cover-letter upload targets the cover slot + upload read-back | todo | `upload_resume(target=...)` |
| B: Cover-letter pipeline: written template, letterhead, lint, Opus-pinned answer-crafter agent, per-role tailoring in web app | todo | |
| C: Refresh reliability: Workday removal grace (schema v5), staleness banner + health file, wake-to-run scheduler | todo | |
| D: Workday M1: credentials + password masking, Apply/auth flow, multi-select fix, experience/education blocks, skills | todo | |
| Verify the 2026-09-16 Stripe IAM submission (cover letter may have replaced the resume) | todo | user action |
| Rotate the Workday password an agent invented on 2026-09-17; delete `profiles/siddharth/data/ats-accounts.md` | todo | user action |
