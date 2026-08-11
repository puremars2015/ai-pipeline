"""pi --mode json 的事件翻譯。

來源：pi-coding-agent 0.84.1 的 docs/json.md（權威的 TypeScript 定義）
加上實機探測到的 session 標頭。

實機確認（tests/fixtures/pi.jsonl 第一行就是實跑抓到的）：

  {"type":"session","version":3,"id":"019ff17b-…","timestamp":"…","cwd":"/path"}

之後依 docs/json.md：

  {"type":"agent_start"} / {"type":"turn_start"}
  {"type":"message_start","message":{…}}
  {"type":"message_update","assistantMessageEvent":{"type":"text_delta",…}}   ← 只有增量
  {"type":"message_end","message":{…}}                                        ← 權威版本
  {"type":"tool_execution_start","toolCallId":…,"toolName":…,"args":{…}}
  {"type":"tool_execution_end","toolCallId":…,"toolName":…,"result":…,"isError":…}
  {"type":"turn_end","message":{…},"toolResults":[…]}
  {"type":"agent_end","messages":[…]}

訊息型別（node_modules/@earendil-works/pi-ai/dist/types.d.ts）：

  AssistantMessage  role:"assistant"  content:(TextContent|ThinkingContent|ToolCall)[]
                    usage:Usage  model  provider  stopReason  errorMessage?
  TextContent       {type:"text", text}
  ThinkingContent   {type:"thinking", thinking}
  ToolCall          {type:"toolCall", id, name, arguments}
  ToolResultMessage role:"toolResult"  toolCallId  toolName  content[]  isError
  Usage             {input, output, cacheRead, cacheWrite, reasoning?, totalTokens,
                     cost:{input,output,cacheRead,cacheWrite,total}}

兩個實測到的坑：
- pi 會讀 stdin，即使加了 -p。stdin 沒關掉就會無限等待（實測卡超過兩分鐘）。
  executor 對 prompt_delivery=argv 的 adapter 一律關 stdin，所以沒事。
- 憑證未設定時錯誤只出現在 stderr，JSON 串流裡只有 session 標頭就結束，exit 1。
  所以節點失敗判定不能只看事件流，要看 exit code（runner 本來就是這樣做）。
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

# pi 的內建工具（dist 內 name: "…" 抓出來的）：read write edit bash grep
_WRITE_TOOLS = {"write", "edit", "multiedit", "patch"}
# write 的參數是 absolutePath / path，edit 也用 path
_PATH_KEYS = ("absolutePath", "path", "file_path", "filePath")


def normalize(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        return [ev(STDOUT, raw)]
    if not isinstance(raw, dict):
        return []

    etype = raw.get("type") or ""

    if etype == "session":
        return [
            ev(
                STATUS,
                f"session 建立 (cwd={raw.get('cwd')})",
                phase="session",
                session_id=raw.get("id"),
                cwd=raw.get("cwd"),
                session_version=raw.get("version"),
            )
        ]

    if etype == "agent_start":
        return [ev(STATUS, "agent 開始", phase="agent_start")]
    if etype == "turn_start":
        return [ev(STATUS, "turn 開始", phase="turn_start")]

    if etype == "message_end":
        return _message(raw.get("message") or {})

    if etype == "tool_execution_start":
        name = raw.get("toolName") or "?"
        args = raw.get("args") or {}
        out = [
            ev(
                TOOL_CALL,
                _describe(name, args),
                tool=name,
                tool_use_id=raw.get("toolCallId"),
                input=args,
            )
        ]
        # file_edit 只能在這裡發：tool_execution_end 只帶
        # {toolCallId, toolName, result, isError}，沒有 args，拿不到路徑。
        # 代價是工具失敗時也會先報一筆改動，這點跟 claude 的 normalizer 一致，
        # 而且「實際改了哪些檔案」的權威來源是每個節點跑完後重算的 git diff，
        # 這個事件只負責 UI 時間軸的顯示。
        if name in _WRITE_TOOLS:
            path = _find_path(args)
            if path:
                out.append(ev(FILE_EDIT, str(path), paths=[str(path)], tool=name))
        return out

    if etype == "tool_execution_end":
        return _tool_end(raw)

    if etype in ("compaction_start", "compaction_end"):
        # context 壓縮。會讓 token 統計看起來不連續，所以要留下痕跡。
        return [ev(STATUS, f"context {etype}", phase=etype)]

    # 刻意忽略的事件：
    #   message_update        只有增量 delta，會把事件流洗爆（message_end 才是權威）
    #   tool_execution_update 工具的部分結果，同理
    #   message_start         內容還是空的，message_end 會帶完整版
    #   turn_end / agent_end  重複 message_end 已經送過的訊息（agent_end 帶全部歷史）
    #   queue_update          steering / follow-up 佇列，跟節點結果無關
    if etype in (
        "message_update",
        "tool_execution_update",
        "message_start",
        "turn_end",
        "agent_end",
        "queue_update",
    ):
        return []

    return [ev(STDOUT, f"[pi:{etype}]")]


def _message(message: dict) -> list[dict[str, Any]]:
    role = message.get("role")

    if role == "toolResult":
        return _tool_result_message(message)

    if role != "assistant":
        # user / bashExecution / compactionSummary 之類，不是節點的產出
        return []

    out: list[dict[str, Any]] = []

    for block in message.get("content") or []:
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
        # toolCall 不在這裡發 —— tool_execution_start 已經送過，會重複

    usage = message.get("usage") or {}
    if usage:
        cost = usage.get("cost") or {}
        out.append(
            ev(
                USAGE,
                f"tokens in={usage.get('input', 0)} out={usage.get('output', 0)}"
                + (f" cost=${cost.get('total', 0):.4f}" if cost.get("total") else ""),
                input_tokens=usage.get("input", 0),
                output_tokens=usage.get("output", 0),
                cache_read_input_tokens=usage.get("cacheRead", 0),
                cache_creation_input_tokens=usage.get("cacheWrite", 0),
                reasoning_output_tokens=usage.get("reasoning") or 0,
                total_tokens=usage.get("totalTokens", 0),
                total_cost_usd=cost.get("total", 0),
                model=message.get("model"),
                provider=message.get("provider"),
            )
        )

    if message.get("errorMessage"):
        out.append(ev(ERROR, str(message["errorMessage"])))

    return out


def _tool_result_message(message: dict) -> list[dict[str, Any]]:
    """message_end 也會帶 toolResult 訊息；tool_execution_end 已經處理過內容，
    這裡只補 isError 的情況，避免同一份輸出送兩次。"""
    if not message.get("isError"):
        return []
    return [
        ev(
            ERROR,
            _content_text(message.get("content")),
            tool=message.get("toolName"),
            tool_use_id=message.get("toolCallId"),
        )
    ]


def _tool_end(raw: dict) -> list[dict[str, Any]]:
    """tool_execution_end 只有 {toolCallId, toolName, result, isError} —— 沒有 args，
    所以檔案改動不在這裡發（見 tool_execution_start）。"""
    return [
        ev(
            TOOL_RESULT,
            _result_text(raw.get("result")),
            tool=raw.get("toolName") or "?",
            tool_use_id=raw.get("toolCallId"),
            is_error=bool(raw.get("isError")),
        )
    ]


def _find_path(obj: dict) -> str | None:
    for key in _PATH_KEYS:
        value = obj.get(key)
        if value:
            return str(value)
    return None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("text")
        )
    return ""


def _result_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("output", "text", "stdout", "content"):
            if key in result:
                return _content_text(result[key]) or str(result[key])
        return str(result)[:2000]
    if isinstance(result, list):
        return _content_text(result)
    return str(result)


def _describe(name: str, args: dict) -> str:
    for key in (*_PATH_KEYS, "pattern", "command", "url"):
        if args.get(key):
            return f"{name}: {args[key]}"
    return name
