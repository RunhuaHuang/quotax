"""用量统计的 SQLite 存储：落库解析结果 + 增量游标 + 聚合查询。

参考 Buktal/VaultOne 的 db 层设计（schema.rs / store_reads.rs），但做了简化：
- 单设备（无 device_id 维度——本面板是单机本地工具，不需要多机合并）；
- 增量游标保存 JSONL 源的 (mtime, line_offset, last_model)，SQLite 源（opencode）保存
  时间戳兼容值和 rowid 水位；
- 聚合全部实时 GROUP BY，不预计算 rollup 表（数据量级远小于 VaultOne 的多端场景）。

主键用 (source, uuid)：uuid 是各解析器产出的稳定去重 id（如 claude 用 message.id，
OpenCode 老版消息还会加入 rowid），同一来源内 uuid 唯一即可。重复解析同一文件不会
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
_conn_lock = threading.Lock()


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
        # 用独立锁创建连接，避免与业务锁 _lock 重入死锁
        with _conn_lock:
            if _conn is None:
                DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                conn = _connect()
                _init_schema(conn)
                _conn = conn
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
            pricing_model    TEXT,                   -- 原始模型名副本（与 model 一致；当前无查询使用，保留备用）
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
        -- rebill 只扫 has_cost=0 的记录：部分索引让候选集从全表缩小到无价行，
        -- 否则 CAST(total_cost_usd AS REAL) 无法走索引，大库全表扫描 + 逐行更新
        -- 会长时间卡住所有用量查询（它们共用同一个连接锁）。
        CREATE INDEX IF NOT EXISTS idx_usage_rebill ON usage_records(has_cost) WHERE has_cost = 0;

        -- 增量扫描游标：JSONL 源记 (mtime_ns, line_offset)；SQLite 源记水位线。
        CREATE TABLE IF NOT EXISTS scan_progress (
            source            TEXT NOT NULL,
            file_path         TEXT NOT NULL,
            last_modified_ns  INTEGER,
            last_line_offset  INTEGER,
            watermark_ms      INTEGER,
            watermark_rowid   INTEGER,
            last_model        TEXT,
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

        -- 低频维护任务的运行水位（例如历史保留清理），避免每次 API 请求都重复做。
        CREATE TABLE IF NOT EXISTS app_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    # Existing installations were created before rowid/model-aware cursors
    # existed. CREATE TABLE IF NOT EXISTS does not migrate those tables, so
    # add nullable columns explicitly while keeping old databases usable.
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(scan_progress)")}
    for column, definition in (("watermark_rowid", "INTEGER"), ("last_model", "TEXT")):
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE scan_progress ADD COLUMN {column} {definition}")
    conn.commit()


def reset_for_test() -> None:
    """测试专用：关闭并重置全局连接（配合 monkeypatch DB_PATH 到 tmp 路径）。"""
    close()


def close() -> None:
    """关闭全局连接；退出前先 checkpoint WAL（TRUNCATE），让 -wal/-shm 落回主库。

    应用退出时若直接丢弃连接，WAL 可能长期不 checkpoint，-wal/-shm 文件持续
    膨胀（get_database_status 统计的 size 包含它们）。TRUNCATE 模式把已提交内容
    收进主文件并截断 WAL，优雅退出后不会留下巨大的临时文件。
    """
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                # checkpoint 失败不影响关闭（WAL 内容仍已持久化，只是没合并回主文件）
                pass
            _conn.close()
            _conn = None


# ── 写入：解析后的记录批量入库 ──────────────────────────────────


def _day_from_ts(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def upsert_records(source: str, records: list[dict], *, update_existing: bool = False) -> int:
    """批量入库解析记录。

    默认按 (source, uuid) 主键去重（INSERT OR IGNORE）。对于 OpenCode 新版 session
    表，token 列是同一 session 的当前聚合值而不是不可变事件；调用方可传
    ``update_existing=True`` 让后续扫描覆盖旧聚合值。

    records 每项字段：
        uuid, timestamp_ms, model, pricing_model, session_id,
        input_tokens, output_tokens, cache_creation, cache_read,
        input_cost_usd, output_cost_usd, cache_read_cost, cache_create_cost,
        total_cost_usd, has_cost, stop_reason

    返回 SQLite 报告的受影响行数。默认模式下重复行不计；更新模式下同 UUID 的
    新聚合值会计为一次受影响操作。
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
        statement = """INSERT INTO usage_records
               (source, uuid, timestamp_ms, day, model, pricing_model, session_id,
                input_tokens, output_tokens, cache_creation, cache_read,
                input_cost_usd, output_cost_usd, cache_read_cost, cache_create_cost,
                total_cost_usd, has_cost, stop_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        if update_existing:
            statement += """ ON CONFLICT(source, uuid) DO UPDATE SET
                timestamp_ms=excluded.timestamp_ms,
                day=excluded.day,
                model=excluded.model,
                pricing_model=excluded.pricing_model,
                session_id=excluded.session_id,
                input_tokens=excluded.input_tokens,
                output_tokens=excluded.output_tokens,
                cache_creation=excluded.cache_creation,
                cache_read=excluded.cache_read,
                input_cost_usd=excluded.input_cost_usd,
                output_cost_usd=excluded.output_cost_usd,
                cache_read_cost=excluded.cache_read_cost,
                cache_create_cost=excluded.cache_create_cost,
                total_cost_usd=excluded.total_cost_usd,
                has_cost=excluded.has_cost,
                stop_reason=excluded.stop_reason"""
        else:
            statement = statement.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)
        try:
            cur = conn.executemany(statement, rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return cur.rowcount


# ── 增量游标 ────────────────────────────────────────────────────


def get_cursor(source: str, file_path: str) -> dict | None:
    conn = get_conn()
    with _lock:
        row = conn.execute(
            "SELECT last_modified_ns, last_line_offset, watermark_ms, "
            "watermark_rowid, last_model "
            "FROM scan_progress WHERE source=? AND file_path=?",
            (source, file_path),
        ).fetchone()
    if row is None:
        return None
    return {
        "last_modified_ns": row["last_modified_ns"],
        "last_line_offset": row["last_line_offset"],
        "watermark_ms": row["watermark_ms"],
        "watermark_rowid": row["watermark_rowid"],
        "last_model": row["last_model"],
    }


def set_cursor(
    source: str,
    file_path: str,
    last_modified_ns: int | None = None,
    last_line_offset: int | None = None,
    watermark_ms: int | None = None,
    watermark_rowid: int | None = None,
    last_model: str | None = None,
) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            """INSERT INTO scan_progress
               (source, file_path, last_modified_ns, last_line_offset, watermark_ms,
                watermark_rowid, last_model)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(source, file_path) DO UPDATE SET
                 last_modified_ns=COALESCE(excluded.last_modified_ns, scan_progress.last_modified_ns),
                 last_line_offset=COALESCE(excluded.last_line_offset, scan_progress.last_line_offset),
                 watermark_ms=COALESCE(excluded.watermark_ms, scan_progress.watermark_ms),
                 watermark_rowid=COALESCE(excluded.watermark_rowid, scan_progress.watermark_rowid),
                 last_model=COALESCE(excluded.last_model, scan_progress.last_model)""",
            (
                source,
                file_path,
                last_modified_ns,
                last_line_offset,
                watermark_ms,
                watermark_rowid,
                last_model,
            ),
        )
        conn.commit()


def list_cursors(source: str) -> list[dict]:
    conn = get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT file_path, last_modified_ns, last_line_offset, watermark_ms, "
            "watermark_rowid, last_model "
            "FROM scan_progress WHERE source=?",
            (source,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_cursor(source: str, file_path: str) -> None:
    """删除一个增量游标，用于采集落库失败后的回滚。"""
    conn = get_conn()
    with _lock:
        conn.execute(
            "DELETE FROM scan_progress WHERE source=? AND file_path=?",
            (source, file_path),
        )
        conn.commit()


# ── 健康诊断与历史保留清理 ──────────────────────────────────────


def _database_size_bytes() -> int:
    """统计 SQLite 主文件和 WAL/SHM 辅助文件的实际磁盘占用。"""
    total = 0
    for path in (DB_PATH, Path(f"{DB_PATH}-wal"), Path(f"{DB_PATH}-shm")):
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def get_database_status() -> dict:
    """轻量数据库诊断，不修改任何数据。"""
    if _conn is None and not DB_PATH.exists():
        return {
            "ok": True,
            "exists": False,
            "path": str(DB_PATH),
            "size_bytes": 0,
            "records": 0,
            "cursors": 0,
            "oldest_record_at": None,
            "newest_record_at": None,
            "last_cleanup_at": None,
        }
    conn = get_conn()
    with _lock:
        row = conn.execute(
            "SELECT COUNT(*) AS records, MIN(timestamp_ms) AS oldest, MAX(timestamp_ms) AS newest "
            "FROM usage_records"
        ).fetchone()
        cursors = conn.execute("SELECT COUNT(*) AS n FROM scan_progress").fetchone()["n"]
        cleanup_row = conn.execute("SELECT value FROM app_state WHERE key='usage_cleanup_at'").fetchone()
    return {
        "ok": True,
        "exists": True,
        "path": str(DB_PATH),
        "size_bytes": _database_size_bytes(),
        "records": int(row["records"]),
        "cursors": int(cursors),
        "oldest_record_at": int(row["oldest"]) if row["oldest"] is not None else None,
        "newest_record_at": int(row["newest"]) if row["newest"] is not None else None,
        "last_cleanup_at": int(cleanup_row["value"]) if cleanup_row else None,
    }


def cleanup_old_records(retention_days: int, *, force: bool = False, now_ms: int | None = None) -> dict:
    """删除保留期之外的用量记录和已不存在文件的游标。

    默认 24 小时内最多实际执行一次；force=True 供显式维护端点和测试使用。
    不主动 VACUUM，避免大库清理时长时间锁表；WAL 会在后续 checkpoint 中复用空间。
    """
    retention_days = min(max(int(retention_days), 1), 3650)
    current_ms = int(now_ms if now_ms is not None else datetime.now(UTC).timestamp() * 1000)
    conn = get_conn()
    with _lock:
        last_row = conn.execute("SELECT value FROM app_state WHERE key='usage_cleanup_at'").fetchone()
        last_cleanup_at = int(last_row["value"]) if last_row else None
        if not force and last_cleanup_at is not None and current_ms - last_cleanup_at < 86_400_000:
            return {
                "skipped": True,
                "reason": "24 小时内已执行过清理",
                "last_cleanup_at": last_cleanup_at,
                "deleted_records": 0,
                "deleted_cursors": 0,
                "size_bytes": _database_size_bytes(),
            }

        cutoff_ms = current_ms - retention_days * 86_400_000
        deleted_records = conn.execute(
            "DELETE FROM usage_records WHERE timestamp_ms < ?", (cutoff_ms,)
        ).rowcount

        cursor_rows = conn.execute("SELECT source, file_path FROM scan_progress").fetchall()
        stale_cursors = []
        for row in cursor_rows:
            path = Path(row["file_path"])
            # 解析器目前都存绝对路径；只清理明确的绝对路径，避免未来新增的逻辑游标
            # 名称被 Path.exists() 误判后删除。
            if path.is_absolute() and not path.exists():
                stale_cursors.append((row["source"], row["file_path"]))
        if stale_cursors:
            conn.executemany("DELETE FROM scan_progress WHERE source=? AND file_path=?", stale_cursors)

        conn.execute(
            "INSERT INTO app_state(key,value) VALUES('usage_cleanup_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(current_ms),),
        )
        conn.commit()

    return {
        "skipped": False,
        "retention_days": retention_days,
        "cutoff_at": cutoff_ms,
        "last_cleanup_at": current_ms,
        "deleted_records": int(deleted_records),
        "deleted_cursors": len(stale_cursors),
        "size_bytes": _database_size_bytes(),
    }


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
    from . import pricing

    conn = get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT model_key, input_per_million, output_per_million, "
            "cache_read_per_million, cache_creation_per_million, is_builtin FROM model_pricing"
        ).fetchall()
    for r in rows:
        try:
            key = r["model_key"]
            normalized = pricing.normalize_key(key)
            persisted_builtin = bool(r["is_builtin"])
            # 旧版本曾把“手动覆盖内置模型”错误保存成 is_builtin=1。若持久化值
            # 与当前源码默认值不同，按覆盖项迁移，恢复按钮才不会被永久隐藏。
            if persisted_builtin and normalized in pricing.BUILTIN_PRICES:
                defaults = pricing.BUILTIN_PRICES[normalized]
                persisted_values = tuple(
                    pricing._to_decimal(r[field])
                    for field in (
                        "input_per_million",
                        "output_per_million",
                        "cache_read_per_million",
                        "cache_creation_per_million",
                    )
                )
                if persisted_values != tuple(pricing._to_decimal(v) for v in defaults):
                    persisted_builtin = False
            book.upsert(
                key,
                r["input_per_million"],
                r["output_per_million"],
                r["cache_read_per_million"],
                r["cache_creation_per_million"],
                is_builtin=persisted_builtin,
            )
        except (TypeError, ValueError) as e:
            logger.warning("忽略无效的持久化模型单价 %r: %s", r["model_key"], e)


# ── rebill：只补 0 成本记录 ─────────────────────────────────────


# rebill 的逐行 UPDATE 语句（复用，避免在循环里重复拼字符串）
_REBILL_UPDATE_SQL = """UPDATE usage_records SET
                   input_cost_usd=?, output_cost_usd=?, cache_read_cost=?,
                   cache_create_cost=?, total_cost_usd=?, has_cost=1,
                   pricing_model=?
                   WHERE source=? AND uuid=?"""


def rebill_zero_cost(book) -> dict:
    """用最新 PricingBook 重算所有"当初无价记为 0"的记录成本。

    与 VaultOne 的 rebill_zero_cost 一致：只补 total_cost_usd <= 0 且 has_cost=0
    的记录；已有真实价格的记录不动（避免历史账目被反复改写）。

    返回 {recounted, still_zero}：重算后仍为 0 的（新单价表也没匹配上）计数。

    性能与并发：改用独立连接 + 批量提交执行。SQLite 单连接 + 全局 _lock 的模型
    下，原实现会在持锁状态下全表 fetch + 逐行 UPDATE，大库（数十万行）会让所有
    用量查询接口（overview/trend/log）在这期间全部排队。独立连接在 WAL 模式下
    读取者可以并行，只在与采集写入重叠时有短暂写锁等待（timeout=10 兜底）；
    批量 commit 进一步缩短单次持锁时间。部分索引 idx_usage_rebill 把候选集限制
    在 has_cost=0 的行内（见 _init_schema）。
    """
    from .pricing import calc_cost  # 局部导入避免循环

    # 先走 get_conn() 确保 schema（含部分索引）已初始化，再开独立连接跑长事务
    get_conn()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT source, uuid, model, input_tokens, output_tokens, "
            "cache_creation, cache_read FROM usage_records "
            "WHERE has_cost = 0 AND CAST(total_cost_usd AS REAL) <= 0"
        ).fetchall()
        recounted = 0
        still_zero = 0
        updates: list[tuple] = []

        def _flush() -> None:
            nonlocal updates
            if updates:
                conn.executemany(_REBILL_UPDATE_SQL, updates)
                conn.commit()
                updates = []

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
            updates.append(
                (
                    str(cost["input_usd"]),
                    str(cost["output_usd"]),
                    str(cost["cache_read_usd"]),
                    str(cost["cache_creation_usd"]),
                    str(cost["total_usd"]),
                    r["model"],
                    r["source"],
                    r["uuid"],
                )
            )
            recounted += 1
            if len(updates) >= 1000:  # 分批提交，避免单事务持有写锁过长
                _flush()
        _flush()
    finally:
        conn.close()
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


def _calendar_window_start_ms(days: int) -> int:
    """返回包含今天在内的最近 ``days`` 个 UTC 自然日的起点。"""
    day_count = max(int(days), 1)
    start = datetime.now(UTC).date() - timedelta(days=day_count - 1)
    return int(datetime.combine(start, datetime.min.time(), tzinfo=UTC).timestamp() * 1000)


def query_overview(days: int = 14, source: str | None = None, model: str | None = None) -> dict:
    """总览：四桶 token 总量 + 总成本 + 缓存命中率 + 请求数 + 会话数。

    缓存命中率口径与 VaultOne 一致：cache_read / (input + cache_creation + cache_read)，
    output 不进分母（输出不占缓存池）。分母 ≤ 0 时返回 0。
    """
    since_ms = _calendar_window_start_ms(days)
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
    today = datetime.now(UTC).date()
    start = today - timedelta(days=max(days, 1) - 1)
    since_ms = int(datetime.combine(start, datetime.min.time(), tzinfo=UTC).timestamp() * 1000)
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
    since_ms = _calendar_window_start_ms(days)
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
        rest_tokens = sum(int(r["tot_tokens"]) for r in rest)
        rest_cost = sum(float(r["tot_cost"]) for r in rest)
        # 聚合行：展示文案由前端按当前语言本地化（见 i18n 的 usage.otherLabel，
        # count 取 other_count）；is_other=True 标记该行是聚合行，前端据此跳过
        # 点击筛选，不依赖展示字符串做逻辑判断（避免 i18n 误判）。model 字段对
        # 聚合行无实际语义，用 "_other" 占位即可（前端不读取它）。
        out.append(
            {
                "model": "_other",
                "is_other": True,
                "other_count": len(rest),
                "tokens": rest_tokens,
                "cost": round(rest_cost, 6),
                "requests": sum(int(r["reqs"]) for r in rest),
                "pct_tokens": round(rest_tokens / total_tokens * 100, 1) if total_tokens > 0 else 0.0,
                "pct_cost": round(rest_cost / total_cost * 100, 1) if total_cost > 0 else 0.0,
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
    since_ms = _calendar_window_start_ms(days)
    conn = get_conn()
    where, params = _build_where(since_ms, None, source, model)
    with _lock:
        rows = conn.execute(
            f"""SELECT source, model, timestamp_ms, session_id,
                input_tokens, output_tokens, cache_creation, cache_read,
                CAST(total_cost_usd AS REAL) AS cost, has_cost, stop_reason
                FROM usage_records{where}
                ORDER BY timestamp_ms DESC LIMIT ? OFFSET ?""",
            (*params, limit + 1, offset),
        ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
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
    since_ms = _calendar_window_start_ms(days)
    conn = get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT model, COUNT(*) AS n FROM usage_records WHERE timestamp_ms >= ? "
            "GROUP BY model ORDER BY n DESC",
            (since_ms,),
        ).fetchall()
    return [r["model"] or "unknown" for r in rows]
