#!/bin/bash
# ============================================
# Pipeline 設定檔 — 依你的專案調整這裡就好
# ============================================

# 你實際的專案 repo 路徑(Codex / Claude Code 要操作的目標程式碼庫)
PROJECT_REPO="${PROJECT_REPO:-$HOME/my-project}"

# worktree 存放路徑(每個 task 會在這底下開一個獨立資料夾工作)
WORKTREE_ROOT="${WORKTREE_ROOT:-$HOME/ai-pipeline-worktrees}"

# 需求文件存放路徑
REQUIREMENTS_FILE="${REQUIREMENTS_FILE:-$PROJECT_REPO/requirements.md}"

# QA 最多重試修正的輪數,避免無限迴圈燒 token
MAX_QA_ROUNDS=3

# QA 通過後是否自動合併回 main
# true  = 全自動合併(建議先觀察一陣子,信任 pipeline 品質後再打開)
# false = 留在 task branch,由人工 review 後手動合併(建議初期設這個)
AUTO_MERGE=false

# 合併目標分支
MAIN_BRANCH="main"

# log 檔存放路徑
LOG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/logs"
