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


def make_normalizer():
    """每次執行給一個新的翻譯器。

    需要狀態是因為 tool_execution_end 只帶 {toolCallId, toolName, result, isError}
    —— 沒有 args。要在「工具真的成功之後」才回報檔案改動，就得把 start 的參數
    記下來等 end。狀態必須是每次執行獨立的：normalizer 模組層級的 dict 會讓
    並行的節點互相污染。
    """
    pending_paths: dict[str, str] = {}

    def normalize_stateful(raw: Any) -> list[dict[str, Any]]:
        return _normalize(raw, pending_paths)

    return normalize_stateful


def normalize(raw: Any) -> list[dict[str, Any]]:
    """無狀態入口，給不需要追蹤工具參數的呼叫端（例如測試單一事件）。"""
    return _normalize(raw, {})


def _normalize(raw: Any, pending_paths: dict[str, str]) -> list[dict[str, Any]]:
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
        call_id = str(raw.get("toolCallId") or "")
        # 把寫檔工具的路徑記下來，等 end 確認成功了才回報改動。
        # 不在這裡就發 file_edit —— NodeResult.files 是會被持久化的節點產出，
        # 失敗的 write 不該讓它宣稱改過某個檔案。
        if name in _WRITE_TOOLS and call_id:
            path = _find_path(args)
            if path:
                pending_paths[call_id] = str(path)
        return [
            ev(
                TOOL_CALL,
                _describe(name, args),
                tool=name,
                tool_use_id=call_id or None,
                input=args,
            )
        ]

    if etype == "tool_execution_end":
        return _tool_end(raw, pending_paths)

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
    """message_end 也會帶 toolResult 訊息，但 tool_execution_end 已經送過內容了，
    所以這裡什麼都不發。

    尤其**不能**在 isError 時發 ERROR 事件：工具失敗（bash 回非零、edit 找不到
    要替換的文字）對 agent 來說是可恢復的，它會看到錯誤然後換個做法繼續，最後
    正常結束並 exit 0。但 executor 的 _absorb 只要看到任何 ERROR 事件就會設定
    result.error，於是 runner 會把一個其實成功的節點判成失敗。

    節點該不該算失敗只看兩件事：CLI 的 exit code，以及 assistant 自己回報的
    errorMessage（那才是真的走不下去）。
    """
    return []


def _tool_end(raw: dict, pending_paths: dict[str, str]) -> list[dict[str, Any]]:
    """tool_execution_end 沒有 args，路徑要從 start 記下來的對照表取。"""
    name = raw.get("toolName") or "?"
    call_id = str(raw.get("toolCallId") or "")
    is_error = bool(raw.get("isError"))
    path = pending_paths.pop(call_id, None)

    out = [
        ev(
            TOOL_RESULT,
            _result_text(raw.get("result")),
            tool=name,
            tool_use_id=call_id or None,
            is_error=is_error,
        )
    ]
    # 只有成功的寫入才算檔案改動
    if path and not is_error:
        out.append(ev(FILE_EDIT, path, paths=[path], tool=name))
    return out


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
