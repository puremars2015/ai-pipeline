"""隨附範本：工具目錄的 workflows/*.json。

這些檔案以前是「種子」—— 啟動服務時會被塞進 sqlite，之後就再也不會被讀。
現在工作流住在各專案裡，這些就變成單純的範本：使用者明確要求時才複製進
某個專案，複製完就是那個專案自己的檔案，跟這裡再無關係。

改成「明確複製」而不是「自動種入」的理由：新註冊一個專案時自動塞四個範本
進去，那是在別人的 repo 裡放他沒要求的檔案 —— 而且那些檔案會進 git。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "workflows"


def list_templates(directory: Path | None = None) -> list[dict[str, Any]]:
    """可用的範本。壞掉的檔案直接略過 —— 範本是我們自己附的，
    壞掉是我們的 bug，不該變成使用者畫面上的錯誤訊息。"""
    base = directory or TEMPLATE_DIR
    if not base.is_dir():
        return []

    items = []
    for path in sorted(base.glob("*.json")):
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        items.append(
            {
                "id": payload.get("id") or path.stem,
                "name": payload.get("name") or path.stem,
                "nodes": len(payload.get("nodes") or []),
            }
        )
    return items


def load_template(template_id: str, directory: Path | None = None) -> dict[str, Any] | None:
    """讀出一個範本。template_id 來自使用者輸入，所以比對的是「已知清單」，
    不是拿它去組路徑 —— 組路徑就要處理 ../ 之類的問題，比對清單不用。"""
    base = directory or TEMPLATE_DIR
    for path in sorted(base.glob("*.json")):
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if (payload.get("id") or path.stem) == template_id:
            return payload
    return None
