"""opencode run --format json 的事件翻譯。

實測 schema（opencode 1.17.9，tests/fixtures/opencode.jsonl）：

  {"type":"step_start","sessionID":"ses_...","timestamp":N,
   "part":{"type":"step-start","id":"prt_...","messageID":"msg_...","snapshot":"..."}}
  {"type":"tool_use","sessionID":"...","part":{"tool":"write","callID":"...",
   "state":{"status":"completed","input":{"filePath":"...","content":"..."},
            "output":"...","title":"...","metadata":{...},
            "time":{"start":N,"end":N}}}}
  {"type":"text","part":{"text":"...","time":{...}}}
  {"type":"step_finish","part":{"reason":"stop","cost":0,
   "tokens":{"input":N,"output":N,"reasoning":N,"total":N,
             "cache":{"read":N,"write":N}}}}

sessionID 在每則事件的最上層，所以任何一則都能拿到續接用的 session id。
一次 run 會有多組 step_start/step_finish（每個 LLM turn 一組）。
"""

from __future__ import annotations

from typing import Any

from engine.events import (
    ERROR,
    FILE_EDIT,
    MESSAGE,
    REASONING,
    STATUS,
    STDOUT,
    TOOL_CALL,
    TOOL_RESULT,
    USAGE,
    ev,
)

# opencode 的工具名是小寫的
_WRITE_TOOLS = {"write", "edit", "patch", "multiedit"}


def normalize(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        return [ev(STDOUT, raw)]
    if not isinstance(raw, dict):
        return []

    etype = raw.get("type") or ""
    part = raw.get("part") or {}
    session_id = raw.get("sessionID")

    if etype == "step_start":
        return [ev(STATUS, "step 開始", phase="step_start", session_id=session_id)]

    if etype == "step_finish":
        tokens = part.get("tokens") or {}
        cache = tokens.get("cache") or {}
        return [
            ev(
                USAGE,
                f"tokens in={tokens.get('input', 0)} out={tokens.get('output', 0)}",
                session_id=session_id,
                reason=part.get("reason"),
                cost=part.get("cost"),
                input_tokens=tokens.get("input", 0),
                output_tokens=tokens.get("output", 0),
                reasoning_output_tokens=tokens.get("reasoning", 0),
                total_tokens=tokens.get("total", 0),
                cache_read_input_tokens=cache.get("read", 0),
                cache_creation_input_tokens=cache.get("write", 0),
            )
        ]

    if etype == "text":
        text = part.get("text") or ""
        if not text.strip():
            return []
        return [ev(MESSAGE, text, session_id=session_id)]

    if etype == "reasoning":
        text = part.get("text") or ""
        return [ev(REASONING, text, session_id=session_id)] if text.strip() else []

    if etype == "tool_use":
        return _tool_use(part, session_id)

    if etype in ("error", "session_error"):
        message = part.get("message") or raw.get("error") or ""
        return [ev(ERROR, str(message), session_id=session_id)]

    return [ev(STDOUT, f"[opencode:{etype}]", session_id=session_id)]


def _tool_use(part: dict, session_id: str | None) -> list[dict[str, Any]]:
    tool = part.get("tool") or "?"
    state = part.get("state") or {}
    status = state.get("status")
    tool_input = state.get("input") or {}

    out: list[dict[str, Any]] = [
        ev(
            TOOL_CALL,
            state.get("title") or _describe(tool, tool_input),
            tool=tool,
            tool_use_id=part.get("callID"),
            status=status,
            input=tool_input,
            session_id=session_id,
        )
    ]

    if status in ("completed", "error"):
        out.append(
            ev(
                TOOL_RESULT,
                str(state.get("output") or ""),
                tool=tool,
                tool_use_id=part.get("callID"),
                is_error=status == "error",
                session_id=session_id,
            )
        )
        if tool in _WRITE_TOOLS:
            path = (
                tool_input.get("filePath")
                or tool_input.get("path")
                or (state.get("metadata") or {}).get("filepath")
            )
            if path:
                out.append(ev(FILE_EDIT, str(path), paths=[str(path)], tool=tool))

    return out


def _describe(tool: str, tool_input: dict) -> str:
    for key in ("filePath", "path", "pattern", "command", "url"):
        if key in tool_input:
            return f"{tool}: {tool_input[key]}"
    return tool
