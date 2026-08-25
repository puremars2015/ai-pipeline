# AI Workflow Builder

AI Workflow Builder 是一個在本機執行的視覺化 AI 工作流編排器。你可以把 Codex、
Claude Code、opencode、pi、Shell、條件判斷與 Git 操作拖到畫布上，連成一條可重複
執行、可觀察、可版本控制的軟體開發流程。

它不是另一支寫死步驟的 pipeline 腳本。工作流本身是 JSON 資料，跟著目標專案進
Git；每次執行則發生在獨立 worktree 和 task branch，不會直接改動你正在工作的目錄。

## 能做什麼

- 用拖曳與連線建立規劃、實作、測試、審查及重試流程。
- 同一個服務管理多個 Git 專案，每個專案擁有自己的工作流與執行歷史。
- 即時顯示節點狀態、Agent 訊息、工具呼叫、檔案變更、測試輸出與 token usage。
- 支援 fan-out、fan-in、條件分支、重試迴圈，以及 `all` / `any` join。
- 用 typed JSON 傳遞 QA 結果，不必靠解析自由文字判斷成功或失敗。
- 透過 SQLite 保存事件；重新整理頁面後仍可回放，SSE 斷線也能接續。
- 提供 `shared` 與 `per_node` 兩種 Git 隔離模式。
- 以 YAML adapter + Python normalizer 接入新的 Agent CLI，不必修改排程引擎。

一個典型流程如下：

```text
需求 → Codex 規劃 → Claude 實作 → Git commit → 跑測試 → Codex QA → 通過？
                         ↑                                      │ false
                         └──────────────────────────────────────┘
                                                                │ true
                                                              完成
```

## 快速開始

### 1. 安裝

需求：

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- 至少一個準備使用的 Agent CLI，例如 `codex`、`claude`、`opencode` 或 `pi`
- 一個已有初始 commit 的 Git repository

```bash
uv venv
uv pip install -r requirements.txt
```

### 2. 檢查環境

```bash
.venv/bin/python -m tools.doctor
```

Doctor 會檢查 Agent CLI 是否可執行、Codex 模型與 CLI 版本是否相容，以及已註冊專案
目前是否可用。

### 3. 啟動服務

```bash
.venv/bin/python app.py
```

開啟 [http://localhost:5111](http://localhost:5111)，從右上角的專案管理加入一個 Git
repository。服務會在該專案內初始化 `.ai-workflow-proj/`，接著即可建立工作流，或先
匯入一個隨附範本試跑。

若 Agent 需要 API key，請在**啟動服務的同一個 shell**中設定。Agent 子行程會繼承
Flask 服務啟動時的環境；`config.local.yaml` 不會自動載入 `.env`。

## 使用流程

1. 註冊一個目標 Git 專案。
2. 新增工作流，或匯入隨附範本。
3. 將節點拖到畫布、設定 Prompt 與參數，再連接執行順序。
4. 儲存並執行工作流。
5. 在執行頁即時查看節點狀態、事件、變更與產物。
6. 完成後檢查 `task/<run-id>`，再自行決定是否合併：

```bash
git merge --no-ff task/<run-id>
```

系統不會自動合併成果，也不會直接修改原本 checkout 的工作目錄。

## 專案中的資料

註冊專案時會建立以下結構：

```text
<project>/.ai-workflow-proj/
├── project.yaml              # 進 Git：專案名稱、基準分支、預設隔離模式、guards
├── workflows/
│   └── *.json                # 進 Git：工作流定義，檔名就是 workflow id
├── .gitignore                # 進 Git：排除 local/
├── local.yaml                # 不進 Git：本機專用覆寫，可自行建立
└── local/                    # 不進 Git
    ├── ai-workflow.sqlite    # run、node 與 event 紀錄
    └── runs/<run-id>/
        └── artifacts/        # schema、結構化輸出等執行產物
```

設計原則很簡單：

- **工作流定義屬於專案**，所以存成可 review、可分享的 JSON 檔。
- **執行紀錄屬於本機**，所以放在 `local/` 並排除於 Git。
- worktree 位於全域 `worktree_root/<project-id>/<run-id>/`，不放進目標 repository。

同事 clone 專案後，只要在自己的 AI Workflow Builder 中註冊該路徑，就會看到相同的
`project.yaml` 與工作流；每個人的執行歷史和本機路徑互不影響。

### 設定優先順序

設定由低到高依序覆蓋：

1. `config.yaml`：工具預設值。
2. `config.local.yaml`：這台機器的工具設定，不進 Git。
3. `<project>/.ai-workflow-proj/project.yaml`：專案共用設定，進 Git。
4. `<project>/.ai-workflow-proj/local.yaml`：該專案在這台機器的設定，不進 Git。
5. 工作流的 `settings`：目前支援覆蓋 `isolation` 與 `max_run_steps`。

專案層可覆蓋 `main_branch`、`isolation` 與 `guards`；`worktree_root` 應放在
`local.yaml`。執行資料庫與 artifacts 固定留在 `.ai-workflow-proj/local/`。

## 節點型別

| 節點 | 用途 | 主要輸出 |
|---|---|---|
| Requirement | 工作流起點；可使用執行時輸入的需求 | `requirement`、`last_message` |
| Codex | 規劃、實作、審查；支援 JSON schema | `last_message`、`structured`、`session_id`、`files`、`usage` |
| Claude Code | Agent 實作與多檔案修改；支援 JSON schema | 同上 |
| opencode | 使用指定 provider、model 或自訂 agent | 同上，但不支援 schema |
| pi | 可限制為 read / grep 等唯讀工具 | 同上，但不支援 schema |
| Shell | 執行測試、lint、build 或其他命令 | `stdout`、`exit_code` |
| Condition | 安全求值後選擇 `true` / `false` 出口 | 判斷結果 |
| Git | commit 目前變更，或刷新 diff | `sha`、`diff` |

每個節點還可設定：

- `mutates`：是否會修改工作目錄；影響平行排程方式。
- `max_visits`：單次 run 中最多執行幾次。
- `timeout_sec`：節點 timeout。
- `join`：多條入邊要等待 `all` 或任一條 `any`。
- `on_error`：失敗時中止整個 run，或繼續交給下游條件判斷。

## Prompt 與條件

Prompt、Shell 指令和部分節點設定支援 Jinja 模板：

```jinja
依照以下計畫實作，這是第 {{ loop.iteration }} 輪：

{{ nodes.plan.last_message }}

{% if nodes.qa.structured %}
上一輪 QA 問題：
{% for issue in nodes.qa.structured.issues %}
- {{ issue }}
{% endfor %}
{% endif %}
```

常用變數：

- `requirement`
- `nodes.<id>.last_message`
- `nodes.<id>.structured`
- `nodes.<id>.exit_code`
- `nodes.<id>.files`
- `run.id`、`run.repo`、`run.branch`
- `run.diff`、`run.changed_files`
- `loop.iteration`

Condition 使用白名單運算式求值，不使用 Python `eval`：

```text
nodes.qa.structured.verdict == 'PASS' and nodes.tests.exit_code == 0
```

要建立重試迴圈，將 Condition 的 `false` 出口連回要重跑的上游節點即可。引擎用三層
限制防止無限迴圈：節點 `max_visits`、run 的 `max_run_steps`，以及 run timeout。

## 隔離模式

隔離模式可在工作流的 `settings.isolation` 設定。

### `shared`（預設）

整個 run 共用一個 worktree 和 `task/<run-id>` branch。

- 節點可直接接續前一步留下的工作目錄狀態。
- 唯讀節點可以平行執行。
- `mutates=true` 的節點共用寫入鎖，因此會序列執行。
- run 成功結束時，尚未 commit 的成果會自動 commit 到 task branch。
- 結構簡單，適合線性流程與一般 QA 重試。

### `per_node`

每個節點使用自己的 worktree 和 `node/<run-id>/<node-id>` branch。

- 寫入節點也能真正平行執行。
- 節點完成後必須自動 commit，才能把狀態交給下游。
- fan-in 會合併多個上游 commit；若內容衝突，run 會明確失敗並列出衝突。
- run 結束時整合所有最終結果，讓 `task/<run-id>` 代表完整成果。
- 磁碟用量較高，約為「節點 worktree 數量 × repository 大小」。

若流程主要是線性接力，選 `shared`；只有確實需要多個寫入節點同時工作時，再考慮
`per_node`，並讓平行分支負責不同檔案或模組。

> Git worktree 可以隔離檔案與 branch，但不是作業系統安全沙箱。Agent CLI 的權限、
> 可用工具與憑證仍應依工作內容採最小權限設定。

## 隨附範本

| ID | 用途 |
|---|---|
| `sample-project-notes` | 第一次試跑用；分析 repository 並產生 `PROJECT_NOTES.md` |
| `sample-opencode-pi` | opencode 實作、pi 唯讀審查，失敗則打回重做 |
| `plan-impl-qa` | Codex 規劃、Claude 實作、測試、Codex QA 與修正迴圈 |
| `codex-review` | Codex 審查目前 branch，交給 Claude 修正後再次審查 |

可以在新增專案時透過 UI 匯入，也可以使用命令列：

```bash
# 列出範本與已註冊專案
.venv/bin/python -m tools.templates

# 查看專案內現有工作流
.venv/bin/python -m tools.templates --project <project-id>

# 匯入單一範本或全部範本
.venv/bin/python -m tools.templates --project <project-id> --import plan-impl-qa
.venv/bin/python -m tools.templates --project <project-id> --import all
```

匯入只會複製檔案，不會覆蓋同名工作流。複製後的 JSON 就是專案自己的版本，應由
專案自行 commit 與維護。

## Agent CLI 與憑證

| Adapter | 執行檔 | 結構化輸出 | 備註 |
|---|---|---:|---|
| Codex | `codex` | 是 | 唯讀節點建議使用 `read-only` sandbox |
| Claude Code | `claude` | 是 | 無人看顧的 Shell 操作需選擇合適的 permission mode |
| opencode | `opencode` | 否 | model 格式通常為 `provider/model` |
| pi | `pi` | 否 | 需先設定 provider 憑證，可用工具白名單限制為唯讀 |
| Shell | `bash` | 否 | 在節點 worktree 中執行 |

執行前建議先跑 `tools.doctor`。特別注意：

- Agent CLI 必須已完成登入或 provider 設定。
- Codex CLI 太舊時，可能無法使用 `~/.codex/config.toml` 指定的新模型。
- pi 常安裝在不屬於 `PATH` 的 Node/Bun 目錄；adapter 已提供常見候選路徑。
- worktree 不會帶入被 Git 忽略的 `.venv` 或 `node_modules`，測試節點需自行使用目標
  repository 可取得的環境或明確指定依賴位置。

## 新增 Adapter

接入新的 Agent CLI 通常只需要兩個檔案：

```text
adapters/<id>.yaml
adapters/normalizers/<id>.py
```

YAML 定義執行檔、參數、工作目錄、Prompt 傳遞方式、UI 欄位與能力；normalizer 將 CLI
的實際輸出轉成統一事件。

先以 probe 捕捉真實 CLI 輸出，再據此實作 normalizer：

```bash
.venv/bin/python -m tools.probe <adapter-id>
.venv/bin/python -m pytest tests/test_normalizers.py -q
```

Fixture 的來源與重新產生方式記錄在 `tests/fixtures/README.md`。若 CLI 不在 `PATH`，
可在 adapter YAML 的 `binary_candidates` 列出候選路徑。

## API 概覽

所有工作流與 run 都屬於特定專案：

```text
GET    /api/projects
POST   /api/projects
DELETE /api/projects/<project-id>

GET    /api/projects/<project-id>/workflows
GET    /api/projects/<project-id>/workflows/<workflow-id>
POST   /api/projects/<project-id>/workflows
DELETE /api/projects/<project-id>/workflows/<workflow-id>
POST   /api/projects/<project-id>/workflows/import/<template-id>

POST   /api/projects/<project-id>/runs
GET    /api/projects/<project-id>/runs
GET    /api/projects/<project-id>/runs/<run-id>
GET    /api/projects/<project-id>/runs/<run-id>/diff
GET    /api/projects/<project-id>/runs/<run-id>/artifacts
GET    /api/projects/<project-id>/runs/<run-id>/events
POST   /api/projects/<project-id>/runs/<run-id>/cancel

GET    /api/runs
GET    /api/templates
POST   /api/workflows/validate
```

`/events` 使用 SSE；事件會先寫入 SQLite，因此瀏覽器可用 `Last-Event-ID` 或 `after`
參數補回中斷期間漏掉的內容。

## 開發與測試

```bash
# 全部測試；測試不會呼叫付費 LLM
.venv/bin/python -m pytest -q

# 擷取特定 Agent CLI 的真實事件格式
.venv/bin/python -m tools.probe <adapter-id>

# 檢查環境、CLI 與專案
.venv/bin/python -m tools.doctor
```

引擎測試使用 mock adapter，但仍走與真實 Agent 相同的 subprocess、逐行讀取、事件
normalization、timeout 與取消路徑。

## 程式結構

```text
app.py                       Flask 頁面、REST API、SSE
settings.py                  工具層設定載入與合併
engine/
  project.py                 專案初始化與 .ai-workflow-proj 設定
  graph.py                   圖形解析、驗證、環與 join 規則
  runner.py                  節點排程、分支、迴圈、timeout 與取消
  isolation.py               shared / per_node 執行策略
  executor.py                Agent subprocess 與串流事件收集
  context.py                 Jinja context 與安全條件求值
  workspace.py               Git worktree、branch、diff、commit 與合併
  bus.py                     SQLite 事件持久化與 SSE 訂閱扇出
  service.py                 背景 run 生命週期與收尾
store/
  projects.py                中央專案註冊表
  workflows.py               專案內 workflow JSON 存取
  stores.py                  每專案 Store 管理與跨專案 run 彙整
  db.py                      run、node_run、event 的 SQLite 存取
adapters/                    CLI 規格與 normalizers
static/js/
  project.js                 專案選擇與管理
  editor.js                  工作流編輯器
  graph.js                   Drawflow 與引擎圖格式轉換
  run.js                     即時執行畫面
workflows/                   可供匯入的隨附範本
tools/                       doctor、probe、templates、watch
tests/                       引擎、API、隔離、adapter 與資料層測試
```

引擎只接受自己的正規圖格式；只有 `static/js/graph.js` 知道 Drawflow 的資料結構。
未來若更換畫布套件，主要需要替換的是這一層轉換，而不是整個後端引擎。

## 目前限制

- 服務以 Flask 背景執行緒執行 run；服務重啟後，尚未完成的 run 會標記失敗，不會續跑。
- `shared` 模式無法讓兩個寫入節點真正平行。
- `per_node` 模式可能產生 Git merge conflict，且需要更多磁碟空間。
- worktree 中不會自然出現 Git ignored 的依賴目錄。
- 目前定位是本機工具，沒有多使用者認證與遠端權限模型。
