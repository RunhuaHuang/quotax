"""本地 AI CLI 日志解析器包。

每个解析器实现统一的 collect_incremental() 接口：增量扫描本机 CLI 日志，产出
NormalizedRecord 列表（已去重、已 cache-inclusive 归一化），由 store 落库。

参考 Buktal/VaultOne 的 source_parser 设计：
- JSONL 源记 (mtime, line_offset) 游标，只读游标之后的新行；
- SQLite 源（opencode）记水位线，只查 time_updated > watermark 的行；
- 各源 input 若含 cache（Codex / Gemini / Grok），解析时减去 cache_read 得真实 input。

NormalizedRecord 字段与 store.upsert_records 的 records 项对齐（除 source 外）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class NormalizedRecord:
    """归一化后的单条 API 调用记录（一个 assistant 回复 = 一条）。

    uuid：解析器产出的稳定去重 id（同 source 内唯一）。timestamp_ms：调用时间。
    cache_inclusive_input 已在解析层减去 cache_read，这里 input 是"新鲜输入"。
    成本字段由采集编排层（collect.py）根据 PricingBook 计算，解析器只产 token。
    """

    uuid: str
    timestamp_ms: int
    model: str
    session_id: str
    input_tokens: int
    output_tokens: int
    cache_creation: int
    cache_read: int
    stop_reason: str | None = None

    def to_storage_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "timestamp_ms": self.timestamp_ms,
            "model": self.model,
            "session_id": self.session_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation": self.cache_creation,
            "cache_read": self.cache_read,
            "stop_reason": self.stop_reason,
        }


@dataclass
class CollectResult:
    """单次增量扫描的结果。records 是新记录，files_scanned 是本次扫描的文件数。"""

    records: list[NormalizedRecord] = field(default_factory=list)
    files_scanned: int = 0
    lines_skipped: int = 0
    errors: list[str] = field(default_factory=list)


class BaseParser:
    """解析器基类。子类实现 source 名 + discover + collect_incremental。"""

    source: str = "base"

    def discover(self) -> list[Path]:
        """定位本机的日志文件 / 目录。无则返回空列表。"""
        raise NotImplementedError

    def collect_incremental(self) -> CollectResult:
        """增量扫描：读游标之后的新内容，返回 NormalizedRecord 列表。"""
        raise NotImplementedError


def all_parsers() -> list[BaseParser]:
    """注册全部解析器。缺目录时各自 discover() 返回空，不报错。"""
    # 局部导入避免包初始化时拉起所有依赖
    from .claude import ClaudeParser
    from .codex import CodexParser
    from .gemini import GeminiParser
    from .grok import GrokParser
    from .opencode import OpenCodeParser

    return [
        ClaudeParser(),
        CodexParser(),
        GeminiParser(),
        GrokParser(),
        OpenCodeParser(),
    ]
