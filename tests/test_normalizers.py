"""Normalizer 測試 —— 輸入是 tools/probe 實跑三個 CLI 抓回來的真實輸出。

fixture 不是手刻的，是 tests/fixtures/*.jsonl，由：
    .venv/bin/python -m tools.probe <adapter>
產生。CLI 升版後重跑 probe，這裡的測試就會抓到 schema 變動。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapters.normalizers import claude as claude_norm
from adapters.normalizers import codex as codex_norm
from adapters.normalizers import opencode as opencode_norm
from adapters.normalizers import pi as pi_norm
from engine.events import (
    ALL_KINDS,
    ERROR,
    FILE_EDIT,
    MESSAGE,
    STATUS,
    STDOUT,
    TOOL_CALL,
    TOOL_RESULT,
    USAGE,
    collect_text,
)

FIXTURES = Path(__file__).parent / "fixtures"


def run_fixture(normalize, name: str) -> list[dict]:
    """把 fixture 每一行餵進 normalizer，非 JSON 行以 str 傳入（模擬 runner 行為）。"""
    events: list[dict] = []
    for line in (FIXTURES / name).read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            raw = line
        events.extend(normalize(raw))
    return events


def kinds(events: list[dict]) -> list[str]:
    return [e["kind"] for e in events]


def first(events: list[dict], kind: str) -> dict:
    for e in events:
        if e["kind"] == kind:
            return e
    raise AssertionError(f"沒有 {kind} 事件，實際有: {sorted(set(kinds(events)))}")


def session_id_of(events: list[dict]) -> str | None:
    for e in events:
        sid = e["data"].get("session_id")
        if sid:
            return sid
    return None


ALL_NORMALIZERS = [
    (codex_norm, "codex.jsonl"),
    (claude_norm, "claude.jsonl"),
    (opencode_norm, "opencode.jsonl"),
    # pi.jsonl 只有第一行是實跑抓的，其餘依 docs/json.md 與 pi-ai 的 types.d.ts
    # 構造（本機沒設定 provider 憑證）。詳見 tests/fixtures/README.md。
    (pi_norm, "pi.jsonl"),
]


# ---------------------------------------------------------- 通用不變條件


@pytest.mark.parametrize("module,fixture", ALL_NORMALIZERS, ids=lambda x: str(x))
def test_all_kinds_are_valid(module, fixture):
    """所有 normalizer 只能吐出 engine.events 定義的 kind。"""
    for kind in kinds(run_fixture(module.normalize, fixture)):
        assert kind in ALL_KINDS


@pytest.mark.parametrize("module,fixture", ALL_NORMALIZERS, ids=lambda x: str(x))
def test_every_run_yields_message_and_usage(module, fixture):
    """三個 CLI 都要能抽出「最終回覆」與「token 用量」—— 引擎依賴這兩者。"""
    events = run_fixture(module.normalize, fixture)
    assert first(events, MESSAGE)["text"].strip()
    assert first(events, USAGE)["data"]


@pytest.mark.parametrize("module,fixture", ALL_NORMALIZERS, ids=lambda x: str(x))
def test_every_run_exposes_session_id(module, fixture):
    """續接（resume）要靠 session id，三個 CLI 都必須拿得到。"""
    assert session_id_of(run_fixture(module.normalize, fixture))


@pytest.mark.parametrize("module,fixture", ALL_NORMALIZERS, ids=lambda x: str(x))
def test_file_edit_detected(module, fixture):
    """探測用的 prompt 是「建立 hello.txt」，三者都該回報檔案改動。"""
    events = run_fixture(module.normalize, fixture)
    edit = first(events, FILE_EDIT)
    assert any("hello.txt" in p for p in edit["data"]["paths"])


@pytest.mark.parametrize("module,_f", ALL_NORMALIZERS, ids=lambda x: str(x))
def test_non_json_line_becomes_stdout(module, _f):
    """codex 會把診斷訊息混進 stdout，所有 normalizer 都要容忍非 JSON 輸入。"""
    out = module.normalize("Reading prompt from stdin...")
    assert out == [{"kind": STDOUT, "text": "Reading prompt from stdin...", "data": {}}]


@pytest.mark.parametrize("module,_f", ALL_NORMALIZERS, ids=lambda x: str(x))
def test_unknown_event_is_not_swallowed(module, _f):
    """未知事件型別要留成 stdout，不能靜默丟掉 —— 否則 CLI 升版時會查不出問題。"""
    out = module.normalize({"type": "some_future_event_type"})
    assert out and out[0]["kind"] == STDOUT


@pytest.mark.parametrize("module,_f", ALL_NORMALIZERS, ids=lambda x: str(x))
def test_garbage_input_does_not_crash(module, _f):
    assert module.normalize(None) == []
    assert module.normalize(12345) == []
    assert module.normalize({}) != []  # 空 dict 當未知事件處理，不炸


# ---------------------------------------------------------- codex


def test_codex_maps_real_events():
    events = run_fixture(codex_norm.normalize, "codex.jsonl")

    # thread_id 是 codex 的續接 id，要確實轉成 session_id（不寫死值，比對 fixture 本身）
    raw_thread_id = json.loads(
        (FIXTURES / "codex.jsonl").read_text("utf-8").splitlines()[0]
    )["thread_id"]
    assert session_id_of(events) == raw_thread_id

    usage = first(events, USAGE)["data"]
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0

    edit = first(events, FILE_EDIT)
    assert edit["data"]["paths"] == ["hello.txt"] or "hello.txt" in edit["text"]

    assert "done" in collect_text(events).lower()


def test_codex_file_change_emitted_once():
    """item.started 與 item.completed 都帶 file_change，只能算一次。"""
    events = run_fixture(codex_norm.normalize, "codex.jsonl")
    assert kinds(events).count(FILE_EDIT) == 1


def test_codex_error_fixture():
    """真實失敗案例：本機 codex 版本比 config 設定的模型舊，API 回 400。"""
    events = run_fixture(codex_norm.normalize, "codex.error.jsonl")
    err = first(events, ERROR)
    assert "requires a newer version of Codex" in err["text"]
    # 失敗的 run 沒有 usage，引擎不能假設一定有
    assert USAGE not in kinds(events)


def test_codex_noise_lines_are_stdout():
    events = run_fixture(codex_norm.normalize, "codex.noise.txt")
    assert kinds(events) == [STDOUT, STDOUT]
    assert "Reading prompt from stdin" in events[0]["text"]


def test_codex_command_execution():
    """實測 fixture 沒跑到指令，用 schema 形狀補測 command_execution 分支。"""
    started = codex_norm.normalize(
        {
            "type": "item.started",
            "item": {"id": "i1", "type": "command_execution", "command": "pytest -q"},
        }
    )
    assert started[0]["kind"] == TOOL_CALL
    assert "pytest" in started[0]["text"]

    done = codex_norm.normalize(
        {
            "type": "item.completed",
            "item": {
                "id": "i1",
                "type": "command_execution",
                "command": "pytest -q",
                "aggregated_output": "3 passed",
                "exit_code": 0,
                "status": "completed",
            },
        }
    )
    assert done[0]["kind"] == TOOL_RESULT
    assert done[0]["data"]["exit_code"] == 0


# ---------------------------------------------------------- claude


def test_claude_maps_real_events():
    events = run_fixture(claude_norm.normalize, "claude.jsonl")

    assert session_id_of(events)

    call = first(events, TOOL_CALL)
    assert call["data"]["tool"] == "Write"
    assert "hello.txt" in call["text"]

    assert first(events, TOOL_RESULT)["data"]["tool_use_id"]

    usage = first(events, USAGE)["data"]
    assert usage["total_cost_usd"] > 0
    assert usage["num_turns"] >= 1
    # result.result 存進 data，不重複發成 MESSAGE
    assert "result_text" in usage


def test_claude_final_text_not_duplicated():
    """assistant 的 text block 已經是最終回覆，result 事件不能再發一次。"""
    events = run_fixture(claude_norm.normalize, "claude.jsonl")
    assert kinds(events).count(MESSAGE) == 1


def test_claude_multiple_blocks_in_one_event():
    """一則 assistant 事件可含多個 block，要展開成多則正規化事件。"""
    out = claude_norm.normalize(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "先想一下"},
                    {"type": "text", "text": "我要改檔案"},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x/a.py"},
                    },
                ]
            },
        }
    )
    assert kinds(out) == ["reasoning", MESSAGE, TOOL_CALL, FILE_EDIT]
    assert out[3]["data"]["paths"] == ["/x/a.py"]


def test_claude_error_result():
    out = claude_norm.normalize(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "boom",
            "total_cost_usd": 0.01,
            "num_turns": 1,
            "usage": {},
        }
    )
    assert first(out, ERROR)["text"] == "boom"


def test_claude_partial_messages_ignored():
    """--include-partial-messages 的 stream_event 會洗爆事件流，v1 忽略。"""
    assert claude_norm.normalize({"type": "stream_event", "event": {}}) == []


# ---------------------------------------------------------- opencode


def test_opencode_maps_real_events():
    events = run_fixture(opencode_norm.normalize, "opencode.jsonl")

    assert session_id_of(events) == "ses_010ffb1c8ffeAewEv5YrqlvMup"

    call = first(events, TOOL_CALL)
    assert call["data"]["tool"]
    assert first(events, TOOL_RESULT)

    edit = first(events, FILE_EDIT)
    assert any("hello.txt" in p for p in edit["data"]["paths"])


def test_opencode_multiple_steps_yield_multiple_usage():
    """opencode 每個 LLM turn 一組 step_start/step_finish，usage 會有多則。"""
    events = run_fixture(opencode_norm.normalize, "opencode.jsonl")
    assert kinds(events).count(USAGE) == 2
    assert kinds(events).count(STATUS) == 2


def test_opencode_tool_call_before_result():
    events = run_fixture(opencode_norm.normalize, "opencode.jsonl")
    order = kinds(events)
    assert order.index(TOOL_CALL) < order.index(TOOL_RESULT)


# ---------------------------------------------------------------- pi


def test_pi_session_id_from_real_probe():
    """session 標頭是實跑抓的，id 就是續接用的 session id。"""
    events = run_fixture(pi_norm.normalize, "pi.jsonl")
    header = json.loads((FIXTURES / "pi.jsonl").read_text("utf-8").splitlines()[0])
    assert header["type"] == "session"
    assert session_id_of(events) == header["id"]
    assert first(events, STATUS)["data"]["cwd"] == header["cwd"]


def test_pi_maps_real_events():
    events = run_fixture(pi_norm.normalize, "pi.jsonl")

    call = first(events, TOOL_CALL)
    assert call["data"]["tool"] == "write"
    assert "hello.txt" in call["text"]

    assert first(events, TOOL_RESULT)["data"]["tool"] == "write"
    assert "hello.txt" in first(events, FILE_EDIT)["data"]["paths"][0]
    assert "done" in collect_text(events)


def test_pi_usage_uses_pi_field_names():
    """pi 的 Usage 是 input/output/cacheRead/cacheWrite/totalTokens/cost.total，
    欄位名跟其他三家都不一樣，要正確映射成統一的名字。"""
    events = run_fixture(pi_norm.normalize, "pi.jsonl")
    usage = first(events, USAGE)["data"]
    assert usage["input_tokens"] == 1520
    assert usage["output_tokens"] == 48
    assert usage["reasoning_output_tokens"] == 12
    assert usage["total_tokens"] == 1568
    assert usage["total_cost_usd"] > 0
    assert usage["provider"] == "google"


def test_pi_ignores_delta_and_duplicate_events():
    """message_update 是純增量、agent_end/turn_end 重複已送過的訊息 —— 都要忽略，
    否則同一段文字會出現好幾次，事件流也會被洗爆。"""
    assert pi_norm.normalize({"type": "message_update",
                              "assistantMessageEvent": {"type": "text_delta",
                                                        "contentIndex": 0,
                                                        "delta": "abc"}}) == []
    assert pi_norm.normalize({"type": "tool_execution_update"}) == []
    assert pi_norm.normalize({"type": "message_start", "message": {}}) == []
    assert pi_norm.normalize({"type": "turn_end", "message": {}, "toolResults": []}) == []
    assert pi_norm.normalize({"type": "agent_end", "messages": []}) == []
    assert pi_norm.normalize({"type": "queue_update"}) == []

    # 同一段文字只能出現一次
    events = run_fixture(pi_norm.normalize, "pi.jsonl")
    assert [e["text"] for e in events if e["kind"] == MESSAGE].count("done") == 1


def test_pi_tool_call_not_duplicated_by_message_end():
    """assistant 的 content 裡有 toolCall block，tool_execution_start 也會送一次。
    只能算一次，否則 UI 時間軸會看到每個工具呼叫都出現兩遍。"""
    events = run_fixture(pi_norm.normalize, "pi.jsonl")
    assert kinds(events).count(TOOL_CALL) == 1
    assert kinds(events).count(FILE_EDIT) == 1


def test_pi_thinking_becomes_reasoning():
    events = run_fixture(pi_norm.normalize, "pi.jsonl")
    assert "write" in first(events, "reasoning")["text"]


def test_pi_noauth_fixture_is_header_only():
    """實測：憑證未設定時 JSON 串流只有 session 標頭，錯誤在 stderr、exit 1。

    所以節點失敗判定不能只靠事件流裡有沒有 error 事件 —— 一定要看 exit code。
    """
    events = run_fixture(pi_norm.normalize, "pi.noauth.jsonl")
    assert kinds(events) == [STATUS]
    assert ERROR not in kinds(events)
    assert USAGE not in kinds(events)
    assert session_id_of(events)


def test_pi_tool_error_surfaces():
    out = pi_norm.normalize({
        "type": "tool_execution_end", "toolCallId": "t1", "toolName": "bash",
        "result": {"output": "command not found"}, "isError": True,
    })
    assert out[0]["kind"] == TOOL_RESULT
    assert out[0]["data"]["is_error"] is True


def test_pi_file_edit_comes_from_start_not_end():
    """tool_execution_end 沒有 args，拿不到路徑，所以 file_edit 只能在 start 發。"""
    start = pi_norm.normalize({
        "type": "tool_execution_start", "toolCallId": "t1", "toolName": "write",
        "args": {"path": "a.py", "absolutePath": "/wt/a.py", "content": "x"},
    })
    assert kinds(start) == [TOOL_CALL, FILE_EDIT]
    assert start[1]["data"]["paths"] == ["/wt/a.py"]

    end = pi_norm.normalize({
        "type": "tool_execution_end", "toolCallId": "t1", "toolName": "write",
        "result": {"output": "ok"}, "isError": False,
    })
    assert kinds(end) == [TOOL_RESULT], "end 不該再發一次 file_edit"

    # 唯讀工具不算檔案改動
    read = pi_norm.normalize({
        "type": "tool_execution_start", "toolCallId": "t2", "toolName": "read",
        "args": {"path": "a.py"},
    })
    assert kinds(read) == [TOOL_CALL]


def test_pi_assistant_error_message():
    out = pi_norm.normalize({
        "type": "message_end",
        "message": {"role": "assistant", "content": [], "usage": {},
                    "errorMessage": "context length exceeded"},
    })
    assert first(out, ERROR)["text"] == "context length exceeded"
