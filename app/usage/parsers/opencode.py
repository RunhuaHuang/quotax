"""OpenCode SQLite 本地日志解析器（增量）。

数据库：~/.local/share/opencode/opencode.db（SQLite），表 session / message / part。
与 app/local_usage.py 的 get_opencode_usage 同源，但这里增量落库（记水位线）。

字段提取（与 local_usage.py 一致）：
- session 表新版自带 tokens_input 等聚合列 → 直接按会话聚合；
- 老版只有 message.data JSON → 解析 data.tokens / data.cost；
- 模型名 data.modelID 可能是 JSON 字符串，_norm_model 归一化。

水位线：记每个 session 的 max(time_updated) 作为 watermark，下次只扫 time_updated > w。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

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
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


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
    tokens = data.get("tokens") or {}
    cache = tokens.get("cache") or {}
    time_obj = data.get("time")
    created = time_obj.get("created") if isinstance(time_obj, dict) else None
    return {
        "input": _safe_int(tokens.get("input")),
        "output": _safe_int(tokens.get("output")),
        "reasoning": _safe_int(tokens.get("reasoning")),
        "cache_read": _safe_int(cache.get("read")),
        "cache_write": _safe_int(cache.get("write")),
        "model": _norm_model(str(data.get("modelID") or "unknown")),
        "created": int(created) if isinstance(created, (int, float)) else None,
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
            watermark = cursor.get("watermark_ms") if cursor else 0
            new_watermark = watermark

            if "tokens_input" in session_cols:
                records = self._from_session_table(cur, watermark)
            else:
                records = self._from_messages(cur, watermark)

            for r in records:
                new_watermark = max(new_watermark, r.timestamp_ms)
            store.set_cursor(self.source, str(db_path), watermark_ms=new_watermark)
            result.records.extend(records)
        finally:
            conn.close()

    def _from_session_table(self, cur, watermark: int) -> list[NormalizedRecord]:
        cur.execute(
            "SELECT id, model, time_updated, tokens_input, tokens_output, "
            "tokens_reasoning, tokens_cache_read, tokens_cache_write "
            "FROM session WHERE time_updated > ? AND time_archived IS NULL",
            (watermark,),
        )
        records: list[NormalizedRecord] = []
        for row in cur.fetchall():
            sid, model, ts, ti, to, tr, tcr, tcw = row
            ts_ms = int(ts) if ts else 0
            if ts_ms == 0:
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
        return records

    def _from_messages(self, cur, watermark: int) -> list[NormalizedRecord]:
        cur.execute(
            "SELECT m.session_id, m.data FROM message m "
            "JOIN session s ON s.id = m.session_id WHERE s.time_updated > ?",
            (watermark,),
        )
        records: list[NormalizedRecord] = []
        for session_id, data_json in cur.fetchall():
            parsed = _parse_data(data_json)
            if parsed is None:
                continue
            ts_ms = parsed["created"] or 0
            if ts_ms == 0:
                continue
            records.append(
                NormalizedRecord(
                    uuid=f"opencode:{session_id}:{ts_ms}",
                    timestamp_ms=ts_ms,
                    model=parsed["model"],
                    session_id=f"opencode:{session_id}",
                    input_tokens=parsed["input"],
                    output_tokens=parsed["output"] + parsed["reasoning"],
                    cache_creation=parsed["cache_write"],
                    cache_read=parsed["cache_read"],
                )
            )
        return records
