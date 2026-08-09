"""Claude Code 本地 transcript 解析器。

日志路径：~/.claude/projects/*/*.jsonl（单层目录 <项目名>/<会话id>.jsonl）。
每行一个 JSON event，type=="assistant" 的行含 message.model + message.usage。

增量：每个文件记 (mtime_ns, line_offset) 游标，只读游标之后的新行；文件 mtime
未变则跳过。末尾半行（被并发写入截断）留到下次。

去重：同一 message.id 在续接 / 分支会话里重复出现，按 uuid="claude:{msg_id}" 去重
（store 主键 INSERT OR IGNORE 兜底）。

与 app/local_usage.py 的 get_claude_code_usage 的差异：那个是实时全量聚合（不落库），
这个是增量落库（供趋势 / 请求日志用）。字段提取口径完全一致。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .. import store
from . import BaseParser, CollectResult, NormalizedRecord

logger = logging.getLogger(__name__)

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _safe_int(v, default: int = 0) -> int:
    """安全转 int（transcript 可能含畸形字段，详见 local_usage.py 的同名牌）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _parse_ts(value) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1000)
    except ValueError:
        return None


class ClaudeParser(BaseParser):
    source = "claude_code"

    def discover(self) -> list[Path]:
        if not CLAUDE_PROJECTS_DIR.exists():
            return []
        try:
            # 单层 */*.jsonl，不递归——Claude Code transcript 就这一层
            return sorted(CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"))
        except OSError:
            return []

    def collect_incremental(self) -> CollectResult:
        result = CollectResult()
        files = self.discover()
        result.files_scanned = len(files)
        if not files:
            return result

        for file_path in files:
            try:
                self._scan_file(file_path, result)
            except OSError as e:
                result.errors.append(f"{file_path.name}: {e}")
        return result

    def _scan_file(self, file_path: Path, result: CollectResult) -> None:
        session_id = file_path.stem
        stat = file_path.stat()
        mtime_ns = stat.st_mtime_ns
        cursor = store.get_cursor(self.source, str(file_path))

        # mtime 未变且游标存在：跳过（无新内容）
        if cursor and cursor.get("last_modified_ns") == mtime_ns:
            return

        offset = cursor.get("last_line_offset", 0) if cursor else 0
        records: list[NormalizedRecord] = []

        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            if offset > 0:
                f.seek(offset)
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # 末尾半行：文件正被 Claude Code 并发写入。游标回退到这行起点，
                    # 下次重试。这里不 advance offset。
                    result.lines_skipped += 1
                    break
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue

                ts_ms = _parse_ts(entry.get("timestamp"))
                if ts_ms is None:
                    continue

                model = str(message.get("model") or "unknown")
                if model == "<synthetic>":  # Claude Code 内部占位，token 全 0
                    continue

                msg_id = message.get("id")
                uuid = f"claude:{msg_id}" if isinstance(msg_id, str) and msg_id else (
                    f"claude:{ts_ms}:{model}:{usage.get('output_tokens')}"
                )

                records.append(
                    NormalizedRecord(
                        uuid=uuid,
                        timestamp_ms=ts_ms,
                        model=model,
                        session_id=f"claude:{session_id}",
                        input_tokens=_safe_int(usage.get("input_tokens")),
                        output_tokens=_safe_int(usage.get("output_tokens")),
                        cache_creation=_safe_int(usage.get("cache_creation_input_tokens")),
                        cache_read=_safe_int(usage.get("cache_read_input_tokens")),
                        stop_reason=message.get("stop_reason"),
                    )
                )
            # 游标推进到文件末尾（已成功读完所有完整行）
            new_offset = f.tell()

        store.set_cursor(
            self.source,
            str(file_path),
            last_modified_ns=mtime_ns,
            last_line_offset=new_offset,
        )
        result.records.extend(records)
