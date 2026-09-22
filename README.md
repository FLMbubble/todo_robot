# TODO Robot 桌面悬浮球

一个桌面悬浮球待办管理应用，聚合 **D-Chat 待办**、**D-Chat 日历**、**Cooper 文档**（日报/周报/OKR）数据，支持待办的增删改查和状态切换，并能将待办同步写回 Cooper 日报和周报文档。

## 架构

```
悬浮球 UI (HTML/CSS/JS)  ←→  Python HTTP Server (app.py)  ←→  dws CLI  ←→  D-Chat / Cooper
```

- **后端**: Python stdlib HTTP server，封装 `dws` CLI 调用
- **前端**: 悬浮球 HTML/CSS/JS 面板，支持拖拽定位、4 个 Tab 页
- **数据源**: 通过 `dws` CLI 访问 D-Chat 待办/日历和 Cooper 文档

## 功能

| 功能 | 说明 |
|------|------|
| 📝 待办管理 | 创建/完成/编辑/删除 D-Chat 待办，支持优先级 |
| 📅 日程查看 | 查看今日 D-Chat 日历事件 |
| 📄 文档浏览 | 查看 Cooper 日报/周报/OKR 文档内容 |
| 🎯 OKR | 查看 OKR 相关文档 |
| 📥 同步日报 | 将当前待办同步写入/追加到今日日报 Cooper 文档 |
| 📥 同步周报 | 将待办按日期分组同步到周报 Cooper 文档 |
| 🔔 徽标提示 | 悬浮球显示未完成待办数量 |
| 🔄 自动刷新 | 每 5 分钟自动刷新数据 |

## 使用方法

```bash
cd /path/to/todo_robot
./start.sh
```

浏览器自动打开 `http://localhost:8443`，点击悬浮球展开面板。

### 本地待办存储

待办数据按日期存储在 `data/` 目录下，文件格式为 `todos-YYYY-MM-DD.json`。

- 添加待办时选择「📋 本地」目标，数据将存入本地文件
- 点击「📤 同步到 Cooper 日报」按钮将本地待办同步到 Cooper 文档
- 本地数据独立于 D-Chat 待办和 Cooper 文档，可离线使用

## 文件结构

```
todo_robot/
├── app.py          # Python 后端 (HTTP server + dws CLI 封装 + 同步逻辑)
├── index.html      # 悬浮球前端 UI (HTML/CSS/JS)
├── start.sh        # 启动脚本
├── data/           # 本地待办数据 (按日期存储 JSON 文件)
│   └── todos-YYYY-MM-DD.json
├── .dws/dws        # dws CLI 二进制
└── README.md
```

## API 端点

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/summary` | 一次性获取所有面板数据 |
| GET | `/api/todos` | 获取待办列表 |
| GET | `/api/calendar` | 获取日历事件 |
| GET | `/api/docs/daily` | 获取日报文档列表 |
| GET | `/api/docs/weekly` | 获取周报文档列表 |
| GET | `/api/docs/okr` | 获取 OKR 文档列表 |
| GET | `/api/docs/content?id=X` | 获取文档正文 |
| POST | `/api/todos/create` | 创建待办 |
| POST | `/api/todos/update` | 更新待办 |
| POST | `/api/todos/complete` | 完成/恢复待办 |
| POST | `/api/todos/delete` | 删除待办 |
| POST | `/api/sync/daily` | 同步待办到日报 |
| POST | `/api/sync/weekly` | 同步待办到周报 |
| POST | `/api/docs/create` | 创建 Cooper 文档 |
| GET | `/api/local/todos` | 获取本地待办列表 |
| GET | `/api/local/dates` | 列出所有有数据的日期 |
| POST | `/api/local/todos/create` | 创建本地待办 |
| POST | `/api/local/todos/update` | 更新本地待办 |
| POST | `/api/local/todos/toggle` | 切换本地待办完成状态 |
| POST | `/api/local/todos/delete` | 删除本地待办 |
| POST | `/api/local/sync` | 同步本地待办到 Cooper 日报 |

## 配置

在 `app.py` 顶部可修改：
- `PORT`: 服务端口 (默认 8420)
- `TIMEZONE`: 时区 (默认 Asia/Shanghai)
- `DAILY_DOC_PATTERN`: 日报文档标题正则 (默认 `MM.DD`)
- `WEEKLY_DOC_PATTERN`: 周报文档标题正则
- `OKR_DOC_PATTERN`: OKR 文档标题正则
