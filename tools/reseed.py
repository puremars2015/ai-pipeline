"""把 workflows/*.json 重新匯入資料庫。

平常啟動服務時只會匯入「db 裡還沒有」的工作流，所以隨附範本被改壞或覆蓋之後
不會自己還原。這支工具用來強制還原。

用法：
    .venv/bin/python -m tools.reseed              # 列出差異，不動任何東西
    .venv/bin/python -m tools.reseed --apply      # 真的覆蓋回檔案的版本
    .venv/bin/python -m tools.reseed --apply plan-impl-qa
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import settings
from engine import graph as g
from store.db import Store

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / "workflows"


def _shape(payload: dict) -> str:
    """只比對真正重要的部分（節點與邊），忽略排版之類的差異。"""
    return json.dumps(
        {"nodes": payload.get("nodes"), "edges": payload.get("edges")},
        sort_keys=True, ensure_ascii=False,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("only", nargs="*", help="只處理這些 id（預設全部）")
    ap.add_argument("--apply", action="store_true", help="真的寫入，不加就只是預覽")
    args = ap.parse_args()

    cfg = settings.load()
    store = Store(cfg.database)

    changed = 0
    for path in sorted(WORKFLOW_DIR.glob("*.json")):
        payload = json.loads(path.read_text("utf-8"))
        wf_id = payload.get("id") or path.stem
        if args.only and wf_id not in args.only:
            continue

        try:
            g.parse(payload)
        except g.GraphError as exc:
            print(f"✗ {wf_id}: 檔案本身有問題，跳過 —— {exc}")
            continue

        current = store.get_workflow(wf_id)
        if current is None:
            print(f"+ {wf_id}: db 裡沒有，會新增")
        elif _shape(current) == _shape(payload) and current.get("name") == payload.get("name"):
            print(f"= {wf_id}: 與檔案一致")
            continue
        else:
            print(
                f"! {wf_id}: 與檔案不一致\n"
                f"    db   名稱={current.get('name')!r} "
                f"節點={len(current.get('nodes') or [])} 邊={len(current.get('edges') or [])}\n"
                f"    檔案 名稱={payload.get('name')!r} "
                f"節點={len(payload.get('nodes') or [])} 邊={len(payload.get('edges') or [])}"
            )

        changed += 1
        if args.apply:
            store.save_workflow(payload)
            print(f"    → 已覆蓋成檔案的版本")

    if not changed:
        print("\n沒有需要處理的。")
    elif not args.apply:
        print(f"\n{changed} 個有差異。加上 --apply 才會真的寫入。")
    else:
        print(f"\n已還原 {changed} 個。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
