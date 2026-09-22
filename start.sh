#!/bin/bash
# TODO Robot 悬浮球启动脚本
cd "$(dirname "$0")"

# 检查 dws 二进制
if [ ! -x ".dws/dws" ]; then
    echo "📦 复制 dws 二进制..."
    mkdir -p .dws
    cp ~/.SmartWork/skills/smartwork-cli/smartwork-shared/assets/dws-darwin-arm64 .dws/dws 2>/dev/null || \
    cp ~/.SmartWork/skills/smartwork-cli/smartwork-shared/assets/dws-linux-x64 .dws/dws 2>/dev/null
    chmod +x .dws/dws
fi

# 确保数据目录存在
mkdir -p data

# 杀掉旧实例
echo "🔄 重启服务 (端口 8500)..."
PIDS=$(lsof -ti :8500 2>/dev/null)
if [ -n "$PIDS" ]; then
    echo "  发现旧进程: $PIDS"
    kill $PIDS 2>/dev/null
    sleep 1
    PIDS=$(lsof -ti :8500 2>/dev/null)
    if [ -n "$PIDS" ]; then
        echo "  强制终止: $PIDS"
        kill -9 $PIDS 2>/dev/null
        sleep 1
    fi
fi

# 启动 (后台运行，不受终端关闭影响)
nohup python3 app.py > /tmp/todo_robot.log 2>&1 &
echo "PID: "
sleep 2
if lsof -ti :8500 > /dev/null 2>&1; then
    echo "✅ 服务已启动: http://localhost:8500"
else
    echo "❌ 启动失败，查看日志: /tmp/todo_robot.log"
    cat /tmp/todo_robot.log
fi
