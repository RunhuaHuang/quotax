"""用量统计模块：本地 AI CLI 用量的深度分析看板。

子模块：
- pricing：模型单价表 + LiteLLM 拉取 + 成本四桶计算（Decimal）+ rebill。
- store：SQLite 存储（usage_records / scan_progress / model_pricing）+ 聚合查询。
- parsers：5 个 CLI 日志解析器（claude / codex / gemini / grok / opencode）。
- collect：采集编排（解析 → 算成本 → 落库）。

对外高阶入口（main.py 调用）：
- collect.collect_all()：触发增量采集。
- collect.get_book()：获取 PricingBook（读 / 改单价）。
- store.query_*：聚合查询（总览 / 趋势 / 模型分布 / 请求日志 / 源 / 模型列表）。
- store.rebill_zero_cost(book)：补算 0 成本记录。
- pricing.update_from_litellm(book)：拉取 LiteLLM 更新单价。
"""

from __future__ import annotations
