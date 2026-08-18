"""工作流的檔案存取：<專案>/.ai-workflow-proj/workflows/<id>.json。

為什麼是檔案而不是資料庫
------------------------
工作流是「這個專案要怎麼被 agent 處理」的知識，跟 .github/workflows 一樣屬於
專案本身。存成檔案才能進 git、被 review、被同事 clone 下來直接用，也才能用
編輯器直接改。檔名就是 id，資料夾就是索引 —— 不需要第二份紀錄，也就不會有
「資料庫跟檔案對不上」這種問題。

id 現在來自網址
---------------
以前 workflow id 是 sqlite 的主鍵，怎麼寫都不會有事。現在它會變成檔名，
而且是從 URL 路徑段拿到的 —— "../../.ssh/authorized_keys" 這種 id 必須在
碰到檔案系統之前就被擋掉。safe_id() 負責這件事，_path_for() 再做一次
「解析後必須落在 workflows/ 之內」的複驗。單靠字元集過濾不夠：真正要保證的
是最終路徑的位置，而不是輸入長什麼樣。
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any

SUFFIX = ".json"
MAX_ID_LEN = 80

# 檔名允許 Unicode：使用者的工作流叫「規劃 → 實作 → QA」，硬要轉成 ascii
# 只會得到一整個資料夾的 workflow.json / workflow-a3f2.json，誰都看不懂。
# 工作流 id 不會被拿去當 git ref（那是節點 id 的事，走 graph.safe_name），
# 所以沒有 ref 名稱的限制要遵守。
#
# 擋掉的是真正會出事的東西：路徑分隔符、控制字元、以及在 Windows / 各種
# 工具鏈上會出問題的保留字元。
_ID_BAD = re.compile(r'[/\\\x00-\x1f\x7f:*?"<>|]')
_SLUG_COLLAPSE = re.compile(r"\s+")


class WorkflowError(Exception):
    """工作流的 id 不合法，或檔案讀不出來。"""


def _nfc(text: str) -> str:
    """統一成 NFC。

    macOS 的檔案系統會把檔名正規化成 NFD，所以「規劃」寫進去再列出來，
    拿到的位元組序列跟原本不一樣 —— 不統一的話，存完馬上用同一個字串去
    讀會讀不到。
    """
    return unicodedata.normalize("NFC", str(text))


def slugify(text: str) -> str:
    """把名稱轉成檔名。保留 Unicode，只清掉不能當檔名的部分。"""
    cleaned = _ID_BAD.sub("-", _nfc(text))
    cleaned = _SLUG_COLLAPSE.sub(" ", cleaned).strip(" .-")
    return cleaned[:MAX_ID_LEN].strip(" .-")


def safe_id(wf_id: str) -> str:
    """驗證一個 id 可以安全地當檔名用。不合法就丟例外，不做「盡力修正」。

    盡力修正是錯的：使用者要求刪除 "../x" 而我們默默改成刪 "x"，
    那就刪掉了他們沒有要刪的東西。
    """
    candidate = _nfc(wf_id).strip()
    if not candidate:
        raise WorkflowError("工作流 id 不能是空的")
    if len(candidate) > MAX_ID_LEN:
        raise WorkflowError(f"工作流 id 太長（最多 {MAX_ID_LEN} 字）: {wf_id!r}")
    if _ID_BAD.search(candidate):
        raise WorkflowError(
            f"工作流 id 不能包含路徑分隔符、控制字元或 : * ? \" < > |：{wf_id!r}"
        )
    # "." 與 ".." 是目錄本身與上層目錄；開頭是 "." 的檔案在 glob 裡也看不到
    if candidate.startswith("."):
        raise WorkflowError(f"工作流 id 不能以 '.' 開頭: {wf_id!r}")
    return candidate


class WorkflowStore:
    """一個專案的工作流資料夾。"""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    def _path_for(self, wf_id: str) -> Path:
        """id → 檔案路徑，並複驗它確實落在 workflows/ 之內。

        safe_id() 已經擋掉了路徑分隔符，這裡是第二道防線 —— 底下會讀寫與
        刪除檔案，不能只靠上游的字串過濾正確。
        """
        base = self.dir.resolve()
        path = (base / f"{safe_id(wf_id)}{SUFFIX}").resolve()
        if path.parent != base:
            raise WorkflowError(f"工作流 id 會讓路徑逃出 {base}: {wf_id!r}")
        return path

    # ------------------------------------------------------------ 讀

    def list(self) -> list[dict[str, Any]]:
        """清單。壞掉的檔案不會讓整份清單消失，只是標記出來。

        手改壞一個 json 就看不到其他所有工作流，是最糟的失敗方式 ——
        使用者會以為自己的東西全部不見了。
        """
        if not self.dir.is_dir():
            return []

        items: list[dict[str, Any]] = []
        for path in sorted(self.dir.glob(f"*{SUFFIX}")):
            entry = {
                "id": _nfc(path.stem),
                "name": _nfc(path.stem),
                "updated_at": path.stat().st_mtime,
            }
            try:
                payload = json.loads(path.read_text("utf-8"))
                entry["name"] = payload.get("name") or path.stem
            except (json.JSONDecodeError, OSError) as exc:
                entry |= {"broken": True, "error": str(exc)}
            items.append(entry)

        items.sort(key=lambda i: i["updated_at"], reverse=True)
        return items

    def get(self, wf_id: str) -> dict[str, Any] | None:
        path = self._path_for(wf_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"{path.name} 不是合法的 JSON: {exc}") from exc
        # 檔名才是真正的 id。檔案被改名之後，內文那個 id 就過期了。
        return {**payload, "id": _nfc(path.stem)}

    def exists(self, wf_id: str) -> bool:
        return self._path_for(wf_id).exists()

    # ------------------------------------------------------------ 寫

    def new_id(self, name: str, fallback: str = "workflow") -> str:
        """為新工作流配一個沒被用過的 id。

        直接用名稱當檔名，讓 workflows/ 目錄用肉眼就看得懂。撞名補一段隨機碼
        而不是流水號 —— 流水號要先掃描整個目錄，而且兩個人同時新增時會撞在一起。

        exists() 在 macOS 這種不分大小寫的檔案系統上會把 "QA" 與 "qa" 視為
        同一個，所以那種情況也會走到補隨機碼這條路，不會互相覆蓋。
        """
        base = slugify(name) or fallback
        if not self.exists(base):
            return base
        while True:
            candidate = f"{base}-{uuid.uuid4().hex[:4]}"
            if not self.exists(candidate):
                return candidate

    def save(self, graph: dict[str, Any]) -> str:
        """寫入工作流。沒有 id 就配一個新的，回傳最終的 id。"""
        wf_id = graph.get("id") or self.new_id(graph.get("name") or "")
        path = self._path_for(wf_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = {**graph, "id": wf_id}
        # 寫成人看得懂的格式：這個檔案會進 git，diff 要能 review。
        # 縮排與 ensure_ascii=False 讓「改了哪個節點」在 diff 上是一行，
        # 而不是整包擠成一行、什麼都看不出來。
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8"
        )
        return wf_id

    def delete(self, wf_id: str) -> bool:
        path = self._path_for(wf_id)
        if not path.exists():
            return False
        path.unlink()
        return True
