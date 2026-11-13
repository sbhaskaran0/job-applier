"""TaskmanBackend -- a `Repository` that dual-writes to taskman (a local,
generic typed-property-graph service: one `nodes` table, a JSONB `props` bag,
REST under `/api/v1`) alongside the file backend.

Phase T3. Files remain the master; taskman is a MIRROR, not a replacement:

- **Write order is file-first, taskman-second.** `save_history` /
  `save_applications` always write the file (via `FileBackend`) before
  attempting anything against taskman, and the file write's success is all
  the caller ever depends on.
- **Reads never touch taskman.** `load_history` / `load_applications`
  delegate straight to `FileBackend`. `src/data.py`'s `search_history` scores
  question-against-question client-side (SequenceMatcher blended with token
  overlap); taskman's `websearch_to_tsquery` ranking is a different function
  entirely, and swapping it in would silently change which past answers
  auto-fill a form. This backend does not add a search method -- it hands
  back the exact same plain records `FileBackend` would.
- **taskman has no export or backup path of any kind** (nothing under
  `taskman/api/routers/` writes a dump anywhere). The file mirror this class
  keeps is therefore the *only* backup taskman's data has, and stays worth
  keeping even after some future cutover -- this dual-write is not a
  migration shim to be deleted once taskman "works".
- **Offline is a requirement, not a nicety.** Any taskman error --
  connection refused, timeout, 401, 5xx, or a malformed/unexpected response
  -- is caught here, logged, and never propagated. Logging a verified submit
  must never fail because a tracker is unreachable; `save_applications` is
  exactly as reliable with this backend as with `FileBackend` alone.
- **Single-owner convention, not real scoping.** taskman's REST API has no
  per-user scoping: every API key can read and write the entire graph, and
  there is no endpoint to create a second workspace. This backend does not
  pretend otherwise -- it stamps the active profile's id as an explicit
  `owner` prop on every node it writes, so the data is already shaped for
  real per-owner scoping later, but nothing here enforces or filters on it.
  Do not treat the `owner` prop as access control.
- **EEO carve-out.** Voluntary self-identification data (gender, race,
  veteran, disability status) never reaches `history.json` or
  `applications.json` in the first place -- `src/data.py`'s
  `capture_submission` filters it out before either file is written. This
  module mirrors whatever those files already contain and adds no path that
  could carry EEO data to taskman.
- **Mirroring granularity.** `save_history` / `save_applications` are always
  handed the FULL current list (files are whole-file rewrites), so this
  backend diffs the incoming list against the last on-disk snapshot and
  mirrors only records that are new since then, each as one taskman node
  create. A record mutated in place (e.g. an application's `status`, or an
  outcome recorded later) is therefore not patched onto its earlier mirrored
  node -- it shows up as a second, divergent one. That is accepted drift for
  a mirror: divergence is logged, never silently reconciled, and the file
  stays authoritative regardless of what taskman holds.

Endpoints used (see `taskman/api/routers/nodes.py`, `taskman/schemas.py` in
the taskman checkout): a single `POST /api/v1/nodes` per new record, body
shaped like `NodeCreate` (`type`, `title`, `description`, `status`, `props`).
Nothing here reads from taskman, lists nodes, or requires the registry types
(`job_application`, `qa_answer`) to already exist -- if they don't, taskman
answers 422 and that create is logged and dropped like any other failure.
"""

import logging
import os
import re

import httpx

from .. import profiles
from .file_backend import FileBackend

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://localhost:8000"
_TIMEOUT = 3.0  # seconds -- an unreachable tracker must never stall an apply run

_warned_no_key = False  # module-level latch: log the "no key" notice once


def _base_url() -> str:
    """Re-read per call, never cached at import time (the env can change
    between calls, e.g. in tests)."""
    return os.environ.get("TASKMAN_URL", _DEFAULT_URL).rstrip("/")


def _api_key() -> str | None:
    return os.environ.get("TASKMAN_API_KEY") or None


def _owner() -> str:
    """The single-owner convention (see module docstring): re-resolved every
    call so a profile switch is observed immediately, matching every other
    per-call profile lookup in this package."""
    return profiles.active().profile_id


def _normalize(text: str) -> str:
    """Light local normalization for the mirror-side dedupe prop. Deliberately
    NOT imported from src/data.py -- that module imports src/config.py, which
    imports this package, so importing it back here would be a cycle. Needn't
    be byte-identical to data.py's matching logic: this key is carried as a
    `props` field for a future taskman-side lookup, not used for any matching
    in this phase (requirement: search_history's scoring is unchanged)."""
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def _normalize_url(url: str) -> str:
    u = (url or "").strip().lower()
    u = u.split("#", 1)[0].split("?", 1)[0]
    return u.rstrip("/")


def _dedupe_key(company: str, job_title: str, url: str) -> str:
    c, t = _normalize(company), _normalize(job_title)
    if c and t:
        return f"{c}|{t}"
    return f"{c}|{t}|{_normalize_url(url)}"


class TaskmanBackend:
    """Repository that dual-writes: `FileBackend` is always the master; a
    best-effort mirror to taskman follows every save. See the module
    docstring for the write/read split and the offline guarantee."""

    def __init__(self) -> None:
        self._files = FileBackend()

    # -- reads: always the file backend, never taskman -------------------- #
    def load_history(self) -> list:
        return self._files.load_history()

    def load_applications(self) -> list:
        return self._files.load_applications()

    # -- writes: file first (must succeed), then a best-effort mirror ----- #
    def save_history(self, history: list) -> None:
        previous = self._files.load_history()
        self._files.save_history(history)
        self._mirror_new(previous, history, self._qa_answer_payload)

    def save_applications(self, applications: list) -> None:
        previous = self._files.load_applications()
        self._files.save_applications(applications)
        self._mirror_new(previous, applications, self._job_application_payload)

    # -- mirroring ---------------------------------------------------------#
    def _mirror_new(self, previous: list, current: list, to_payload) -> None:
        """Push a taskman create for every record in `current` that wasn't in
        `previous`. Never lets a bad record, a bug in `to_payload`, or a
        taskman failure escape -- this is a mirror, not a dependency."""
        try:
            for record in current:
                if record in previous:
                    continue
                payload = to_payload(record)
                if payload is not None:
                    self._create_node(payload)
        except Exception as exc:  # belt-and-suspenders: the mirror must never
            # take a save_* call down with it.
            logger.warning("taskman mirror aborted unexpectedly: %s", exc)

    def _qa_answer_payload(self, record: dict) -> dict | None:
        question = (record.get("question") or "").strip()
        if not question:
            return None
        scope = record.get("scope") or "evergreen"
        props = {
            "scope": scope,
            "company": record.get("company", ""),
            "date": record.get("date", ""),
            "owner": _owner(),
        }
        if record.get("alternates"):
            props["alternates"] = record["alternates"]
        return {
            "type": "qa_answer",
            "title": question,
            "description": record.get("answer", ""),
            "status": scope,
            "props": props,
        }

    def _job_application_payload(self, record: dict) -> dict:
        company = record.get("company", "")
        job_title = record.get("job_title", "")
        url = record.get("url", "")
        props = {
            "url": url,
            "company": company,
            "job_title": job_title,
            "date": record.get("date", ""),
            "outcome": record.get("outcome", "none"),
            "outcome_date": record.get("outcome_date", ""),
            "fields": record.get("fields", []),
            "dedupe_key": _dedupe_key(company, job_title, url),
            "owner": _owner(),
        }
        return {
            "type": "job_application",
            "title": f"{company} — {job_title}",
            "status": record.get("status", "submitted"),
            "props": props,
        }

    def _create_node(self, payload: dict) -> None:
        """POST one node to taskman. Any failure -- no key, connection
        refused, timeout, 401/422/5xx, or a response body that doesn't even
        parse as JSON -- is caught and logged here; the caller never sees it."""
        global _warned_no_key
        key = _api_key()
        if not key:
            if not _warned_no_key:
                logger.warning(
                    "TASKMAN_API_KEY not set; taskman mirror disabled (file-only)")
                _warned_no_key = True
            return
        try:
            resp = httpx.post(
                f"{_base_url()}/api/v1/nodes",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
                timeout=_TIMEOUT,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "taskman mirror rejected %s %r: %s %s",
                    payload.get("type"), payload.get("title"),
                    resp.status_code, resp.text[:300])
                return
            resp.json()  # confirm the success body actually parses
        except httpx.HTTPError as exc:
            logger.warning("taskman unreachable, mirror skipped: %s", exc)
        except ValueError as exc:  # malformed JSON body on an ostensible 2xx
            logger.warning("taskman returned an unparseable response: %s", exc)
        except Exception as exc:  # never let the mirror take down a save
            logger.warning("taskman mirror unexpected error: %s", exc)
