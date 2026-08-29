"""WebSocket chat bridge: browser ⇄ a real Claude Code session in this repo.

Each WebSocket connection owns one ClaudeSDKClient (one conversation, so
follow-ups keep context). Messages the UI sends:
    {"type": "user", "text": "/find-jobs fintech product strategy"}
    {"type": "interrupt"}
Messages the UI receives:
    {"type": "ready"}                       session connected
    {"type": "agent_text", "text": ...}     an assistant text block
    {"type": "tool", "name", "detail"}      a tool call started (run-card step)
    {"type": "done", "ok": bool, "summary"} turn finished
    {"type": "error", "message": ...}
    {"type": "restarting"}                  session is dead; socket will close
                                            and the UI should let its
                                            auto-reconnect build a fresh one

The session runs with permission_mode="bypassPermissions" inside this repo —
the same trust level as running the skills from a terminal. Approval gates
remain conversational: the skills ask before submitting, and the agent's
questions arrive as agent_text for the user to answer in the chat.

Two structural rules, both learned from a real wedge (2026-08-11):

- **The WS read loop and the agent turn are separate tasks.** A single loop
  that runs the turn inline never reads the socket while streaming, so an
  {"type": "interrupt"} would only be seen after the turn already ended —
  i.e. never when it matters.
- **Transport failures are session-fatal.** When the SDK's stdout reader dies
  (e.g. one oversized JSON message), the ClaudeSDKClient never yields another
  message: later queries write to stdin fine but stream back nothing, the CLI
  child wedges on a full stdout pipe, and its MCP server keeps the persistent
  Chromium profile locked. So a turn that raises — or ends without a
  ResultMessage — closes the socket instead of pretending the client is still
  usable. The frontend auto-reconnects into a fresh session, and disconnect()
  kills the wedged CLI tree, releasing the browser-profile lock.
"""

import asyncio
import json
import shutil
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

from src import config, profiles

TOOL_PREFIX = "mcp__job-applier__"

# The SDK hard-fails the whole session when a single stdout JSON message
# exceeds this (default 1 MiB — a big form dump or JD in a tool result is
# enough). Raise it well past any realistic tool payload.
MAX_SDK_MESSAGE_BYTES = 32 * 1024 * 1024

# An interrupt is a control round-trip through the same reader task that
# streams messages — if it can't complete quickly, the session is wedged.
INTERRUPT_TIMEOUT_S = 15

# Starting two CLI processes at the same instant can crash one with an access
# violation (seen on Windows ARM64 with the SDK's emulated x64 bundled CLI) —
# serialize session startup and retry once.
_connect_lock = asyncio.Lock()


def find_cli() -> str | None:
    """Prefer a native CLI install over the SDK's bundled binary (which is
    x64 and runs under emulation on ARM64 Windows — flaky under concurrency).
    Returns None to let the SDK fall back to its bundled CLI."""
    if cli := shutil.which("claude"):
        return cli
    candidates: list[Path] = []
    # VS Code/Cursor extension native binaries (version-suffixed dirs)
    for ide_dir in (Path.home() / ".cursor" / "extensions",
                    Path.home() / ".vscode" / "extensions"):
        candidates += ide_dir.glob(
            "anthropic.claude-code-*/resources/native-binary/claude.exe")
    # Claude Desktop's managed claude-code install
    candidates += (Path.home() / "AppData" / "Local" / "Packages").glob(
        "Claude_*/LocalCache/Roaming/Claude/claude-code/*/claude.exe")
    if not candidates:
        return None
    # Highest version-ish path wins (lexicographic on the version dir name)
    return str(sorted(candidates, key=lambda p: p.parent.as_posix())[-1])
def _log_usage(result) -> None:
    """Append the SDK-reported usage/cost for one web-chat turn to the active
    profile's token_usage.jsonl (dev-loop cost metric). Tagged source=webchat:
    the repo's Stop hook (token_report.py) also fires for SDK-spawned sessions
    and upserts a per-session total keyed by the same session_id, so readers
    should prefer hook records and use these only for uncovered sessions.
    Best-effort — accounting must never break a chat turn."""
    try:
        rec = {
            "session_id": getattr(result, "session_id", None),
            "source": "webchat",
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "turns": 1,
            "usage": getattr(result, "usage", None),
            "cost_usd": getattr(result, "total_cost_usd", None),
        }
        out = profiles.active().data_dir / "token_usage.jsonl"
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


_DETAIL_KEYS = ("url", "label", "question", "company", "query", "skill", "path",
                "file_path", "command")


def _tool_step(name: str, tool_input: dict) -> dict:
    if name.startswith(TOOL_PREFIX):
        name = name[len(TOOL_PREFIX):]
    detail = ""
    for k in _DETAIL_KEYS:
        v = (tool_input or {}).get(k)
        if isinstance(v, str) and v.strip():
            detail = v if len(v) <= 90 else v[:87] + "…"
            break
    return {"type": "tool", "name": name, "detail": detail}


async def chat_session(ws: WebSocket) -> None:
    await ws.accept()
    try:
        from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                      ClaudeSDKClient, ResultMessage,
                                      TextBlock, ToolUseBlock)
    except ImportError:
        await ws.send_json({"type": "error", "message":
                            "claude-agent-sdk is not installed — "
                            "pip install claude-agent-sdk"})
        await ws.close()
        return

    options = ClaudeAgentOptions(
        cwd=str(config.BASE_DIR),
        permission_mode="bypassPermissions",
        # user+project: load ~/.claude auth/config AND this repo's .mcp.json,
        # .claude/skills, CLAUDE.md — the same surface a terminal session gets.
        setting_sources=["user", "project"],
        cli_path=find_cli(),
        max_buffer_size=MAX_SDK_MESSAGE_BYTES,
    )
    client = ClaudeSDKClient(options=options)

    send_lock = asyncio.Lock()  # turn task + WS loop both send

    async def send(payload: dict) -> None:
        async with send_lock:
            await ws.send_json(payload)

    async def die(message: str) -> None:
        """Session-fatal path: tell the UI, then close so its auto-reconnect
        replaces us (and `finally` below kills the CLI tree)."""
        with suppress(Exception):
            await send({"type": "error", "message": message})
            await send({"type": "restarting"})
        with suppress(Exception):
            await ws.close()

    async def run_turn(text: str) -> None:
        got_result = False
        try:
            await client.query(text)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            await send({"type": "agent_text",
                                        "text": block.text})
                        elif isinstance(block, ToolUseBlock):
                            await send(_tool_step(block.name, block.input))
                elif isinstance(message, ResultMessage):
                    got_result = True
                    _log_usage(message)
                    await send({
                        "type": "done",
                        "ok": not message.is_error,
                        "summary": (message.result or "")[:400],
                    })
        except Exception as e:
            # Anything the stream raises is transport-level (reader death,
            # CLI exit, unparseable message) — turn-level failures arrive as
            # a ResultMessage with is_error instead. Not recoverable in-place.
            await die(f"The agent session failed: {e}")
            return
        if not got_result:
            # The message stream ended without a result: the reader is dead
            # and every later turn would stream back nothing, forever.
            await die("The agent session ended unexpectedly.")

    turn: asyncio.Task | None = None
    try:
        async with _connect_lock:
            try:
                await client.connect()
            except Exception:
                await asyncio.sleep(2)  # transient spawn crash — one retry
                client = ClaudeSDKClient(options=options)
                try:
                    await client.connect()
                except Exception as e:
                    await ws.send_json({"type": "error", "message":
                                        f"Could not start a Claude Code "
                                        f"session: {e}"})
                    await ws.close()
                    return
        await send({"type": "ready"})
        while True:
            incoming = await ws.receive_json()
            kind = incoming.get("type")
            busy = turn is not None and not turn.done()
            if kind == "interrupt":
                if busy:
                    try:
                        await asyncio.wait_for(client.interrupt(),
                                               timeout=INTERRUPT_TIMEOUT_S)
                    except Exception:
                        turn.cancel()
                        await die("Could not interrupt the agent — the "
                                  "session is stuck.")
                continue
            if kind != "user":
                continue
            text = (incoming.get("text") or "").strip()
            if not text:
                continue
            if busy:
                await send({"type": "error", "message":
                            "The agent is still working — interrupt "
                            "it first or wait for it to finish."})
                continue
            turn = asyncio.create_task(run_turn(text))
    except WebSocketDisconnect:
        pass
    finally:
        if turn is not None and not turn.done():
            turn.cancel()
            with suppress(BaseException):
                await turn
        # Kills the CLI child tree even when it's wedged mid-write — this is
        # what releases the persistent Chromium profile for the next session.
        try:
            await asyncio.wait_for(client.disconnect(), timeout=10)
        except Exception:
            pass
