# Tracker updates pending (taskman MCP unavailable in the 2026-10-01 session)

The taskman server answered `/health` 200, but the MCP connection failed at
session start (ECONNREFUSED), so these weren't filed. After restarting Claude
Code, file each one with `mcp__taskman__create_story` under a **hand-filed** epic
(`list_children(key="JOB", type="epic")`, not dev-loop's "Loop backlog").
Resolve existing issues **by title** first (`search_nodes`, `under: "JOB"`).
File the done ones, close them with a comment citing the merge SHA, then delete
this file once nothing is left todo.

Plan: `~/.claude/plans/create-a-multi-step-glittery-thacker.md`.
Integration branch: `integrate/fixes-2026-10` (tip `573123e` as of 2026-10-03;
PR to `main` not yet opened).

Status as of 2026-10-03. "Merged" means merged into the integration branch and
verified by standalone suites and synthetic DOM fixtures; **no live verification
has been run for any of it**.

| Story | Status | Notes |
|---|---|---|
| Phase 0: commit Workday M0 / wizard / storage-seam WIP as baseline; delete credential-bearing probe scripts | done | commits 37e814c..3f5e435 on feat/workday-m0-sourcing |
| A: Greenhouse cover-letter upload targets the cover slot + upload read-back | merged, live check todo | merge `5e9b330`; `upload_resume(target=...)` |
| B: Cover-letter pipeline: written template, letterhead, lint, Opus-pinned answer-crafter agent, per-role tailoring in web app | merged | merge `11daf6a`; `lint_text`, `save_cover_letter(force)` |
| C: Refresh reliability: Workday removal grace (schema v5), staleness banner + health file, wake-to-run scheduler | merged | merge `312f5c3`; run `scripts/setup_refresh_task.ps1` on the target machine to register the new task |
| D1: Workday credentials + password masking, `fill_credential` | merged | merge `02098b5` |
| D2: Workday Apply / auth flow (`start_application`, `authenticate`, `open_verification_link`, `clear_auth_ledger`) | merged, live check todo | merge `1f687a2` |
| D3: Workday multi-select fix | todo | see `docs/handoff-workday-multiselect.md` |
| D4: experience / education profile blocks + `fill_repeating_section` + zip aliases | merged, live check todo | merges `2b5b69d`, `8a943fe` |
| D5: Workday flow in apply-to-job / apply-batch skills | merged | merge `0ea9286`, follow-up `573123e` |
| Workday live validation: real sign-in, My Experience, wizard to the gated submit | todo | needs the user's `credentials.yaml` |
| Open the PR `integrate/fixes-2026-10` to `main` | todo | |
| Verify the 2026-09-16 Stripe IAM submission (cover letter may have replaced the resume) | todo | user action |
| Rotate the Workday password an agent invented on 2026-09-17; delete the active profile's `data/ats-accounts.md` | todo | user action |
| Create `profiles/<id>/secrets/credentials.yaml`; fill profile `zip`, `experience:`, `education:` | todo | user action |
