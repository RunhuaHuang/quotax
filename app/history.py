"""历史趋势记录：把每次成功的渠道查询结果追加成 JSONL，供前端画趋势图。

设计要点（对齐 cc-switch 的 trend charts 思路）：
- 每个渠道一个 JSONL 文件（history/<channel_id>.jsonl），追加写，不重写整文件；
- 只记录 status == "ok" 的结果（error/info/expired 不记，画趋势没意义）；
- 每条记录精简到画图必需字段：时间戳、status、amount（value/currency）、
  各 window 的 used_percent/remaining_percent，丢弃 message/source 等易变文本；
- 写失败绝不影响主查询流程（趋势记录是附加价值，不能让磁盘写失败把额度查询拖垮）；
- 按天聚合时，每个渠道同一天只保留最后一条（避免一天刷新 100 次产生 100 个点）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows 没有 fcntl，仍保留线程内锁
    fcntl = None

from . import config as config_store

logger = logging.getLogger(__name__)

# 每个渠道 JSONL 文件最多保留多少条记录（按时间倒序截断）。
# 每天最多 1 条，MAX_POINTS 条够画约半年的趋势；超出后自动淘汰最早的。
MAX_POINTS = 200

_SAFE_CHANNEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_lock_guard = threading.Lock()
_history_locks: dict[str, threading.Lock] = {}


def _legacy_channel_history_path(channel_id: str) -> Path:
    """旧版本 sanitize 文件名，仅用于兼容已有历史文件的读取/删除。"""
    safe = "".join(c for c in str(channel_id) if c.isalnum() or c in "-_") or "unknown"
    return config_store.HISTORY_DIR / f"{safe}.jsonl"


def _channel_history_path(channel_id: str) -> Path:
    """单个渠道的 JSONL 路径，并保证不同 ID 不会因 sanitize 互相碰撞。

    正常的 ``ch_xxx`` / 导入的安全 ID 保留可读文件名；包含路径分隔符、空格或
    Unicode 的 ID 使用 SHA-256 文件名，避免 ``a/b`` 与 ``ab`` 共用同一历史文件，
    同时不会让用户可控 ID 逃逸出 history 目录。
    """
    raw = str(channel_id)
    if _SAFE_CHANNEL_ID.fullmatch(raw):
        stem = raw
    else:
        stem = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()
    return config_store.HISTORY_DIR / f"{stem}.jsonl"


def _history_read_path(channel_id: str) -> Path:
    """优先读取新安全路径；升级旧版本时回退到 legacy sanitize 路径。"""
    path = _channel_history_path(channel_id)
    if path.exists():
        return path
    legacy = _legacy_channel_history_path(channel_id)
    return legacy if legacy.exists() else path


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _lock_guard:
        lock = _history_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _history_locks[key] = lock
        return lock


@contextmanager
def _history_transaction_lock(path: Path):
    """串行化同一渠道的读-改-写，并尽力跨进程互斥。

    历史记录通常由后台线程写入；仅靠 os.replace 只能保证单次写入原子，两个线程
    仍可能同时读到旧文件并互相覆盖。线程锁覆盖本进程的常见场景，fcntl 锁文件则
    让多个 uvicorn worker/CLI 进程也能避免同一渠道的丢更新。
    """
    lock = _lock_for(path)
    with lock:
        lock_path = path.with_name(f".{path.name}.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _day_key(ts_ms: int) -> str:
    """epoch 毫秒 → 'YYYY-MM-DD'（UTC）。用于同一天去重。"""
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def _slim_result(result: dict, now_ms: int) -> dict:
    """把完整的 ChannelResult.to_dict() 精简成趋势记录——只留画图要用的字段，
    丢掉 message/source/plan_name 等易变大文本，减小文件体积。"""
    slim = {
        "ts": now_ms,
        "status": result.get("status"),
        "type": result.get("type"),
        "category": result.get("category"),
    }
    amount = result.get("amount")
    if isinstance(amount, dict):
        slim["amount"] = {
            "value": amount.get("value"),
            "currency": amount.get("currency"),
        }
    windows = result.get("windows")
    if isinstance(windows, list):
        slim["windows"] = [
            {
                "key": w.get("key"),
                "label": w.get("label"),
                "used_percent": w.get("used_percent"),
                "remaining_percent": w.get("remaining_percent"),
                "used_label": w.get("used_label"),
                "max_label": w.get("max_label"),
            }
            for w in windows
            if isinstance(w, dict)
        ]
    return slim


def record_result(channel_id: str, result: dict) -> None:
    """把一条查询结果追加进该渠道的历史 JSONL。

    只记 status == "ok" 的结果。同一天（UTC）只保留最后一条：读出现有记录，
    若今天已有一条就替换它，否则追加新条目，最后按 MAX_POINTS 截断。
    所有 I/O 异常都被吞掉（趋势记录是附加功能，绝不能拖垮主查询）。
    """
    if result.get("status") != "ok":
        return
    try:
        now_ms = int(time.time() * 1000)
        entry = _slim_result(result, now_ms)
        path = _channel_history_path(channel_id)

        with _history_transaction_lock(path):
            # 兼容旧版 sanitize 文件：首次写入新 ID 时先读旧记录，再写入安全路径。
            records = _read_channel_history(_history_read_path(channel_id))
            today = _day_key(now_ms)

            # 同一天去重：替换今天的旧记录（保留当天最后一次刷新的值）
            if records and _day_key(records[-1]["ts"]) == today:
                records[-1] = entry
            else:
                records.append(entry)

            # 按 MAX_POINTS 截断（保留最近的）
            if len(records) > MAX_POINTS:
                records = records[-MAX_POINTS:]

            _write_channel_history(path, records)
    except Exception as e:  # 趋势记录失败不该影响额度查询
        logger.debug("记录历史趋势失败（已忽略，不影响查询）: %s", e)


def _read_channel_history(path: Path) -> list[dict]:
    """读取单个渠道的 JSONL 全量记录（已排序、已跳过坏行）。"""
    if not path.exists():
        return []
    records: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = rec.get("ts") if isinstance(rec, dict) else None
                    if (
                        isinstance(rec, dict)
                        and isinstance(ts, (int, float))
                        and not isinstance(ts, bool)
                        and math.isfinite(float(ts))
                        and float(ts) > 0
                    ):
                        records.append(rec)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    records.sort(key=lambda r: r["ts"])
    return records


def _write_channel_history(path: Path, records: list[dict]) -> None:
    """原子写入渠道历史 JSONL（先写临时文件再 replace，避免半写坏）。"""
    # 临时文件名必须每次唯一；固定 .jsonl.tmp 在并发写入时会互相截断/删除对方
    # 的临时文件，即使最终 replace 是原子的也可能丢整批记录。
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = None
    try:
        fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, allow_nan=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def get_history(channel_ids: list[str] | None = None, days: int = 30) -> dict:
    """读取历史趋势数据，供 GET /api/history 返回。

    channel_ids：只返回这些渠道的历史（None = 全部已配置渠道）。
    days：返回最近 N 天的数据（1-365）。

    返回 {"days": N, "channels": {id: [records...]}}，没有历史的渠道返回空列表。
    """
    days = max(1, min(days, 365))
    since_ms = int((datetime.now(UTC).timestamp() - days * 86400) * 1000)

    # 只对实际配置了的渠道画趋势——避免已删除渠道的孤儿 JSONL 还被返回
    configured = {c.id for c in config_store.list_channels()}
    if channel_ids is not None:
        wanted = {cid for cid in channel_ids if cid in configured}
    else:
        wanted = configured

    out: dict[str, list] = {}
    for cid in wanted:
        records = _read_channel_history(_history_read_path(cid))
        out[cid] = [r for r in records if r["ts"] >= since_ms]
    return {"days": days, "channels": out}


def delete_channel_history(channel_id: str) -> None:
    """渠道被删除时清理它的历史 JSONL。失败静默（孤儿文件不影响功能）。"""
    try:
        path = _channel_history_path(channel_id)
        with _history_transaction_lock(path):
            path.unlink(missing_ok=True)
            # 清理升级前旧版本的 sanitize 文件；若它与另一个恶意/旧 ID 碰撞，
            # 这是旧格式本身无法区分的遗留问题，新写入不会再产生这种碰撞。
            legacy = _legacy_channel_history_path(channel_id)
            if legacy != path:
                legacy.unlink(missing_ok=True)
    except Exception:  # noqa: S110 — 删除孤儿文件失败不影响功能，无需处理
        pass
