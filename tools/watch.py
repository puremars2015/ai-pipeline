"""在終端機盯一個 run 到結束。

用法：
    .venv/bin/python -m tools.watch <run-id> [--host http://localhost:5111]

刻意用 Python 讀 API 而不是 shell 的 $(curl ...) —— agent 的輸出常常含有
zsh 在當前 locale 下處理不了的位元組，用命令替換去接會直接爆掉。
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request

ORDER = ["pending", "running", "passed", "failed", "skipped", "cancelled"]
MARK = {"pending": "·", "running": "▶", "passed": "✓", "failed": "✗",
        "skipped": "–", "cancelled": "⏹"}


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--host", default="http://localhost:5111")
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--timeout", type=float, default=1800.0)
    args = ap.parse_args()

    base = f"{args.host}/api/runs/{args.run_id}"
    deadline = time.time() + args.timeout
    last = ""

    while time.time() < deadline:
        run = get(base)
        nodes = sorted(run.get("nodes") or [], key=lambda n: n["node_id"])
        line = "  ".join(
            f"{MARK.get(n['status'], '?')}{n['node_id']}"
            + (f"×{n['visits']}" if n["visits"] > 1 else "")
            for n in nodes
        )
        if line != last:
            print(f"[{time.strftime('%H:%M:%S')}] {run['status']:9} {line}", flush=True)
            last = line

        if run["status"] not in ("queued", "running"):
            print(f"\n最終狀態: {run['status']}")
            if run.get("reason"):
                print(f"原因: {run['reason']}")
            print(f"branch: {run.get('branch')}")
            return 0 if run["status"] == "passed" else 1

        time.sleep(args.interval)

    print("盯到 timeout 還沒結束")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
