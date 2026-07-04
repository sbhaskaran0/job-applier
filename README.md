# Job Applier

An AI job-application agent that runs inside **Claude Code**. It finds high-quality
product/strategy roles across a curated company watchlist and fills out their
application forms on any ATS (Greenhouse, Lever, Ashby, Workday…) — intelligently,
and **never submitting without your say-so**.

Claude Code is the reasoner; a local **MCP server** gives it a real browser
(Playwright), your profile/history/knowledge base, and live jobs from company
career boards. **No LLM API key or cost** in the core flow.

## Quick start

```bash
pip install -r requirements.txt
playwright install chromium
```

Open this project in Claude Code and reload it (loads `.mcp.json`), then:

- `/find-jobs fintech product strategy` — search your watchlist, ranked & filtered.
- `/apply-to-job <url>` — open an application and fill it from your data.

Edit `user_profile.yaml`, `job_criteria.yaml`, `watchlist.yaml`, `resume.txt`
(+ optional `resume.pdf`), and `context/` to make it yours.

> **⚠️ EEO / self-identification data:** `user_profile.yaml` may contain
> voluntary EEO self-identification values (gender, race/ethnicity,
> Hispanic/Latino status, veteran status, disability status), each marked with
> `eeo: true`. This is sensitive demographic data: it lives in plain text in
> this repo, and when present the agent will auto-answer the corresponding
> *voluntary* self-ID sections on applications. Providing it is always
> optional — delete the values to have those sections left blank instead. EEO
> answers are never written to the answer history or the application log.

**Full setup and usage: [USER_GUIDE.md](USER_GUIDE.md).**
