"""采集编排：运行所有解析器 → 用 PricingBook 计算成本 → 落库。

这是 /api/usage/collect 的核心。流程：
1. load book（内置价 + 持久化价）；
2. 对每个解析器 collect_incremental() 拿新记录；
3. 每条记录 resolve 单价 → calc_cost 算四桶成本（has_cost = 非 unknown）；
4. upsert_records 批量入库。

采集是幂等的：重复跑只处理增量（游标保证），INSERT OR IGNORE 兜底去重。
"""

from __future__ import annotations

import logging
import threading
import time
from copy import deepcopy

from . import store
from .parsers import all_parsers
from .pricing import PricingBook, calc_cost

logger = logging.getLogger(__name__)

# 模块级单例 book：内置价 + 持久化价。LiteLLM 更新 / 手动编辑后调 save_pricing 落盘。
_book: PricingBook | None = None
_book_lock = threading.Lock()

# 增量采集会先读各解析器游标，再扫描文件，最后更新游标和入库。SQLite 的写锁只能
# 保证单条 SQL 安全，无法把这整段跨文件事务变成原子操作；两个浏览器标签页同时
# 触发 /api/usage/collect 时，可能基于同一旧游标重复扫描，并以不同完成顺序回写
# 游标。主键去重通常能挡住重复记录，但挡不住多余 I/O、错误统计漂移和游标倒退。
# 采集是本机单进程任务，直接串行化整轮 collect_all 最简单也最可靠。
_collect_lock = threading.Lock()
_status_lock = threading.Lock()
_status = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_result": None,
    "last_error": None,
}


def get_status() -> dict:
    """返回最近一次采集状态，供健康检查和前端诊断展示。"""
    with _status_lock:
        return deepcopy(_status)


def _snapshot_cursors(source: str) -> dict[str, dict]:
    """保存某个解析器运行前的游标，供落库失败时恢复。

    解析器为了支持独立调用会在扫描结束后立即推进游标；若随后 SQLite 批量写入
    失败，下一轮就会从新游标开始，造成已解析但未落库的记录永久跳过。采集编排层
    在每个解析器边界保存快照，失败时恢复旧值或删除新建游标。
    """
    return {row["file_path"]: row for row in store.list_cursors(source)}


def _restore_cursors(source: str, before: dict[str, dict]) -> None:
    after = {row["file_path"]: row for row in store.list_cursors(source)}
    for file_path, row in before.items():
        # set_cursor intentionally treats None as "preserve existing" for
        # ordinary partial updates. Rollback is different: a previous NULL
        # must be restored as NULL, so remove the row before recreating it.
        store.delete_cursor(source, file_path)
        store.set_cursor(
            source,
            file_path,
            last_modified_ns=row.get("last_modified_ns"),
            last_line_offset=row.get("last_line_offset"),
            watermark_ms=row.get("watermark_ms"),
            watermark_rowid=row.get("watermark_rowid"),
            last_model=row.get("last_model"),
        )
    for file_path in set(after) - set(before):
        store.delete_cursor(source, file_path)


def get_book() -> PricingBook:
    """获取 / 惰性初始化全局 PricingBook（内置价 + 持久化价合并）。"""
    global _book
    if _book is None:
        with _book_lock:
            if _book is None:
                book = PricingBook()
                try:
                    store.load_pricing(book)
                except Exception as e:  # 加载失败不阻塞——用内置价也能跑
                    logger.debug("加载持久化单价失败（用内置价）: %s", e)
                _book = book
    return _book


def collect_all() -> dict:
    """运行全部解析器采集增量 + 算成本 + 入库。

    返回 {sources: [{source, new_records, files_scanned, errors}], total_new}。
    单个解析器异常不影响其它源（各自 try/except 兜底）。
    """
    with _collect_lock:
        with _status_lock:
            _status.update(
                {
                    "running": True,
                    "last_started_at": int(time.time() * 1000),
                    "last_error": None,
                }
            )
        try:
            book = get_book()
            sources: list[dict] = []
            total_new = 0
            for parser in all_parsers():
                info = {"source": parser.source, "new_records": 0, "files_scanned": 0, "errors": []}
                cursor_snapshot: dict[str, dict] = {}
                try:
                    cursor_snapshot = _snapshot_cursors(parser.source)
                    result = parser.collect_incremental()
                    info["files_scanned"] = result.files_scanned
                    info["errors"] = result.errors
                    if result.records:
                        # 算成本
                        records_with_cost = []
                        for rec in result.records:
                            rate = book.resolve(rec.model)
                            cost = calc_cost(
                                {
                                    "input": rec.input_tokens,
                                    "output": rec.output_tokens,
                                    "cache_read": rec.cache_read,
                                    "cache_creation": rec.cache_creation,
                                },
                                rate,
                            )
                            d = rec.to_storage_dict()
                            d.update(
                                {
                                    "pricing_model": rec.model,
                                    "input_cost_usd": str(cost["input_usd"]),
                                    "output_cost_usd": str(cost["output_usd"]),
                                    "cache_read_cost": str(cost["cache_read_usd"]),
                                    "cache_create_cost": str(cost["cache_creation_usd"]),
                                    "total_cost_usd": str(cost["total_usd"]),
                                    "has_cost": not rate.is_unknown(),
                                }
                            )
                            records_with_cost.append(d)
                        # OpenCode 新版 session 表会原地更新同一 session 的聚合 token
                        # 值；同 UUID 必须覆盖旧行，否则后续更大的聚合值会被 INSERT
                        # OR IGNORE 静默丢掉。其它解析器仍保持不可变事件的去重语义。
                        n = store.upsert_records(
                            parser.source,
                            records_with_cost,
                            update_existing=parser.source == "opencode",
                        )
                        info["new_records"] = n
                        total_new += n
                except Exception as e:
                    try:
                        _restore_cursors(parser.source, cursor_snapshot)
                    except Exception:
                        logger.exception("恢复采集 %s 游标失败", parser.source)
                    logger.exception("采集 %s 失败", parser.source)
                    info["errors"].append(f"{type(e).__name__}: {e}")
                sources.append(info)
            result_payload = {"sources": sources, "total_new": total_new}
            with _status_lock:
                _status["last_result"] = deepcopy(result_payload)
            return result_payload
        except Exception as e:
            with _status_lock:
                _status["last_error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            with _status_lock:
                _status["running"] = False
                _status["last_finished_at"] = int(time.time() * 1000)
