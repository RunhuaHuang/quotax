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

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from . import config as config_store

logger = logging.getLogger(__name__)

# 每个渠道 JSONL 文件最多保留多少条记录（按时间倒序截断）。
# 每天最多 1 条，MAX_POINTS 条够画约半年的趋势；超出后自动淘汰最早的。
MAX_POINTS = 200


def _channel_history_path(channel_id: str) -> Path:
    """单个渠道的 JSONL 路径。channel_id 是我们自己生成的 ch_xxxxxx，不含路径分隔符，
    但仍做一层 sanitize 防御（避免 id 被构造成 ../ 逃逸出 history 目录）。"""
    safe = "".join(c for c in channel_id if c.isalnum() or c in "-_") or "unknown"
    return config_store.HISTORY_DIR / f"{safe}.jsonl"


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

        config_store.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        records = _read_channel_history(path)
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
                    if isinstance(rec, dict) and isinstance(rec.get("ts"), (int, float)):
                        records.append(rec)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    records.sort(key=lambda r: r["ts"])
    return records


def _write_channel_history(path: Path, records: list[dict]) -> None:
    """原子写入渠道历史 JSONL（先写临时文件再 replace，避免半写坏）。"""
    tmp = path.with_suffix(".jsonl.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(str(tmp), str(path))
    except BaseException:
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
        records = _read_channel_history(_channel_history_path(cid))
        out[cid] = [r for r in records if r["ts"] >= since_ms]
    return {"days": days, "channels": out}


def delete_channel_history(channel_id: str) -> None:
    """渠道被删除时清理它的历史 JSONL。失败静默（孤儿文件不影响功能）。"""
    try:
        path = _channel_history_path(channel_id)
        path.unlink(missing_ok=True)
    except Exception:  # noqa: S110 — 删除孤儿文件失败不影响功能，无需处理
        pass
