# AI Pipeline: Codex 規劃 → Claude Code 實作 → Codex QA

人工輸入需求(可選)→ Codex 規劃 → Claude Code 實作 → Codex QA,
QA 沒過就自動打回去修正,最多重試 N 輪。每個需求在獨立的 git worktree +
task branch 上執行,不會互相干擾,也不影響你手上正在做的其他工作。

## 檔案結構

```
ai-pipeline/
├── config.sh                          # 所有可調參數在這裡改
├── setup.sh                           # 第一次使用時執行一次
├── pipeline.sh                        # 核心邏輯:規劃→實作→QA
├── hooks/post-commit                  # git hook,監控需求檔變更
├── requirements.template.md           # 需求檔範本
└── logs/                              # 每次 pipeline 執行的 log
```

## 安裝

1. 修改 `config.sh`,把 `PROJECT_REPO` 改成你實際的專案路徑
2. 執行:
   ```bash
   ./setup.sh
   ```
   這會:安裝 git hook 到你的專案、建立 worktree 目錄、
   若專案內沒有 `requirements.md` 會建一份範本。

## 使用方式

### 方式一:自動觸發(需求驅動)

```bash
cd $PROJECT_REPO
# 編輯 requirements.md,寫下這次要做的需求
git add requirements.md
git commit -m "需求: 加上月對月比較圖表"
```

commit 完成後,hook 會偵測到 `requirements.md` 有變動,
自動在背景啟動 pipeline。可用以下指令追蹤進度:

```bash
tail -f ai-pipeline/logs/<commit-hash>.log
```

### 方式二:手動直接跑

```bash
./pipeline.sh "在 SP3 用電量儀表板加上月對月比較的折線圖"
```

## Pipeline 每個 task 會做什麼

1. **建立獨立 worktree**(`$WORKTREE_ROOT/<task-id>`),
   在乾淨的 `task/<task-id>` branch 上工作,不動你目前的工作目錄。
2. **Codex 規劃**:讀需求,產出 `plan.md`(含任務拆解 + 驗收標準),不動程式碼。
3. **Claude Code 實作**:根據 `plan.md` 動手改程式碼,寫 `notes.md` 記錄跟計畫的出入。
4. **Codex QA**:對照 `plan.md` 的驗收標準,審查 `git diff`(不是聽 Claude 自我報告),
   寫 `qa-report.md`,第一行是 `PASS` 或 `FAIL`。
5. 若 `FAIL`,把 QA 報告丟回 Claude Code 修正,重跑第 4 步,最多 `MAX_QA_ROUNDS` 輪。
6. QA 通過後:
   - `AUTO_MERGE=false`(預設,建議初期使用):留在 task branch,印出手動合併指令。
   - `AUTO_MERGE=true`:自動合併回 `main`。

## 重要注意事項

- **先用 `AUTO_MERGE=false` 觀察一陣子**,確認 QA 品質可信任後再考慮打開自動合併。
- **每個 task 都是獨立 worktree**,即使多個需求同時進來也不會互相干擾;
  但 Codex / Claude Code CLI 呼叫本身是序列執行(pipeline.sh 內是同步跑),
  如果要真正平行跑多個 task,需要另外處理併發(例如用 queue 或個別背景程序管理)。
- **QA 是看 `git diff`**,不是看 Claude 自己講了什麼,這是避免球員兼裁判的關鍵設計。
- 需要 `codex` 和 `claude` 兩個 CLI 都已安裝、且已完成登入驗證。
- `config.sh` 裡的 `MAX_QA_ROUNDS` 建議先設 2-3,避免無限迴圈燒 token。

## 調整驗收標準的寫法

`plan.md` 的驗收標準品質直接決定 QA 準不準。建議在 `requirements.md`
裡把「補充限制」寫清楚(效能、風格、不能動到的既有邏輯等),
Codex 規劃時才會把這些轉成具體、可檢查的驗收標準。
