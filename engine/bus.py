"""事件匯流：把引擎發出的事件同時寫進 sqlite 與推給所有訂閱者。

為什麼兩邊都要
--------------
寫 sqlite 是為了「重新整理頁面 / 重啟服務之後還能回放整個 run」；
推 queue 是為了「瀏覽器現在就看到」。少了持久化，關掉分頁就失去整段歷史；
少了 queue，就只能靠輪詢。

訂閱與回放之間不能有縫
----------------------
先訂閱、再從 db 回放、然後丟掉 queue 裡序號已經回放過的事件。
反過來（先回放再訂閱）會漏掉這兩個動作之間產生的事件。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from store.db import Store

# 串流結束的哨兵
DONE = object()


class RunBus:
    """一個 run 對應一個 bus。"""

    def __init__(self, store: Store, run_id: str) -> None:
        self.store = store
        self.run_id = run_id
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._closed = False
        self._max_seq = 0

    # ------------------------------------------------------------ 發佈

    def publish(self, node_id: str, event: dict[str, Any]) -> None:
        """發佈一則事件。序號由 bus 指派，這是全 run 唯一的序號來源。

        序號不能由發佈端各自產生 —— 引擎的節點事件與服務層的 run_start /
        run_end 是兩個不同的發佈端，各自編號會讓 ORDER BY seq 的結果錯亂。
        """
        with self._lock:
            self._max_seq += 1
            seq = self._max_seq
            targets = list(self._subscribers)

        payload = {
            "run_id": self.run_id,
            "node_id": node_id,
            "seq": seq,
            "ts": event.get("ts") or time.time(),
            "kind": event["kind"],
            "text": event.get("text", ""),
            "data": event.get("data") or {},
        }

        self.store.append_events(self.run_id, [payload])

        for sub in targets:
            try:
                sub.put_nowait(payload)
            except queue.Full:
                # 訂閱者跟不上（分頁在背景被節流）。丟掉即時事件不影響正確性 ——
                # 對方重連時會用 after_seq 從 db 把漏掉的補回來。
                pass

    # ------------------------------------------------------------ 訂閱

    def subscribe(self) -> queue.Queue:
        sub: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subscribers.add(sub)
            if self._closed:
                sub.put_nowait(DONE)
        return sub

    def unsubscribe(self, sub: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(sub)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            targets = list(self._subscribers)
        for sub in targets:
            try:
                sub.put_nowait(DONE)
            except queue.Full:
                pass

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------ 串流

    def stream(self, after_seq: int = 0, keepalive: float = 15.0):
        """產出事件 dict，直到 run 結束。

        先訂閱再回放，避免兩者之間漏事件；回放過的序號會從即時佇列裡跳過。
        每隔 keepalive 秒吐一個 None，讓呼叫端送 SSE 註解行維持連線。
        """
        sub = self.subscribe()
        try:
            sent = after_seq
            for event in self.store.get_events(self.run_id, after_seq=after_seq):
                sent = max(sent, event["seq"])
                yield {**event, "run_id": self.run_id}

            # 回放完才發現 run 已經結束，且沒有新事件 → 直接收尾
            if self._closed and self._max_seq <= sent:
                return

            last_beat = time.monotonic()
            while True:
                try:
                    item = sub.get(timeout=1.0)
                except queue.Empty:
                    if time.monotonic() - last_beat >= keepalive:
                        last_beat = time.monotonic()
                        yield None
                    if self._closed:
                        # 收尾前把 db 裡可能漏掉的補齊（queue 曾經滿過）
                        for event in self.store.get_events(self.run_id, after_seq=sent):
                            sent = max(sent, event["seq"])
                            yield {**event, "run_id": self.run_id}
                        return
                    continue

                if item is DONE:
                    for event in self.store.get_events(self.run_id, after_seq=sent):
                        sent = max(sent, event["seq"])
                        yield {**event, "run_id": self.run_id}
                    return

                if item["seq"] <= sent:
                    continue  # 已經在回放階段送過了
                sent = item["seq"]
                yield item
        finally:
            self.unsubscribe(sub)
