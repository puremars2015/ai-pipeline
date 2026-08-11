#!/bin/bash
# ============================================
# 初始化:把 pipeline 接到你的專案 repo
# 用法: ./setup.sh
# ============================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.sh"

echo "== 檢查必要工具 =="
for cmd in git codex claude; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "⚠️  找不到指令: $cmd,請先安裝並確認可在終端機直接呼叫"
  else
    echo "✅ $cmd 已安裝"
  fi
done
echo ""

if [ ! -d "$PROJECT_REPO/.git" ]; then
  echo "❌ 找不到 git repo: $PROJECT_REPO"
  echo "   請先修改 config.sh 裡的 PROJECT_REPO,指到你實際的專案路徑"
  exit 1
fi

mkdir -p "$WORKTREE_ROOT" "$LOG_DIR"
chmod +x "$SCRIPT_DIR/pipeline.sh"

# 安裝 post-commit hook 到目標 repo
cp "$SCRIPT_DIR/hooks/post-commit" "$PROJECT_REPO/.git/hooks/post-commit"
chmod +x "$PROJECT_REPO/.git/hooks/post-commit"

# 讓 hook 知道 pipeline 腳本放在哪(避免寫死路徑)
echo "$SCRIPT_DIR" > "$PROJECT_REPO/.git/hooks/.ai-pipeline-dir"

# requirements.md 不存在就建一個範本
if [ ! -f "$REQUIREMENTS_FILE" ]; then
  cp "$SCRIPT_DIR/requirements.template.md" "$REQUIREMENTS_FILE"
  echo "📝 已建立範本需求檔: $REQUIREMENTS_FILE"
fi

echo ""
echo "✅ 設定完成"
echo "   目標 repo:     $PROJECT_REPO"
echo "   worktree 目錄: $WORKTREE_ROOT"
echo "   需求檔:        $REQUIREMENTS_FILE"
echo ""
echo "接下來的使用方式:"
echo "  自動模式: 編輯 $REQUIREMENTS_FILE,git add + commit,pipeline 會自動在背景觸發"
echo "  手動模式: 直接執行 ./pipeline.sh \"你的需求描述\""
echo "  查看進度: tail -f $LOG_DIR/<commit-hash>.log"
