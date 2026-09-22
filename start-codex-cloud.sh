#!/bin/bash
# start-codex-cloud.sh — 快速启动 Codex 云端 app-server
#
# 解决的问题：
#   1. experimental_thread_store="local" 配置无效 → 移除
#   2. sqlite 锁冲突 → 使用独立 CODEX_HOME (/tmp/codex-cloud-home)
#   3. 端口 8421 被 VSCode 占用 → 自动切换到 8430
#   4. 后台进程被 PTY 会话杀死 → 使用 Python start_new_session
#
# 用法:
#   bash start-codex-cloud.sh              # 默认端口 8430 + 建立隧道
#   bash start-codex-cloud.sh 8430         # 指定端口
#   bash start-codex-cloud.sh 8430 notunnel # 不建立隧道

set -euo pipefail

PORT="${1:-8430}"
MODE="${2:-tunnel}"
SERVERS_FILE="/Users/didi/Documents/project/codex-cross-platform/codex-cloud-servers.conf"
TOKEN_FILE="$HOME/.codex/ws-token"
CLOUD_HOME="/tmp/codex-cloud-home"
PID_FILE="/tmp/codex-cloud-app-server.pid"
LOG_FILE="/tmp/codex-cloud-app-server.log"

detect_codex() {
    local candidates=()
    [ -x "/Applications/ChatGPT.app/Contents/Resources/codex" ] && candidates+=("/Applications/ChatGPT.app/Contents/Resources/codex")
    local vscode_codex
    vscode_codex=$(ls ~/.vscode/extensions/openai.chatgpt-*/bin/*/codex 2>/dev/null | head -1)
    [ -n "$vscode_codex" ] && candidates+=("$vscode_codex")
    command -v codex >/dev/null 2>&1 && candidates+=("$(command -v codex)")
    local best="" best_ver=""
    for bin in "${candidates[@]}"; do
        local ver
        ver=$("$bin" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
        if [ -n "$ver" ]; then
            if [ -z "$best_ver" ] || printf '%s\n%s\n' "$ver" "$best_ver" | sort -V | head -1 | grep -q "$best_ver"; then
                best="$bin"; best_ver="$ver"
            fi
        fi
    done
    echo "${best:-codex}"
}

update_remote_connect_script() {
    local ssh_target="$1" token="$2" port="$3"
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$ssh_target" \
        "cat > ~/codex-connect.sh << 'CONN'
#!/bin/bash
export PATH=\"\$HOME/.local/bin:\$PATH\"
export CODEX_WS_TOKEN=${token}
exec codex --remote ws://127.0.0.1:${port} --remote-auth-token-env CODEX_WS_TOKEN -c history_mode=\"legacy\" \"\$@\"
CONN
chmod +x ~/codex-connect.sh" 2>/dev/null || true
}

start_tunnels() {
    local port="$1" token="$2"
    local tunnel_dir="/tmp/codex-cloud-tunnels"
    mkdir -p "$tunnel_dir"
    if [ ! -f "$SERVERS_FILE" ]; then return; fi
    echo ""
    echo "🔗 启动 SSH 隧道..."
    while IFS='|' read -r name host user enabled; do
        [[ "$name" =~ ^#.*$ ]] && continue
        [[ -z "$name" ]] && continue
        [[ "$enabled" != "yes" ]] && continue
        local ssh_target="${user:+$user@}$host"
        echo "   [$name] → $ssh_target"
        # 尝试建立隧道（如果已存在会报 warning，忽略即可）
        ssh -o BatchMode=yes -o ConnectTimeout=10 -fN -R "${port}:127.0.0.1:${port}" "$ssh_target" 2>/dev/null || true
        # 更新远程 codex-connect.sh
        update_remote_connect_script "$ssh_target" "$token" "$port"
        echo "   ✓ [$name] codex-connect.sh 已更新 (端口 $port)"
    done < "$SERVERS_FILE"
}

# 准备 token
if [ ! -f "$TOKEN_FILE" ] || [ ! -s "$TOKEN_FILE" ]; then
    echo "🔑 生成新的 WS token..."
    python3 -c "import secrets; print(secrets.token_hex(32))" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
fi
TOKEN=$(cat "$TOKEN_FILE")

# 0. 检查是否已运行
EXISTING_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://127.0.0.1:$PORT/healthz" 2>/dev/null || echo "000")
if [ "$EXISTING_HEALTH" = "200" ]; then
    echo "✓ app-server 已在运行 (端口 $PORT, health=200)"
    if [ "$MODE" = "tunnel" ]; then
        start_tunnels "$PORT" "$TOKEN"
    fi
    echo ""
    echo "🎉 远程服务器执行: bash codex-connect.sh"
    exit 0
fi

# 1. 杀掉旧实例
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null)
    if [ -n "$OLD_PID" ] && lsof -p "$OLD_PID" >/dev/null 2>&1; then
        echo "🔄 停止旧 app-server (PID $OLD_PID)..."
        kill "$OLD_PID" 2>/dev/null || true
        sleep 2
    fi
fi

# 2. 检查端口是否被占用
if lsof -i ":$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "⚠ 端口 $PORT 被占用"
    for p in 8430 8431 8432 8433 8434 8435 8436 8437 8438 8439; do
        if ! lsof -i ":$p" -sTCP:LISTEN >/dev/null 2>&1; then
            PORT="$p"
            echo "  → 使用端口 $PORT"
            break
        fi
    done
fi

# 3. 准备独立 CODEX_HOME
mkdir -p "$CLOUD_HOME"
echo "📦 CODEX_HOME: $CLOUD_HOME"

# 4. 启动 app-server
CODEX_BIN=$(detect_codex)
echo "🚀 启动 app-server: $CODEX_BIN (端口 $PORT)"

python3 << PYEOF
import subprocess, os, time, sys
env = os.environ.copy()
env['CODEX_HOME'] = '$CLOUD_HOME'
log = open('$LOG_FILE', 'w')
proc = subprocess.Popen(
    ['$CODEX_BIN', 'app-server',
     '--listen', 'ws://0.0.0.0:$PORT',
     '--ws-auth', 'capability-token',
     '--ws-token-file', os.path.expanduser('~/.codex/ws-token')],
    stdout=log, stderr=subprocess.STDOUT, env=env,
    start_new_session=True
)
with open('$PID_FILE', 'w') as f:
    f.write(str(proc.pid))
time.sleep(3)
import urllib.request
try:
    with urllib.request.urlopen('http://127.0.0.1:$PORT/healthz', timeout=5) as r:
        print(f'✅ app-server 启动成功 (PID {proc.pid}, health={r.status})')
        print(f'   WebSocket: ws://127.0.0.1:$PORT')
        print(f'   日志: tail -f $LOG_FILE')
except Exception as e:
    print(f'❌ app-server 启动失败: {e}', file=sys.stderr)
    with open('$LOG_FILE') as f:
        print(f.read(), file=sys.stderr)
    sys.exit(1)
PYEOF

# 5. 启动 SSH 隧道
if [ "$MODE" = "tunnel" ]; then
    start_tunnels "$PORT" "$TOKEN"
fi

echo ""
echo "🎉 完成！远程服务器执行: bash codex-connect.sh"
echo "   连接地址: ws://127.0.0.1:$PORT"
