#!/usr/bin/env python3
"""Log REAL Claude Code token usage for a job-applier run, per application.

The job-applier MCP server never sees model tokens (Claude is the reasoner,
the server only drives the browser + data). The one real source of token
counts is the Claude Code session transcript (JSONL), which records
`message.usage` per assistant turn. This script reads that transcript, sums
usage, attributes each turn to the posting being worked at the time (via the
`url`/`company`/`job_title` on open_job / snapshot_job / submit_application /
log_application tool calls), prices it at Opus 4.8 rates, and appends one JSON
line to data/token_usage.jsonl. It also prints a compact per-run summary.

Usage:
  - As a Claude Code Stop hook: receives {transcript_path, session_id} as JSON
    on stdin. Add via .claude/settings.json (see USER_GUIDE).
  - Manually:  python scripts/token_report.py <transcript.jsonl>
               python scripts/token_report.py --latest
Exits 0 even on error so it never blocks a session.
"""
import json
import os
import sys
import glob

# Opus 4.8 pricing, USD per 1M tokens. No long-context premium on the 1M tier.
# (input $5 / output $25; cache write ~1.25x input; cache read ~0.1x input.)
PRICE = {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50}

# Tool calls that identify which posting is being worked.
_JOB_TOOLS = ("open_job", "snapshot_job", "submit_application",
              "log_application", "tailor_resume", "read_form")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUT = os.path.join(_REPO, "data", "token_usage.jsonl")


def _empty():
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "turns": 0}


def _cost(t):
    return round(t["input"] / 1e6 * PRICE["input"]
                 + t["output"] / 1e6 * PRICE["output"]
                 + t["cache_write"] / 1e6 * PRICE["cache_write"]
                 + t["cache_read"] / 1e6 * PRICE["cache_read"], 4)


def _tokens(t):
    return t["input"] + t["output"] + t["cache_write"] + t["cache_read"]


def _url_tail(url):
    if not url:
        return ""
    # Greenhouse token=... or Ashby last uuid segment -> a short distinguisher.
    if "token=" in url:
        return url.split("token=")[-1].split("&")[0]
    parts = [p for p in url.rstrip("/").split("/") if p]
    for p in reversed(parts):
        if p not in ("application", "apply", "job_app", "embed"):
            return p[:12]
    return parts[-1][:12] if parts else ""


def parse(path):
    seen = set()      # dedupe by API message id
    cur = {"url": None, "company": None, "title": None}
    turns = []        # ordered: {add, key, label, had_job}

    def key():
        if cur["url"]:
            return cur["url"]
        if cur["company"]:
            return "co:" + cur["company"]
        return "setup/other"

    def label():
        if not cur["company"] and not cur["url"]:
            return "setup / discovery / ranking"
        co = cur["company"] or "?"
        detail = cur["title"] or _url_tail(cur["url"])
        return (co + " - " + detail).strip(" -")

    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            msg = rec.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            # Update current posting from any job tool call in this turn.
            had_job = False
            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name = b.get("name", "")
                    if not any(j in name for j in _JOB_TOOLS):
                        continue
                    had_job = True
                    inp = b.get("input") or {}
                    if inp.get("url"):
                        cur = {"url": inp["url"], "company": inp.get("company") or cur["company"],
                               "title": None}
                    if inp.get("company"):
                        cur["company"] = inp["company"]
                    if inp.get("job_title"):
                        cur["title"] = inp["job_title"]
            # Attribute usage (deduped by API message id).
            u = msg.get("usage") or {}
            if not u:
                continue
            mid = msg.get("id") or rec.get("uuid")
            if mid in seen:
                continue
            seen.add(mid)
            add = {
                "input": u.get("input_tokens", 0) or 0,
                "output": u.get("output_tokens", 0) or 0,
                "cache_write": u.get("cache_creation_input_tokens", 0) or 0,
                "cache_read": u.get("cache_read_input_tokens", 0) or 0,
            }
            turns.append({"add": add, "key": key(), "label": label(), "had_job": had_job})

    # Everything after the final job-identifying tool call is wrap-up/reporting,
    # not the last application (avoids dumping the audit + reporting tail on it).
    last_job = max((i for i, t in enumerate(turns) if t["had_job"]), default=-1)

    run = _empty()
    per = {}
    labels = {}
    for i, t in enumerate(turns):
        k, lbl = t["key"], t["label"]
        if i > last_job:
            k, lbl = "wrap-up", "wrap-up / audit / reporting"
        per.setdefault(k, _empty())
        labels[k] = lbl
        for f in ("input", "output", "cache_write", "cache_read"):
            run[f] += t["add"][f]
            per[k][f] += t["add"][f]
        run["turns"] += 1
        per[k]["turns"] += 1
    return run, per, labels


def main():
    # Resolve the transcript path from hook stdin, an arg, or --latest.
    path = None
    session_id = None
    args = sys.argv[1:]
    if args and args[0] == "--latest":
        proj = os.path.join(os.path.expanduser("~"), ".claude", "projects",
                            "c--Users-siddh-Job-Applier")
        cands = sorted(glob.glob(os.path.join(proj, "*.jsonl")),
                       key=os.path.getmtime, reverse=True)
        path = cands[0] if cands else None
    elif args:
        path = args[0]
    else:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            path = payload.get("transcript_path")
            session_id = payload.get("session_id")
        except Exception:
            path = None
    if not path or not os.path.exists(path):
        return 0

    run, per, labels = parse(path)
    if run["turns"] == 0:
        return 0
    run_cost = _cost(run)

    # Per-application breakdown, biggest cost first.
    apps = sorted(per.items(), key=lambda kv: _cost(kv[1]), reverse=True)
    breakdown = [{
        "label": labels.get(k, k),
        "turns": t["turns"],
        "total_tokens": _tokens(t),
        "cost_usd": _cost(t),
    } for k, t in apps]

    rec = {
        "session_id": session_id or os.path.splitext(os.path.basename(path))[0],
        "transcript": os.path.basename(path),
        "model": "claude-opus-4-8",
        "turns": run["turns"],
        "tokens": {**{k: run[k] for k in ("input", "output", "cache_write", "cache_read")},
                   "total": _tokens(run)},
        "cost_usd": run_cost,
        "per_application": breakdown,
    }
    # Upsert one line per session (Stop fires each turn-end; keep the latest
    # cumulative total rather than appending a snapshot every time).
    try:
        os.makedirs(os.path.dirname(_OUT), exist_ok=True)
        kept = []
        if os.path.exists(_OUT):
            with open(_OUT, "r", encoding="utf-8") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        prev = json.loads(ln)
                    except Exception:
                        continue
                    if prev.get("session_id") != rec["session_id"]:
                        kept.append(prev)
        kept.append(rec)
        with open(_OUT, "w", encoding="utf-8") as fh:
            for r in kept:
                fh.write(json.dumps(r) + "\n")
    except Exception:
        pass

    # Compact human summary (ASCII only) to stderr.
    tk = run
    sys.stderr.write(
        "[token_report] {tot:,} tokens over {turns} turns "
        "(in {i:,} / out {o:,} / cache-write {cw:,} / cache-read {cr:,}) "
        "= ${cost:.2f} @ Opus 4.8 -> data/token_usage.jsonl\n".format(
            tot=_tokens(tk), turns=tk["turns"], i=tk["input"], o=tk["output"],
            cw=tk["cache_write"], cr=tk["cache_read"], cost=run_cost))
    for a in breakdown[:8]:
        sys.stderr.write("  ${c:>6.2f}  {tks:>8,} tok  {lbl}\n".format(
            c=a["cost_usd"], tks=a["total_tokens"], lbl=a["label"][:52]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never block the session
        sys.stderr.write("[token_report] skipped: {}\n".format(e))
        sys.exit(0)
