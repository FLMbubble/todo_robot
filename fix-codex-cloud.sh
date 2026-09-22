#!/bin/bash
# fix-codex-cloud.sh — 修复 codex-cross-platform/codex-local-cloud.sh 的三个问题
#
# 用法: bash fix-codex-cloud.sh

set -euo pipefail

SCRIPT="/Users/didi/Documents/project/codex-cross-platform/codex-local-cloud.sh"

echo "🔧 修复 codex-local-cloud.sh..."

# 备份
cp "$SCRIPT" "$SCRIPT.bak.$(date +%s)"

# 1. 移除无效的 experimental_thread_store 行
if grep -q 'experimental_thread_store' "$SCRIPT"; then
    sed -i '' '/experimental_thread_store/d' "$SCRIPT"
    echo "  ✓ 移除了无效的 experimental_thread_store=\"local\" 配置"
fi

# 2. 在 nohup 前添加独立 CODEX_HOME（避免 sqlite 锁冲突）
if ! grep -q 'CODEX_CLOUD_HOME' "$SCRIPT"; then
    # 找到 nohup 行，在前面插入 CODEX_HOME 设置
    sed -i '' '/nohup "\$CODEX_BIN" app-server/i\
        CODEX_HOME="${CODEX_CLOUD_HOME:-/tmp/codex-cloud-home}"\
        mkdir -p "$CODEX_HOME"\
' "$SCRIPT"
    echo "  ✓ 添加了独立 CODEX_HOME 避免 sqlite 锁冲突"
fi

# 3. 在 is_port_free 检查后，添加端口被 VSCode 占用时自动切换
# (codex-local-cloud.sh 已有 find_free_port 逻辑，无需额外修改)

echo ""
echo "✅ 修复完成！"
echo ""
echo "现在可以用原始脚本启动："
echo "  cd /Users/didi/Documents/project/codex-cross-platform"
echo "  ./codex-local-cloud.sh start"
echo ""
echo "或者使用简化版："
echo "  bash /Users/didi/Documents/work_skills/todo_robot/start-codex-cloud.sh"
