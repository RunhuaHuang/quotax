"""Codex CLI 本地日志解析器。

日志路径：~/.codex/sessions/**/*.jsonl（YYYY/MM/DD 三层）+ ~/.codex/archived_sessions/*.jsonl
文件名 rollout-<ts>-<uuid>.jsonl，每行一个 JSON 事件（顶层 type + payload + timestamp）。

真实日志结构（实测 ~/.codex/sessions 下的格式，2026 版 Codex CLI）：
- 顶层事件类型：session_meta / event_msg / response_item / turn_context / world_state
- token 用量在 `type=="event_msg"` 且 `payload.type=="token_count"` 的事件里，
  字段路径：payload.info.last_token_usage（本轮增量，**无需再算 delta**），
  另有 payload.info.total_token_usage（累计值，不用）。
- last_token_usage 字段：input_tokens（含缓存，cache-inclusive）、cached_input_tokens
  （缓存读）、cache_write_input_tokens（缓存写）、output_tokens、reasoning_output_tokens
  （推理输出，按 output 价计费，并入 output）。
- 模型名在 `turn_context.payload.model`，形如 "gpt-5.6-terra" 或
  "aimami_relay_xxx::gpt-5.6-sol"（带 provider:: 前缀，归一化时去掉）。

cache-inclusive 归一化：input_tokens 包含 cached_input_tokens 部分，解析时减去得
"新鲜 input"，与 Claude Code 口径对齐。

游标：(mtime_ns, line_offset)，JSONL 追加写。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .. import store
from . import BaseParser, CollectResult, NormalizedRecord

logger = logging.getLogger(__name__)

CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CODEX_ARCHIVED_DIR = Path.home() / ".codex" / "archived_sessions"


def _safe_int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _norm_model(model: str) -> str:
    """去掉 "provider::model" 的 provider 前缀，只留模型名。

    turn_context.payload.model 可能是 "aimami_relay_xxx::gpt-5.6-sol" 这种中转格式，
    '::' 之前是中转站标识，真正用于定价匹配的是 '::' 之后的模型名。
    """
    if not model:
        return "gpt-5"
    if "::" in model:
        model = model.rsplit("::", 1)[-1]
    return model


class CodexParser(BaseParser):
    source = "codex"

    def discover(self) -> list[Path]:
        out: list[Path] = []
        for d in (CODEX_SESSIONS_DIR, CODEX_ARCHIVED_DIR):
            if d.exists():
                try:
                    out.extend(sorted(d.rglob("*.jsonl")))
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
                result.errors.append(f"{file_path.name}: {e}")
        return result

    def _scan_file(self, file_path: Path, result: CollectResult) -> None:
        session_id = f"codex:{file_path.stem}"
        stat = file_path.stat()
        mtime_ns = stat.st_mtime_ns
        cursor = store.get_cursor(self.source, str(file_path))
        if cursor and cursor.get("last_modified_ns") == mtime_ns:
            return
        offset = cursor.get("last_line_offset", 0) if cursor else 0

        records: list[NormalizedRecord] = []
        current_model: str | None = None
        truncated = False

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
                    # 末尾半行（Codex 正在写），保留旧游标，下次从当前行起点重试
                    result.lines_skipped += 1
                    truncated = True
                    break
                if not isinstance(entry, dict):
                    continue

                etype = entry.get("type")
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    continue

                # turn_context 更新当前模型（含 turn_id，用于去重）
                if etype == "turn_context":
                    m = payload.get("model")
                    if isinstance(m, str) and m:
                        current_model = _norm_model(m)
                    continue

                # token 用量事件：event_msg + payload.type=token_count
                if etype != "event_msg" or payload.get("type") != "token_count":
                    continue

                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                usage = info.get("last_token_usage")
                if not isinstance(usage, dict):
                    continue

                ts_ms = _parse_ts(entry.get("timestamp"))
                if ts_ms is None:
                    continue

                # last_token_usage 已经是本轮增量，直接用
                input_total = _safe_int(usage.get("input_tokens"))
                cache_read = _safe_int(usage.get("cached_input_tokens"))
                cache_write = _safe_int(usage.get("cache_write_input_tokens"))
                output = _safe_int(usage.get("output_tokens"))
                reasoning = _safe_int(usage.get("reasoning_output_tokens"))

                # cache-inclusive：input 含 cache_read，减去得新鲜 input（钳到 ≥0）
                fresh_input = max(0, input_total - cache_read)
                # reasoning 按 output 价计费，并入 output
                out_total = output + reasoning

                if fresh_input == 0 and out_total == 0 and cache_read == 0 and cache_write == 0:
                    continue

                records.append(
                    NormalizedRecord(
                        uuid=f"{session_id}:{ts_ms}",
                        timestamp_ms=ts_ms,
                        model=str(current_model or "gpt-5"),
                        session_id=session_id,
                        input_tokens=fresh_input,
                        output_tokens=out_total,
                        cache_creation=cache_write,
                        cache_read=cache_read,
                    )
                )
            new_offset = offset if truncated else f.tell()

        store.set_cursor(
            self.source, str(file_path), last_modified_ns=mtime_ns, last_line_offset=new_offset
        )
        result.records.extend(records)


def _parse_ts(value) -> int | None:
    """codex 时间戳兼容 epoch 秒 / 毫秒 / ISO8601。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = int(value)
        return v if v > 1_000_000_000_000 else v * 1000  # < 1e12 视为秒
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).timestamp() * 1000)
        except ValueError:
            return None
    return None
