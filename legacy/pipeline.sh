#!/bin/bash
# ============================================
# 主 pipeline: Codex 規劃 → Claude Code 實作 → Codex QA
# 用法:
#   ./pipeline.sh "<需求描述>"              # 手動直接跑
#   ./pipeline.sh <task-id> <requirements檔路徑>  # 由 hook 自動呼叫
# ============================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.sh"

# ---- 解析參數:支援「手動丟需求文字」或「hook 帶 task-id + 檔案」兩種呼叫方式 ----
if [ -f "$2" ] 2>/dev/null; then
  TASK_ID="$1"
  REQ_FILE="$2"
else
  TASK_ID="manual-$(date +%s)"
  REQ_FILE="/tmp/req-${TASK_ID}.md"
  echo "$1" > "$REQ_FILE"
fi

TASK_BRANCH="task/${TASK_ID}"
WORKTREE_DIR="${WORKTREE_ROOT}/${TASK_ID}"

echo "===================================================="
echo "🎯 Task: $TASK_ID"
echo "📄 需求檔: $REQ_FILE"
echo "🌿 Branch: $TASK_BRANCH"
echo "===================================================="

# ---- 建立獨立 worktree,不干擾你手上正在跑的其他工作 ----
cd "$PROJECT_REPO"
git fetch origin "$MAIN_BRANCH" --quiet 2>/dev/null || true
git worktree add -b "$TASK_BRANCH" "$WORKTREE_DIR" "$MAIN_BRANCH"

cp "$REQ_FILE" "$WORKTREE_DIR/requirements.md"
cd "$WORKTREE_DIR"

# ---- Stage 1: Codex 規劃 ----
echo ""
echo "🧠 [Stage 1/3] Codex 規劃中..."
codex exec "讀取 requirements.md 的需求,產出詳細實作計畫,寫到 plan.md。
計畫需包含:
1. 任務拆解(每個子任務要改哪些檔案)
2. 明確的驗收標準(給後續 QA 用,條列式)
3. 需要特別注意的邊界情況
不要動任何程式碼,只產出 plan.md。"

git add plan.md requirements.md
git commit -m "plan: Codex 規劃 [$TASK_ID]" --quiet
echo "   ✅ plan.md 完成"

# ---- Stage 2: Claude Code 實作 ----
echo ""
echo "🔨 [Stage 2/3] Claude Code 實作中..."
claude -p "根據 plan.md 的計畫進行實作。完成後把做了什麼、
跟計畫有沒有出入、為什麼,寫到 notes.md。" \
  --permission-mode acceptEdits

git add -A
git commit -m "impl: Claude Code 實作 [$TASK_ID]" --quiet
echo "   ✅ 實作完成"

# ---- Stage 3: Codex QA(可重試修正) ----
echo ""
echo "🔍 [Stage 3/3] Codex QA 審查中..."
QA_PASSED=false

for round in $(seq 1 "$MAX_QA_ROUNDS"); do
  echo "   --- QA 第 $round 輪 ---"
  git diff "$MAIN_BRANCH"...HEAD -- . ':!plan.md' ':!notes.md' ':!qa-report.md' > changes.diff

  codex exec "這是根據 plan.md 計畫所做的程式碼變更(changes.diff)。
plan.md 是原始計畫,裡面有驗收標準。
請對照驗收標準做完整 code review + QA,檢查:
1. 是否符合需求
2. 有沒有明顯 bug、邊界情況沒處理
3. 是否偏離計畫,偏離是否合理
把審查結果寫到 qa-report.md,第一行必須是 PASS 或 FAIL。"

  git add qa-report.md changes.diff
  git commit -m "qa: 第 ${round} 輪審查 [$TASK_ID]" --quiet

  if head -1 qa-report.md | grep -qi "^PASS"; then
    QA_PASSED=true
    echo "   ✅ QA 通過(第 $round 輪)"
    break
  fi

  echo "   ⚠️  QA 未通過,交回 Claude Code 修正..."
  if [ "$round" -lt "$MAX_QA_ROUNDS" ]; then
    claude -p "QA 審查有發現問題,內容如下,請針對問題修正程式碼:

$(cat qa-report.md)" --permission-mode acceptEdits

    git add -A
    git commit -m "fix: 根據第 ${round} 輪 QA 修正 [$TASK_ID]" --quiet
  fi
done

# ---- 收尾 ----
echo ""
echo "===================================================="
if [ "$QA_PASSED" = true ]; then
  echo "✅ Pipeline 完成,QA 通過"
  echo "   Branch: $TASK_BRANCH"
  echo "   Worktree: $WORKTREE_DIR"

  if [ "$AUTO_MERGE" = true ]; then
    cd "$PROJECT_REPO"
    git checkout "$MAIN_BRANCH"
    git merge --no-ff "$TASK_BRANCH" -m "merge: $TASK_ID (auto, QA passed)"
    echo "   🔀 已自動合併到 $MAIN_BRANCH"
  else
    echo "   👉 請人工檢查後手動合併:"
    echo "      cd $PROJECT_REPO && git merge --no-ff $TASK_BRANCH"
  fi
else
  echo "❌ 達到最大重試輪數($MAX_QA_ROUNDS)仍未通過 QA"
  echo "   請人工介入檢查: $WORKTREE_DIR"
  echo "   最後一份 QA 報告: $WORKTREE_DIR/qa-report.md"
fi
echo "===================================================="
