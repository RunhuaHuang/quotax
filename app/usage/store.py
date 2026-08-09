"""用量统计的 SQLite 存储：落库解析结果 + 增量游标 + 聚合查询。

参考 Buktal/VaultOne 的 db 层设计（schema.rs / store_reads.rs），但做了简化：
- 单设备（无 device_id 维度——本面板是单机本地工具，不需要多机合并）；
- 增量游标只存 JSONL 源的 (mtime, line_offset)，SQLite 源（opencode）记水位线；
- 聚合全部实时 GROUP BY，不预计算 rollup 表（数据量级远小于 VaultOne 的多端场景）。

主键用 (source, uuid)：uuid 是各解析器产出的稳定去重 id（如 claude 用 message.id，
opencode 用 session_id:created），同一来源内 uuid 唯一即可。重复解析同一文件不会
产生重复行（INSERT OR IGNORE）。

成本列以 TEXT 存 Decimal（与 VaultOne 一致，避免浮点精度问题），读取时转回 Decimal。

数据库路径默认在 config.json 同目录下的 usage.db，可用 QUOTABOARD_USAGE_DB 覆盖
（测试隔离用，与 config.py 的 QUOTABOARD_CONFIG 风格一致）。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import config as config_store

logger = logging.getLogger(__name__)

# usage.db 路径：与 config.json 同目录。测试时 monkeypatch DB_PATH 即可隔离，
# 不必依赖环境变量（与 config.CONFIG_PATH 的隔离方式一致）。
_DB_OVERRIDE = os.environ.get("QUOTABOARD_USAGE_DB")
DB_PATH: Path = (
    Path(_DB_OVERRIDE)
    if _DB_OVERRIDE
    else config_store.CONFIG_PATH.parent / "usage.db"
)

# 写入锁：SQLite 单连接 + 模块级锁，防止多线程并发写（采集在 to_thread 里跑）。
# 用 threading.Lock 而不是依赖 SQLite 自身锁——我们要保证 schema init / 写 / 读
# 在同一个连接生命周期内串行，避免 cursor 交叉。
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # WAL：读不阻塞写，opencode 也是 WAL
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn() -> sqlite3.Connection:
    """获取全局单例连接（惰性建表）。所有读写都走这个连接 + _lock 串行。"""
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                _conn = _connect()
                _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    """建表。IF NOT EXISTS 保证重复调用幂等。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS usage_records (
            source           TEXT NOT NULL,
            uuid             TEXT NOT NULL,
            timestamp_ms     INTEGER NOT NULL,
            day              TEXT NOT NULL,          -- YYYY-MM-DD (UTC)
            model            TEXT,                   -- 原始模型名
            pricing_model    TEXT,                   -- 归一化匹配键
            session_id       TEXT,
            input_tokens     INTEGER NOT NULL DEFAULT 0,
            output_tokens    INTEGER NOT NULL DEFAULT 0,
            cache_creation   INTEGER NOT NULL DEFAULT 0,
            cache_read       INTEGER NOT NULL DEFAULT 0,
            input_cost_usd   TEXT NOT NULL DEFAULT '0',
            output_cost_usd  TEXT NOT NULL DEFAULT '0',
            cache_read_cost  TEXT NOT NULL DEFAULT '0',
            cache_create_cost TEXT NOT NULL DEFAULT '0',
            total_cost_usd   TEXT NOT NULL DEFAULT '0',
            has_cost         INTEGER NOT NULL DEFAULT 0,
            stop_reason      TEXT,
            PRIMARY KEY (source, uuid)
        );
        CREATE INDEX IF NOT EXISTS idx_usage_day ON usage_records(day);
        CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_records(model);
        CREATE INDEX IF NOT EXISTS idx_usage_source ON usage_records(source);
        CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_records(timestamp_ms);
        CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_records(session_id);

        -- 增量扫描游标：JSONL 源记 (mtime_ns, line_offset)；SQLite 源记水位线。
        CREATE TABLE IF NOT EXISTS scan_progress (
            source            TEXT NOT NULL,
            file_path         TEXT NOT NULL,
            last_modified_ns  INTEGER,
            last_line_offset  INTEGER,
            watermark_ms      INTEGER,
            PRIMARY KEY (source, file_path)
        );

        -- 单价表（手动编辑 + 内置的持久化副本，跨重启保留用户改过的价）。
        CREATE TABLE IF NOT EXISTS model_pricing (
            model_key              TEXT PRIMARY KEY,
            input_per_million      TEXT NOT NULL DEFAULT '0',
            output_per_million     TEXT NOT NULL DEFAULT '0',
            cache_read_per_million TEXT NOT NULL DEFAULT '0',
            cache_creation_per_million TEXT NOT NULL DEFAULT '0',
            is_builtin             INTEGER NOT NULL DEFAULT 0,
            updated_at             INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()


def reset_for_test() -> None:
    """测试专用：关闭并重置全局连接（配合 monkeypatch DB_PATH 到 tmp 路径）。"""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


# ── 写入：解析后的记录批量入库 ──────────────────────────────────


def _day_from_ts(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def upsert_records(source: str, records: list[dict]) -> int:
    """批量入库解析记录。按 (source, uuid) 主键去重（INSERT OR IGNORE）。

    records 每项字段：
        uuid, timestamp_ms, model, pricing_model, session_id,
        input_tokens, output_tokens, cache_creation, cache_read,
        input_cost_usd, output_cost_usd, cache_read_cost, cache_create_cost,
        total_cost_usd, has_cost, stop_reason

    返回实际新插入的行数（被忽略的重复行不计）。已在库里的同 uuid 不会更新——
    增量游标保证我们只解析新内容，重复入库说明游标失效，此时保持旧值更安全。
    """
    if not records:
        return 0
    conn = get_conn()
    rows = []
    for r in records:
        ts = int(r.get("timestamp_ms") or 0)
        rows.append(
            (
                source,
                str(r.get("uuid") or ""),
                ts,
                _day_from_ts(ts),
                r.get("model"),
                r.get("pricing_model"),
                r.get("session_id"),
                int(r.get("input_tokens") or 0),
                int(r.get("output_tokens") or 0),
                int(r.get("cache_creation") or 0),
                int(r.get("cache_read") or 0),
                str(r.get("input_cost_usd") or "0"),
                str(r.get("output_cost_usd") or "0"),
                str(r.get("cache_read_cost") or "0"),
                str(r.get("cache_create_cost") or "0"),
                str(r.get("total_cost_usd") or "0"),
                1 if r.get("has_cost") else 0,
                r.get("stop_reason"),
            )
        )
    with _lock:
        cur = conn.executemany(
            """INSERT OR IGNORE INTO usage_records
               (source, uuid, timestamp_ms, day, model, pricing_model, session_id,
                input_tokens, output_tokens, cache_creation, cache_read,
                input_cost_usd, output_cost_usd, cache_read_cost, cache_create_cost,
                total_cost_usd, has_cost, stop_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()
        return cur.rowcount


# ── 增量游标 ────────────────────────────────────────────────────


def get_cursor(source: str, file_path: str) -> dict | None:
    conn = get_conn()
    with _lock:
        row = conn.execute(
            "SELECT last_modified_ns, last_line_offset, watermark_ms "
            "FROM scan_progress WHERE source=? AND file_path=?",
            (source, file_path),
        ).fetchone()
    if row is None:
        return None
    return {
        "last_modified_ns": row["last_modified_ns"],
        "last_line_offset": row["last_line_offset"],
        "watermark_ms": row["watermark_ms"],
    }


def set_cursor(
    source: str,
    file_path: str,
    last_modified_ns: int | None = None,
    last_line_offset: int | None = None,
    watermark_ms: int | None = None,
) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            """INSERT INTO scan_progress
               (source, file_path, last_modified_ns, last_line_offset, watermark_ms)
               VALUES (?,?,?,?,?)
               ON CONFLICT(source, file_path) DO UPDATE SET
                 last_modified_ns=excluded.last_modified_ns,
                 last_line_offset=excluded.last_line_offset,
                 watermark_ms=excluded.watermark_ms""",
            (source, file_path, last_modified_ns, last_line_offset, watermark_ms),
        )
        conn.commit()


def list_cursors(source: str) -> list[dict]:
    conn = get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT file_path, last_modified_ns, last_line_offset, watermark_ms "
            "FROM scan_progress WHERE source=?",
            (source,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── 定价表持久化 ────────────────────────────────────────────────


def save_pricing(book) -> None:
    """把 PricingBook 落库到 model_pricing 表（全量替换）。"""
    conn = get_conn()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    with _lock:
        conn.execute("DELETE FROM model_pricing")
        conn.executemany(
            """INSERT INTO model_pricing
               (model_key, input_per_million, output_per_million,
                cache_read_per_million, cache_creation_per_million, is_builtin, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            [
                (
                    e["model_key"],
                    str(e["input_per_million"]),
                    str(e["output_per_million"]),
                    str(e["cache_read_per_million"]),
                    str(e["cache_creation_per_million"]),
                    1 if e["is_builtin"] else 0,
                    now_ms,
                )
                for e in book.entries()
            ],
        )
        conn.commit()


def load_pricing(book) -> None:
    """从 model_pricing 表加载用户持久化的单价，合并进 book（覆盖同名内置价）。

    book 先在 __init__ 里加载内置价，这里把库里的（可能是用户改过的 / LiteLLM 拉过的）
    覆盖上去——库里的 is_builtin 标记保留，这样前端能区分"这是我改的"vs"内置默认"。
    """
    conn = get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT model_key, input_per_million, output_per_million, "
            "cache_read_per_million, cache_creation_per_million, is_builtin FROM model_pricing"
        ).fetchall()
    for r in rows:
        book.upsert(
            r["model_key"],
            r["input_per_million"],
            r["output_per_million"],
            r["cache_read_per_million"],
            r["cache_creation_per_million"],
            is_builtin=bool(r["is_builtin"]),
        )


# ── rebill：只补 0 成本记录 ─────────────────────────────────────


def rebill_zero_cost(book) -> dict:
    """用最新 PricingBook 重算所有"当初无价记为 0"的记录成本。

    与 VaultOne 的 rebill_zero_cost 一致：只补 total_cost_usd <= 0 且 has_cost=0
    的记录；已有真实价格的记录不动（避免历史账目被反复改写）。

    返回 {recounted, still_zero}：重算后仍为 0 的（新单价表也没匹配上）计数。
    """
    from .pricing import calc_cost  # 局部导入避免循环

    conn = get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT source, uuid, model, input_tokens, output_tokens, "
            "cache_creation, cache_read FROM usage_records "
            "WHERE CAST(total_cost_usd AS REAL) <= 0 AND has_cost = 0"
        ).fetchall()
        recounted = 0
        still_zero = 0
        for r in rows:
            rate = book.resolve(r["model"] or "")
            if rate.is_unknown():
                still_zero += 1
                continue
            cost = calc_cost(
                {
                    "input": r["input_tokens"],
                    "output": r["output_tokens"],
                    "cache_creation": r["cache_creation"],
                    "cache_read": r["cache_read"],
                },
                rate,
            )
            if cost["total_usd"] <= 0:
                still_zero += 1
                continue
            conn.execute(
                """UPDATE usage_records SET
                   input_cost_usd=?, output_cost_usd=?, cache_read_cost=?,
                   cache_create_cost=?, total_cost_usd=?, has_cost=1,
                   pricing_model=?
                   WHERE source=? AND uuid=?""",
                (
                    str(cost["input_usd"]),
                    str(cost["output_usd"]),
                    str(cost["cache_read_usd"]),
                    str(cost["cache_creation_usd"]),
                    str(cost["total_usd"]),
                    r["model"],
                    r["source"],
                    r["uuid"],
                ),
            )
            recounted += 1
        conn.commit()
    return {"recounted": recounted, "still_zero": still_zero}


# ── 聚合查询 ────────────────────────────────────────────────────


def _dec(row, col) -> float:
    v = row[col]
    return float(v) if v is not None else 0.0


def _build_where(from_ts: int | None, to_ts: int | None, source: str | None, model: str | None):
    """构造 WHERE 子句 + 参数。时间用 timestamp_ms，源 / 模型精确匹配。"""
    clauses: list[str] = []
    params: list = []
    if from_ts is not None:
        clauses.append("timestamp_ms >= ?")
        params.append(from_ts)
    if to_ts is not None:
        clauses.append("timestamp_ms < ?")
        params.append(to_ts)
    if source:
        clauses.append("source = ?")
        params.append(source)
    if model:
        # 模型筛选用原始 model 列；定价归一化键不参与前端筛选
        clauses.append("model = ?")
        params.append(model)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def query_overview(days: int = 14, source: str | None = None, model: str | None = None) -> dict:
    """总览：四桶 token 总量 + 总成本 + 缓存命中率 + 请求数 + 会话数。

    缓存命中率口径与 VaultOne 一致：cache_read / (input + cache_creation + cache_read)，
    output 不进分母（输出不占缓存池）。分母 ≤ 0 时返回 0。
    """
    since_ms = int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)
    conn = get_conn()
    where, params = _build_where(since_ms, None, source, model)
    with _lock:
        row = conn.execute(
            f"""SELECT
                COALESCE(SUM(input_tokens),0) AS inp,
                COALESCE(SUM(output_tokens),0) AS outp,
                COALESCE(SUM(cache_creation),0) AS cc,
                COALESCE(SUM(cache_read),0) AS cr,
                COALESCE(SUM(CAST(total_cost_usd AS REAL)),0) AS cost,
                SUM(CASE WHEN has_cost=1 THEN 1 ELSE 0 END) AS cost_rows,
                COUNT(*) AS reqs,
                COUNT(DISTINCT session_id) AS sessions
                FROM usage_records{where}""",
            params,
        ).fetchone()
    inp = int(row["inp"])
    outp = int(row["outp"])
    cc = int(row["cc"])
    cr = int(row["cr"])
    denom = inp + cc + cr
    hit_rate = round(cr / denom, 4) if denom > 0 else 0.0
    return {
        "days": days,
        "input": inp,
        "output": outp,
        "cache_creation": cc,
        "cache_read": cr,
        "total_tokens": inp + outp + cc + cr,
        "cost": round(float(row["cost"]), 6),
        "has_cost": int(row["cost_rows"] or 0) > 0,
        "cache_hit_rate": hit_rate,
        "requests": int(row["reqs"]),
        "sessions": int(row["sessions"]),
    }


def query_trend(days: int = 14, source: str | None = None, model: str | None = None) -> list[dict]:
    """按天趋势：每天的四桶 token + 成本。用于趋势曲线。

    补零：窗口内缺失的日期填 0，保证前端曲线连续。返回按 day 升序的数组。
    """
    since_ms = int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)
    conn = get_conn()
    where, params = _build_where(since_ms, None, source, model)
    with _lock:
        rows = conn.execute(
            f"""SELECT day,
                COALESCE(SUM(input_tokens),0) AS inp,
                COALESCE(SUM(output_tokens),0) AS outp,
                COALESCE(SUM(cache_creation),0) AS cc,
                COALESCE(SUM(cache_read),0) AS cr,
                COALESCE(SUM(CAST(total_cost_usd AS REAL)),0) AS cost
                FROM usage_records{where}
                GROUP BY day ORDER BY day""",
            params,
        ).fetchall()
    by_day = {r["day"]: r for r in rows}
    # 补零：从 since 当天到今天，逐天填
    out: list[dict] = []
    today = datetime.now(UTC).date()
    start = (datetime.now(UTC) - timedelta(days=days)).date()
    d = start
    while d <= today:
        key = d.strftime("%Y-%m-%d")
        r = by_day.get(key)
        if r:
            out.append(
                {
                    "day": key,
                    "input": int(r["inp"]),
                    "output": int(r["outp"]),
                    "cache_creation": int(r["cc"]),
                    "cache_read": int(r["cr"]),
                    "cost": round(float(r["cost"]), 6),
                }
            )
        else:
            out.append({"day": key, "input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "cost": 0.0})
        d += timedelta(days=1)
    return out


def query_model_breakdown(
    days: int = 14, source: str | None = None, metric: str = "tokens"
) -> list[dict]:
    """按模型分布。metric=tokens 按总 token 排序，metric=cost 按成本排序。

    返回 Top N + "其他"汇总（N=8）。每项含 model / tokens / cost / requests / pct。
    """
    since_ms = int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)
    conn = get_conn()
    where, params = _build_where(since_ms, None, source, None)
    order = "tot_cost DESC" if metric == "cost" else "tot_tokens DESC"
    with _lock:
        rows = conn.execute(
            f"""SELECT COALESCE(model,'unknown') AS m,
                COALESCE(SUM(input_tokens+output_tokens+cache_creation+cache_read),0) AS tot_tokens,
                COALESCE(SUM(CAST(total_cost_usd AS REAL)),0) AS tot_cost,
                COUNT(*) AS reqs
                FROM usage_records{where}
                GROUP BY m ORDER BY {order}""",
            params,
        ).fetchall()
    total_tokens = sum(int(r["tot_tokens"]) for r in rows)
    total_cost = sum(float(r["tot_cost"]) for r in rows)
    out: list[dict] = []
    top = rows[:8]
    for r in top:
        t = int(r["tot_tokens"])
        c = float(r["tot_cost"])
        out.append(
            {
                "model": r["m"],
                "tokens": t,
                "cost": round(c, 6),
                "requests": int(r["reqs"]),
                "pct_tokens": round(t / total_tokens * 100, 1) if total_tokens > 0 else 0.0,
                "pct_cost": round(c / total_cost * 100, 1) if total_cost > 0 else 0.0,
            }
        )
    rest = rows[8:]
    if rest:
        out.append(
            {
                "model": f"其他 ({len(rest)})",
                "tokens": sum(int(r["tot_tokens"]) for r in rest),
                "cost": round(sum(float(r["tot_cost"]) for r in rest), 6),
                "requests": sum(int(r["reqs"]) for r in rest),
                "pct_tokens": round(sum(int(r["tot_tokens"]) for r in rest) / total_tokens * 100, 1)
                if total_tokens > 0
                else 0.0,
                "pct_cost": 0.0,
            }
        )
    return out


def query_request_log(
    days: int = 14,
    source: str | None = None,
    model: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """逐请求日志（分页）。按时间倒序，最近 limit 条。"""
    since_ms = int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)
    conn = get_conn()
    where, params = _build_where(since_ms, None, source, model)
    with _lock:
        rows = conn.execute(
            f"""SELECT source, model, timestamp_ms, session_id,
                input_tokens, output_tokens, cache_creation, cache_read,
                CAST(total_cost_usd AS REAL) AS cost, has_cost, stop_reason
                FROM usage_records{where}
                ORDER BY timestamp_ms DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
    return {
        "limit": limit,
        "offset": offset,
        "rows": [
            {
                "source": r["source"],
                "model": r["model"],
                "timestamp_ms": int(r["timestamp_ms"]),
                "session_id": r["session_id"],
                "input": int(r["input_tokens"]),
                "output": int(r["output_tokens"]),
                "cache_creation": int(r["cache_creation"]),
                "cache_read": int(r["cache_read"]),
                "total_tokens": int(r["input_tokens"] + r["output_tokens"] + r["cache_creation"] + r["cache_read"]),
                "cost": round(float(r["cost"] or 0), 6),
                "has_cost": bool(r["has_cost"]),
                "stop_reason": r["stop_reason"],
            }
            for r in rows
        ],
    }


def query_sources() -> list[dict]:
    """已入库的数据源列表 + 各自记录数、最早 / 最晚时间。供前端源筛选下拉。"""
    conn = get_conn()
    with _lock:
        rows = conn.execute(
            """SELECT source, COUNT(*) AS n,
               MIN(timestamp_ms) AS first_ts, MAX(timestamp_ms) AS last_ts
               FROM usage_records GROUP BY source ORDER BY source"""
        ).fetchall()
    return [
        {
            "source": r["source"],
            "records": int(r["n"]),
            "first_ts": int(r["first_ts"]) if r["first_ts"] else None,
            "last_ts": int(r["last_ts"]) if r["last_ts"] else None,
        }
        for r in rows
    ]


def query_models(days: int = 14) -> list[str]:
    """窗口内出现过的模型名列表（去重，按出现频次降序），供前端模型筛选下拉。"""
    since_ms = int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)
    conn = get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT model, COUNT(*) AS n FROM usage_records WHERE timestamp_ms >= ? "
            "GROUP BY model ORDER BY n DESC",
            (since_ms,),
        ).fetchall()
    return [r["model"] or "unknown" for r in rows]
