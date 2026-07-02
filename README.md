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

**Full setup and usage: [USER_GUIDE.md](USER_GUIDE.md).**
