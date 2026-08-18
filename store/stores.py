"""每個專案一個執行紀錄資料庫，這裡負責管理那些實例。

為什麼不是一個中央資料庫
------------------------
執行紀錄屬於專案：它記的是「這個 repo 上跑過什麼」。放進專案資料夾之後，
專案搬走、備份、或整個刪掉，歷史都跟著走，不會在別的地方留下孤兒紀錄。

代價是 Store 從一個變成 N 個，而且「所有專案的總覽」要合併查詢。
N 是使用者手動註冊的專案數（個位數），所以直接查完再合併就好，
不需要另外維護一份索引 —— 索引會有跟真實資料不同步的問題。

不會自己建出專案資料夾
----------------------
Store 的建構會 mkdir -p 資料庫的上層目錄。如果專案資料夾已經被使用者刪掉，
那個 mkdir 會把整棵目錄樹重新長出來 —— 看起來像工具自己復活了一個已刪除的
專案。所以開啟之前一定先確認 repo 目錄還在。
"""

from __future__ import annotations

import threading
from typing import Any, Iterable

from engine.project import ProjectSettings
from store.db import Store


class StoreRegistry:
    """project id -> Store。實例會快取，因為 Store 內部持有連線池。"""

    def __init__(self) -> None:
        self._stores: dict[str, Store] = {}
        self._lock = threading.Lock()

    def for_project(self, project: ProjectSettings) -> Store:
        """取得（必要時建立）這個專案的紀錄資料庫。

        專案資料夾不在了就丟 FileNotFoundError，不要默默把它重建出來。
        """
        if not project.repo.is_dir():
            raise FileNotFoundError(f"專案資料夾不存在: {project.repo}")

        with self._lock:
            existing = self._stores.get(project.id)
            if existing is not None and existing.path == project.database:
                return existing
            store = Store(project.database)
            self._stores[project.id] = store
            return store

    def close_current_thread(self) -> None:
        """關掉這個執行緒在所有專案上的連線。

        背景執行緒跑完就消失，不關的話那些連線會一直掛著。
        """
        with self._lock:
            stores = list(self._stores.values())
        for store in stores:
            store.close()

    def forget(self, project_id: str) -> None:
        with self._lock:
            self._stores.pop(project_id, None)


def merge_runs(
    stores: Iterable[tuple[str, Store]], limit: int
) -> list[dict[str, Any]]:
    """把多個專案的執行紀錄合併成一份，最新的排前面。

    每個資料庫各取 limit 筆再合併後截斷 —— 只從其中一個取 limit 筆的話，
    某個專案跑得特別頻繁時會把其他專案完全擠掉。
    """
    merged: list[dict[str, Any]] = []
    for project_id, store in stores:
        for run in store.list_runs(limit):
            # project_id 是欄位裡就有的，但舊紀錄可能是空的；用註冊表的 id 補上，
            # 前端要靠它顯示「這筆屬於哪個專案」。
            merged.append({**run, "project_id": run.get("project_id") or project_id})

    merged.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return merged[:limit]
