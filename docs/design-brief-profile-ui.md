# Design brief — Applyer profile-system UI (pass to Claude design)

You are designing new screens for **Applyer**, a desktop web app (React SPA served locally at localhost:8765) that wraps an AI job-application agent. The app just gained a **multi-user profile system** on the backend — all personal data (facts, resume, career knowledge base, answer history, application log, EEO self-identification) now lives in per-user profiles — but there is **no UI for it yet**. Design that UI.

## Existing design system — match it exactly, do not invent a new one

- **Themes:** warm **espresso dark** (default) and warm **paper light**. Every color is a semantic CSS token; use ONLY these token names in specs/mockups (values exist for both themes): `--bg-app`, `--bg-sidebar`, `--bg-card`, `--bg-rail`, `--chip`, `--row-selected`, `--divider`, `--border-1..4`, `--ink` (primary text), `--text-2..5` (descending emphasis), `--clay` / `--clay-text` / `--clay-hover` / `--on-clay` (primary accent, terracotta — buttons, active states), `--sage` / `--sage-text` / `--sage-soft` (success/positive), `--amber` / `--amber-text` / `--amber-soft` / `--amber-header` (warning/attention), `--purple` / `--purple-text` / `--purple-soft` (informational/special), `--accent-soft`, `--bar-track`.
- **Type:** Newsreader (serif — page titles, display numbers) + Hanken Grotesk (sans — everything else). Small-caps micro-labels for section headers, generous letter-spacing.
- **Existing component vocabulary to reuse:** left sidebar nav with avatar block at the bottom; content cards (`--bg-card`, 12px radius, 1px `--border-1`); chip/tag pills; **MultiSelect** (chip list + typeahead + free entry); **RangeSlider** (dual thumb); **Toggle**; full-screen modal wizard with a top stepper (numbered dots + labels, Back/Continue buttons) — this is the existing 5-step onboarding shell you are extending; toast confirmations; small stat/completeness meters.
- Layout: single-window desktop app feel, max-width content column, no browser-y chrome. Navigation is state-switched (no URLs) across 5 pages: Jobs (chat), Postings, Applications, Profile, Connections.

## Product context (for correct copy + hierarchy)

- One machine can hold several profiles (e.g. two family members sharing a PC); exactly one is **active** at a time and every page reflects it. Switching profiles is rare but must be obvious and safe.
- A profile = facts (name, email, phone, location, links, work authorization…), criteria (job search filters), resume file, a "knowledge base" of career documents, answer history, application log.
- **EEO self-identification** (gender, race/ethnicity, Hispanic/Latino, veteran, disability) is special: voluntary, stored in a **separate local-only file that never leaves the device** — never synced to the cloud, never shown in history. The UI must communicate this privacy boundary clearly and calmly (not scary), and make opting out trivial.
- A future cloud account links via a **personal access token (PAT)**: user signs in with Google on a small hosted page, copies a token, pastes it into the app once per device. Cloud sync is optional — "local only" is a fully supported choice.
- Heavy AI extraction (resume → facts, pasted cover letters → background stories) runs in a local agent session and takes seconds-to-minutes; design honest in-progress states, not fake spinners.

## Screens to design

### 1. Profile switcher (sidebar)
The sidebar avatar block becomes profile-aware: active profile name + monogram; clicking opens a compact menu — list of profiles on this machine (monogram, name, "active" marker), "New profile…", "Manage profile". Include a confirm step on switch (the whole app re-scopes). Empty state: no profile yet → single "Set up your profile" CTA.

### 2. New-profile onboarding wizard (the core deliverable)
Extend the existing stepper modal to these steps. Every step needs: default, in-progress, error, and skip-allowed states where noted.

1. **Welcome** — what Applyer does, what a profile is, 3 quiet value cards.
2. **Link account** *(skippable — "Keep everything on this device")* — paste-a-token field with verify button; states: unverified / verifying / linked (shows Google account email) / invalid token. Explain in one sentence why linking exists (sync + shared job feed).
3. **Résumé** — drag-drop (PDF/DOCX/TXT). After upload: instant lightweight prefill (email/phone found in the file), then an "extracting with your local agent…" progress state, then a **review grid of extracted facts** (each field editable, confidence-neutral, user confirms before save). Skippable.
4. **Background & voice** — three parallel input modes on one screen: (a) paste past cover letters / application answers (multi-entry), (b) free-text "tell us about a role or project" entries, (c) file drop for extra docs. Each feeds the knowledge base; show a running list of what's been added. Extraction state as in step 3. Skippable.
5. **Preferences (tag-driven)** — title chips (typeahead over a generic taxonomy + free entry), seniority include/exclude chips, locations MultiSelect + remote Toggle, salary floor RangeSlider. This writes the job-search criteria.
6. **Requirements** — the explicit hard-filter facts forms will ask: work authorization (select), needs sponsorship (toggle), willing to relocate (toggle), notice period, desired salary. Visually distinguish "hard requirements" (gate the job feed) from step-5 "preferences" (rank it).
7. **Connections** — status cards: local agent (Claude Code) detected / not; Gmail connector — **must match the profile email**: success state and a clear amber mismatch warning ("verification codes will be read from X, but you're applying as Y").
8. **Done** — summary of what was set up, what was skipped (with "finish later" links), completeness meter, primary CTA into the app.

Auto-open behavior: wizard opens on first load of an incomplete profile; must be dismissible and resumable (steps remember state).

### 3. Profile page additions
- Active-profile header (monogram, name, profile id, completeness meter — meter exists today).
- **EEO card**: two states — *not provided* (explain voluntary + local-only, "Add self-identification" affordance) and *provided* (shows only WHICH fields are set, never values; "stored only on this device, never uploaded" badge — suggest `--purple-soft` informational treatment; edit + "remove all" affordances).
- **Cloud account card**: local-only state (CTA to link) vs linked state (Google email, device token list: label / created / last-used / revoke per row, offline indicator when the cloud is unreachable).

### 4. Postings page filter banner (small)
A one-line affordance above the postings list: "Showing roles matching **your criteria** — N hidden by your filters · Edit" (links to the criteria editor). Makes per-user filtering explicit and editable.

## Deliverable format

- High-fidelity mockups of: the full wizard (all 8 steps, key states), profile switcher menu (both states), Profile page with the three new cards (both EEO states), and the postings banner — in **dark theme primarily**, plus 2–3 light-theme spot checks.
- Implementation-ready: name the tokens used per surface, spacing in px, and which existing components (MultiSelect / RangeSlider / Toggle / stepper shell / chip / card) each element reuses vs. what's genuinely new. Output as annotated HTML/CSS (or React TSX) using the token variable names verbatim — it will be translated directly into the existing codebase.
- Real content in mocks (realistic names, roles, cities — not lorem ipsum); calm, quietly confident copy; no exclamation points; privacy explanations in plain sentences, not legal tone.
