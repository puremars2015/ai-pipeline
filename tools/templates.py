"""把隨附範本複製進某個專案。

以前這支工具叫 reseed，做的是「把 workflows/*.json 覆蓋回資料庫」。
工作流搬進各專案的 .ai-workflow-proj/workflows/ 之後，那個動作沒有意義了 ——
資料庫裡不再有工作流，而各專案裡的那份是使用者自己的檔案，不該被還原成範本。

現在只做一件事：列出範本、把選中的複製進指定專案。

用法：
    .venv/bin/python -m tools.templates                       # 列出範本與已註冊專案
    .venv/bin/python -m tools.templates --project <id>        # 列出該專案現有的工作流
    .venv/bin/python -m tools.templates --project <id> --import plan-impl-qa
    .venv/bin/python -m tools.templates --project <id> --import all
"""

from __future__ import annotations

import argparse
import sys

import settings
from engine import project as proj
from store import templates
from store.projects import ProjectRegistry
from store.workflows import WorkflowStore


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", help="目標專案 id")
    ap.add_argument("--import", dest="wanted", help="要複製的範本 id，或 all")
    args = ap.parse_args()

    cfg = settings.load()
    registry = ProjectRegistry(cfg.database)
    available = templates.list_templates()

    if not args.project:
        print("== 隨附範本 ==")
        for item in available:
            print(f"  {item['id']:24} {item['name']}（{item['nodes']} 個節點）")
        print("\n== 已註冊專案 ==")
        entries = registry.list()
        if not entries:
            print("  （還沒有註冊任何專案，在網頁上新增，或 POST /api/projects）")
        for entry in entries:
            print(f"  {entry.id:24} {entry.path}")
        print("\n加上 --project <id> --import <範本 id> 才會真的複製。")
        return 0

    entry = registry.get(args.project)
    if entry is None:
        print(f"✗ 找不到專案: {args.project}", file=sys.stderr)
        return 1

    resolved = proj.for_project(cfg, entry.id, entry.path, entry.name)
    store = WorkflowStore(resolved.workflows_dir)

    if not args.wanted:
        print(f"== {resolved.name} 現有的工作流 ==")
        existing = store.list()
        if not existing:
            print("  （空的。用 --import 複製一個範本進來。）")
        for item in existing:
            mark = " ✗ 壞掉" if item.get("broken") else ""
            print(f"  {item['id']:24} {item['name']}{mark}")
        print(f"\n位置：{resolved.workflows_dir}")
        return 0

    wanted = [t["id"] for t in available] if args.wanted == "all" else [args.wanted]
    for template_id in wanted:
        payload = templates.load_template(template_id)
        if payload is None:
            print(f"✗ 找不到範本: {template_id}", file=sys.stderr)
            return 1

        # 不覆蓋既有的 —— 使用者可能已經改過那一份
        if store.exists(template_id):
            payload = {**payload, "id": store.new_id(template_id)}
            print(f"! {template_id} 已存在，改存成 {payload['id']}")

        print(f"+ {store.save(payload)}")

    print(f"\n已寫進 {resolved.workflows_dir}")
    print("這些檔案要進 git —— 記得 commit。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
