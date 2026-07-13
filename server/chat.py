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

The session runs with permission_mode="bypassPermissions" inside this repo —
the same trust level as running the skills from a terminal. Approval gates
remain conversational: the skills ask before submitting, and the agent's
questions arrive as agent_text for the user to answer in the chat.
"""

import asyncio

from fastapi import WebSocket, WebSocketDisconnect

from src import config

TOOL_PREFIX = "mcp__job-applier__"
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
    )
    client = ClaudeSDKClient(options=options)
    busy = False
    try:
        await client.connect()
        await ws.send_json({"type": "ready"})
        while True:
            incoming = await ws.receive_json()
            kind = incoming.get("type")
            if kind == "interrupt":
                if busy:
                    await client.interrupt()
                continue
            if kind != "user":
                continue
            text = (incoming.get("text") or "").strip()
            if not text:
                continue
            if busy:
                await ws.send_json({"type": "error", "message":
                                    "The agent is still working — interrupt "
                                    "it first or wait for it to finish."})
                continue
            busy = True
            try:
                await client.query(text)
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock) and block.text.strip():
                                await ws.send_json({"type": "agent_text",
                                                    "text": block.text})
                            elif isinstance(block, ToolUseBlock):
                                await ws.send_json(_tool_step(block.name,
                                                              block.input))
                    elif isinstance(message, ResultMessage):
                        await ws.send_json({
                            "type": "done",
                            "ok": not message.is_error,
                            "summary": (message.result or "")[:400],
                        })
            except Exception as e:  # keep the socket alive on a failed turn
                await ws.send_json({"type": "error", "message": str(e)})
            finally:
                busy = False
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=10)
        except Exception:
            pass
