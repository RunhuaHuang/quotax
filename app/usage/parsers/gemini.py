"""Gemini CLI 本地日志解析器。

日志路径：~/.gemini/tmp/<project_hash>/chats/session-*.json
格式是单个 JSON 对象（含 messages 数组，**不是** JSONL）。

坑（来自 VaultOne gemini.rs）：
1. input 是 cache-inclusive：tokens.input 含缓存命中部分，要减 tokens.cached 得真实 input；
2. output = output + thoughts（thinking 按 output 价计费）；
3. cache_creation 恒为 0（Gemini 不单独报）。

游标：Gemini 是单 JSON 文件而非追加式 JSONL，记 mtime 游标；mtime 变了就整体重解析
（store 主键 INSERT OR IGNORE 保证重复解析不产生重复行）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ...models import to_ts
from .. import store
from . import BaseParser, CollectResult, NormalizedRecord

logger = logging.getLogger(__name__)

GEMINI_CHATS_DIR = Path.home() / ".gemini" / "tmp"


def _safe_int(v, default=0) -> int:
    if isinstance(v, bool):
        return default
    try:
        converted = int(v)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, converted)


class GeminiParser(BaseParser):
    source = "gemini_cli"

    def discover(self) -> list[Path]:
        if not GEMINI_CHATS_DIR.exists():
            return []
        try:
            return sorted(GEMINI_CHATS_DIR.glob("*/chats/session-*.json"))
        except OSError:
            return []

    def collect_incremental(self) -> CollectResult:
        result = CollectResult()
        files = self.discover()
        result.files_scanned = len(files)
        for file_path in files:
            try:
                self._scan_file(file_path, result)
            except (OSError, json.JSONDecodeError) as e:
                result.errors.append(f"{file_path.name}: {e}")
        return result

    def _scan_file(self, file_path: Path, result: CollectResult) -> None:
        session_id = f"gemini:{file_path.stem}"
        stat = file_path.stat()
        mtime_ns = stat.st_mtime_ns
        cursor = store.get_cursor(self.source, str(file_path))
        # Gemini 文件不是追加式 JSONL，仍用文件大小辅助 mtime 判断。某些文件系统
        # 的 mtime 精度较粗，文件被追加/重写后 mtime 可能暂时不变；旧游标为 0
        # 或大小发生变化时必须重新解析。
        if (
            cursor
            and cursor.get("last_modified_ns") == mtime_ns
            and cursor.get("last_line_offset") == stat.st_size
        ):
            return

        # 单 JSON 文件，mtime 变则整体重解析
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return
        messages = data.get("messages")
        if not isinstance(messages, list):
            return

        records: list[NormalizedRecord] = []
        for message_index, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "model":  # Gemini 用 role=model 表示 assistant
                continue
            tokens = msg.get("tokens")
            if not isinstance(tokens, dict):
                continue
            ts_ms = _parse_ts(msg.get("timestamp") or msg.get("createTime"))
            if ts_ms is None:
                continue

            input_total = _safe_int(tokens.get("input"))
            cached = _safe_int(tokens.get("cached"))
            output = _safe_int(tokens.get("output"))
            thoughts = _safe_int(tokens.get("thoughts"))
            fresh_input = max(0, input_total - cached)
            out_total = output + thoughts

            if fresh_input == 0 and out_total == 0 and cached == 0:
                continue

            records.append(
                NormalizedRecord(
                    # 同一 session 的两条消息可能共享时间戳；消息索引保证稳定去重键
                    # 不发生碰撞。
                    uuid=f"{session_id}:{ts_ms}:{message_index}",
                    timestamp_ms=ts_ms,
                    model=str(msg.get("model") or "gemini"),
                    session_id=session_id,
                    input_tokens=fresh_input,
                    output_tokens=out_total,
                    cache_creation=0,
                    cache_read=cached,
                )
            )

        store.set_cursor(
            self.source,
            str(file_path),
            last_modified_ns=mtime_ns,
            last_line_offset=stat.st_size,
        )
        result.records.extend(records)


def _parse_ts(value) -> int | None:
    return to_ts(value)
