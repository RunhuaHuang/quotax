"""OpenCode SQLite 本地日志解析器（增量）。

数据库：~/.local/share/opencode/opencode.db（SQLite），表 session / message / part。
与 app/local_usage.py 的 get_opencode_usage 同源，但这里增量落库（记水位线）。

字段提取（与 local_usage.py 一致）：
- session 表新版自带 tokens_input 等聚合列 → 直接按会话聚合；
- 老版只有 message.data JSON → 解析 data.tokens / data.cost；
- 模型名 data.modelID 可能是 JSON 字符串，_norm_model 归一化。

增量游标：新版可变 session 聚合按 rowid 含最后一行重读，老版 message 事件按 rowid
严格递增；旧版本只有时间戳水位线时先完整迁移一次，避免秒/毫秒单位差异和同毫秒漏采。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

from ...models import to_ts
from .. import store
from . import BaseParser, CollectResult, NormalizedRecord

logger = logging.getLogger(__name__)


def _db_path() -> Path | None:
    override = os.environ.get("OPENCODE_DB")
    if override:
        return Path(override)
    for p in (
        Path.home() / ".local" / "share" / "opencode" / "opencode.db",
        Path.home() / ".config" / "opencode" / "opencode.db",
    ):
        if p.exists():
            return p
    return None


def _safe_int(v, default=0) -> int:
    if isinstance(v, bool):
        return default
    try:
        converted = int(v)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, converted)


def _norm_model(model: str) -> str:
    """新版 opencode 的 model 字段是 JSON 字符串，提取可读形式。"""
    if not model:
        return "unknown"
    if model.startswith("{"):
        try:
            d = json.loads(model)
            parts = [str(d.get("id") or model)]
            if d.get("providerID"):
                parts.append(str(d["providerID"]))
            return " · ".join(parts)
        except (json.JSONDecodeError, AttributeError):
            return model
    return model


def _parse_data(data_json: str | None) -> dict | None:
    if not data_json:
        return None
    try:
        data = json.loads(data_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("role") != "assistant":
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {}
    cache = tokens.get("cache")
    if not isinstance(cache, dict):
        cache = {}
    time_obj = data.get("time")
    created = time_obj.get("created") if isinstance(time_obj, dict) else None
    return {
        "input": _safe_int(tokens.get("input")),
        "output": _safe_int(tokens.get("output")),
        "reasoning": _safe_int(tokens.get("reasoning")),
        "cache_read": _safe_int(cache.get("read")),
        "cache_write": _safe_int(cache.get("write")),
        "model": _norm_model(str(data.get("modelID") or "unknown")),
        # OpenCode has emitted epoch seconds, epoch milliseconds and numeric
        # strings across versions; use the shared normaliser instead of only
        # accepting numeric Python values.
        "created": to_ts(created),
    }


class OpenCodeParser(BaseParser):
    source = "opencode"

    def discover(self) -> list[Path]:
        p = _db_path()
        return [p] if p else []

    def collect_incremental(self) -> CollectResult:
        result = CollectResult()
        db_path = _db_path()
        if db_path is None:
            return result
        result.files_scanned = 1
        try:
            self._scan_db(db_path, result)
        except sqlite3.Error as e:
            result.errors.append(f"opencode.db: {e}")
        return result

    def _scan_db(self, db_path: Path, result: CollectResult) -> None:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            cur = conn.cursor()
            tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "session" not in tables:
                result.errors.append("opencode.db 缺少 session 表")
                return
            session_cols = {r[1] for r in cur.execute("PRAGMA table_info(session)")}

            # 水位线：所有 session 文件共用一个虚拟游标键
            cursor = store.get_cursor(self.source, str(db_path))
            watermark = int(cursor.get("watermark_ms") or 0) if cursor else 0
            watermark_rowid = int(cursor.get("watermark_rowid") or 0) if cursor else 0
            new_watermark = watermark
            new_rowid = watermark_rowid

            if "tokens_input" in session_cols:
                records, scanned_rowid = self._from_session_table(cur, watermark, watermark_rowid, session_cols)
            else:
                records, scanned_rowid = self._from_messages(cur, watermark, watermark_rowid)

            for r in records:
                new_watermark = max(new_watermark, r.timestamp_ms)
            new_rowid = max(new_rowid, scanned_rowid)
            store.set_cursor(
                self.source,
                str(db_path),
                watermark_ms=new_watermark,
                watermark_rowid=new_rowid,
            )
            result.records.extend(records)
        finally:
            conn.close()

    def _from_session_table(
        self,
        cur,
        watermark: int,
        watermark_rowid: int,
        session_cols: set[str],
    ) -> tuple[list[NormalizedRecord], int]:
        archive_clause = "time_archived IS NULL" if "time_archived" in session_cols else "1=1"
        try:
            # Session rows are mutable aggregates. Re-read the last rowid
            # inclusively so token totals updated in place are upserted, while
            # newer rows are discovered even when their timestamps collide.
            if watermark_rowid > 0:
                query = (
                    "SELECT rowid, id, model, time_updated, tokens_input, tokens_output, "
                    "tokens_reasoning, tokens_cache_read, tokens_cache_write "
                    f"FROM session WHERE {archive_clause} AND rowid >= ?"
                )
                cur.execute(query, (watermark_rowid,))
            else:
                query = (
                    "SELECT rowid, id, model, time_updated, tokens_input, tokens_output, "
                    "tokens_reasoning, tokens_cache_read, tokens_cache_write "
                    f"FROM session WHERE {archive_clause}"
                )
                # A legacy cursor stores the already-normalised watermark in
                # milliseconds, while the SQLite column may be seconds. Scan
                # once without a SQL timestamp comparison and migrate to the
                # rowid cursor; this avoids unit-dependent data loss.
                cur.execute(query)
            rows = cur.fetchall()
        except sqlite3.OperationalError:
            # WITHOUT ROWID tables are uncommon but valid SQLite. Fall back
            # to the legacy timestamp cursor instead of failing the source.
            query = (
                "SELECT id, model, time_updated, tokens_input, tokens_output, "
                "tokens_reasoning, tokens_cache_read, tokens_cache_write "
                f"FROM session WHERE {archive_clause}"
            )
            cur.execute(query)
            rows = [(None, *row) for row in cur.fetchall()]

        records: list[NormalizedRecord] = []
        max_rowid = watermark_rowid
        for row in rows:
            rowid, sid, model, ts, ti, to, tr, tcr, tcw = row
            if isinstance(rowid, int):
                max_rowid = max(max_rowid, rowid)
            ts_ms = to_ts(ts) or 0
            if ts_ms == 0:
                continue
            if watermark_rowid == 0 and watermark > 0 and ts_ms < watermark:
                continue
            records.append(
                NormalizedRecord(
                    uuid=f"opencode:session:{sid}",
                    timestamp_ms=ts_ms,
                    model=_norm_model(str(model or "unknown")),
                    session_id=f"opencode:{sid}",
                    input_tokens=_safe_int(ti),
                    output_tokens=_safe_int(to) + _safe_int(tr),
                    cache_creation=_safe_int(tcw),
                    cache_read=_safe_int(tcr),
                )
            )
        return records, max_rowid

    def _from_messages(self, cur, watermark: int, watermark_rowid: int) -> tuple[list[NormalizedRecord], int]:
        # Message rows are immutable events, so a strict rowid cursor is safe
        # and avoids relying on coarse/same-millisecond timestamps.
        if watermark_rowid > 0:
            cur.execute(
                "SELECT m.rowid, m.session_id, m.data FROM message m "
                "JOIN session s ON s.id = m.session_id WHERE m.rowid > ?",
                (watermark_rowid,),
            )
        else:
            # First scan of an older installation (which only stored
            # watermark_ms) deliberately performs a one-time full migration
            # to rowid semantics so historical same-timestamp messages cannot
            # be lost.
            cur.execute(
                "SELECT m.rowid, m.session_id, m.data FROM message m "
                "JOIN session s ON s.id = m.session_id"
            )
        rows = cur.fetchall()
        records: list[NormalizedRecord] = []
        max_rowid = watermark_rowid
        for rowid, session_id, data_json in rows:
            max_rowid = max(max_rowid, int(rowid))
            parsed = _parse_data(data_json)
            if parsed is None:
                continue
            ts_ms = parsed["created"] or 0
            # WITHOUT ROWID fallback (not expected for the normal message
            # table) still needs the old timestamp guard. Rowid-backed scans
            # are already strictly incremental and must not discard equal
            # timestamps.
            if watermark_rowid == 0 and watermark > 0 and ts_ms < watermark:
                continue
            records.append(
                NormalizedRecord(
                    # 同一 session 可能有多条消息落在同一毫秒；SQLite rowid
                    # 保证它们不会因时间戳相同而互相去重。
                    uuid=f"opencode:{session_id}:{ts_ms}:{rowid}",
                    timestamp_ms=ts_ms,
                    model=parsed["model"],
                    session_id=f"opencode:{session_id}",
                    input_tokens=parsed["input"],
                    output_tokens=parsed["output"] + parsed["reasoning"],
                    cache_creation=parsed["cache_write"],
                    cache_read=parsed["cache_read"],
                )
            )
        return records, max_rowid
