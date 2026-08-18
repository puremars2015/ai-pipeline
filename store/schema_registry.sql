-- 專案註冊表：這台機器上有哪些專案。
--
-- 刻意只有這一張表。工作流住在各專案的 .ai-workflow-proj/workflows/，
-- 執行紀錄住在各專案的 .ai-workflow-proj/local/ai-workflow.sqlite；
-- 中央資料庫只回答「有哪些專案」這一個問題。
--
-- path 是解析過的 git repo 根目錄，UNIQUE 擋掉「同一個專案用不同寫法
-- （相對路徑、~、symlink）註冊兩次」——那會變成兩份互相看不見的歷史。
CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL DEFAULT '',
    added_at     REAL NOT NULL,
    last_used_at REAL
);
