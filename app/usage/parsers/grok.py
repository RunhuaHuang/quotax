"""Grok CLI 本地日志解析器。

日志路径：~/.grok/sessions/<enc-cwd>/<uuid>/ 和 ~/.grok/archived_sessions/<enc-cwd>/<uuid>/
三件套：summary.json (JSON) + chat_history.jsonl + updates.jsonl (JSON-RPC 通知)

usage 在 updates.jsonl 里：method="_x.ai/session/update" 且 sessionUpdate=turn_completed
的事件含 usage.inputTokens / cachedReadTokens / outputTokens。reasoningTokens 已含在
output 里，忽略。

坑（来自 VaultOne grok.rs）：
1. input 含 cache（cache-inclusive），减 cachedReadTokens 得真实 input；
2. 忽略 usage_snapshot 中途快照（只取 turn_completed）防重复；
3. prompt_id (UUIDv7) 去重；时间戳兼容 epoch 秒 / 毫秒 / RFC3339；
4. 忽略 CLI 自报的 cost（我们自己用 PricingBook 重算）。

游标：(mtime_ns, line_offset)，针对 updates.jsonl 追加式 JSONL。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ...models import to_ts
from .. import store
from . import BaseParser, CollectResult, NormalizedRecord

logger = logging.getLogger(__name__)

GROK_SESSIONS_DIR = Path.home() / ".grok" / "sessions"
GROK_ARCHIVED_DIR = Path.home() / ".grok" / "archived_sessions"


def _safe_int(v, default=0) -> int:
    if isinstance(v, bool):
        return default
    try:
        converted = int(v)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, converted)


def _parse_ts(value) -> int | None:
    """时间戳兼容 epoch 秒 / 毫秒 / RFC3339（按 UTC 解析）。

    grok 日志时间是 UTC（带 Z 后缀），统一走 fromisoformat（把 Z 换成 +00:00 让
    Python 认作 UTC），再取 timestamp。naive 的 strptime 分支按本地时区算会偏移，
    这里不再用。
    """
    return to_ts(value)


class GrokParser(BaseParser):
    source = "grok_cli"

    def discover(self) -> list[Path]:
        """定位 updates.jsonl 文件（每个会话目录下一份）。"""
        out: list[Path] = []
        for base in (GROK_SESSIONS_DIR, GROK_ARCHIVED_DIR):
            if not base.exists():
                continue
            try:
                # <enc-cwd>/<uuid>/updates.jsonl 两层
                out.extend(sorted(base.glob("*/*/updates.jsonl")))
            except OSError:
                pass
        return out

    def collect_incremental(self) -> CollectResult:
        result = CollectResult()
        files = self.discover()
        result.files_scanned = len(files)
        for file_path in files:
            try:
                self._scan_file(file_path, result)
            except OSError as e:
                result.errors.append(f"{file_path.parent.name}: {e}")
        return result

    def _scan_file(self, file_path: Path, result: CollectResult) -> None:
        session_uuid = file_path.parent.name
        session_id = f"grok:{session_uuid}"
        stat = file_path.stat()
        mtime_ns = stat.st_mtime_ns
        cursor = store.get_cursor(self.source, str(file_path))
        offset = cursor.get("last_line_offset", 0) if cursor else 0
        rewound = offset < 0 or offset > stat.st_size
        if rewound:
            offset = 0
        # mtime 精度较粗时，追加内容可能暂时保持同一 mtime；文件大小变化仍应
        # 触发增量扫描，不能只看 mtime。
        if (
            cursor
            and cursor.get("last_modified_ns") == mtime_ns
            and not rewound
            and stat.st_size == offset
        ):
            return

        # 先从 summary.json 取当前模型名（若存在）
        model = self._read_model(file_path.parent)
        records: list[NormalizedRecord] = []
        truncated = False

        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            if offset > 0:
                f.seek(offset)
            while True:
                line_offset = f.tell()
                raw_line = f.readline()
                if not raw_line:
                    break
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    result.lines_skipped += 1
                    truncated = True
                    break
                if not isinstance(entry, dict):
                    continue
                if entry.get("method") != "_x.ai/session/update":
                    continue
                params = entry.get("params") or {}
                if params.get("sessionUpdate") != "turn_completed":
                    continue
                usage = params.get("usage")
                if not isinstance(usage, dict):
                    continue

                prompt_id = params.get("prompt_id") or params.get("id")
                ts_ms = _parse_ts(params.get("timestamp") or usage.get("timestamp"))
                if ts_ms is None:
                    continue

                input_total = _safe_int(usage.get("inputTokens"))
                cached = _safe_int(usage.get("cachedReadTokens"))
                output = _safe_int(usage.get("outputTokens"))
                fresh_input = max(0, input_total - cached)

                uuid = (
                    f"{session_id}:{prompt_id}"
                    if prompt_id
                    else f"{session_id}:{ts_ms}:{line_offset}"
                )
                records.append(
                    NormalizedRecord(
                        uuid=uuid,
                        timestamp_ms=ts_ms,
                        model=str(model or "grok"),
                        session_id=session_id,
                        input_tokens=fresh_input,
                        output_tokens=output,
                        cache_creation=0,  # grok 不单独报 cache_creation
                        cache_read=cached,
                    )
                )
            new_offset = offset if truncated else f.tell()

        store.set_cursor(
            self.source, str(file_path), last_modified_ns=mtime_ns, last_line_offset=new_offset
        )
        result.records.extend(records)

    @staticmethod
    def _read_model(session_dir: Path) -> str | None:
        summary = session_dir / "summary.json"
        if not summary.exists():
            return None
        try:
            with summary.open("r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(data, dict):
            return data.get("model")
        return None
