"""執行單一節點：起 subprocess、逐行讀輸出、正規化事件、收集結果。

為什麼用「reader thread + queue」而不是直接 for line in proc.stdout：
直接迭代會卡在 readline 上，卡住的時候沒辦法檢查取消旗標或 timeout。
把讀取丟到背景執行緒、主迴圈用 queue.get(timeout=…) 輪詢，取消與逾時才真的有效。
stderr 也必須另開執行緒讀，否則 stderr 塞滿 pipe buffer 時 subprocess 會死鎖。
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from adapters.base import AdapterSpec
from engine.events import (
    ERROR,
    FILE_EDIT,
    MESSAGE,
    STATUS,
    STDOUT,
    USAGE,
    ev,
    passthrough,
)

EmitFn = Callable[[dict[str, Any]], None]

_STDOUT_STREAM = "stdout"
_STDERR_STREAM = "stderr"
_EOF = object()


class Cancelled(Exception):
    """使用者取消。"""


class TimedOut(Exception):
    """超過節點 timeout。"""


@dataclass
class NodeResult:
    exit_code: int | None = None
    last_message: str = ""
    structured: Any = None
    session_id: str = ""
    files: list[str] = field(default_factory=list)
    stdout: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.error


def _pump(stream, tag: str, sink: queue.Queue) -> None:
    """把一個檔案流逐行推進 queue，結束時推 EOF 哨兵。"""
    try:
        for line in stream:
            sink.put((tag, line.rstrip("\n")))
    except (ValueError, OSError):
        pass  # 進程被殺掉時 pipe 會關閉
    finally:
        sink.put((tag, _EOF))


def _terminate(proc: subprocess.Popen, grace: int) -> None:
    """先 terminate 整個 process group，寬限期過了再 kill。

    用 process group 是因為 agent CLI 會自己再開子進程（跑測試、呼叫工具），
    只殺父進程會留下孤兒繼續動我們的 worktree。
    """
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()

    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def execute(
    spec: AdapterSpec,
    normalizer: Callable[[Any], list[dict[str, Any]]] | None,
    variables: dict[str, Any],
    prompt: str,
    emit: EmitFn,
    cancel: threading.Event,
    timeout_sec: int,
    kill_grace: int = 10,
    last_message_file: Path | None = None,
    binary: str | None = None,
) -> NodeResult:
    """跑一個節點到結束，過程中把事件透過 emit 送出。

    binary 是 registry 解析出來的實際執行檔路徑（PATH 找不到時會用 adapter
    宣告的候選位置）。
    """
    argv = spec.build_argv(variables, binary=binary)
    cwd = spec.build_cwd(variables)

    emit(ev(STATUS, f"執行 {' '.join(argv[:4])}…", phase="spawn", argv=argv, cwd=cwd))

    env = {**os.environ, **spec.env}
    stdin_data = prompt if spec.prompt_delivery == "stdin" else None

    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        # 自己一個 process group，才能連子進程一起殺乾淨
        start_new_session=True,
    )

    result = NodeResult()
    lines: queue.Queue = queue.Queue()
    readers = [
        threading.Thread(target=_pump, args=(proc.stdout, _STDOUT_STREAM, lines), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, _STDERR_STREAM, lines), daemon=True),
    ]
    for reader in readers:
        reader.start()

    if stdin_data is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_data)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    deadline = time.monotonic() + timeout_sec
    messages: list[str] = []
    stdout_lines: list[str] = []
    files: list[str] = []
    open_streams = {_STDOUT_STREAM, _STDERR_STREAM}
    failure: Exception | None = None

    try:
        while open_streams:
            if cancel.is_set():
                raise Cancelled()
            if time.monotonic() > deadline:
                raise TimedOut()

            try:
                tag, payload = lines.get(timeout=0.25)
            except queue.Empty:
                continue

            if payload is _EOF:
                open_streams.discard(tag)
                continue

            if tag == _STDERR_STREAM:
                # stderr 一律當診斷輸出。codex 的 models cache 錯誤就走這裡。
                if payload.strip():
                    emit(ev(STDOUT, payload, stream="stderr"))
                continue

            stdout_lines.append(payload)
            for event in _translate(payload, spec, normalizer):
                _absorb(event, result, messages, files)
                emit(event)

    except (Cancelled, TimedOut) as exc:
        failure = exc
        _terminate(proc, kill_grace)
    finally:
        if failure is None:
            try:
                proc.wait(timeout=max(1, int(deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                failure = TimedOut()
                _terminate(proc, kill_grace)
        for reader in readers:
            reader.join(timeout=2)

    result.exit_code = proc.returncode
    result.stdout = "\n".join(stdout_lines)
    result.files = files
    result.last_message = "\n".join(m for m in messages if m.strip())

    if isinstance(failure, Cancelled):
        result.error = "已取消"
        emit(ev(ERROR, "已取消"))
        raise Cancelled()
    if isinstance(failure, TimedOut):
        result.error = f"超過 timeout {timeout_sec}s"
        emit(ev(ERROR, result.error))
        return result

    result.structured = _read_structured(last_message_file, result)
    return result


def _translate(
    line: str,
    spec: AdapterSpec,
    normalizer: Callable[[Any], list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    """一行原始輸出 → 零或多則正規化事件。"""
    if not line.strip():
        return []
    if spec.events != "jsonl" or normalizer is None:
        return passthrough(line)

    try:
        raw: Any = json.loads(line)
    except json.JSONDecodeError:
        # 不是 JSON。codex 會把 "Reading prompt from stdin..." 這種訊息
        # 混在 JSONL 裡，交給 normalizer 當純文字處理。
        raw = line

    return normalizer(raw)


def _absorb(
    event: dict[str, Any],
    result: NodeResult,
    messages: list[str],
    files: list[str],
) -> None:
    """從事件流裡累積節點的結果。"""
    kind = event["kind"]
    data = event.get("data") or {}

    session_id = data.get("session_id")
    if session_id and not result.session_id:
        result.session_id = str(session_id)

    if kind == MESSAGE:
        messages.append(event["text"])
    elif kind == FILE_EDIT:
        for path in data.get("paths") or []:
            if path not in files:
                files.append(path)
    elif kind == USAGE:
        for key, value in data.items():
            if isinstance(value, (int, float)) and key != "session_id":
                result.usage[key] = result.usage.get(key, 0) + value
        # claude 把權威的最終回覆放在 result.result
        if data.get("result_text") and not result.last_message:
            messages.append(str(data["result_text"]))
    elif kind == ERROR:
        result.error = event["text"] or "agent 回報錯誤"


def _read_structured(last_message_file: Path | None, result: NodeResult) -> Any:
    """取得結構化輸出。

    codex 走 --output-schema + -o <file>（寫檔），claude 走 --json-schema
    （最終回覆本身就是 JSON）。兩條路都試，都拿不到就回 None。
    """
    if last_message_file and last_message_file.exists():
        text = last_message_file.read_text("utf-8").strip()
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return None

    text = (result.last_message or "").strip()
    if not text:
        return None

    # 容忍被 ```json 圍起來的輸出
    if text.startswith("```"):
        body = text.split("\n", 1)[1] if "\n" in text else ""
        text = body.rsplit("```", 1)[0].strip()

    if not text.startswith(("{", "[")):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
