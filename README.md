# AI Workflow Builder

在畫布上拖曳節點、連線組出自己的 AI 工作流。節點可以是 `codex`、`claude`、
`opencode`、`pi`、shell 指令、條件判斷、git 操作。執行時每個節點的進度、agent 的
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
| **Codex / Claude Code / opencode / pi** | 呼叫 agent CLI | `last_message`、`structured`、`session_id`、`files`、`usage` |
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

無論哪種模式，都**不會動到你手上的工作目錄**，也不會自動合併 —— 跑完留在
branch 上，UI 的「變更」分頁直接給你合併指令。`plan.md` / QA 報告之類的產物
存在 `runs/<run-id>/artifacts/`，在 repo 之外，不會混進 `main`。

模式在編輯器工具列切換（存進工作流的 `settings.isolation`）。

### `shared`（預設）

整個 run 一個 worktree + `task/<run-id>` branch，所有節點在裡面接力。
會寫檔的節點（`mutates=true`）搶同一把寫入鎖，**實際上是序列化的**；
只有唯讀節點（審查、規劃、read-only 指令）真的平行。節點在等鎖時會發事件，
UI 會顯示 —— 不會讓你以為平行了卻在背後偷偷排隊。

簡單、diff 一路累積、要不要 commit 由你用 git 節點明確決定。

### `per_node`

每個節點自己的 worktree + `node/<run-id>/<node-id>` branch，從上游節點的產出
commit 開始。沒有共用狀態就不需要鎖，**會寫檔的節點也真的平行**。

```
              ┌── 改模組 A ──┐        各自一個 worktree
需求 ─────────┤              ├── 合流   合流時 merge 兩邊的 commit
              └── 改模組 B ──┘
```

代價是三件事，都是真的：

1. **每個會寫檔的節點跑完會自動 commit**。狀態只有變成 commit 才交得給下游，
   所以這步是必要的，不是可選的。
2. **fan-in 要合併多個上游，可能衝突**。兩個平行節點改到同一處，會在合流的
   節點上失敗，錯誤訊息會列出衝突檔案。要避免就讓平行的節點碰不同的檔案。
3. **磁碟用量是「節點數 × repo 大小」**。大 repo 要留意。

`task/<run-id>` 會代表**整個 run 的完整結果**：收尾時算出所有 tip（沒有被其他
commit 包含的那些），只有一個就直接指過去，多個（圖 fan-out 成好幾個各自結束的
分支）就在一個獨立的整合 worktree 裡合併起來。合併不起來就讓整個 run 失敗 ——
絕不安靜地只採用其中一邊。想看某一段的中間結果，去看它自己的節點 branch。

迴圈重入時，節點會從**自己上一輪的 commit** 繼續，不是回到 run 起點重做。

每個節點（包含條件節點）都有自己的工作目錄。條件節點也要，因為它可能引用
`run.diff` / `changed_files`，而且它可能自己就是 fan-in 點 —— 得真的把多個上游
合併起來才算得出正確的狀態。唯一的例外是沒有入邊的需求節點，它的狀態定義上
就是 run 起點、diff 定義上是空的。

三個實作上的細節（踩過才知道）：

- 節點的 branch 不能叫 `task/<run-id>/<node-id>` —— git 的 ref 存成檔案，
  `refs/heads/task/<run-id>` 一存在就不可能再有同名目錄底下的 ref。所以用
  `node/` 另一個前綴。
- `per_node` 模式刻意**不**開 run 層級的 worktree：`task/<run-id>` 必須保持
  沒有被任何 worktree 佔用，否則跑完之後 `git branch -f` 會被 git 拒絕。
- **節點 id 不直接當目錄名或 ref 名稱。** id 是使用者可見的穩定識別字，可以是
  中文、可以含空白；內部名稱由 `safe_name()` 產生（可讀前綴 + id 的 SHA1 前 16 碼，
  並且圖驗證會確認全圖的內部名稱沒有碰撞）。
  這同時解決三件事：`../..` 之類的路徑穿越、macOS 檔案系統不分大小寫導致
  節點 `A` 與 `a` 撞到同一個目錄、以及不必為了安全去限制既有工作流的 id 格式。

## 新增工作流

三種方式：

**在編輯器裡拉** —— 按「新增」清空畫布，從左邊拖節點、連線，填名稱後按
「另存新檔」。已經開著某個工作流時，「儲存」是**覆蓋它**，「另存新檔」才是
產生一份新的。開著隨附範本按儲存會先跳確認。

**放一個 JSON 到 `workflows/`** —— 下次啟動服務時自動匯入（只匯入 db 裡還沒有
的 id）。格式見現有的兩個範本，或用編輯器存一份再從 API 拉下來當骨架：

```bash
curl -s localhost:5111/api/workflows/<id> > workflows/my-flow.json
```

**打 API**：

```bash
curl -X POST localhost:5111/api/workflows -H 'Content-Type: application/json' -d @my-flow.json
```

沒有 `id` 欄位就配一個新的；有 `id` 就是覆蓋那一個。

### 範本被改壞了怎麼還原

啟動時只會匯入「db 裡還沒有」的工作流，所以覆蓋掉不會自己還原：

```bash
.venv/bin/python -m tools.reseed            # 先看差異
.venv/bin/python -m tools.reseed --apply    # 從 workflows/ 覆蓋回去
```

## 隨附的範本

| 範本 | 做什麼 |
|---|---|
| `sample-project-notes` | **第一次試跑用這個。需求已經填好**，設定好 `project_repo` 直接按執行 |
| `sample-opencode-pi` | 需求已填好。opencode 寫小工具 → pi 唯讀審查 → 沒過打回去改 |
| `plan-impl-qa` | 需求 → Codex 規劃 → Claude 實作 → 測試 → Codex QA，沒過就打回去修 |
| `codex-review` | 把目前 branch 對齊進 worktree → Codex 對照 main 審查 → 有問題就讓 Claude 修 → 再審 |

### `sample-project-notes`（需求已填好）

要它讀你的 repo，然後產生一份 `PROJECT_NOTES.md`（新人五分鐘速覽）。挑這個當
第一次試跑是因為：任何 repo 都適用、以讀為主只新增一個檔案、只用 codex
（不需要 Claude 額度）、跑完你手上的東西完全沒被動到。

需求裡有一條硬性要求是「不確定的就寫『未確認』，不要猜」，所以它同時示範了
QA 的價值。實跑在一個只有 `calc.py` 的 repo 上，它的產出是：

```
## 3. 怎麼跑起來
正式啟動或測試指令：**未確認**。
目前 repo 沒有 README、pyproject.toml、package.json、Makefile … 可用來確認
專案支援的指令。tests/test_calc.py 的寫法雖與 pytest 相容，但 repo 沒有宣告
pytest 依賴或記載 pytest 指令，因此不將其列為正式支援的指令。
```

一次完整 run 約 4 分鐘、38 萬 tokens（explore / write / qa 三個節點）。
內容豐富的 repo 產出會實用得多。

`codex-review` 的第一個節點會 `git reset --hard` 到目標 branch，所以之後
`{{ run.diff }}` 剛好是「這條 branch 相對 main 的完整變更」，修正也 commit 在
它上面。動的是拋棄式的 `task/<run-id>`，你手上的 branch 不會被碰。
預設審目標 repo 目前 checkout 的那條；想固定審某一條，把 checkout 節點的
`TARGET=` 那行改掉。

審查節點是唯讀 sandbox + JSON schema，每個 finding 都必須說得出
`why_it_breaks`（什麼情況下會壞）。實測它會為了確認而真的去跑實驗，
也會拒絕回報「這是設計偏好而非 bug」的東西。

### `sample-opencode-pi`（需求已填好，示範 opencode + pi）

opencode 寫一個字串處理小工具（`stringutils.py` + 測試），pi 只給 `read`/`grep`
兩個工具做唯讀審查，沒過就打回去改。兩者都走 OpenRouter：

```bash
source ~/.hermes/.env        # 或你放 OPENROUTER_API_KEY 的地方
.venv/bin/python app.py      # 要在有這個環境變數的 shell 裡啟動
```

`OPENROUTER_API_KEY` 要在**啟動 `app.py` 的那個 shell**裡就有 —— 子行程繼承的
是 Flask 服務行程的環境變數，不是你之後在別的分頁匯出的。config.local.yaml
不會幫你載入 `.env`。

pi 這個 adapter 不支援結構化輸出（`supports_schema: false`），所以審查結論走
純文字的 `VERDICT: PASS` / `VERDICT: FAIL` 約定，`gate` 節點的運算式對應著
比對這個字串，不是讀 JSON。

實跑一次（scratch repo，claude-haiku-4.5 經 OpenRouter）第一輪就過，
18 個測試全過，pi 的審查逐項對照實際檔案內容確認（不是空泛的稱讚），
總花費約 $0.02。

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

內建 adapter 的 normalizer 都是照 probe 抓回來的 fixture 寫的，CLI 升版後重跑
probe，測試就會抓到 schema 變動。每份 fixture 的來源與可信度記在
`tests/fixtures/README.md`（pi 的部分因為本機沒憑證，是依它自己的 docs/json.md
與 types.d.ts 構造的，設定憑證後請重跑 probe 覆蓋）。

CLI 不在 PATH 上時，yaml 可以用 `binary_candidates` 列候選絕對路徑。

## 開發

```bash
.venv/bin/python -m pytest -q          # 219 個測試，不呼叫 LLM
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

**pi 需要先設定 provider 憑證**，而且它即使加了 `-p` 也會讀 stdin —— 沒關掉
就會無限等待。本工具一律關閉 stdin，但你自己在終端機試的時候要記得。
`pi` 常裝在自帶的 node 發行版底下（例如 `~/.hermes/node/bin`），不在 PATH 上也能用，
adapter 的 `binary_candidates` 有列候選位置。

## 檔案結構

```
app.py                    Flask 入口：頁面、API、SSE
settings.py               config.yaml + config.local.yaml 疊加載入
engine/
  graph.py                正規格式的解析與驗證（環、孤島、必填設定）
  runner.py               排程：join / fan-out / 環 / 寫入鎖 / 三道上限
  isolation.py            shared / per_node 兩種隔離模式
  executor.py             跑 subprocess、串流事件、timeout 與取消
  context.py              節點間資料傳遞、Jinja 渲染、安全運算式求值
  workspace.py            worktree 生命週期 + 拒絕在 $HOME 上動手
  bus.py                  事件匯流：sqlite 持久化 + 多訂閱者扇出
  service.py              run 生命週期（背景執行緒）
adapters/                 <id>.yaml + normalizers/<id>.py
nodes/ store/ templates/ static/
workflows/  plan-impl-qa.json  codex-review.json
tools/  doctor.py probe.py watch.py reseed.py
```

前端只有 `static/js/graph.js` 知道 Drawflow 的資料長相，想換成 LiteGraph
（ComfyUI 用的那個）只要重寫那一個檔案。
