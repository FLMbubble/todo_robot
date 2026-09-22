#!/usr/bin/env python3
"""
TODO Robot — 桌面悬浮球待办管理应用
数据源: Cooper文档(日报/周报/OKR) + D-Chat会话 + D-Chat日历
后端: Python stdlib HTTP server 封装 dws CLI
前端: HTML/CSS/JS 悬浮球 UI
"""
import json
import os
import subprocess
import sys
import threading
import time
import re
import http.server
import socketserver
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor

# ---- 配置 ----
DWS_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dws", "dws")
TIMEZONE = "Asia/Shanghai"
SHANGHAI_TZ = timezone(timedelta(hours=8))
PORT = 8511

# ---- 日报/周报文档映射配置 (可按需修改) ----
# 通过日期自动生成日报文档标题，周报同理
# 日报标题格式: "MM.DD"  周报标题格式: "WW 周报"
# OKR文档标题格式包含 "OKR" 或 "季度OKR"

DAILY_DOC_PATTERN = r"^\d{2}\.\d{2}$"          # 如 09.20

# 日报文档存放的 Cooper 个人空间文件夹配置
# 结构: 个人空间(space-id=0) → Daily 文件夹 → YYYY.MM 月份文件夹
DAILY_SPACE_ID = 0                                    # 个人空间
DAILY_ROOT_FOLDER_ID = 2209182943521                   # "Daily" 文件夹
# 月份子文件夹会在同步时自动查找或创建 (如 "2026.09")

# 记录最近同步创建的日报文档 ID (date_str -> resource_id)
_RECENT_SYNCED_DOCS = {}
WEEKLY_DOC_PATTERN = r"周报|weekly|week"       # 如 38周报 / 周报 / weekly
OKR_DOC_PATTERN = r"OKR|okr|季度|目标"          # 季度OKR



# ---- 缓存层 ----
_CACHE = {}
_CACHE_TTL = 60  # 秒：文档列表/日历等缓存有效期
_CACHE_LOCK = threading.Lock()

def cache_get(key, ttl=_CACHE_TTL):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and (time.time() - entry[0]) < ttl:
            return entry[1]
    return None

def cache_set(key, value, ttl=_CACHE_TTL):
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() + ttl, value)

def cache_invalidate(key=None):
    """使缓存失效。key 带下划线后缀时，删除所有以该前缀开头的缓存项。"""
    with _CACHE_LOCK:
        if key:
            # 支持前缀匹配：如 "todos_" 匹配 "todos_None", "todos_2", "todos_3"
            if key.endswith("_"):
                prefix = key
                for k in list(_CACHE.keys()):
                    if k.startswith(prefix):
                        del _CACHE[k]
            else:
                _CACHE.pop(key, None)
        else:
            _CACHE.clear()


def run_dws(args, timeout=30):
    """执行 dws CLI 命令，返回解析后的 JSON dict"""
    cmd = [DWS_BIN] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        stdout = result.stdout.strip()
        if not stdout:
            return {"ok": False, "error": {"type": "empty", "message": result.stderr.strip()[:500] or "无输出"}}
        data = json.loads(stdout)
        return data
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"type": "timeout", "message": "命令执行超时"}}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": {"type": "parse_error", "message": f"JSON解析失败: {e}", "raw": stdout[:500]}}
    except Exception as e:
        return {"ok": False, "error": {"type": "exec_error", "message": str(e)}}


def now_ms():
    return int(datetime.now(SHANGHAI_TZ).timestamp() * 1000)


def ms_to_str(ms, fmt="%H:%M"):
    if not ms or ms < 0:
        return ""
    return datetime.fromtimestamp(ms / 1000, SHANGHAI_TZ).strftime(fmt)


def ms_to_date_str(ms, fmt="%m-%d"):
    if not ms or ms < 0:
        return ""
    return datetime.fromtimestamp(ms / 1000, SHANGHAI_TZ).strftime(fmt)




# ==================== 本地数据存储 (JSON 文件) ====================
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

def _date_key(dt=None):
    """返回 YYYY-MM-DD 格式的日期键"""
    if dt is None:
        dt = datetime.now(SHANGHAI_TZ)
    if isinstance(dt, str):
        return dt
    return dt.strftime("%Y-%m-%d")

def _date_file(date_str=None):
    """返回指定日期的 JSON 文件路径"""
    date_str = date_str or _date_key()
    return os.path.join(DATA_DIR, f"todos-{date_str}.json")

def load_local_todos(date_str=None, filename=None):
    """加载指定日期的本地待办列表。filename 可指定具体文件名如 weekly-YYYY-MM-DD.json"""
    if filename:
        path = os.path.join(DATA_DIR, filename)
    else:
        path = _date_file(date_str)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("todos", [])
    except Exception:
        return []

def save_local_todos(todos, date_str=None):
    """保存指定日期的本地待办列表"""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _date_file(date_str)
    data = {
        "date": date_str or _date_key(),
        "updated_at": now_ms(),
        "todos": todos,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _next_local_id(todos):
    """生成下一个本地待办 ID"""
    max_id = 0
    for t in todos:
        tid = t.get("id", 0)
        if isinstance(tid, int) and tid > max_id:
            max_id = tid
    return max_id + 1

def add_local_todo(title, priority="none", summary="", progress="", date_str=None):
    """添加一条本地待办，返回新待办对象"""
    todos = load_local_todos(date_str)
    todo = {
        "id": _next_local_id(todos),
        "title": title,
        "summary": summary or "",
        "priority": priority,
        "status": "未完成",
        "completed": False,
        "progress": progress or "",
        "created_at": now_ms(),
        "source": "local",
    }
    todos.append(todo)
    save_local_todos(todos, date_str)
    return todo

def update_local_todo(todo_id, title=None, priority=None, summary=None, progress=None, date_str=None):
    """更新本地待办字段"""
    todos = load_local_todos(date_str)
    for t in todos:
        if t.get("id") == todo_id:
            if title is not None:
                t["title"] = title
            if priority is not None:
                t["priority"] = priority
            if summary is not None:
                t["summary"] = summary
            if progress is not None:
                t["progress"] = progress
            save_local_todos(todos, date_str)
            return t
    return None

def toggle_local_todo(todo_id, completed=None, date_str=None):
    """切换本地待办完成状态"""
    todos = load_local_todos(date_str)
    for t in todos:
        if t.get("id") == todo_id:
            if completed is not None:
                t["completed"] = completed
            else:
                t["completed"] = not t.get("completed", False)
            t["status"] = "已完成" if t["completed"] else "未完成"
            save_local_todos(todos, date_str)
            return t
    return None

def delete_local_todo(todo_id, date_str=None):
    """删除本地待办"""
    todos = load_local_todos(date_str)
    new_todos = [t for t in todos if t.get("id") != todo_id]
    if len(new_todos) == len(todos):
        return False
    save_local_todos(new_todos, date_str)
    return True

def list_local_dates():
    """列出所有有数据的日期，返回排序后的日期字符串列表"""
    if not os.path.exists(DATA_DIR):
        return []
    dates = []
    for fn in os.listdir(DATA_DIR):
        if fn.startswith("todos-") and fn.endswith(".json"):
            ds = fn[6:-5]
            dates.append(ds)
    dates.sort(reverse=True)
    return dates

def sync_local_to_cooper(date_str=None):
    """将本地待办同步到 Cooper 日报文档。
    策略:
    1. 查找今日日报文档 (如 "09.21")
    2. 若不存在则创建
    3. 确保 TO DO 标题和表格存在
    4. 清空旧表格行，重新写入本地待办
    返回操作结果 dict。
    """
    date_str = date_str or _date_key()
    todos = load_local_todos(date_str)

    # 从日期字符串构建日报标题: "2026-09-21" -> "09.21"
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        dt = datetime.now(SHANGHAI_TZ)
    today_title = dt.strftime("%m.%d")

    # 查找今日日报文档
    daily_docs = find_daily_report_docs()
    target_doc = None
    for doc in daily_docs:
        if doc.get("resourceName", "").strip() == today_title:
            target_doc = doc
            break

    # 如果没有找到，尝试搜索更广泛的日报文档
    if not target_doc and daily_docs:
        for doc in daily_docs:
            if today_title in doc.get("resourceName", ""):
                target_doc = doc
                break

    if not target_doc:
        # 创建新日报（含 TO DO 标题）
        full_content = f"# {today_title}\n\n# TO DO\n"
        month_folder = find_or_create_month_folder()
        res = create_doc(today_title, full_content, kind="cooper",
                         space_id=DAILY_SPACE_ID,
                         parent_id=month_folder or DAILY_ROOT_FOLDER_ID)
        resource_id = None
        if res.get("ok"):
            resource_id = res.get("data", {}).get("resourceId") or res.get("data", {}).get("id")
        cache_invalidate("recent_docs")

        if not resource_id:
            return {"action": "error", "error": "创建文档失败", "synced": 0}

        # 确保表格存在，然后逐条写入待办（与更新逻辑一致）
        ensure_todo_table(resource_id, app="cooper")
        synced = 0
        for t in todos:
            priority_map = {"high": "高", "medium": "中", "low": "低", "none": ""}
            pri = priority_map.get(t.get("priority", "none"), "")
            status = "已完成" if t.get("completed") else "未完成"
            progress = t.get("progress", "")
            add_res = add_doc_todo(resource_id, t.get("title", ""), priority=pri, status=status, progress=progress, app="cooper")
            if add_res.get("ok"):
                synced += 1
        _RECENT_SYNCED_DOCS[today_title] = resource_id
        return {"action": "created", "resource_id": resource_id, "title": today_title, "synced": synced, "total": len(todos)}

    resource_id = target_doc.get("resourceId")
    if not resource_id:
        return {"action": "error", "error": "目标文档无 resourceId", "synced": 0}

    # 确保有 TO DO 表格
    table_anchor = ensure_todo_table(resource_id, app="cooper")

    # 读取现有表格行数，逐行删除已有数据行
    existing = parse_doc_todos(resource_id, app="cooper")
    for et in reversed(existing):
        delete_doc_todo(resource_id, et.get("row_index"), app="cooper")

    # 将本地待办逐条写入表格
    synced = 0
    for t in todos:
        priority_map = {"high": "高", "medium": "中", "low": "低", "none": ""}
        pri = priority_map.get(t.get("priority", "none"), "")
        status = "已完成" if t.get("completed") else "未完成"
        progress = t.get("progress", "")
        res = add_doc_todo(resource_id, t.get("title", ""), priority=pri, status=status, progress=progress, app="cooper")
        if res.get("ok"):
            synced += 1

    cache_invalidate("recent_docs")
    return {"action": "updated", "resource_id": resource_id, "title": today_title, "synced": synced, "total": len(todos)}


# ==================== 数据源 API ====================

def get_todos(status=None):
    """获取 D-Chat 待办列表（带缓存）
    status: None=全部, 2=未完成, 3=已完成
    """
    cache_key = f"todos_{status}"
    cached = cache_get(cache_key, ttl=30)
    if cached is not None:
        return cached
    statuses = [status] if status is not None else [2, 3]
    all_items = []
    for s in statuses:
        res = run_dws([
            "todo", "list",
            "--status", str(s),
            "--page", "1",
            "--size", "100",
            "--timezone", TIMEZONE,
            "--order-type", "asc",
            "--output", "json"
        ])
        if res.get("ok"):
            items = res.get("data", {}).get("items", [])
            all_items.extend(items)
    cache_set(cache_key, all_items, ttl=30)
    return all_items


def get_todo_info(todo_id):
    """获取单个待办详情"""
    return run_dws(["todo", "info", str(todo_id), "--output", "json"])


def create_todo(title, priority="none", summary="", due_time=None):
    """创建待办"""
    args = ["todo", "create", title, "--priority", priority, "--timezone", TIMEZONE, "--output", "json"]
    if summary:
        args += ["--summary", summary]
    if due_time:
        args += ["--due-time", str(due_time)]
    return run_dws(args)


def update_todo(todo_id, title=None, priority=None, summary=None, due_time=None):
    """更新待办"""
    args = ["todo", "update", str(todo_id), "--output", "json"]
    if title:
        args += ["--title", title]
    if priority:
        args += ["--priority", priority]
    if summary is not None:
        args += ["--summary", summary]
    if due_time is not None:
        args += ["--due-time", str(due_time)]
    return run_dws(args)


def complete_todo(todo_id, status=3):
    """完成/标记待办状态"""
    return run_dws(["todo", "complete", str(todo_id), "--status", str(status), "--output", "json"])


def delete_todo(todo_id):
    """删除待办"""
    return run_dws(["todo", "delete", str(todo_id), "--output", "json"])


def get_recent_docs():
    """获取最近访问的 Cooper 文档（带缓存）"""
    cached = cache_get("recent_docs")
    if cached is not None:
        return cached
    res = run_dws(["doc", "search", "--output", "json"])
    if not res.get("ok"):
        return []
    data = res.get("data", {})
    # dws 返回嵌套结构 data.data.items
    if isinstance(data, dict):
        items = data.get("items") or data.get("data", {}).get("items", [])
    elif isinstance(data, str):
        return []
    else:
        items = []
    result = items if isinstance(items, list) else []
    cache_set("recent_docs", result)
    return result


def get_doc_content(resource_id, app="cooper"):
    """获取 Cooper 文档正文"""
    res = run_dws(["doc", "fetch", str(resource_id), "--app", app, "--output", "json"])
    if not res.get("ok"):
        return ""
    content = res.get("data", "")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return content.get("content", "") or content.get("text", "")
    return ""


def find_or_create_month_folder():
    """查找当月文件夹 (如 "2026.09")，不存在则创建。
    返回 folder_id 或 None。
    """
    month_title = datetime.now(SHANGHAI_TZ).strftime("%Y.%m")

    # 列出 Daily 根文件夹下的子文件夹
    res = run_dws([
        "space", "list",
        "--space-id", str(DAILY_SPACE_ID),
        "--parent-id", str(DAILY_ROOT_FOLDER_ID),
        "--output", "json"
    ], timeout=15)
    if not res.get("ok"):
        return None
    d = res.get("data", {})
    if isinstance(d, dict):
        items = d.get("data", {}).get("items", []) if isinstance(d.get("data"), dict) else d.get("items", [])
    else:
        items = []

    for item in items:
        name = item.get("display_name", "")
        if name == month_title and item.get("space_resource_type") == "DIR":
            return item.get("id")

    # 文件夹不存在，创建
    res = run_dws([
        "space", "folder-create",
        "--space-id", str(DAILY_SPACE_ID),
        "--parent-id", str(DAILY_ROOT_FOLDER_ID),
        month_title,
        "--output", "json"
    ], timeout=15)
    if res.get("ok"):
        d = res.get("data", {})
        if isinstance(d, dict):
            return d.get("id") or d.get("data", {}).get("id")
    return None


def list_folder_docs(folder_id):
    """列出某个文件夹下的所有文档"""
    if not folder_id:
        return []
    res = run_dws([
        "space", "list",
        "--space-id", str(DAILY_SPACE_ID),
        "--parent-id", str(folder_id),
        "--output", "json"
    ], timeout=15)
    if not res.get("ok"):
        return []
    d = res.get("data", {})
    if isinstance(d, dict):
        items = d.get("data", {}).get("items", []) if isinstance(d.get("data"), dict) else d.get("items", [])
    else:
        items = []
    # 转换为与 get_recent_docs 兼容的格式
    docs = []
    for item in items:
        if item.get("space_resource_type") in ("NEWDOC", "SHIMO2_DOC", "DK_PAGE"):
            docs.append({
                "resourceId": item.get("id"),
                "resourceName": item.get("display_name", ""),
                "resourceTypeStr": item.get("space_resource_type", ""),
                "filePath": "",
            })
    return docs


def find_daily_report_docs():
    """查找日报文档 — 优先从当月文件夹查找，回退到最近文档列表"""
    daily = []
    seen_ids = set()

    # 1. 优先从当月文件夹查找
    month_folder = find_or_create_month_folder()
    if month_folder:
        folder_docs = list_folder_docs(month_folder)
        for doc in folder_docs:
            name = doc.get("resourceName", "")
            if re.match(DAILY_DOC_PATTERN, name.strip()):
                daily.append(doc)
                seen_ids.add(doc.get("resourceId"))

    # 2. 回退到最近文档列表（可能有未归档的日报）
    # 只接受 NEWDOC 类型（Cooper 文档），排除 DK_PAGE（知识库页面）等
    docs = get_recent_docs()
    for doc in docs:
        name = doc.get("resourceName", "")
        rid = doc.get("resourceId")
        rtype = doc.get("resourceTypeStr", "")
        if re.match(DAILY_DOC_PATTERN, name.strip()) and rid not in seen_ids and rtype == "NEWDOC":
            daily.append(doc)
            seen_ids.add(rid)

    return daily


def find_weekly_report_docs():
    """查找周报文档"""
    docs = get_recent_docs()
    weekly = []
    for doc in docs:
        name = doc.get("resourceName", "")
        path = doc.get("filePath", "")
        if re.search(WEEKLY_DOC_PATTERN, name) or re.search(WEEKLY_DOC_PATTERN, path):
            weekly.append(doc)
    return weekly


def find_okr_docs():
    """查找 OKR 文档"""
    docs = get_recent_docs()
    okr = []
    for doc in docs:
        name = doc.get("resourceName", "")
        path = doc.get("filePath", "")
        if re.search(OKR_DOC_PATTERN, name) or re.search(OKR_DOC_PATTERN, path):
            okr.append(doc)
    return okr


def get_calendar_events(days=1):
    """获取日历事件（带缓存）"""
    cal_key = f"calendar_{days}"
    cached = cache_get(cal_key)
    if cached is not None:
        return cached
    start = datetime.now(SHANGHAI_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=days)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    res = run_dws([
        "calendar", "search",
        "--start-time", str(start_ms),
        "--end-time", str(end_ms),
        "--output", "json"
    ])
    if not res.get("ok"):
        return []
    data = res.get("data")
    if isinstance(data, str):
        return []  # "未搜索到匹配的日程"
    if isinstance(data, dict):
        result = data.get("items", []) or data.get("events", [])
    else:
        result = []
    cache_set(cal_key, result)
    return result


def get_dm_messages(max_chats=15, msg_per_chat=10):
    """获取近期 D-Chat 消息，分为两类:
    1. dm_others: 私聊中对方发来的消息 (只关注对方发来的)
    2. group_atme: 群聊中 @我的消息
    返回: {"dm_others": [...], "group_atme": [...]}
    """
    dm_key = "dm_messages_v2"
    cached = cache_get(dm_key, ttl=60)
    if cached is not None:
        return cached

    # 获取自己的 UID
    my_uid = None
    my_res = run_dws(["user", "info", "--self", "--output", "json"], timeout=10)
    if my_res.get("ok"):
        my_uid = str(my_res.get("data", {}).get("user", {}).get("id", ""))

    # 1. Dump chat list
    chat_file = os.path.join(DATA_DIR, "..", "_dchats_tmp.json")
    chat_file = os.path.normpath(chat_file)
    try:
        subprocess.run(
            [DWS_BIN, "chat", "+dump-chats", chat_file, "--output", "json"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return {"dm_others": [], "group_atme": []}

    try:
        with open(chat_file, "r", encoding="utf-8") as f:
            chat_data = json.load(f)
    except Exception:
        return {"dm_others": [], "group_atme": []}

    chats = chat_data.get("data", {}).get("chats", [])
    current_ms = now_ms()

    # Separate private chats and group chats
    p2p_chats = [c for c in chats if c.get("type") in ("p2p", "p2ai")]
    p2p_chats.sort(key=lambda c: c.get("latest_ts", 0), reverse=True)
    p2p_chats = p2p_chats[:max_chats]

    group_chats = [c for c in chats if c.get("type") == "channel"]
    # For group chats, prioritize those with unread or mention_me
    group_chats.sort(key=lambda c: (
        c.get("mention_me_count", 0) > 0,
        c.get("unread_count", 0) > 0,
        c.get("latest_ts", 0),
    ), reverse=True)
    group_chats = group_chats[:max_chats]

    temp_files = [chat_file]
    dm_others = []
    group_atme = []

    # 2. Process private chats - only messages from others
    for chat in p2p_chats:
        vid = chat.get("vchannel_id")
        name = chat.get("name", "")
        if not vid:
            continue
        latest_ts = chat.get("latest_ts", 0)
        if latest_ts and (current_ms - latest_ts) > 86400000 * 2:
            continue

        msg_file = os.path.join(DATA_DIR, "..", f"_dm_{vid}.json")
        msg_file = os.path.normpath(msg_file)
        temp_files.append(msg_file)
        try:
            subprocess.run(
                [DWS_BIN, "message", "+dump-by-chat", "--by-chat-id", str(vid), "--today", msg_file, "--output", "json"],
                capture_output=True, text=True, timeout=20,
            )
        except Exception:
            continue
        try:
            with open(msg_file, "r", encoding="utf-8") as f:
                mdata = json.load(f)
        except Exception:
            continue

        messages = mdata.get("data", {}).get("messages", [])
        for m in messages[:msg_per_chat]:
            subtype = m.get("subtype", "")
            if subtype not in ("normal", "combined"):
                continue
            text = m.get("text", "").strip()
            if not text or len(text) < 2:
                continue
            if text in ("[图片]", "[文件]", "[语音]", "[视频]"):
                continue
            if subtype == "combined" and ("的聊天记录" in text or "的合并转发" in text):
                continue
            if text.startswith("http://") or text.startswith("https://"):
                if " " not in text:
                    continue

            # Only keep messages from others (not my own)
            msg_uid = str(m.get("uid", ""))
            frm_uid = str(m.get("from", {}).get("uid", ""))
            if my_uid and (msg_uid == my_uid or frm_uid == my_uid):
                continue

            author_info = m.get("content", {}).get("author", {})
            dm_others.append({
                "chat_name": name,
                "author": author_info.get("fullname", ""),
                "text": text[:500],
                "created_at": m.get("created_at", ""),
                "vchannel_id": vid,
                "subtype": subtype,
            })

    # 3. Process group chats - only @me messages
    my_mention_tag = f"@<={my_uid}=>" if my_uid else None
    for chat in group_chats:
        vid = chat.get("vchannel_id")
        name = chat.get("name", "")
        if not vid:
            continue
        latest_ts = chat.get("latest_ts", 0)
        if latest_ts and (current_ms - latest_ts) > 86400000 * 2:
            continue

        msg_file = os.path.join(DATA_DIR, "..", f"_grp_{vid}.json")
        msg_file = os.path.normpath(msg_file)
        temp_files.append(msg_file)
        try:
            subprocess.run(
                [DWS_BIN, "message", "+dump-by-chat", "--by-chat-id", str(vid), "--today", msg_file, "--output", "json"],
                capture_output=True, text=True, timeout=20,
            )
        except Exception:
            continue
        try:
            with open(msg_file, "r", encoding="utf-8") as f:
                mdata = json.load(f)
        except Exception:
            continue

        messages = mdata.get("data", {}).get("messages", [])
        for m in messages[:msg_per_chat * 2]:
            subtype = m.get("subtype", "")
            if subtype not in ("normal", "combined"):
                continue
            text = m.get("text", "").strip()
            if not text or len(text) < 2:
                continue
            if text in ("[图片]", "[文件]", "[语音]", "[视频]"):
                continue

            # Check if this message @mentions me
            is_at_me = False
            if my_mention_tag and my_mention_tag in text:
                is_at_me = True
            # Also check metadata for mention info
            metadata = m.get("content", {}).get("metadata", {})
            if isinstance(metadata, dict):
                mentions = metadata.get("mentions", [])
                if isinstance(mentions, list):
                    for mention in mentions:
                        if isinstance(mention, dict) and str(mention.get("uid", "")) == my_uid:
                            is_at_me = True
                            break
                        if isinstance(mention, str) and my_uid in mention:
                            is_at_me = True
                            break
                # Also check if mention_me flag is set
                if metadata.get("mention_me") or metadata.get("at_me"):
                    is_at_me = True

            if not is_at_me:
                continue

            author_info = m.get("content", {}).get("author", {})
            group_atme.append({
                "chat_name": name,
                "author": author_info.get("fullname", ""),
                "text": text[:500],
                "created_at": m.get("created_at", ""),
                "vchannel_id": vid,
                "subtype": subtype,
            })

    # Clean up temp files
    for fn in temp_files:
        try:
            os.remove(os.path.normpath(fn))
        except Exception:
            pass

    result = {"dm_others": dm_others, "group_atme": group_atme}
    cache_set(dm_key, result, ttl=60)
    return result



def create_doc(title, content, kind="cooper", space_id=None, parent_id=None):
    """创建 Cooper 文档，可指定空间和父文件夹"""
    args = [
        "doc", "create",
        "--kind", kind,
        "--title", title,
        "--content", content,
        "--output", "json"
    ]
    if space_id is not None:
        args += ["--space-id", str(space_id)]
    if parent_id is not None:
        args += ["--parent-id", str(parent_id)]
    return run_dws(args)


def update_doc_content(resource_id, content, app="cooper"):
    """更新文档内容 — 使用 update-v2 replace 全文
    先 pull 获取结构，再使用 apply 操作
    简化策略：对日报/周报，先删除旧内容块再插入新内容块
    """
    # 先 pull 获取文档结构
    pull_res = run_dws([
        "doc", "update-v2", str(resource_id),
        "--app", app,
        "--operation", "pull",
        "--output", "json"
    ])
    if not pull_res.get("ok"):
        return pull_res

    pull_data = pull_res.get("data", {})
    blocks = []
    if isinstance(pull_data, dict):
        blocks = pull_data.get("blocks", []) or pull_data.get("content", [])
    elif isinstance(pull_data, list):
        blocks = pull_data

    # 删除所有现有块（从后往前删）
    for block in reversed(blocks):
        anchor = block.get("anchor", "")
        if anchor:
            run_dws([
                "doc", "update-v2", str(resource_id),
                "--app", app,
                "--operation", "apply",
                "--apply-action", "delete_block",
                "--anchor", anchor,
                "--output", "json"
            ])

    # 在文档末尾插入新内容
    insert_res = run_dws([
        "doc", "update-v2", str(resource_id),
        "--app", app,
        "--operation", "apply",
        "--apply-action", "insert_blocks",
        "--position", "end",
        "--text", content,
        "--markdown", "true",
        "--output", "json"
    ])
    return insert_res


def append_to_doc(resource_id, text, app="cooper"):
    """在文档末尾追加内容"""
    return run_dws([
        "doc", "update-v2", str(resource_id),
        "--app", app,
        "--operation", "apply",
        "--apply-action", "insert_blocks",
        "--position", "end",
        "--text", text,
        "--markdown", "true",
        "--output", "json"
    ])


# ==================== 同步逻辑 ====================

def sync_todo_to_daily_report(todo_items):
    """将当前待办同步到今日日报文档
    策略: 查找今日日报文档(如 "09.20")，若存在则追加 TO DO 区域，不存在则创建
    """
    today_str = datetime.now(SHANGHAI_TZ).strftime("%m.%d")
    today_title = today_str

    # 构建待办内容 (使用纯文本而非 markdown checkbox 避免转义问题)
    todo_lines = []
    for t in todo_items:
        title = t.get("title", t.get("new_title", ""))
        status = t.get("status", 2)
        priority = t.get("priority", "none")
        prefix = {"high": "[高]", "medium": "[中]", "low": "[低]", "none": ""}.get(priority, "")
        mark = "✅" if status == 3 else "⬜"
        if prefix:
            todo_lines.append(f"{mark} {prefix} {title}")
        else:
            todo_lines.append(f"{mark} {title}")

    # 查找今日日报
    daily_docs = find_daily_report_docs()
    target_doc = None
    for doc in daily_docs:
        if doc.get("resourceName", "").strip() == today_title:
            target_doc = doc
            break

    if target_doc:
        # 读取已有内容，在 TO DO 区域后追加同步标记的待办概览
        resource_id = target_doc.get("resourceId")
        content = get_doc_content(resource_id)
        # 清理 HTML 注释
        if isinstance(content, str) and content.strip().startswith("<!--"):
            content = content.split("-->", 1)[-1].strip() if "-->" in content else content

        # 在文档末尾追加同步的待办概览
        sync_section = f"\n---\n## 待办同步 ({datetime.now(SHANGHAI_TZ).strftime('%H:%M')})\n\n"
        if todo_lines:
            sync_section += "\n".join(f"- {line}" for line in todo_lines) + "\n"
        else:
            sync_section += "- 暂无待办\n"

        append_to_doc(resource_id, sync_section)
        return {"action": "updated", "resource_id": resource_id, "title": today_title}
    else:
        # 创建新日报
        full_content = f"# {today_title}\n\n# TO DO\n\n"
        for line in todo_lines:
            full_content += f"- {line}\n"
        month_folder = find_or_create_month_folder()
        res = create_doc(today_title, full_content, kind="cooper",
                         space_id=DAILY_SPACE_ID,
                         parent_id=month_folder or DAILY_ROOT_FOLDER_ID)
        resource_id = None
        if res.get("ok"):
            resource_id = res.get("data", {}).get("resourceId") or res.get("data", {}).get("id")
        return {"action": "created", "resource_id": resource_id, "title": today_title}


def sync_todo_to_weekly_report(todo_items):
    """将待办同步到周报文档"""
    now = datetime.now(SHANGHAI_TZ)
    week_num = now.isocalendar()[1]
    week_title = f"W{week_num} 周报"

    # 按日期分组
    by_date = {}
    for t in todo_items:
        due = t.get("due_time") or t.get("created_at")
        if due and due > 0:
            date_str = ms_to_date_str(due, "%m-%d")
        else:
            date_str = "未排期"
        by_date.setdefault(date_str, []).append(t)

    content = f"# {week_title}\n\n"
    for date_str in sorted(by_date.keys()):
        items = by_date[date_str]
        content += f"## {date_str}\n\n"
        for t in items:
            title = t.get("title", t.get("new_title", ""))
            status = t.get("status", 2)
            mark = "✅" if status == 3 else "⬜"
            content += f"- {mark} {title}\n"
        content += "\n"

    # 查找周报
    weekly_docs = find_weekly_report_docs()
    target_doc = None
    for doc in weekly_docs:
        if week_title in doc.get("resourceName", ""):
            target_doc = doc
            break

    if target_doc:
        # 在现有周报末尾追加同步概览
        resource_id = target_doc.get("resourceId")
        sync_section = f"\n---\n## 待办同步 ({now.strftime('%m-%d %H:%M')})\n\n"
        for date_str in sorted(by_date.keys()):
            items = by_date[date_str]
            sync_section += f"### {date_str}\n"
            for t in items:
                title = t.get("title", t.get("new_title", ""))
                status = t.get("status", 2)
                mark = "✅" if status == 3 else "⬜"
                sync_section += f"- {mark} {title}\n"
            sync_section += "\n"
        append_to_doc(resource_id, sync_section)
        return {"action": "updated", "resource_id": resource_id, "title": week_title}
    else:
        res = create_doc(week_title, content, kind="cooper")
        resource_id = None
        if res.get("ok"):
            resource_id = res.get("data", {}).get("resourceId") or res.get("data", {}).get("id")
        return {"action": "created", "resource_id": resource_id, "title": week_title}


# ==================== LLM 草稿处理 ====================

import urllib.request

LLM_BASE_URL = "http://127.0.0.1:9999/v1"
LLM_MODEL = "glm-5.2"

def get_llm_api_key():
    """从 codex auth.json 读取 API key"""
    auth_path = os.path.expanduser("~/.codex/auth.json")
    try:
        with open(auth_path, "r") as f:
            return json.load(f).get("OPENAI_API_KEY", "")
    except Exception:
        return ""


def llm_chat(system_prompt, user_prompt, max_tokens=1000):
    """调用本地 LLM proxy 进行对话"""
    api_key = get_llm_api_key()
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    except Exception as e:
        return ""


SYSTEM_PROMPT_PROCESS_DRAFT = """你是一个待办事项整理助手。用户会给你一段自由格式的草稿笔记，可能包含工作想法、临时记录、任务描述等。

请将草稿内容整理成结构化的待办事项列表。规则：
1. 每个待办事项单独一行，以 "- " 开头
2. 如果能推断出优先级，在标题前加 [高]/[中]/[低] 标记，否则不加
3. 如果能推断出进度状态（如"已完成"、"做完了"），标记为已完成，在行首加 [x]
4. 去除口语化表达，保留核心任务描述
5. 如果草稿中包含多个任务，拆分成多条
6. 保持原文的语言（中文/英文）
7. 只输出待办列表，不要加任何解释或前后缀

示例输入: "今天要把autopipeline跑起来看看数据，然后跟张三对一下vlm的标注结果，另外那个红绿灯的trigger要改成周期任务"
示例输出:
- 跑autopipeline查看数据
- 跟张三对vlm标注结果
- [中] 红绿灯trigger改成周期任务"""


def process_draft_with_llm(draft_text):
    """用 LLM 将草稿文本整理成待办列表，返回 list[str]"""
    if not draft_text.strip():
        return []
    result = llm_chat(SYSTEM_PROMPT_PROCESS_DRAFT, draft_text)
    if not result:
        return []
    lines = []
    for line in result.strip().split("\n"):
        line = line.strip()
        if line.startswith("- "):
            line = line[2:].strip()
        if line:
            lines.append(line)
    return lines


# ==================== Cooper 文档 草稿区管理 ====================

def get_doc_drafts(resource_id, app="cooper"):
    """解析 Cooper 文档中的 # 草稿 区域，返回草稿段落列表
    每个草稿段落是一个自由格式的文本块。
    """
    pull = run_dws([
        "doc", "update-v2", str(resource_id),
        "--app", app,
        "--operation", "pull",
        "--output", "json"
    ])
    if not pull.get("ok"):
        return []

    content = pull.get("data", {}).get("content", []) if isinstance(pull.get("data"), dict) else []
    sections = pull.get("data", {}).get("sections", []) if isinstance(pull.get("data"), dict) else []

    # 找到 "草稿" section
    draft_section_anchor = None
    for sec in sections:
        if "草稿" in sec.get("headingText", ""):
            draft_section_anchor = sec.get("anchor")
            break

    if not draft_section_anchor:
        return []

    # 收集草稿 section 下的段落
    drafts = []
    collecting = False
    draft_level = None
    for block in content:
        if block.get("type") == "heading":
            txt = "".join(c.get("text", "") for c in block.get("content", []) if isinstance(c, dict))
            if "草稿" in txt:
                collecting = True
                draft_level = block.get("attrs", {}).get("level", 1)
                continue
            if collecting:
                current_level = block.get("attrs", {}).get("level", 1)
                if current_level <= draft_level:
                    break
        if collecting and block.get("type") in ("paragraph", "bulletList", "listItem"):
            txt = "".join(c.get("text", "") for c in block.get("content", []) if isinstance(c, dict))
            txt = txt.strip()
            if not txt:
                continue
            drafts.append({
                "doc_id": resource_id,
                "anchor": block.get("anchor", ""),
                "text": txt,
            })

    return drafts


def ensure_draft_section(resource_id, app="cooper"):
    """确保文档中有 # 草稿 区域，如果没有则创建。
    草稿区域放在 TO DO 区域之前。
    返回草稿 section 是否已存在。
    """
    pull = run_dws([
        "doc", "update-v2", str(resource_id),
        "--app", app,
        "--operation", "pull",
        "--output", "json"
    ])
    if not pull.get("ok"):
        return False

    content = pull.get("data", {}).get("content", []) if isinstance(pull.get("data"), dict) else []
    sections = pull.get("data", {}).get("sections", []) if isinstance(pull.get("data"), dict) else []

    # 检查是否已有草稿 section
    for sec in sections:
        if "草稿" in sec.get("headingText", ""):
            return True

    # 没有，在 TO DO 标题之前插入
    todo_anchor = None
    for block in content:
        if block.get("type") == "heading":
            txt = "".join(c.get("text", "") for c in block.get("content", []) if isinstance(c, dict))
            if txt.strip().upper() == "TO DO":
                todo_anchor = block.get("anchor")
                break

    if todo_anchor:
        run_dws([
            "doc", "update-v2", str(resource_id),
            "--app", app,
            "--operation", "apply",
            "--apply-action", "insert_blocks_before",
            "--anchor", todo_anchor,
            "--text", "# 草稿",
            "--markdown", "true",
            "--output", "json"
        ])
    else:
        # 在文档开头插入
        run_dws([
            "doc", "update-v2", str(resource_id),
            "--app", app,
            "--operation", "apply",
            "--apply-action", "insert_blocks",
            "--position", "start",
            "--text", "# 草稿",
            "--markdown", "true",
            "--output", "json"
        ])
    return False


def add_doc_draft(resource_id, text, app="cooper"):
    """在草稿区域添加一条草稿"""
    ensure_draft_section(resource_id, app)
    drafts = get_doc_drafts(resource_id, app)
    if drafts:
        last_anchor = drafts[-1]["anchor"]
        return run_dws([
            "doc", "update-v2", str(resource_id),
            "--app", app,
            "--operation", "apply",
            "--apply-action", "insert_blocks_after",
            "--anchor", last_anchor,
            "--text", text,
            "--markdown", "true",
            "--output", "json"
        ])
    else:
        # 草稿区域为空，在标题后插入
        pull = run_dws([
            "doc", "update-v2", str(resource_id),
            "--app", app,
            "--operation", "pull",
            "--output", "json"
        ])
        content = pull.get("data", {}).get("content", []) if isinstance(pull.get("data"), dict) else []
        draft_anchor = None
        for block in content:
            if block.get("type") == "heading":
                txt = "".join(c.get("text", "") for c in block.get("content", []) if isinstance(c, dict))
                if "草稿" in txt:
                    draft_anchor = block.get("anchor")
                    break

        # Also try locate to find it
        if not draft_anchor:
            loc = run_dws([
                "doc", "update-v2", str(resource_id),
                "--app", app,
                "--operation", "locate",
                "--text", "草稿",
                "--output", "json"
            ])
            if loc.get("ok"):
                loc_data = loc.get("data", {})
                if isinstance(loc_data, dict):
                    draft_anchor = loc_data.get("anchor")
                elif isinstance(loc_data, list) and loc_data:
                    draft_anchor = loc_data[0].get("anchor")
        if draft_anchor:
            return run_dws([
                "doc", "update-v2", str(resource_id),
                "--app", app,
                "--operation", "apply",
                "--apply-action", "insert_blocks_after",
                "--anchor", draft_anchor,
                "--text", text,
                "--markdown", "true",
                "--output", "json"
            ])
    return {"ok": False, "error": {"message": "无法定位草稿区域"}}


def delete_doc_draft(resource_id, anchor, app="cooper"):
    """删除一条草稿"""
    return run_dws([
        "doc", "update-v2", str(resource_id),
        "--app", app,
        "--operation", "apply",
        "--apply-action", "delete_block",
        "--anchor", anchor,
        "--output", "json"
    ])


def process_drafts_to_todos(resource_id, app="cooper"):
    """将草稿区域的所有草稿用 LLM 整理成待办列表，追加到 TO DO 区域"""
    drafts = get_doc_drafts(resource_id, app)
    if not drafts:
        return {"ok": False, "error": {"message": "草稿区域为空"}}

    # 合并所有草稿文本
    all_draft = "\n".join(d["text"] for d in drafts)
    new_todos = process_draft_with_llm(all_draft)

    if not new_todos:
        return {"ok": False, "error": {"message": "LLM 未生成待办项"}}

    # 解析优先级和完成状态
    parsed = []
    for todo in new_todos:
        completed = False
        priority = None
        clean = todo
        if clean.startswith("[x]"):
            completed = True
            clean = clean[3:].strip()
        for tag, val in [("[高]", "high"), ("[中]", "medium"), ("[低]", "low")]:
            if tag in clean:
                priority = val
                clean = clean.replace(tag, "").strip()
        parsed.append({"title": clean, "completed": completed, "priority": priority})

    # 逐条添加到 TO DO 表格
    ensure_todo_table(resource_id, app)
    added = []
    for item in parsed:
        status = "已完成" if item["completed"] else "未完成"
        priority = item.get("priority") or " "
        res = add_doc_todo(resource_id, item["title"], priority=priority, status=status, progress=" ", app=app)
        if res.get("ok"):
            added.append(item["title"])

    # 清空草稿区域（删除已处理的草稿）
    for d in drafts:
        delete_doc_draft(resource_id, d["anchor"], app)

    return {
        "ok": True,
        "data": {
            "draft_count": len(drafts),
            "added_todos": added,
            "parsed": parsed,
        }
    }


# ==================== Cooper 文档 TO DO 管理（表格格式） ====================

# 表格列: 任务 | 优先级 | 完成状态 | 进度说明
# 列索引: 0=任务, 1=优先级, 2=完成状态, 3=进度说明

def find_todo_table(resource_id, app="cooper"):
    """在文档中找到 TO DO 标题下的表格，返回表格 anchor
    如果没有表格，返回 None。
    """
    pull = run_dws([
        "doc", "update-v2", str(resource_id),
        "--app", app, "--operation", "pull", "--output", "json"
    ])
    if not pull.get("ok"):
        return None

    content = pull.get("data", {}).get("content", []) if isinstance(pull.get("data"), dict) else []
    in_todo = False
    for block in content:
        if block.get("type") == "heading":
            txt = "".join(c.get("text", "") for c in block.get("content", []) if isinstance(c, dict))
            in_todo = (txt.strip().upper() == "TO DO")
        elif block.get("type") == "table" and in_todo:
            return block.get("anchor")
    return None


def parse_doc_todos(resource_id, app="cooper"):
    """解析 Cooper 文档中 TO DO 表格的待办列表。
    返回格式: [{doc_id, title, priority, status, progress, row_index, cell_anchors}]
    """
    pull = run_dws([
        "doc", "update-v2", str(resource_id),
        "--app", app, "--operation", "pull", "--output", "json"
    ])
    if not pull.get("ok"):
        return []

    content = pull.get("data", {}).get("content", []) if isinstance(pull.get("data"), dict) else []

    # 找到 TO DO 下的表格
    in_todo = False
    table_block = None
    for block in content:
        if block.get("type") == "heading":
            txt = "".join(c.get("text", "") for c in block.get("content", []) if isinstance(c, dict))
            in_todo = (txt.strip().upper() == "TO DO")
        elif block.get("type") == "table" and in_todo:
            table_block = block
            break

    if not table_block:
        return []

    rows = table_block.get("content", [])
    table_anchor = table_block.get("anchor", "")
    todos = []

    # 跳过第 0 行（表头），从第 1 行开始解析数据
    for row_idx, row in enumerate(rows):
        if row_idx == 0:
            continue  # 表头行
        cells = row.get("content", [])
        cell_texts = []
        cell_anchors = []
        for cell in cells:
            cell_text = ""
            cell_anchor = ""
            for para in cell.get("content", []):
                if isinstance(para, dict):
                    cell_text += "".join(t.get("text", "") for t in para.get("content", []) if isinstance(t, dict))
                    if para.get("anchor"):
                        cell_anchor = para["anchor"]
            cell_texts.append(cell_text.strip().replace("<br>", "").strip())
            cell_anchors.append(cell_anchor)

        # 列: 0=任务, 1=优先级, 2=完成状态, 3=进度说明
        title = cell_texts[0] if len(cell_texts) > 0 else ""
        priority = cell_texts[1] if len(cell_texts) > 1 else ""
        status = cell_texts[2] if len(cell_texts) > 2 else "未完成"
        progress = cell_texts[3] if len(cell_texts) > 3 else ""

        if not title:
            continue

        completed = "已完成" in status or "完成" == status.strip()

        todos.append({
            "doc_id": resource_id,
            "title": title,
            "priority": priority,
            "status": status,
            "progress": progress,
            "completed": completed,
            "row_index": row_idx,
            "cell_anchors": cell_anchors,
            "table_anchor": table_anchor,
            "source": "doc_table",
        })

    return todos


def add_doc_todo(resource_id, title, priority="", status="未完成", progress="", app="cooper"):
    """在 TO DO 表格末尾追加一行"""
    table_anchor = find_todo_table(resource_id, app)
    if not table_anchor:
        return {"ok": False, "error": {"message": "未找到 TO DO 表格"}}

    # 空字段用空格填充，确保 replace_text 能正常工作
    cells = [title, priority or " ", status or " ", progress or " "]
    res = run_dws([
        "doc", "update-v2", str(resource_id),
        "--app", app, "--operation", "apply",
        "--apply-action", "append_table_row",
        "--anchor", table_anchor,
        "--cells", json.dumps(cells, ensure_ascii=False),
        "--output", "json"
    ])
    return res


def toggle_doc_todo(resource_id, row_index, table_anchor, current_status, completed, app="cooper"):
    """切换某行待办的完成状态：修改"完成状态"列的文本"""
    # 需要 pull 找到该行的第 2 列(完成状态) cell anchor
    pull = run_dws([
        "doc", "update-v2", str(resource_id),
        "--app", app, "--operation", "pull", "--output", "json"
    ])
    if not pull.get("ok"):
        return pull

    content = pull.get("data", {}).get("content", []) if isinstance(pull.get("data"), dict) else []
    in_todo = False
    for block in content:
        if block.get("type") == "heading":
            txt = "".join(c.get("text", "") for c in block.get("content", []) if isinstance(c, dict))
            in_todo = (txt.strip().upper() == "TO DO")
        elif block.get("type") == "table" and in_todo:
            rows = block.get("content", [])
            if row_index < len(rows):
                cells = rows[row_index].get("content", [])
                if len(cells) > 2:
                    status_cell = cells[2]
                    for para in status_cell.get("content", []):
                        if isinstance(para, dict) and para.get("anchor"):
                            cell_anchor = para["anchor"]
                            old_text = "".join(t.get("text", "") for t in para.get("content", []) if isinstance(t, dict))
                            new_status = "已完成" if completed else "未完成"
                            return run_dws([
                                "doc", "update-v2", str(resource_id),
                                "--app", app, "--operation", "apply",
                                "--apply-action", "replace_text",
                                "--anchor", cell_anchor,
                                "--match-text", old_text,
                                "--text", new_status,
                                "--output", "json"
                            ])
            break
    return {"ok": False, "error": {"message": "未找到对应的表格行"}}


def update_doc_todo(resource_id, row_index, field, old_value, new_value, app="cooper"):
    """更新某行待办的某个字段: field = title/priority/status/progress (对应列 0/1/2/3)"""
    field_to_col = {"title": 0, "priority": 1, "status": 2, "progress": 3}
    col_idx = field_to_col.get(field, 0)

    pull = run_dws([
        "doc", "update-v2", str(resource_id),
        "--app", app, "--operation", "pull", "--output", "json"
    ])
    if not pull.get("ok"):
        return pull

    content = pull.get("data", {}).get("content", []) if isinstance(pull.get("data"), dict) else []
    in_todo = False
    for block in content:
        if block.get("type") == "heading":
            txt = "".join(c.get("text", "") for c in block.get("content", []) if isinstance(c, dict))
            in_todo = (txt.strip().upper() == "TO DO")
        elif block.get("type") == "table" and in_todo:
            rows = block.get("content", [])
            if row_index < len(rows):
                cells = rows[row_index].get("content", [])
                if col_idx < len(cells):
                    cell = cells[col_idx]
                    for para in cell.get("content", []):
                        if isinstance(para, dict) and para.get("anchor"):
                            cell_anchor = para["anchor"]
                            old_text = "".join(t.get("text", "") for t in para.get("content", []) if isinstance(t, dict))
                            text_value = new_value if new_value.strip() else " "
                            # Always use replace_text with the raw old text (may be " ")
                            return run_dws([
                                "doc", "update-v2", str(resource_id),
                                "--app", app, "--operation", "apply",
                                "--apply-action", "replace_text",
                                "--anchor", cell_anchor,
                                "--match-text", old_text,
                                "--text", text_value,
                                "--output", "json"
                            ])
            break
    return {"ok": False, "error": {"message": "未找到对应的表格行"}}


def delete_doc_todo(resource_id, row_index, app="cooper"):
    """删除某行待办：读取表格所有行数据，删除旧表格，用 append_table_row 重建"""
    # 先用 fetch 读取表格内容（pull 读不到 cell text，但 fetch 可以）
    content_str = get_doc_content(resource_id, app)
    if not isinstance(content_str, str):
        return {"ok": False, "error": {"message": "无法读取文档内容"}}

    # 解析 markdown 表格行
    lines = content_str.split("\n")
    table_lines = []
    in_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|"):
            in_table = True
            table_lines.append(stripped)
        else:
            if in_table:
                break  # 表格结束

    if not table_lines:
        return {"ok": False, "error": {"message": "未找到表格"}}

    # 解析表格行: row 0 = header, row 1 = separator, row 2+ = data
    # Clean cell text: remove <br> and extra |
    all_rows = []
    for line in table_lines:
        cells = [c.strip().replace("<br>", "").strip() for c in line.split("|")]
        # Remove empty first/last from split
        cells = [c for c in cells if c != "" or True]
        # Actually split properly: | a | b | c | -> ['', 'a', 'b', 'c', '']
        parts = line.split("|")
        parts = [p.strip().replace("<br>", "").strip() for p in parts]
        # Remove first and last empty
        if parts and parts[0] == "":
            parts = parts[1:]
        if parts and parts[-1] == "":
            parts = parts[:-1]
        all_rows.append(parts)

    # Identify header (row 0), separator (row 1), data rows (row 2+)
    # The row_index from parse_doc_todos is 1-based (0=header, 1+=data)
    # In table_lines: index 0 = header, 1 = separator, 2+ = data rows
    # So actual line index = row_index + 1 (because of separator row)
    data_rows = []
    for idx, row_cells in enumerate(all_rows):
        if idx == 0:
            continue  # header
        if idx == 1:
            continue  # separator (--- | --- | ---)
        # This is a data row, line_index = idx
        data_rows.append((idx, row_cells))

    # Find the row to delete: row_index in parse_doc_todos is 1-based from header
    # parse_doc_todos row_index=1 means first data row = all_rows[2]
    # So actual all_rows index = row_index + 1
    delete_all_rows_idx = row_index + 1

    # Build list of rows to keep
    keep_rows = []
    for idx, row_cells in data_rows:
        if idx == delete_all_rows_idx:
            continue
        # Pad to 4 columns
        while len(row_cells) < 4:
            row_cells.append("")
        # Skip fully empty rows
        if any(c.strip() for c in row_cells[:4]):
            keep_rows.append(row_cells[:4])

    # Now: delete old table and rebuild via ensure_todo_table + append_table_row
    pull = run_dws([
        "doc", "update-v2", str(resource_id),
        "--app", app, "--operation", "pull", "--output", "json"
    ])
    if not pull.get("ok"):
        return pull

    pull_content = pull.get("data", {}).get("content", []) if isinstance(pull.get("data"), dict) else []
    in_todo = False
    todo_heading_anchor = None
    old_table_anchor = None
    for block in pull_content:
        if block.get("type") == "heading":
            txt = "".join(c.get("text", "") for c in block.get("content", []) if isinstance(c, dict))
            if txt.strip().upper() == "TO DO":
                todo_heading_anchor = block.get("anchor")
                in_todo = True
            else:
                in_todo = False
        elif block.get("type") == "table" and in_todo:
            old_table_anchor = block.get("anchor")
            break

    if not old_table_anchor:
        return {"ok": False, "error": {"message": "未找到 TO DO 表格"}}

    # Delete old table
    run_dws([
        "doc", "update-v2", str(resource_id),
        "--app", app, "--operation", "apply",
        "--apply-action", "delete_block",
        "--anchor", old_table_anchor,
        "--output", "json"
    ])

    # Create new table
    ensure_todo_table(resource_id, app)

    # Re-add all kept rows
    for row_cells in keep_rows:
        run_dws([
            "doc", "update-v2", str(resource_id),
            "--app", app, "--operation", "apply",
            "--apply-action", "append_table_row",
            "--anchor", find_todo_table(resource_id, app) or "",
            "--cells", json.dumps(row_cells, ensure_ascii=False),
            "--output", "json"
        ])

    return {"ok": True, "data": {"deleted_row": row_index, "remaining": len(keep_rows)}}


def ensure_todo_table(resource_id, app="cooper"):
    """确保 TO DO 标题下有表格，没有则创建"""
    table_anchor = find_todo_table(resource_id, app)
    if table_anchor:
        return table_anchor

    # 创建表格
    pull = run_dws([
        "doc", "update-v2", str(resource_id),
        "--app", app, "--operation", "pull", "--output", "json"
    ])
    content = pull.get("data", {}).get("content", []) if pull.get("ok") and isinstance(pull.get("data"), dict) else []
    todo_heading_anchor = None
    for block in content:
        if block.get("type") == "heading":
            txt = "".join(c.get("text", "") for c in block.get("content", []) if isinstance(c, dict))
            if txt.strip().upper() == "TO DO":
                todo_heading_anchor = block.get("anchor")
                break

    header_row = {"cells": [
        {"type": "paragraph", "content": [{"type": "text", "text": "任务"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "优先级"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "完成状态"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "进度说明"}]},
    ]}
    nodes = [{"type": "table", "attrs": {"headerrow": True}, "rows": [header_row]}]

    if todo_heading_anchor:
        run_dws([
            "doc", "update-v2", str(resource_id),
            "--app", app, "--operation", "apply",
            "--apply-action", "insert_nodes_after",
            "--anchor", todo_heading_anchor,
            "--nodes", json.dumps(nodes, ensure_ascii=False),
            "--output", "json"
        ])
    else:
        run_dws([
            "doc", "update-v2", str(resource_id),
            "--app", app, "--operation", "apply",
            "--apply-action", "insert_nodes",
            "--position", "end",
            "--nodes", json.dumps(nodes, ensure_ascii=False),
            "--output", "json"
        ])

    return find_todo_table(resource_id, app)


# ==================== HTTP Server ====================

class TodoRobotHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默日志

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._serve_html()
        elif path == "/api/todos":
            self._handle_get_todos(params)
        elif path == "/api/calendar":
            self._handle_get_calendar(params)
        elif path == "/api/docs/daily":
            self._handle_get_daily_docs()
        elif path == "/api/docs/weekly":
            self._handle_get_weekly_docs()
        elif path == "/api/docs/okr":
            self._handle_get_okr_docs()
        elif path == "/api/docs/content":
            self._handle_get_doc_content(params)
        elif path == "/api/docs/todos":
            self._handle_get_doc_todos(params)
        elif path == "/api/docs/drafts":
            self._handle_get_doc_drafts(params)
        elif path == "/api/local/todos":
            self._handle_get_local_todos(params)
        elif path == "/api/local/dates":
            self._handle_list_local_dates()
        elif path == "/api/summary":
            self._handle_get_summary()
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        body = self._read_body()

        if path == "/api/todos/create":
            self._handle_create_todo(body)
        elif path == "/api/todos/update":
            self._handle_update_todo(body)
        elif path == "/api/todos/complete":
            self._handle_complete_todo(body)
        elif path == "/api/todos/delete":
            self._handle_delete_todo(body)
        elif path == "/api/sync/daily":
            self._handle_sync_daily()
        elif path == "/api/sync/weekly":
            self._handle_sync_weekly()
        elif path == "/api/docs/create":
            self._handle_create_doc(body)
        elif path == "/api/docs/todos/toggle":
            self._handle_toggle_doc_todo(body)
        elif path == "/api/docs/todos/add":
            self._handle_add_doc_todo(body)
        elif path == "/api/docs/todos/delete":
            self._handle_delete_doc_todo(body)
        elif path == "/api/docs/todos/update":
            self._handle_update_doc_todo(body)
        elif path == "/api/docs/drafts/add":
            self._handle_add_doc_draft(body)
        elif path == "/api/docs/drafts/delete":
            self._handle_delete_doc_draft(body)
        elif path == "/api/docs/drafts/process":
            self._handle_process_drafts(body)
        elif path == "/api/local/todos/create":
            self._handle_create_local_todo(body)
        elif path == "/api/local/todos/update":
            self._handle_update_local_todo(body)
        elif path == "/api/local/todos/toggle":
            self._handle_toggle_local_todo(body)
        elif path == "/api/local/todos/delete":
            self._handle_delete_local_todo(body)
        elif path == "/api/local/sync":
            self._handle_sync_local(body)
        elif path == "/api/diag":
            self._handle_diag(body)
        else:
            self._json({"error": "not found"}, 404)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                html = f.read()
        except FileNotFoundError:
            self._json({"error": "index.html not found"}, 404)
            return
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    # ---- GET handlers ----

    def _handle_get_todos(self, params):
        status = params.get("status", [None])[0]
        if status:
            status = int(status)
        items = get_todos(status)
        formatted = []
        for t in items:
            formatted.append({
                "id": t.get("id"),
                "title": t.get("title") or t.get("new_title", ""),
                "summary": t.get("summary", ""),
                "priority": t.get("priority", "none"),
                "status": t.get("status", 2),
                "due_time": t.get("due_time", -1),
                "due_time_str": ms_to_str(t.get("due_time", -1)),
                "created_at": t.get("created_at", 0),
                "completed": t.get("status") == 3,
                "origin": t.get("origin_name", t.get("origin", {}).get("name", {}).get("en", "")) if isinstance(t.get("origin"), dict) else "",
            })
        self._json({"ok": True, "data": formatted})

    def _handle_get_calendar(self, params):
        days = int(params.get("days", ["1"])[0])
        events = get_calendar_events(days)
        formatted = []
        for e in events:
            dtstart = e.get("dtstart") or e.get("start_time", 0)
            dtend = e.get("dtend") or e.get("end_time", 0)
            formatted.append({
                "title": e.get("title") or e.get("summary", ""),
                "start": dtstart,
                "start_str": ms_to_str(dtstart, "%m-%d %H:%M"),
                "end": dtend,
                "end_str": ms_to_str(dtend, "%H:%M"),
                "location": e.get("location", ""),
                "attendees": e.get("attendees", []),
            })
        self._json({"ok": True, "data": formatted})

    def _handle_get_daily_docs(self):
        docs = find_daily_report_docs()
        result = []
        for d in docs:
            result.append({
                "id": d.get("resourceId"),
                "name": d.get("resourceName"),
                "type": d.get("resourceTypeStr"),
                "path": d.get("filePath"),
            })
        self._json({"ok": True, "data": result})

    def _handle_get_weekly_docs(self):
        docs = find_weekly_report_docs()
        result = []
        for d in docs:
            result.append({
                "id": d.get("resourceId"),
                "name": d.get("resourceName"),
                "type": d.get("resourceTypeStr"),
                "path": d.get("filePath"),
            })
        self._json({"ok": True, "data": result})

    def _handle_get_okr_docs(self):
        docs = find_okr_docs()
        result = []
        for d in docs:
            result.append({
                "id": d.get("resourceId"),
                "name": d.get("resourceName"),
                "type": d.get("resourceTypeStr"),
                "path": d.get("filePath"),
            })
        self._json({"ok": True, "data": result})

    def _handle_get_doc_content(self, params):
        resource_id = params.get("id", [""])[0]
        app = params.get("app", ["cooper"])[0]
        if not resource_id:
            self._json({"ok": False, "error": "id required"}, 400)
            return
        content = get_doc_content(resource_id, app)
        self._json({"ok": True, "data": content})

    def _handle_get_doc_todos(self, params):
        resource_id = params.get("id", [""])[0]
        app = params.get("app", ["cooper"])[0]
        if not resource_id:
            # 默认取第一个日报文档
            daily_docs = find_daily_report_docs()
            if daily_docs:
                resource_id = daily_docs[0].get("resourceId")
            else:
                self._json({"ok": False, "error": "no doc id and no daily doc found"}, 400)
                return
        todos = parse_doc_todos(resource_id, app)
        self._json({"ok": True, "data": todos, "doc_id": resource_id})

    def _handle_toggle_doc_todo(self, body):
        resource_id = body.get("doc_id") or body.get("resource_id")
        row_index = body.get("row_index")
        table_anchor = body.get("table_anchor", "")
        current_status = body.get("status", "未完成")
        completed = body.get("completed")
        app = body.get("app", "cooper")
        if not resource_id or row_index is None:
            self._json({"ok": False, "error": "doc_id and row_index required"}, 400)
            return
        res = toggle_doc_todo(resource_id, row_index, table_anchor, current_status, completed, app)
        cache_invalidate("recent_docs")
        self._json(res)

    def _handle_add_doc_todo(self, body):
        resource_id = body.get("doc_id") or body.get("resource_id")
        title = body.get("title", "").strip()
        priority = body.get("priority", "")
        status = body.get("status", "未完成")
        progress = body.get("progress", "")
        app = body.get("app", "cooper")
        if not resource_id or not title:
            self._json({"ok": False, "error": "doc_id and title required"}, 400)
            return
        res = add_doc_todo(resource_id, title, priority=priority, status=status, progress=progress, app=app)
        cache_invalidate("recent_docs")
        self._json(res)

    def _handle_delete_doc_todo(self, body):
        resource_id = body.get("doc_id") or body.get("resource_id")
        row_index = body.get("row_index")
        app = body.get("app", "cooper")
        if not resource_id or row_index is None:
            self._json({"ok": False, "error": "doc_id and row_index required"}, 400)
            return
        res = delete_doc_todo(resource_id, row_index, app)
        cache_invalidate("recent_docs")
        self._json(res)

    def _handle_update_doc_todo(self, body):
        resource_id = body.get("doc_id") or body.get("resource_id")
        row_index = body.get("row_index")
        field = body.get("field", "title")
        old_value = body.get("old_value", "")
        new_value = body.get("new_value", "").strip()
        app = body.get("app", "cooper")
        if not resource_id or row_index is None or not new_value:
            self._json({"ok": False, "error": "doc_id, row_index and new_value required"}, 400)
            return
        res = update_doc_todo(resource_id, row_index, field, old_value, new_value, app)
        cache_invalidate("recent_docs")
        self._json(res)

    def _handle_get_doc_drafts(self, params):
        resource_id = params.get("id", [""])[0]
        app = params.get("app", ["cooper"])[0]
        if not resource_id:
            daily_docs = find_daily_report_docs()
            if daily_docs:
                resource_id = daily_docs[0].get("resourceId")
            else:
                self._json({"ok": False, "error": "no doc id"}, 400)
                return
        drafts = get_doc_drafts(resource_id, app)
        self._json({"ok": True, "data": drafts, "doc_id": resource_id})

    def _handle_add_doc_draft(self, body):
        resource_id = body.get("doc_id") or body.get("resource_id")
        text = body.get("text", "").strip()
        app = body.get("app", "cooper")
        if not resource_id or not text:
            self._json({"ok": False, "error": "doc_id and text required"}, 400)
            return
        res = add_doc_draft(resource_id, text, app)
        cache_invalidate("recent_docs")
        self._json(res)

    def _handle_delete_doc_draft(self, body):
        resource_id = body.get("doc_id") or body.get("resource_id")
        anchor = body.get("anchor")
        app = body.get("app", "cooper")
        if not resource_id or not anchor:
            self._json({"ok": False, "error": "doc_id and anchor required"}, 400)
            return
        res = delete_doc_draft(resource_id, anchor, app)
        cache_invalidate("recent_docs")
        self._json(res)

    def _handle_process_drafts(self, body):
        resource_id = body.get("doc_id") or body.get("resource_id")
        app = body.get("app", "cooper")
        if not resource_id:
            daily_docs = find_daily_report_docs()
            if daily_docs:
                resource_id = daily_docs[0].get("resourceId")
            else:
                self._json({"ok": False, "error": "no doc id"}, 400)
                return
        res = process_drafts_to_todos(resource_id, app)
        cache_invalidate("recent_docs")
        self._json(res)

    # ---- 本地待办 handlers ----

    def _handle_get_local_todos(self, params):
        date_str = params.get("date", [None])[0]
        filename = params.get("file", [None])[0]
        todos = load_local_todos(date_str, filename)
        self._json({"ok": True, "data": todos, "date": date_str or _date_key()})

    def _handle_list_local_dates(self):
        dates = list_local_dates()
        self._json({"ok": True, "data": dates})

    def _handle_create_local_todo(self, body):
        title = body.get("title", "").strip()
        if not title:
            self._json({"ok": False, "error": "title required"}, 400)
            return
        priority = body.get("priority", "none")
        summary = body.get("summary", "")
        progress = body.get("progress", "")
        date_str = body.get("date")
        todo = add_local_todo(title, priority, summary, progress, date_str)
        self._json({"ok": True, "data": todo})

    def _handle_update_local_todo(self, body):
        todo_id = body.get("id")
        if todo_id is None:
            self._json({"ok": False, "error": "id required"}, 400)
            return
        date_str = body.get("date")
        todo = update_local_todo(
            todo_id,
            title=body.get("title"),
            priority=body.get("priority"),
            summary=body.get("summary"),
            progress=body.get("progress"),
            date_str=date_str,
        )
        if todo:
            self._json({"ok": True, "data": todo})
        else:
            self._json({"ok": False, "error": "todo not found"}, 404)

    def _handle_toggle_local_todo(self, body):
        todo_id = body.get("id")
        if todo_id is None:
            self._json({"ok": False, "error": "id required"}, 400)
            return
        date_str = body.get("date")
        completed = body.get("completed")
        todo = toggle_local_todo(todo_id, completed, date_str)
        if todo:
            self._json({"ok": True, "data": todo})
        else:
            self._json({"ok": False, "error": "todo not found"}, 404)

    def _handle_delete_local_todo(self, body):
        todo_id = body.get("id")
        if todo_id is None:
            self._json({"ok": False, "error": "id required"}, 400)
            return
        date_str = body.get("date")
        ok = delete_local_todo(todo_id, date_str)
        if ok:
            self._json({"ok": True, "data": {"deleted": True}})
        else:
            self._json({"ok": False, "error": "todo not found"}, 404)

    def _handle_sync_local(self, body):
        date_str = body.get("date")
        result = sync_local_to_cooper(date_str)
        self._json({"ok": True, "data": result})

    def _handle_diag(self, body):
        """接收前端诊断报告并打印到服务器控制台"""
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[DIAG {ts}] {json.dumps(body, ensure_ascii=False)}")
        self._json({"ok": True})

    def _handle_get_summary(self):
        """一次性获取所有面板数据（并行获取 + 缓存）"""
        with ThreadPoolExecutor(max_workers=4) as pool:
            fut_todos = pool.submit(get_todos)
            fut_cal = pool.submit(get_calendar_events, 1)
            fut_docs = pool.submit(get_recent_docs)
            fut_dm = pool.submit(get_dm_messages)

            todos = fut_todos.result()
            calendar = fut_cal.result()
            all_docs = fut_docs.result()
            dm_messages = fut_dm.result()

        # 从同一份文档列表中分类
        daily_docs = []
        weekly_docs = []
        okr_docs = []
        existing_doc_ids = set()
        for doc in all_docs:
            name = doc.get("resourceName", "").strip()
            path = doc.get("filePath", "")
            existing_doc_ids.add(doc.get("resourceId"))
            if re.match(DAILY_DOC_PATTERN, name) and doc.get("resourceTypeStr") == "NEWDOC":
                daily_docs.append(doc)
            elif re.search(WEEKLY_DOC_PATTERN, name) or re.search(WEEKLY_DOC_PATTERN, path):
                weekly_docs.append(doc)
            elif re.search(OKR_DOC_PATTERN, name) or re.search(OKR_DOC_PATTERN, path):
                okr_docs.append(doc)

        # 合并最近同步创建的日报文档（如果尚未在最近列表中）
        for title, rid in _RECENT_SYNCED_DOCS.items():
            if rid not in existing_doc_ids:
                daily_docs.append({"resourceId": rid, "resourceName": title, "resourceTypeStr": "NEWDOC", "filePath": ""})

        todo_fmt = []
        for t in todos:
            todo_fmt.append({
                "id": t.get("id"),
                "title": t.get("title") or t.get("new_title", ""),
                "summary": t.get("summary", ""),
                "priority": t.get("priority", "none"),
                "status": t.get("status", 2),
                "due_time": t.get("due_time", -1),
                "due_time_str": ms_to_str(t.get("due_time", -1)),
                "completed": t.get("status") == 3,
            })

        cal_fmt = []
        for e in calendar:
            dtstart = e.get("dtstart") or e.get("start_time", 0)
            dtend = e.get("dtend") or e.get("end_time", 0)
            cal_fmt.append({
                "title": e.get("title") or e.get("summary", ""),
                "start_str": ms_to_str(dtstart, "%H:%M"),
                "end_str": ms_to_str(dtend, "%H:%M"),
                "location": e.get("location", ""),
            })

        def fmt_docs(docs):
            return [{"id": d.get("resourceId"), "name": d.get("resourceName"), "type": d.get("resourceTypeStr"), "path": d.get("filePath")} for d in docs]

        # 解析日报文档中的 TO DO 区域和草稿区域（并行）
        doc_todos = []
        doc_drafts = []
        daily_doc_id = None
        # 优先匹配今日日期的日报文档 (如 "09.22")
        today_title = datetime.now(SHANGHAI_TZ).strftime("%m.%d")
        for doc in daily_docs:
            if doc.get("resourceName", "").strip() == today_title:
                daily_doc_id = doc.get("resourceId")
                break
        # 如果没找到今日日报，取第一个日报文档
        if not daily_doc_id:
            for doc in daily_docs:
                rid = doc.get("resourceId")
                if rid:
                    daily_doc_id = rid
                    break

        if daily_doc_id:
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_dt = pool.submit(parse_doc_todos, daily_doc_id)
                fut_dd = pool.submit(get_doc_drafts, daily_doc_id)
                try:
                    doc_todos = fut_dt.result()
                except Exception:
                    doc_todos = []
                try:
                    doc_drafts = fut_dd.result()
                except Exception:
                    doc_drafts = []

        # 加载本地待办
        local_todos = load_local_todos()
        local_dates = list_local_dates()
        # 加载周报和OKR本地数据
        today_key = _date_key()
        weekly_todos = load_local_todos(today_key, f"weekly-{today_key}.json")
        okr_todos = load_local_todos(today_key, f"okr-{today_key}.json")

        self._json({
            "ok": True,
            "data": {
                "todos": todo_fmt,
                "local_todos": local_todos,
                "local_dates": local_dates,
                "doc_todos": doc_todos,
                "doc_drafts": doc_drafts,
                "daily_doc_id": daily_doc_id,
                "calendar": cal_fmt,
                "daily_docs": fmt_docs(daily_docs),
                "weekly_docs": fmt_docs(weekly_docs),
                "okr_docs": fmt_docs(okr_docs),
                "weekly_todos": weekly_todos,
                "okr_todos": okr_todos,
                "dm_messages": dm_messages,
                "today": datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %A"),
                "week": datetime.now(SHANGHAI_TZ).isocalendar()[1],
            }
        })

    # ---- POST handlers ----

    def _handle_create_todo(self, body):
        title = body.get("title", "").strip()
        if not title:
            self._json({"ok": False, "error": "title required"}, 400)
            return
        priority = body.get("priority", "none")
        summary = body.get("summary", "")
        due_time = body.get("due_time")
        res = create_todo(title, priority, summary, due_time)
        cache_invalidate("todos_")
        self._json(res)

    def _handle_update_todo(self, body):
        todo_id = body.get("id")
        if not todo_id:
            self._json({"ok": False, "error": "id required"}, 400)
            return
        res = update_todo(
            todo_id,
            title=body.get("title"),
            priority=body.get("priority"),
            summary=body.get("summary"),
            due_time=body.get("due_time"),
        )
        cache_invalidate("todos_")
        self._json(res)

    def _handle_complete_todo(self, body):
        todo_id = body.get("id")
        if not todo_id:
            self._json({"ok": False, "error": "id required"}, 400)
            return
        status = body.get("status", 3)
        res = complete_todo(todo_id, status)
        cache_invalidate("todos_")
        self._json(res)

    def _handle_delete_todo(self, body):
        todo_id = body.get("id")
        if not todo_id:
            self._json({"ok": False, "error": "id required"}, 400)
            return
        res = delete_todo(todo_id)
        cache_invalidate("todos_")
        self._json(res)

    def _handle_sync_daily(self):
        todos = get_todos()
        result = sync_todo_to_daily_report(todos)
        self._json({"ok": True, "data": result})

    def _handle_sync_weekly(self):
        todos = get_todos()
        result = sync_todo_to_weekly_report(todos)
        self._json({"ok": True, "data": result})

    def _handle_create_doc(self, body):
        title = body.get("title", "").strip()
        content = body.get("content", "")
        kind = body.get("kind", "cooper")
        if not title:
            self._json({"ok": False, "error": "title required"}, 400)
            return
        res = create_doc(title, content, kind)
        self._json(res)


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # 检查 dws 二进制
    if not os.path.isfile(DWS_BIN):
        print(f"❌ dws 二进制不存在: {DWS_BIN}")
        print("请先运行: cp ~/.SmartWork/skills/smartwork-cli/smartwork-shared/assets/dws-darwin-arm64 .dws/dws")
        sys.exit(1)

    class TodoRobotServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with TodoRobotServer(("", PORT), TodoRobotHandler) as httpd:
        url = f"http://localhost:{PORT}"
        print(f"🤖 TODO Robot 悬浮球已启动: {url}")
        print(f"   数据源: D-Chat待办 + D-Chat日历 + Cooper文档(日报/周报/OKR)")
        print(f"   按 Ctrl+C 退出")

        # 自动打开浏览器
        import subprocess as _sp
        threading.Timer(0.5, lambda: _sp.Popen(["open", url], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)).start()

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n👋 已退出")
            httpd.shutdown()


if __name__ == "__main__":
    main()
