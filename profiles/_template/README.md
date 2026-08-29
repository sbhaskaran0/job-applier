# Profile template

1. Copy this directory to `profiles/<your-id>/` (everything under `profiles/` except `_template/` is gitignored — your data stays local).
2. Fill in `profile.yaml` and `criteria.yaml`; optionally uncomment entries in `eeo.yaml`; drop your `resume.pdf` / `resume.docx` / `resume.txt` into the profile root.
3. Select it: write `{"profile": "<your-id>"}` to `applyer.local.json` at the repo root (or set `JOB_APPLIER_PROFILE`).
