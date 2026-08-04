"""本地已用统计：多数据源只读聚合（不查任何远程接口）。

目前接入两个数据源：
- Claude Code：解析 ~/.claude/projects/*/*.jsonl 会话 transcript（单层目录结构，
  即 <项目名>/<会话id>.jsonl，这就是 Claude Code 实际落盘的层级，不需要递归
  扫描；无远程用量接口可查时——见 app/credentials.py 的 CRED_NO_TOKEN——用本地
  统计兜底）；
- OpenCode：读取 ~/.local/share/opencode/opencode.db（SQLite），与 cc-switch 思路
  一致，opencode 没有远程额度接口，只能做本地已用统计。

对外统一接口 get_local_usage(days) -> {"days", "sources": [...]}，每个 source
字段名一致：key / label / available / message / path / model_stats / totals。
所有函数均为同步阻塞 I/O（文件遍历、sqlite），调用方（app/main.py）需要
`await asyncio.to_thread(...)` 包裹，不要在 async 函数里直接调用。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

EMPTY_TOTALS = {
    "sessions": 0,
    "messages": 0,
    "input": 0,
    "output": 0,
    "cache_read": 0,
    "cache_write": 0,
    "cost": 0.0,
    "has_cost": False,
}


def _safe_int(value, default: int = 0) -> int:
    """把本地 transcript / SQLite 里取到的字段安全转成 int。

    Claude Code / OpenCode 落盘的 token 字段正常都是 JSON 整数，但 transcript 是
    明文 JSONL，任何畸形或被改动的行都可能带非数值字段（字符串、列表、对象）。
    直接 int(value or 0) 在遇到 "abc" / [1] / {} 时会抛 ValueError/TypeError，
    而外层 try 只接 OSError，异常会冒泡让整个 /api/local-usage 返回 500——单条
    坏行连累所有数据源（Claude + OpenCode）都拿不到统计。这里吞掉转换异常，
    把畸形字段当 0 处理（与 None/缺失一致），保证统计永远能给出一个数。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _unavailable(key: str, label: str, message: str, path: str | None = None) -> dict:
    return {
        "key": key,
        "label": label,
        "available": False,
        "message": message,
        "path": path,
        "model_stats": [],
        "totals": {},
    }


# ── Claude Code：本地 transcript 统计 ──────────────────────────

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _parse_claude_ts(value) -> int | None:
    """ISO8601（如 "2026-08-03T13:03:05.316Z"）→ epoch 毫秒。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def get_claude_code_usage(days: int = 14) -> dict:
    """扫描 ~/.claude/projects/*/*.jsonl，统计最近 N 天的本地 token 用量。

    只扫一层子目录（<项目名>/<会话id>.jsonl），不用 "**" 递归——Claude Code 的
    transcript 实际就存在这一层，不存在更深的嵌套，递归扫描只是徒增无意义的
    目录遍历开销。

    只统计 type == "assistant" 的行；模型名在 message.model；用量在 message.usage
    （input_tokens / output_tokens / cache_creation_input_tokens /
    cache_read_input_tokens）；时间戳在顶层 timestamp；会话 id 用文件名，项目名用
    父目录名。同一条消息可能在续接/分支会话里重复出现，按 message.id 去重（缺失
    时退化为按 (timestamp, model, output_tokens) 元组去重）。transcript 里没有
    费用字段，只报 token 数，绝不编造费用。
    """
    key, label = "claude_code", "Claude Code 本地已用统计"
    path_str = str(CLAUDE_PROJECTS_DIR)

    if not CLAUDE_PROJECTS_DIR.exists():
        return _unavailable(key, label, f"未找到 Claude Code 本地会话目录（{path_str}）", path_str)

    try:
        files = sorted(CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"))
    except OSError as e:
        return _unavailable(key, label, f"读取 Claude Code 会话目录失败: {e}", path_str)

    since_ms = int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)

    seen: set[tuple] = set()
    by_model: dict[str, dict] = {}
    sessions_seen: set[str] = set()
    read_errors: list[str] = []

    for file_path in files:
        session_id = file_path.stem
        try:
            with file_path.open("r", encoding="utf-8", errors="replace") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict) or entry.get("type") != "assistant":
                        continue
                    message = entry.get("message")
                    if not isinstance(message, dict):
                        continue
                    usage = message.get("usage")
                    if not isinstance(usage, dict):
                        continue

                    ts_raw = entry.get("timestamp")
                    ts_ms = _parse_claude_ts(ts_raw)
                    if ts_ms is None or ts_ms < since_ms:
                        continue

                    msg_id = message.get("id")
                    if isinstance(msg_id, str) and msg_id:
                        dedup_key = ("id", msg_id)
                    else:
                        dedup_key = (
                            "fallback",
                            ts_raw,
                            message.get("model"),
                            usage.get("output_tokens"),
                        )
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)

                    model = str(message.get("model") or "unknown")
                    # <synthetic> 是 Claude Code 内部的占位消息（工具调用摘要等），
                    # token 全为 0，出现在 model_stats 里只是噪音，过滤掉。
                    if model == "<synthetic>":
                        continue
                    entry_stats = by_model.setdefault(
                        model,
                        {
                            "sessions": set(),
                            "messages": 0,
                            "input": 0,
                            "output": 0,
                            "cache_read": 0,
                            "cache_write": 0,
                        },
                    )
                    entry_stats["sessions"].add(session_id)
                    entry_stats["messages"] += 1
                    entry_stats["input"] += _safe_int(usage.get("input_tokens"))
                    entry_stats["output"] += _safe_int(usage.get("output_tokens"))
                    entry_stats["cache_read"] += _safe_int(usage.get("cache_read_input_tokens"))
                    entry_stats["cache_write"] += _safe_int(usage.get("cache_creation_input_tokens"))
                    sessions_seen.add(session_id)
        except OSError as e:
            read_errors.append(f"{file_path.name}: {e}")
            continue

    model_stats = []
    totals = dict(EMPTY_TOTALS)
    for model, stats in sorted(by_model.items(), key=lambda kv: -(kv[1]["input"] + kv[1]["output"])):
        model_stats.append(
            {
                "model": model,
                "sessions": len(stats["sessions"]),
                "messages": stats["messages"],
                "input": stats["input"],
                "output": stats["output"],
                "cache_read": stats["cache_read"],
                "cache_write": stats["cache_write"],
            }
        )
        totals["messages"] += stats["messages"]
        totals["input"] += stats["input"]
        totals["output"] += stats["output"]
        totals["cache_read"] += stats["cache_read"]
        totals["cache_write"] += stats["cache_write"]
    totals["sessions"] = len(sessions_seen)
    # transcript 无 cost 字段：cost 恒为 0.0，has_cost 恒为 False（前端据此判断
    # 是否展示费用列，而不是把 0.0 误当成"确实花了 $0"）。

    message = None
    if not files:
        message = "未找到任何 Claude Code 会话记录（*.jsonl）"
    elif read_errors:
        message = f"部分会话记录读取失败（已跳过）: {'; '.join(read_errors[:3])}"

    return {
        "key": key,
        "label": label,
        "available": True,
        "message": message,
        "path": path_str,
        "model_stats": model_stats,
        "totals": totals,
    }


# ── OpenCode：读取本地 SQLite ───────────────────────────────────


def _db_path() -> Path | None:
    override = os.environ.get("OPENCODE_DB")
    if override:
        return Path(override)
    path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    if not path.exists():
        path = Path.home() / ".config" / "opencode" / "opencode.db"
    return path if path.exists() else None


def _db_path_str() -> str:
    return str(_db_path() or "")


def _parse_message_data(data_json: str | None) -> dict | None:
    if not data_json:
        return None
    try:
        data = json.loads(data_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("role") != "assistant":
        return None
    tokens = data.get("tokens") or {}
    cache = tokens.get("cache") or {}
    cost = data.get("cost")
    return {
        "input": _safe_int(tokens.get("input")),
        "output": _safe_int(tokens.get("output")),
        "reasoning": _safe_int(tokens.get("reasoning")),
        "cache_read": _safe_int(cache.get("read")),
        "cache_write": _safe_int(cache.get("write")),
        "cost": float(cost) if isinstance(cost, (int, float)) else None,
        "model": str(data.get("modelID") or "unknown"),
        "created": (data.get("time") or {}).get("created") if isinstance(data.get("time"), dict) else None,
    }


def get_opencode_usage(days: int = 14) -> dict:
    db_path = _db_path()
    if db_path is None:
        return {
            "available": False,
            "message": "未找到 opencode 数据库",
            "days": days,
            "db_path": None,
            "model_stats": [],
            "totals": {},
        }

    since_ms = int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            cur = conn.cursor()
            tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "session" not in tables:
                return {
                    "available": False,
                    "message": "opencode 数据库缺少 session 表",
                    "days": days,
                    "db_path": str(db_path),
                    "model_stats": [],
                    "totals": {},
                }

            # 新版 opencode：session 表自带聚合列（tokens_input 等），直接按模型聚合
            session_cols = {r[1] for r in cur.execute("PRAGMA table_info(session)")}
            has_message_table = "message" in tables
            if "tokens_input" in session_cols:
                result = _aggregate_from_session(cur, since_ms, has_message_table)
            else:
                result = _aggregate_from_messages(cur, since_ms)
            result["days"] = days
            return result
        finally:
            conn.close()
    except sqlite3.Error as e:
        return {
            "available": False,
            "message": f"读取 opencode 数据库失败: {e}",
            "days": days,
            "db_path": str(db_path),
            "model_stats": [],
            "totals": {},
        }


def _norm_model(model: str) -> str:
    """新版 opencode 的 model 字段是 JSON 字符串，提取可读形式。"""
    if model.startswith("{"):
        try:
            d = json.loads(model)
            parts = [str(d.get("id") or model)]
            if d.get("providerID"):
                parts.append(str(d["providerID"]))
            if d.get("variant"):
                parts.append(str(d["variant"]))
            return " · ".join(parts)
        except (json.JSONDecodeError, AttributeError):
            return model
    return model


def _count_assistant_messages_by_model(cur, since_ms: int) -> dict[str, int]:
    """按 session.model 分组统计真实 assistant 消息数（用于新版 session 聚合列
    场景下的 messages 字段——session 表本身只有会话级 rollup，没有消息条数）。"""
    cur.execute(
        "SELECT s.model, msg.data FROM message msg JOIN session s ON s.id = msg.session_id "
        "WHERE s.time_updated >= ? AND s.time_archived IS NULL",
        (since_ms,),
    )
    counts: dict[str, int] = {}
    for model, data_json in cur.fetchall():
        try:
            data = json.loads(data_json) if data_json else None
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("role") != "assistant":
            continue
        key = _norm_model(str(model or "unknown"))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _aggregate_from_session(cur, since_ms: int, has_message_table: bool) -> dict:
    """新版 opencode：session 表自带 tokens/cost 聚合列，直接按模型聚合。"""
    cur.execute(
        "SELECT COUNT(*) FROM session WHERE time_updated >= ? AND time_archived IS NULL",
        (since_ms,),
    )
    sessions = int(cur.fetchone()[0])

    cur.execute(
        "SELECT COALESCE(model, 'unknown') AS m, COUNT(*) AS n, "
        "COALESCE(SUM(tokens_input),0), COALESCE(SUM(tokens_output),0), "
        "COALESCE(SUM(tokens_reasoning),0), COALESCE(SUM(tokens_cache_read),0), "
        "COALESCE(SUM(tokens_cache_write),0), COALESCE(SUM(cost),0) "
        "FROM session WHERE time_updated >= ? AND time_archived IS NULL GROUP BY m "
        "ORDER BY n DESC",
        (since_ms,),
    )
    rows = cur.fetchall()
    # 消息数不能从 session 表得出（那只是会话级 rollup），需要单独从 message 表
    # 按真实消息行数统计，否则只能拿 session 计数充数——语义是错的（那是会话数，
    # 不是消息数）。
    message_counts = _count_assistant_messages_by_model(cur, since_ms) if has_message_table else {}
    model_stats = [
        {
            "model": _norm_model(r[0]),
            "sessions": int(r[1]),
            "messages": message_counts.get(_norm_model(r[0]), 0),
            "input": int(r[2]),
            "output": int(r[3]),
            "reasoning": int(r[4]),
            "cache_read": int(r[5]),
            "cache_write": int(r[6]),
            "cost": round(float(r[7]), 4),
        }
        for r in rows
    ]
    totals = {
        "sessions": sessions,
        "messages": sum(message_counts.values()) if has_message_table else sum(m["sessions"] for m in model_stats),
        "input": sum(m["input"] for m in model_stats),
        "output": sum(m["output"] for m in model_stats),
        "cache_read": sum(m["cache_read"] for m in model_stats),
        "cache_write": sum(m["cache_write"] for m in model_stats),
        "cost": round(sum(m["cost"] for m in model_stats), 4),
        "has_cost": any(m["cost"] > 0 for m in model_stats),
    }
    return {
        "available": True,
        "message": None if has_message_table else "opencode 数据库缺少 message 表，messages 计数回退为会话数（不精确）",
        "db_path": _db_path_str(),
        "model_stats": model_stats,
        "totals": totals,
    }


def _aggregate_from_messages(cur, since_ms: int) -> dict:
    """更老的 opencode：session 表没有聚合列，从 message.data JSON 解析。

    session.time_updated 只是"最后一次更新时间"，同一个长期存活的会话里可能混着
    很久以前的历史消息——只按 session.time_updated 粗筛会把该会话的全部历史消息
    都计入，统计偏大。这里用 session.time_updated 做粗筛缩小扫描范围后，再按每条
    消息自身的时间戳（data.time.created）精确过滤。
    """
    cur.execute(
        "SELECT m.session_id, m.data FROM message m JOIN session s ON s.id = m.session_id WHERE s.time_updated >= ?",
        (since_ms,),
    )
    totals = dict(EMPTY_TOTALS)
    sessions_seen: set[str] = set()
    by_model: dict[str, dict] = {}
    for session_id, data_json in cur.fetchall():
        parsed = _parse_message_data(data_json)
        if parsed is None:
            continue
        created = parsed.get("created")
        if created is not None and created < since_ms:
            continue  # 消息自身时间早于窗口——所属会话最近被更新过，但这条消息不算
        sessions_seen.add(session_id)
        totals["messages"] += 1
        totals["input"] += parsed["input"]
        totals["output"] += parsed["output"]
        totals["cache_read"] += parsed["cache_read"]
        totals["cache_write"] += parsed["cache_write"]
        if parsed["cost"] is not None:
            totals["cost"] += parsed["cost"]
            totals["has_cost"] = True
        model = _norm_model(parsed["model"])
        entry = by_model.setdefault(
            model,
            {
                "sessions": set(),
                "messages": 0,
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "cost": 0.0,
                "has_cost": False,
            },
        )
        entry["sessions"].add(session_id)
        entry["messages"] += 1
        entry["input"] += parsed["input"]
        entry["output"] += parsed["output"]
        entry["cache_read"] += parsed["cache_read"]
        entry["cache_write"] += parsed["cache_write"]
        if parsed["cost"] is not None:
            entry["cost"] += parsed["cost"]
            entry["has_cost"] = True
    totals["sessions"] = len(sessions_seen)
    totals["cost"] = round(totals["cost"], 4)
    model_stats = [
        {
            "model": model,
            "sessions": len(stats["sessions"]),
            "messages": stats["messages"],
            "input": stats["input"],
            "output": stats["output"],
            "cache_read": stats["cache_read"],
            "cache_write": stats["cache_write"],
            "cost": round(stats["cost"], 4),
            "has_cost": stats["has_cost"],
        }
        for model, stats in sorted(by_model.items(), key=lambda kv: -(kv[1]["input"] + kv[1]["output"]))
    ]
    return {
        "available": True,
        "message": None,
        "db_path": _db_path_str(),
        "model_stats": model_stats,
        "totals": totals,
    }


# ── 统一多数据源接口 ─────────────────────────────────────────────


def get_local_usage(days: int = 14) -> dict:
    """GET /api/local-usage 的数据来源：汇总所有本地统计数据源。

    每个 source 字段名保持一致：key / label / available / message / path /
    model_stats / totals；不可用时 model_stats=[]、totals={}。
    """
    claude = get_claude_code_usage(days=days)

    opencode_raw = get_opencode_usage(days=days)
    opencode = {
        "key": "opencode",
        "label": "OpenCode 本地已用统计",
        "available": bool(opencode_raw.get("available")),
        "message": opencode_raw.get("message"),
        "path": opencode_raw.get("db_path") or None,
        "model_stats": opencode_raw.get("model_stats") or [],
        "totals": opencode_raw.get("totals") or {},
    }

    return {"days": days, "sources": [claude, opencode]}
