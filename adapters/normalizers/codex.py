"""codex exec --json 的事件翻譯。

實測 schema（codex-cli 0.136.0，tests/fixtures/codex.jsonl）：

  {"type":"thread.started","thread_id":"019fef01-..."}
  {"type":"turn.started"}
  {"type":"item.completed","item":{"id":"...","type":"agent_message","text":"..."}}
  {"type":"item.started","item":{"type":"file_change","status":"in_progress",
                                 "changes":[{"path":"...","kind":"add"}]}}
  {"type":"turn.completed","usage":{"input_tokens":N,"cached_input_tokens":N,
                                    "output_tokens":N,"reasoning_output_tokens":N}}
  {"type":"error","message":"..."}
  {"type":"turn.failed","error":{"message":"..."}}

注意：codex 會把診斷訊息以純文字寫到 stdout，和 JSONL 混在一起，例如
  Reading prompt from stdin...
  2026-... ERROR codex_models_manager::cache: failed to load models cache: ...
所以非 JSON 的行必須容忍（runner 會把它們當 str 丟進來）。
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


def normalize(raw: Any) -> list[dict[str, Any]]:
    # 非 JSON 行：codex 的診斷輸出
    if isinstance(raw, str):
        return [ev(STDOUT, raw)]
    if not isinstance(raw, dict):
        return []

    etype = raw.get("type") or ""

    if etype == "thread.started":
        # thread_id 就是續接用的 session id（codex exec resume <id>）
        return [
            ev(STATUS, "session 建立", phase="session", session_id=raw.get("thread_id"))
        ]

    if etype == "turn.started":
        return [ev(STATUS, "turn 開始", phase="turn_start")]

    if etype == "turn.completed":
        usage = raw.get("usage") or {}
        total_in = usage.get("input_tokens", 0)
        total_out = usage.get("output_tokens", 0)
        return [
            ev(
                USAGE,
                f"tokens in={total_in} out={total_out}",
                input_tokens=total_in,
                output_tokens=total_out,
                cached_input_tokens=usage.get("cached_input_tokens", 0),
                reasoning_output_tokens=usage.get("reasoning_output_tokens", 0),
            )
        ]

    if etype in ("error", "turn.failed"):
        message = raw.get("message") or (raw.get("error") or {}).get("message") or ""
        return [ev(ERROR, str(message))]

    if etype in ("item.started", "item.completed", "item.updated"):
        return _item(raw, etype)

    # 未知事件型別不吞掉，留成 stdout 讓使用者看得到
    return [ev(STDOUT, f"[codex:{etype}]")]


def _item(raw: dict, etype: str) -> list[dict[str, Any]]:
    item = raw.get("item") or {}
    itype = item.get("type") or ""
    completed = etype == "item.completed"

    if itype == "agent_message":
        # 只在 completed 時發出，避免同一段文字重複兩次
        if not completed:
            return []
        return [ev(MESSAGE, item.get("text") or "")]

    if itype == "reasoning":
        if not completed:
            return []
        text = item.get("text") or item.get("summary") or ""
        return [ev(REASONING, str(text))]

    if itype == "file_change":
        changes = item.get("changes") or []
        paths = [c.get("path", "") for c in changes if isinstance(c, dict)]
        if not completed:
            return []
        return [
            ev(
                FILE_EDIT,
                ", ".join(paths),
                status=item.get("status"),
                changes=changes,
                paths=paths,
            )
        ]

    if itype == "command_execution":
        command = item.get("command") or ""
        if not completed:
            return [ev(TOOL_CALL, str(command), tool="shell", status="in_progress")]
        return [
            ev(
                TOOL_RESULT,
                str(item.get("aggregated_output") or item.get("output") or ""),
                tool="shell",
                command=command,
                exit_code=item.get("exit_code"),
                status=item.get("status"),
            )
        ]

    if itype == "todo_list":
        if not completed:
            return []
        return [ev(STATUS, "todo 更新", phase="todo", items=item.get("items"))]

    if not completed:
        return []
    return [ev(STDOUT, f"[codex:item:{itype}]", item=item)]
