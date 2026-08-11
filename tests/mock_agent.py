#!/usr/bin/env python3
"""假 agent，給引擎測試用 —— 不呼叫任何 LLM，不花錢，執行是確定性的。

由 adapters/mock.yaml 驅動，行為用一段 JSON 描述：

    {"message": "done",                 最終回覆
     "files": {"a.txt": "content"},     要寫的檔案（相對 cwd）
     "structured": {"verdict": "PASS"}, 結構化輸出（寫到 --output-file）
     "tools": ["read a.txt"],           要模擬的工具呼叫
     "sleep": 0.0,                      每則事件之間的延遲（測 timeout / 取消）
     "exit_code": 0,                    離開碼
     "fail": "訊息",                    發一則 error 事件
     "emit_count": 1}                   重複發 message 幾次（測事件量）

輸出格式刻意就是正規化後的形狀（kind/text/data），讓 mock 走完整條
subprocess → 逐行讀 → normalize → 事件流 的路徑，而不是繞過它。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def emit(kind: str, text: str = "", **data: object) -> None:
    print(json.dumps({"kind": kind, "text": text, "data": data}), flush=True)


def main() -> int:
    spec = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    delay = float(spec.get("sleep", 0) or 0)

    emit("status", "mock session", phase="session", session_id=spec.get("session_id", "mock-session-1"))

    for tool in spec.get("tools") or []:
        if delay:
            time.sleep(delay)
        emit("tool_call", str(tool), tool="mock_tool")
        emit("tool_result", f"{tool} ok", tool="mock_tool")

    for rel, content in (spec.get("files") or {}).items():
        if delay:
            time.sleep(delay)
        path = Path.cwd() / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
        emit("file_edit", rel, paths=[rel], tool="mock_write")

    if spec.get("fail"):
        emit("error", str(spec["fail"]))

    message = spec.get("message")
    if message:
        for _ in range(int(spec.get("emit_count", 1))):
            if delay:
                time.sleep(delay)
            emit("message", str(message))

    structured = spec.get("structured")
    if structured is not None:
        payload = json.dumps(structured, ensure_ascii=False)
        if output_file:
            Path(output_file).write_text(payload, encoding="utf-8")
        else:
            emit("message", payload)

    emit("usage", "mock usage", input_tokens=10, output_tokens=20, total_cost_usd=0.0)
    return int(spec.get("exit_code", 0) or 0)


if __name__ == "__main__":
    sys.exit(main())
