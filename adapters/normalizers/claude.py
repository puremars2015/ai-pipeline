"""claude -p --output-format stream-json 的事件翻譯。

實測 schema（Claude Code 2.0.49，tests/fixtures/claude.jsonl）：

  {"type":"system","subtype":"init","session_id":"...","model":"...","cwd":"...",
   "permissionMode":"...","tools":[...]}
  {"type":"assistant","message":{"role":"assistant","content":[
      {"type":"tool_use","id":"...","name":"Write","input":{...}}]}}
  {"type":"user","message":{"role":"user","content":[
      {"type":"tool_result","tool_use_id":"...","content":"..."}]}}
  {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
  {"type":"result","subtype":"success","result":"最終回覆","is_error":false,
   "total_cost_usd":0.03,"num_turns":3,"usage":{...},"modelUsage":{...}}

Claude 把訊息包在 message.content 的 block 陣列裡，所以一則原始事件可能
翻譯成多則正規化事件（例如同時有 text 和 tool_use）。
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

# 這些工具代表檔案被改動，額外發一則 file_edit 讓 UI 能標出動到哪些檔案
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def normalize(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        return [ev(STDOUT, raw)]
    if not isinstance(raw, dict):
        return []

    etype = raw.get("type") or ""

    if etype == "system":
        if raw.get("subtype") == "init":
            return [
                ev(
                    STATUS,
                    f"session 建立 (model={raw.get('model')})",
                    phase="session",
                    session_id=raw.get("session_id"),
                    model=raw.get("model"),
                    cwd=raw.get("cwd"),
                    permission_mode=raw.get("permissionMode"),
                )
            ]
        return [ev(STATUS, f"system:{raw.get('subtype')}", phase="system")]

    if etype == "assistant":
        return _blocks(raw, role="assistant")

    if etype == "user":
        return _blocks(raw, role="user")

    if etype == "result":
        return _result(raw)

    if etype == "stream_event":
        # --include-partial-messages 才會出現，v1 不開，忽略以免洗爆事件流
        return []

    return [ev(STDOUT, f"[claude:{etype}]")]


def _blocks(raw: dict, role: str) -> list[dict[str, Any]]:
    content = ((raw.get("message") or {}).get("content")) or []
    if isinstance(content, str):
        return [ev(MESSAGE, content)] if role == "assistant" else []

    out: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")

        if btype == "text":
            text = block.get("text") or ""
            if text.strip():
                out.append(ev(MESSAGE, text))

        elif btype == "thinking":
            thought = block.get("thinking") or ""
            if thought.strip():
                out.append(ev(REASONING, thought))

        elif btype == "tool_use":
            name = block.get("name") or "?"
            tool_input = block.get("input") or {}
            out.append(
                ev(
                    TOOL_CALL,
                    _describe(name, tool_input),
                    tool=name,
                    tool_use_id=block.get("id"),
                    input=tool_input,
                )
            )
            if name in _WRITE_TOOLS:
                path = tool_input.get("file_path") or tool_input.get("notebook_path")
                if path:
                    out.append(ev(FILE_EDIT, str(path), paths=[str(path)], tool=name))

        elif btype == "tool_result":
            body = block.get("content")
            if isinstance(body, list):
                body = "\n".join(
                    b.get("text", "") for b in body if isinstance(b, dict)
                )
            out.append(
                ev(
                    TOOL_RESULT,
                    str(body or ""),
                    tool_use_id=block.get("tool_use_id"),
                    is_error=bool(block.get("is_error")),
                )
            )

    return out


def _describe(name: str, tool_input: dict) -> str:
    """給工具呼叫一個一行的人類可讀描述，UI 時間軸用。"""
    for key in ("file_path", "notebook_path", "path", "pattern", "command", "url"):
        if key in tool_input:
            return f"{name}: {tool_input[key]}"
    return name


def _result(raw: dict) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    usage = raw.get("usage") or {}
    out.append(
        ev(
            USAGE,
            f"cost=${raw.get('total_cost_usd', 0):.4f} turns={raw.get('num_turns', 0)}",
            total_cost_usd=raw.get("total_cost_usd"),
            num_turns=raw.get("num_turns"),
            duration_ms=raw.get("duration_ms"),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
            model_usage=raw.get("modelUsage"),
        )
    )

    text = raw.get("result") or ""
    if raw.get("is_error") or raw.get("subtype") not in (None, "success"):
        out.append(ev(ERROR, str(text) or f"claude 回報 {raw.get('subtype')}"))
    else:
        # result.result 是權威的最終回覆（開 --json-schema 時就是那段 JSON）。
        # 不發成 MESSAGE，否則會和前面 assistant 的 text block 重複；
        # 放進 data 讓 runner 拿去當 last_message / 解析 structured。
        out[0]["data"]["result_text"] = str(text)
    return out
