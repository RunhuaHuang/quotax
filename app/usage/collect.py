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

from . import store
from .parsers import all_parsers
from .pricing import PricingBook, calc_cost

logger = logging.getLogger(__name__)

# 模块级单例 book：内置价 + 持久化价。LiteLLM 更新 / 手动编辑后调 save_pricing 落盘。
_book: PricingBook | None = None
_book_lock = threading.Lock()


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
    book = get_book()
    sources: list[dict] = []
    total_new = 0
    for parser in all_parsers():
        info = {"source": parser.source, "new_records": 0, "files_scanned": 0, "errors": []}
        try:
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
                n = store.upsert_records(parser.source, records_with_cost)
                info["new_records"] = n
                total_new += n
        except Exception as e:
            logger.exception("采集 %s 失败", parser.source)
            info["errors"].append(f"{type(e).__name__}: {e}")
        sources.append(info)
    return {"sources": sources, "total_new": total_new}
