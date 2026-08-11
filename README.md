# AI Workflow Builder

在畫布上拖曳節點、連線組出自己的 AI 工作流。節點可以是 `codex`、`claude`、
`opencode`、shell 指令、條件判斷、git 操作。執行時每個節點的進度、agent 的
工具呼叫、改了哪些檔案都即時顯示在同一張圖上。

流程定義是資料，不是程式碼 —— 改流程是拖線，不是改 bash。

## 為什麼不是原本的 bash

前身是一支把「Codex 規劃 → Claude 實作 → Codex QA」寫死的 250 行 bash。
問題不是有 bug，是形狀錯了：改流程要改腳本、看不到執行狀況、QA 判定靠
`head -1 | grep PASS`、失敗只能翻 log。

現在流程存在資料庫裡，節點種類可插拔，QA 回傳的是 typed JSON。

## 安裝

需要 Python 3.12+ 與 [uv](https://docs.astral.sh/uv/)。

```bash
uv venv && uv pip install -r requirements.txt
```

設定目標 repo（`config.local.yaml` 不進 git）：

```bash
printf 'project_repo: "~/你的專案路徑"\n' > config.local.yaml
```

檢查環境（CLI 是否安裝、登入、模型設定是否相容、目標 repo 是否可用）：

```bash
.venv/bin/python -m tools.doctor
```

啟動：

```bash
.venv/bin/python app.py
```

開 http://localhost:5111

## 節點型別

| 節點 | 用途 | 給下游的輸出 |
|---|---|---|
| **需求** | run 的起點 | `requirement` |
| **Codex / Claude Code / opencode** | 呼叫 agent CLI | `last_message`、`structured`、`session_id`、`files`、`usage` |
| **Shell 指令** | 跑測試、lint、build | `stdout`、`exit_code` |
| **條件判斷** | 依運算式選 true / false 出口 | — |
| **Git** | commit、重算 diff | `diff`、`sha` |

Prompt 用 Jinja 模板，可引用上游節點的輸出：

```jinja
依照以下計畫實作，這是第 {{ loop.iteration }} 輪。
{{ nodes.plan.last_message }}

{% if nodes.qa.structured %}上一輪 QA 的問題：
{% for issue in nodes.qa.structured.issues %}- {{ issue }}
{% endfor %}{% endif %}
```

可用變數：`requirement`、`nodes.<id>.*`、`run.diff`、`run.changed_files`、
`run.branch`、`run.repo`、`loop.iteration`。

條件運算式用白名單求值（不是 `eval`）：

```
nodes.qa.structured.verdict == 'PASS' and nodes.tests.exit_code == 0
```

## 迴圈怎麼做

沒有專門的迴圈節點 —— 把條件節點的 **false 出口連回上游**就是重試迴圈，
跟「QA 沒過打回去修」的心智模型一致。安全性靠三道上限：每節點
`max_visits`、每 run `max_run_steps`、run 層級 timeout。

隨附的 `plan-impl-qa` 範本就是這個形狀：

```
需求 → Codex 規劃 → Claude 實作 → Commit → 跑測試 → Codex QA → QA 通過？
                         ↑                                          │ false
                         └──────────────────────────────────────────┘
                                                                    │ true
                                                              完成摘要
```

## 隔離模型

每次 run 開一個獨立 git worktree + `task/<run-id>` branch，所有節點在裡面
接力。**不會動到你手上的工作目錄**，也不會自動合併 —— 跑完留在 branch 上，
UI 的「變更」分頁直接給你合併指令。

`plan.md` / QA 報告之類的產物存在 `runs/<run-id>/artifacts/`，在 repo 之外，
不會混進 `main`。

### 平行執行的限制

所有節點共用一個 worktree，所以會寫檔的節點（`mutates=true`）搶同一把寫入鎖，
實際上是序列化的；只有唯讀節點（審查、規劃、read-only 指令）真的平行。
節點在等鎖時會發事件，UI 會顯示 —— 不會讓你以為平行了卻在背後偷偷排隊。

想真正平行寫入需要每節點各自的 worktree，目前不支援。

## 接一個新的 agent CLI

放兩個檔案，不用改引擎：

```
adapters/<id>.yaml              # 怎麼呼叫（同時驅動 UI 的設定表單）
adapters/normalizers/<id>.py    # 怎麼解讀它的事件輸出
```

寫 normalizer 之前先抓真實輸出，不要憑記憶推測 schema：

```bash
.venv/bin/python -m tools.probe <id>      # 實跑一次，存成 tests/fixtures/<id>.jsonl
```

三個內建 adapter 的 normalizer 都是照 probe 抓回來的 fixture 寫的，
CLI 升版後重跑 probe，測試就會抓到 schema 變動。

## 開發

```bash
.venv/bin/python -m pytest -q          # 154 個測試，不呼叫 LLM
.venv/bin/python -m tools.watch <run>  # 在終端機盯一個 run
```

引擎測試全部走 `mock` adapter —— 它跟真實 CLI 走完全相同的
subprocess → 逐行讀 → normalize 路徑，只是不花錢也不會慢。

## 實際使用時會踩到的幾件事

**agent 需要能執行指令。** `claude` 的 `acceptEdits` 只放行檔案編輯，不放行
Bash。無人看顧的流程一旦需要跑測試就會卡住 —— 實測時 Claude 反覆索取許可，
最後改成寫一堆「驗證報告」檔案來代替真的執行測試（QA 抓到並判 FAIL）。
範本用 `bypassPermissions`：worktree 是拋棄式 branch 上的隔離目錄，這個取捨是合理的。

**worktree 裡沒有 `.venv` / `node_modules`。** 它們被 gitignore，git worktree
不會帶過去。範本的測試節點會回頭借主 repo 的 venv（`{{ run.repo }}`）。

**「測試環境找不到」不等於「測試失敗」。** 範本會明確印出 `SKIPPED: …`，
QA 看得到並會據此判斷，不會把略過當成通過。

**codex 的模型設定可能比 CLI 版本新。** `~/.codex/config.toml` 若指定了較新的
模型，舊版 CLI 送出去會被 API 以 400 擋掉，而且看起來像沒反應。
`tools.doctor` 會直接指出來，修法是 `codex update` 或在節點設定裡填一個舊模型。

**codex 會把診斷訊息混進 JSONL。** normalizer 必須容忍非 JSON 行，已處理。

## 檔案結構

```
app.py                    Flask 入口：頁面、API、SSE
settings.py               config.yaml + config.local.yaml 疊加載入
engine/
  graph.py                正規格式的解析與驗證（環、孤島、必填設定）
  runner.py               排程：join / fan-out / 環 / 寫入鎖 / 三道上限
  executor.py             跑 subprocess、串流事件、timeout 與取消
  context.py              節點間資料傳遞、Jinja 渲染、安全運算式求值
  workspace.py            worktree 生命週期 + 拒絕在 $HOME 上動手
  bus.py                  事件匯流：sqlite 持久化 + 多訂閱者扇出
  service.py              run 生命週期（背景執行緒）
adapters/                 <id>.yaml + normalizers/<id>.py
nodes/ store/ templates/ static/
workflows/plan-impl-qa.json
tools/  doctor.py probe.py watch.py
```

前端只有 `static/js/graph.js` 知道 Drawflow 的資料長相，想換成 LiteGraph
（ComfyUI 用的那個）只要重寫那一個檔案。
