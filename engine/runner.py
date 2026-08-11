"""排程器：把一張圖跑完。

支援分支、迴圈、平行，並且在共用 worktree 的前提下保證寫入不會互相蓋掉。

環的處理
--------
QA 沒過打回去修正本質上就是一個環，所以圖允許有環。安全性靠三道上限：
每節點 max_visits、整個 run 的 max_run_steps、以及 run 層級的 timeout。

`all` join 的節點只等「前向入邊」—— 回頭邊不算。否則實作節點第一輪就會
死等一個還沒跑過的 QA 節點。

平行與共用 worktree 的衝突
--------------------------
所有節點共用同一個 worktree。兩個會寫檔的節點同時跑會互相蓋掉，所以
mutates=True 的節點必須搶同一把寫入鎖，實際上是序列化的。
只有 mutates=False 的節點（read-only 的審查、跑測試以外的唯讀指令）才真的平行。
等鎖的時候會發一則 status 事件，UI 要顯示出來 —— 不能讓使用者以為平行了
卻在背後偷偷序列化。
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from adapters.registry import Registry
from engine import graph as g
from engine.context import ContextError, RunContext, evaluate, render_template
from engine.events import ERROR, MESSAGE, STATUS, ev
from engine.executor import Cancelled, NodeResult, execute
from engine.workspace import Workspace
from settings import Guards

# emit(node_id, event_dict)
EmitFn = Callable[[str, dict[str, Any]], None]

PENDING = "pending"
RUNNING = "running"
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
CANCELLED = "cancelled"


class RunAborted(Exception):
    """守門條件觸發或節點失敗，整個 run 中止。"""

    def __init__(self, reason: str, status: str = FAILED) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass
class RunResult:
    status: str = PASSED
    reason: str = ""
    steps: int = 0
    node_status: dict[str, str] = field(default_factory=dict)
    visits: dict[str, int] = field(default_factory=dict)


class Runner:
    def __init__(
        self,
        graph: g.Graph,
        context: RunContext,
        workspace: Workspace | None,
        registry: Registry,
        guards: Guards,
        emit: EmitFn,
        artifacts_dir: Path | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.graph = graph
        self.ctx = context
        self.ws = workspace
        self.registry = registry
        self.guards = guards
        self._emit = emit
        self.artifacts = artifacts_dir
        self.cancel = cancel or threading.Event()

        self.back_edges = graph.back_edges()
        self.entry_ids = {n.id for n in graph.entrypoints()}
        self.arrived: dict[str, set[g.Edge]] = {nid: set() for nid in graph.nodes}
        self.visits: dict[str, int] = {nid: 0 for nid in graph.nodes}
        self.status: dict[str, str] = {nid: PENDING for nid in graph.nodes}
        self.steps = 0

        # worktree 的寫入鎖：mutates 節點序列化，避免互相蓋檔
        self.write_lock = threading.Lock()

    # ------------------------------------------------------------ 事件

    def emit(self, node_id: str, event: dict[str, Any]) -> None:
        """發事件給下游 sink。序號由 sink 指派 —— 服務層也會發自己的事件
        （run_start / run_end），序號必須有單一來源才不會錯亂。"""
        self._emit(node_id, event)

    # ------------------------------------------------------------ 主迴圈

    def run(self) -> RunResult:
        deadline = time.monotonic() + self.guards.run_timeout
        max_steps = int(
            self.graph.settings.get("max_run_steps") or self.guards.max_run_steps
        )

        ready: list[str] = sorted(self.entry_ids)
        if not ready:
            return RunResult(status=FAILED, reason="沒有起點節點，無法開始")

        running: dict[Future, str] = {}
        result = RunResult()

        with ThreadPoolExecutor(
            max_workers=max(1, self.guards.max_parallel_nodes)
        ) as pool:
            try:
                while ready or running:
                    if self.cancel.is_set():
                        raise RunAborted("使用者取消", CANCELLED)
                    if time.monotonic() > deadline:
                        raise RunAborted(
                            f"整個 run 超過 timeout {self.guards.run_timeout}s"
                        )

                    while ready:
                        node_id = ready.pop(0)
                        self.steps += 1
                        if self.steps > max_steps:
                            raise RunAborted(
                                f"執行步數超過上限 {max_steps}（可能是迴圈收不掉）"
                            )
                        self._check_visits(node_id)
                        self.status[node_id] = RUNNING
                        self.visits[node_id] += 1
                        running[pool.submit(self._run_node, node_id)] = node_id

                    if not running:
                        continue

                    done, _ = wait(
                        list(running), timeout=0.5, return_when=FIRST_COMPLETED
                    )
                    for future in done:
                        node_id = running.pop(future)
                        port = future.result()  # 例外會在這裡浮上來
                        ready.extend(self._propagate(node_id, port))

            except RunAborted as exc:
                result.status = exc.status
                result.reason = exc.reason
                self._abort(running)
            except Cancelled:
                result.status = CANCELLED
                result.reason = "使用者取消"
                self._abort(running)
            except Exception as exc:  # 節點內未預期的錯誤
                result.status = FAILED
                result.reason = f"{type(exc).__name__}: {exc}"
                self._abort(running)

        for node_id, state in self.status.items():
            if state in (PENDING, RUNNING):
                self.status[node_id] = SKIPPED if result.status == PASSED else state

        result.steps = self.steps
        result.node_status = dict(self.status)
        result.visits = dict(self.visits)
        return result

    def _abort(self, running: dict[Future, str]) -> None:
        self.cancel.set()
        for future, node_id in running.items():
            future.cancel()
            if self.status.get(node_id) == RUNNING:
                self.status[node_id] = CANCELLED

    def _check_visits(self, node_id: str) -> None:
        node = self.graph.nodes[node_id]
        limit = node.max_visits or self.guards.default_max_visits
        if self.visits[node_id] >= limit:
            raise RunAborted(
                f"節點「{node.label}」造訪次數已達上限 {limit}。"
                f"若這是 QA 重試迴圈，代表重試 {limit} 次仍未通過。"
            )

    # ------------------------------------------------------- token 傳遞

    def _propagate(self, node_id: str, port: str | None) -> list[str]:
        """節點跑完，依選定的 port 送出 token，回傳新變成 ready 的節點。"""
        self.arrived[node_id].clear()
        if port is None:
            return []

        newly_ready: list[str] = []
        for edge in self.graph.outgoing(node_id, port):
            self.arrived[edge.dst].add(edge)
            if self._is_ready(edge.dst) and self.status[edge.dst] != RUNNING:
                if edge.dst not in newly_ready:
                    newly_ready.append(edge.dst)
        return newly_ready

    def _is_ready(self, node_id: str) -> bool:
        node = self.graph.nodes[node_id]
        arrived = self.arrived[node_id]
        incoming = self.graph.incoming(node_id)
        forward = [e for e in incoming if e not in self.back_edges]
        backward = [e for e in incoming if e in self.back_edges]

        # 回頭邊送達就代表「再跑一輪」，不必等前向入邊
        if any(e in arrived for e in backward):
            return True
        if not forward:
            return bool(arrived)
        if node.join == "any":
            return any(e in arrived for e in forward)
        return all(e in arrived for e in forward)

    # ------------------------------------------------------- 單一節點

    def _run_node(self, node_id: str) -> str | None:
        """執行一個節點，回傳要往哪個 port 送 token（None = 不往下走）。"""
        node = self.graph.nodes[node_id]
        self.ctx.iteration = self.visits[node_id]
        out = self.ctx.output(node_id)
        out.visits = self.visits[node_id]

        self.emit(
            node_id,
            ev(STATUS, f"▶ {node.label}", phase="node_start",
               node_type=node.type, iteration=self.ctx.iteration),
        )

        try:
            if node.type == g.REQUIREMENT:
                port = self._run_requirement(node, out)
            elif node.type == g.CONDITION:
                port = self._run_condition(node, out)
            elif node.type == g.GIT:
                port = self._run_git(node, out)
            else:
                port = self._run_agent(node, out)
        except Cancelled:
            self.status[node_id] = CANCELLED
            raise
        except (ContextError, ValueError) as exc:
            self.status[node_id] = FAILED
            out.status = FAILED
            self.emit(node_id, ev(ERROR, str(exc)))
            raise RunAborted(f"節點「{node.label}」設定有問題: {exc}") from exc

        self.emit(
            node_id,
            ev(STATUS, f"■ {node.label} → {self.status[node_id]}",
               phase="node_end", result=self.status[node_id], port=port),
        )
        return port

    def _run_requirement(self, node: g.Node, out) -> str:
        text = node.config.get("text") or self.ctx.requirement
        self.ctx.requirement = text
        out.last_message = text
        out.status = PASSED
        self.status[node.id] = PASSED
        self.emit(node.id, ev(MESSAGE, text))
        return g.DEFAULT_PORT

    def _run_condition(self, node: g.Node, out) -> str:
        expr = node.config.get("expr") or ""
        verdict = evaluate(expr, self.ctx.as_variables())
        out.status = PASSED
        out.last_message = str(verdict)
        self.status[node.id] = PASSED
        self.emit(
            node.id,
            ev(STATUS, f"條件 {expr} → {verdict}", phase="condition",
               expr=expr, verdict=verdict),
        )
        return "true" if verdict else "false"

    def _run_git(self, node: g.Node, out) -> str:
        if self.ws is None:
            raise ValueError("git 節點需要 worktree，但這個 run 沒有")

        action = node.config.get("action") or "commit"
        if action == "commit":
            message = render_template(
                node.config.get("message") or "wip: {{ run.id }}",
                self.ctx.as_variables(),
            )
            with self.write_lock:
                sha = self.ws.commit(message)
                # commit 之後要重算 diff，否則下游的 QA 節點看到的是 commit 前
                # 的舊快照（通常是空的）。
                self._refresh_diff()
            # 沒有變更時 commit() 回 None，這不是錯誤 —— 舊 bash 在這裡
            # 因為 git commit 回非零加上 set -e 而炸掉整條流程。
            out.last_message = sha or "（沒有變更，未建立 commit）"
            self.emit(
                node.id,
                ev(STATUS, out.last_message, phase="git_commit", sha=sha),
            )
        elif action == "diff":
            self._refresh_diff()
            out.last_message = self.ctx.diff
            self.emit(
                node.id,
                ev(STATUS, f"diff {len(self.ctx.changed_files)} 個檔案",
                   phase="git_diff", files=self.ctx.changed_files),
            )
        else:
            raise ValueError(f"git 節點不支援的 action: {action}")

        out.status = PASSED
        self.status[node.id] = PASSED
        return g.DEFAULT_PORT

    def _run_agent(self, node: g.Node, out) -> str | None:
        spec = self.registry.get(node.type)
        normalizer = self.registry.normalizer(spec)
        mutates = spec.mutates if node.mutates is None else node.mutates

        ctx_vars = self.ctx.as_variables()

        # 節點設定裡的字串本身也可以是模板（shell 的 command、git 的 message…），
        # 必須先渲染過再交給 adapter 組參數。adapter 的參數替換只有單層，
        # 直接把原始設定值塞進 argv 的話，值裡面的 {{ … }} 會原封不動被當成
        # 字面字串傳給 CLI —— 實測時 shell 節點就印出了字面的 "{{ run.repo }}"。
        rendered_config: dict[str, Any] = {}
        for key, value in node.config.items():
            if key == "schema" or not isinstance(value, str):
                rendered_config[key] = value  # schema 是 JSON，不當模板處理
            else:
                rendered_config[key] = render_template(value, ctx_vars)

        variables = {**spec.defaults(), **rendered_config, **ctx_vars}
        variables["workdir"] = self.ctx.workdir
        variables["tool_root"] = self.ctx.tool_root

        prompt = rendered_config.get("prompt") or ""
        variables["prompt"] = prompt

        # 續接同一個節點的前一次 session（迴圈第二輪起）
        if node.config.get("resume") and out.session_id:
            variables["session_id"] = out.session_id

        schema = node.config.get("schema")
        last_message_file: Path | None = None
        if schema and spec.supports_schema:
            schema_obj = json.loads(schema) if isinstance(schema, str) else schema
            variables["schema_json"] = json.dumps(schema_obj, ensure_ascii=False)
            if self.artifacts:
                self.artifacts.mkdir(parents=True, exist_ok=True)
                schema_path = self.artifacts / f"{node.id}.schema.json"
                schema_path.write_text(variables["schema_json"], "utf-8")
                variables["schema_file"] = str(schema_path)
                last_message_file = self.artifacts / f"{node.id}.result.json"
                variables["last_message_file"] = str(last_message_file)
                last_message_file.unlink(missing_ok=True)

        timeout = node.timeout_sec or self.guards.default_node_timeout

        if mutates:
            if self.write_lock.locked():
                self.emit(
                    node.id,
                    ev(STATUS, "等待 worktree 寫入鎖（會寫檔的節點不能平行）",
                       phase="await_lock"),
                )
            with self.write_lock:
                result = self._spawn(node, spec, normalizer, variables, prompt,
                                     timeout, last_message_file)
                self._refresh_diff()
        else:
            result = self._spawn(node, spec, normalizer, variables, prompt,
                                 timeout, last_message_file)

        out.last_message = result.last_message
        out.structured = result.structured
        out.session_id = result.session_id or out.session_id
        out.exit_code = result.exit_code
        out.files = result.files
        out.stdout = result.stdout
        out.usage = result.usage

        expected = rendered_config.get("expect_exit_code")
        expected = 0 if expected in (None, "") else int(expected)
        failed = result.exit_code != expected or bool(result.error)

        if failed:
            out.status = FAILED
            self.status[node.id] = FAILED
            detail = result.error or f"exit code {result.exit_code}（預期 {expected}）"
            self.emit(node.id, ev(ERROR, detail))
            if node.on_error == "fail":
                raise RunAborted(f"節點「{node.label}」失敗: {detail}")
            # on_error=continue：讓下游的條件節點自己判斷 exit_code
            return g.DEFAULT_PORT

        out.status = PASSED
        self.status[node.id] = PASSED
        return g.DEFAULT_PORT

    def _spawn(self, node, spec, normalizer, variables, prompt, timeout,
               last_message_file) -> NodeResult:
        binary = self.registry.resolve_binary(spec)
        if binary is None:
            raise RunAborted(
                f"節點「{node.label}」找不到可執行檔 {spec.binary}。"
                f"請確認它已安裝且在 PATH 上（或在 adapters/{spec.id}.yaml 的 "
                f"binary_candidates 加上實際路徑）。"
            )
        return execute(
            spec=spec,
            normalizer=normalizer,
            variables=variables,
            prompt=prompt,
            emit=lambda event: self.emit(node.id, event),
            cancel=self.cancel,
            timeout_sec=timeout,
            kill_grace=self.guards.kill_grace_seconds,
            last_message_file=last_message_file,
            binary=binary,
        )

    def _refresh_diff(self) -> None:
        """更新 context 裡的 diff，讓下游的 QA 節點看到真實的變更。

        diff 只放在記憶體與 db，絕不寫進 worktree —— 舊 bash 把 changes.diff
        commit 進 repo，導致下一輪的 diff 包含上一輪的 diff，內容平方成長。
        """
        if self.ws is None:
            return
        self.ctx.diff = self.ws.diff()
        self.ctx.changed_files = self.ws.changed_files()
