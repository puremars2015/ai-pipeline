-- 一個專案的執行紀錄。這個檔案建在
-- <專案>/.ai-workflow-proj/local/ai-workflow.sqlite —— 跟著專案走，
-- 但被 .gitignore 擋住，不會被 merge 進 main。
--
-- 工作流定義不在這裡：它們是 workflows/<id>.json（見 store/workflows.py），
-- 那些才是要進 git、要被 review、要跟同事共用的東西。
--
-- 「這台機器有哪些專案」也不在這裡，那是中央註冊表（schema_registry.sql）。

-- 每次執行
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    workflow_id   TEXT,
    workflow_name TEXT NOT NULL DEFAULT '',
    -- 這個 run 是在哪個專案上跑的。
    -- project_path 是路徑快照：專案之後被解除註冊，這段歷史仍然讀得懂
    -- （包括要去哪個 repo 算 diff）。
    project_id    TEXT NOT NULL DEFAULT '',
    project_path  TEXT NOT NULL DEFAULT '',
    -- 執行時的圖快照。之後編輯工作流不會改變已完成 run 的歷史。
    graph         TEXT NOT NULL,
    -- 需求在 run 建立時就固定下來，之後不會變。
    -- 舊 bash 的 4 號 bug：hook 傳的是工作目錄的即時檔案，背景執行期間
    -- 使用者改了 requirements.md 就會讀到錯的需求。
    requirement   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,          -- queued running passed failed cancelled
    reason        TEXT NOT NULL DEFAULT '',
    branch        TEXT NOT NULL DEFAULT '',
    base_sha      TEXT NOT NULL DEFAULT '',
    worktree      TEXT NOT NULL DEFAULT '',
    steps         INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs (created_at DESC);

-- 每個節點在某次 run 中的結果
CREATE TABLE IF NOT EXISTS node_runs (
    run_id       TEXT NOT NULL,
    node_id      TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    node_type    TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,
    visits       INTEGER NOT NULL DEFAULT 0,
    last_message TEXT NOT NULL DEFAULT '',
    structured   TEXT,                    -- JSON
    session_id   TEXT NOT NULL DEFAULT '',
    exit_code    INTEGER,
    files        TEXT NOT NULL DEFAULT '[]',  -- JSON
    usage        TEXT NOT NULL DEFAULT '{}',  -- JSON
    PRIMARY KEY (run_id, node_id)
);

-- 事件流。持久化才能在重新整理頁面 / 重啟服務後回放整個 run。
CREATE TABLE IF NOT EXISTS events (
    run_id  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    node_id TEXT NOT NULL DEFAULT '',
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL,
    text    TEXT NOT NULL DEFAULT '',
    data    TEXT NOT NULL DEFAULT '{}',   -- JSON
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events (run_id, seq);
