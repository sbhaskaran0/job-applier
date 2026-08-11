# Multi-user generalization — Linear backlog (pending push)

> **Status:** Linear MCP was not connected when this backlog was authored
> (2026-08-10). Create these as issues in team **Job Applier Task Management**
> the next time Linear is available, then replace this note with the `JOB-*`
> ids. Until then this file is the source of truth. Full architecture:
> the approved plan (multi-user profile system, hybrid cloud data plane).
>
> Product decisions baked in: hybrid deployment (GCP data plane, local
> execution, each user's own Claude subscription) · ~2–10 known users ·
> EEO self-ID local-only · git PII stop-tracking only · shared global
> watchlist · sequencing M1→M2→M3→M4.

## Epic M1 — Profile system: local abstraction (IN PROGRESS on `feat/profile-system-m1`)

| # | Story | Depends on | Status |
|---|---|---|---|
| M1.1 | **Profile resolution layer.** `src/profiles.py` (ActiveProfile; env → `applyer.local.json` → single-dir → legacy fallback), `src/config.py` lazy `__getattr__` path resolution, EEO merged from local-only `eeo.yaml`, atomic JSON writes, mtime-guarded resume.txt sync. AC: legacy checkout behaves identically; scratch profile via `JOB_APPLIER_PROFILE` isolates fully. | — | done (lane 0) |
| M1.2 | **Browser profile-awareness.** Screenshot/artifact defaults → profile `runtime/`; persistent Chromium context (`launch_persistent_context`, per-profile `user_data_dir`) for durable cookies + stable anti-bot fingerprint. AC: cookie survives a browser restart; screenshots land under the profile. Revert-safe: persistence can be rolled back alone. | M1.1 | in progress |
| M1.3 | **Server + store profile paths.** `data_api.py` resume checks/uploads → profile paths; `store.passes_baseline` computes seniority exclusion at query time from the caller's baseline (stored `seniority_flag` becomes vestigial). AC: uploads land in the profile; Director-title row fails baseline for a user excluding Director without consulting the stored flag. | M1.1 | in progress |
| M1.4 | **De-personalized skills + profile-paths tool.** Remove "Siddharth"/employment-tense facts from apply-to-job, apply-batch, tailor-application skills + CLAUDE.md (identity read from profile + `context/background.md`); Gmail OTP guard (connected inbox must match profile email); `get_profile_paths` MCP tool; prep dir → profile. AC: grep for personal facts across skills returns nothing. | M1.1 (tool only) | in progress |
| M1.5 | **Migration script + template.** `scripts/migrate_profile.py` (line-based EEO split preserving YAML comments, verified round-trip, moves all personal artifacts to `profiles/siddharth/`), tracked `profiles/_template/` skeleton, `.gitignore` for `profiles/*` + `applyer.local.json`. AC: fixture-tree test passes; template has zero personal values. | M1.1 | in progress |
| M1.6 | **Migration run + git untracking + verification + docs.** Run migration on real data; `git rm --cached` all personal files (user_profile.yaml, job_criteria.yaml, resume.*, context/, data/history.json, data/applications.json, data/token_usage.jsonl); golden-path verification (refresh, find-jobs, apply dry-run, resolve_fields diff, second-profile smoke, fresh-clone boot); README/USER_GUIDE/SESSION_HANDOFF + profile-system Mermaid; commit + PR. AC: `git ls-files` shows no personal file; behavior byte-identical. | M1.2–M1.5 | pending |

## Epic M2 — GCP data plane + auth

| # | Story | Depends on |
|---|---|---|
| M2.1 | **Postgres schema + store port.** Cloud SQL Postgres; `cloud/db.py` ports `src/store.py` SQL: `users`, `agent_tokens(token_hash,label,revoked_at,last_used_at)`, `profiles(facts JSONB)`, `criteria(JSONB)`, `history(UNIQUE(user_id,question_norm))`, `applications(UNIQUE(user_id,company_norm,title_norm))`, `boards`, `postings` (no `seniority_flag`), `posting_locations`, `location_observations`, `refresh_runs`, `candidate_boards` (per-user counts computed on demand). | M1 |
| M2.2 | **Auth: `/account` + PAT.** Google Identity Services sign-in on one static page; ID-token JWT verify + `ALLOWED_EMAILS` allowlist; mint/rotate/revoke SHA-256-hashed personal access tokens with `last_used_at`; single `current_user` FastAPI dependency for `/api/*`. | — |
| M2.3 | **Cloud API.** `cloud/api.py`: per-user profile/criteria/history/applications CRUD (server-side EEO-key rejection), postings query, watchlist registry + proposals, GCS signed URLs for resume/context (`users/{uid}/…`). Dockerfile + `docs/deploy.md` (plain gcloud, Secret Manager for DB password). | M2.1, M2.2 |
| M2.4 | **Shared refresh job.** Containerized `python -m src.refresh --backend=cloud` as a Cloud Run Job on Cloud Scheduler; removal sweep keyed to boards fetched OK this run (replaces watchlist-scoped sweep — kills the false-removal hazard). | M2.1 |
| M2.5 | **Local datastore + sync.** `src/datastore.py` (`Store` protocol: `LocalStore` = today/offline, `CloudStore` = httpx + PAT from `profiles/<id>/secrets/agent_token`, TTL cache, write-through, offline fallback + visible flag); `src/sync.py` mirrors GCS resume/context locally so `search_context`/voice corpus stay file-based; backend selection in `applyer.local.json`. `already_applied` moves out of the store into the caller (`list_postings_from_store(..., applied_keys)`). | M2.3 |
| M2.6 | **Corpus migration + isolation verification.** One-shot postings.db → Postgres load; two-account test: full user isolation, shared corpus visible to both, token revoke → 401 + re-link message, offline degradation, EEO-key absence in a captured apply session's outbound traffic. | M2.1–M2.5 |

## Epic M3 — Onboarding workflow

| # | Story | Depends on |
|---|---|---|
| M3.1 | **`/onboard-profile` skill.** Three modes run in the user's own local Claude session: `resume` (parse uploaded resume → `profile.yaml` facts + `context/background.md` career timeline), `letters` (pasted cover letters → `context/stories.md` + voice notes), `background` (free text → structured background). | M1 |
| M3.2 | **Onboarding wizard.** Extend `Onboarding.tsx` stepper: Welcome → Link account (PAT paste+verify) → Résumé (upload → skill parse → confirm facts) → Background & voice → Preferences (tag-driven titles/seniority/locations/remote/salary via existing MultiSelect/RangeSlider/Toggle; neutral taxonomy) → Requirements (work auth, sponsorship, relocation, notice, salary) → Connections (Claude Code + Gmail-identity check) → Done. | M2.2 (PAT step); M3.1 |
| M3.3 | **Completion flag + fallback parse.** `GET/PUT /api/onboarding` ↔ `profiles/<id>/onboarding.json` (mirrored to cloud); auto-open wizard when incomplete; deterministic server-side resume prefill (pypdf text + regex email/phone/LinkedIn) so the wizard is useful before any agent session. | M3.2 |
| M3.4 | **Neutral taxonomy + de-hardcoded defaults.** Shared title/metro taxonomy source; remove Siddharth's defaults from `JobCriteriaCard.tsx`, `PostingsPage.tsx` COMMON_METROS, `ApplyModal.tsx` crafting regex. (Shared with M4.) | — |

## Epic M4 — Per-user filtered shared postings

| # | Story | Depends on |
|---|---|---|
| M4.1 | **Server-side per-user filtering.** Cloud `/api/postings` applies the requester's baseline (`passes_baseline(row, user_baseline)`) + per-user `already_applied` join; response shape unchanged (client filter card remains as refinement). | M2.5 |
| M4.2 | **Filter explicitness UX.** "Filtered by your criteria — edit" affordance + hidden-by-filter count; per-user `is_new` via `last_seen_postings_at`. | M4.1 |
| M4.3 | **Board subscriptions + proposals.** Per-user hide/show toggle (default all-visible); watchlist proposal → admin approve flow. | M4.1 |
| M4.4 | **Per-user digest.** Rendered locally from the user-filtered cloud query. | M4.1 |

## Deferred / explicitly not built at ~10-user scale

Mac/Linux launcher scripts (until a non-Windows user onboards) · git-history PII purge (revisit before any public sharing) · server-side Playwright/agent execution · Firebase Auth/Firestore · OAuth device flow · Terraform · rate limiting/WAF · org/team model · GDPR export tooling · staging env · admin UI (allowlist = env var) · syncing tailored per-job artifacts across a user's devices (second PAT per device instead).
