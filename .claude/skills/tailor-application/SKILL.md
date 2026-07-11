---
name: tailor-application
description: Generate a bespoke, tailored resume and cover letter for ONE job posting from the context knowledge base and the job text — on demand. Edits the user's resume.docx in place (reorder/re-emphasize/trim bullets, sharpen the summary) preserving its exact formatting, exports a PDF, and drafts a cover letter that matches the user's own writing voice from their past cover letters. Saves both under resumes/<job-key>/ for the apply flow to pick up. Pass the job posting URL (or pasted posting text) as the argument. Use this only when tailoring is explicitly asked for — the normal apply flow uses the default resume.
---

# Tailor a resume + cover letter for one job

You drive the `job-applier` MCP server to produce a **bespoke resume and cover
letter for a single posting**. You are the reasoner — the tools read the base
document, apply the edits *you* decide, export PDFs, and persist artifacts. This
is an **on-demand** flow (JOB-6): it does **not** run on every application. The
argument is a job URL (or pasted posting text).

Two artifacts, one shared job identity: everything is stored under
`resumes/<job-key>/`, keyed off the SAME `(company, role)` identity
`applications.json` uses — so pass a **consistent** `company` / `job_title` /
`url` to every tool call in this flow. Use the exact role title and company as
they appear on the posting.

## Workflow

1. **Read the job.** If given a URL, `get_posting(url)` (or `open_job` +
   `get_job_text()`) to get the full description. If given pasted text, use it.
   Extract what the role actually values: the domain (fintech / AI / infra /
   payments…), the must-have skills, the seniority, the top 3–5 themes the JD
   keeps returning to. This is the substance you tailor toward — never invent
   experience the user doesn't have to match it.

2. **Read the base resume.** Call `read_resume_template()`. It returns the base
   `resume.docx` as an indexed paragraph list (`index`, `text`, `style`,
   `is_bullet`). If it errors that no `resume.docx` exists, **stop and tell the
   user** to drop a `resume.docx` in the project root (the base template
   tailoring edits in place) — do the cover letter alone if they still want it.

3. **Plan the resume edits.** Decide which bullets to **reorder** so the most
   JD-relevant experience comes first, which to **re-emphasize** (rewrite to
   foreground the JD's language/metrics — using facts already in the resume),
   which weak/off-target bullets to **drop**, and how to **sharpen the summary**
   for this role. Keep it truthful and grounded in `background.md`
   (`search_context` for specifics) — get role recency/tense right (M Science is
   current; Audare AI ended Nov 2025). Don't pad, don't fabricate numbers.
   - Express edits as ops against the paragraph indexes from step 2:
     `{"op":"replace","index":N,"text":"..."}` to rewrite a paragraph (keeps its
     formatting), `{"op":"delete","index":N}` to remove one. **Reordering** =
     `replace` the bullet texts into the new order; **trimming** = `delete` the
     least-relevant. Preserve every factual anchor (titles, dates, metrics).

4. **Apply the resume tailoring.** Call
   `tailor_resume(company, job_title, url, edits)`. Check `edits_applied` /
   `edit_errors`; it saves `resumes/<slug>/resume.docx` and exports
   `resume.pdf`. If `pdf_exported` is false, the `.docx` is still saved and the
   `note` explains why (e.g. Word unavailable) — tell the user they can upload
   the `.docx` or Save-As-PDF manually.

5. **Gather the voice, then draft the cover letter.** Call
   `get_cover_letter_examples()` — the user's own past cover letters (primary),
   writing samples (secondary), and **`responses`**: their past first-person
   answers to application questions (product/behavioral/strategy prompts), which
   are voice **and** substance. **Match their voice**: sentence rhythm,
   structure, level of formality, how they open and close — not a generic LLM
   cover-letter register. The exemplars supply the *voice*; the JD supplies the
   *substance* (why this company, which of the user's experiences map to this
   role) — and the `responses` often already contain the exact stories/metrics to
   draw on. First person, specific, results-oriented, no clichés. Also ground
   claims in `background.md` / `search_context` (the substance channel).

6. **Show the user, then persist.** Present the drafted cover letter (and a short
   summary of the resume changes) for approval or edits — this is generative
   output, always gated. After they approve or edit, call
   `save_cover_letter(company, job_title, url, text)` (saves `cover_letter.txt`
   + `cover_letter.pdf`).

7. **Report.** Summarize: which bullets you reordered/re-emphasized/dropped, how
   you sharpened the summary, the cover-letter angle, and the saved paths
   (`resumes/<slug>/`). Note that the next `/apply-to-job` or `/apply-batch` for
   this posting will **automatically** pick up these artifacts via
   `get_job_artifacts` (default resume is used for every other job).

## Rules
- **On-demand only.** Never run this as part of a normal apply unless asked.
- **Truthful tailoring.** Reorder, re-emphasize, and trim what's already true;
  never invent roles, dates, metrics, or skills to match a JD.
- **Match the base resume's voice — don't leave AI tells.** Rewritten bullets must
  read like the ones you didn't touch. Mirror the original's syntax and cadence:
  verb-led clauses, comma-joined, `-ing` result tails ("…resulting in / increasing
  …by ~X%"), the same metric phrasing and level of formality. Do NOT introduce
  em-dashes, parenthetical asides/lists, quoted words, colons splicing mid-bullet,
  or buzzy coinages ("0-to-1", "messy, ambiguous problem") if the base doesn't use
  them. Prefer the lightest touch: reorder by swapping original bullet texts
  verbatim, edit as few bullets as possible, and leave already-relevant bullets
  exactly as written. The tailored resume should be indistinguishable in style
  from the user's own — a reader should not be able to tell which bullets changed.
- **Preserve formatting.** Edits go through the `.docx` template in place — never
  regenerate a resume from plain text.
- **Match the user's writing** for the cover letter; gate it for approval before
  saving.
- **One identity.** Use consistent `company` / `job_title` / `url` across all
  calls so the resume, cover letter, and the apply-time lookup share one folder.
