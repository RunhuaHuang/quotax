"""渠道实现注册表：type → 查询函数。

每个查询函数签名：async def query(channel: Channel) -> ChannelResult
网络/解析异常由外层统一兜底，函数内部只需负责业务逻辑与状态标注。
"""

from __future__ import annotations

from ..config import Channel
from ..models import ChannelResult
from . import balances, coding_plans, mimo, subscriptions, volcengine

REGISTRY: dict[str, object] = {
    # 余额类
    "deepseek": balances.query_deepseek,
    "stepfun": balances.query_stepfun,
    "siliconflow": balances.query_siliconflow,
    "openrouter": balances.query_openrouter,
    "novita": balances.query_novita,
    "kimi_api": balances.query_kimi_api,
    "newapi": balances.query_newapi,
    # Coding Plan 类
    "kimi_coding": coding_plans.query_kimi_coding,
    "zhipu_coding": coding_plans.query_zhipu,
    "zhipu_team": coding_plans.query_zhipu_team,
    "minimax": coding_plans.query_minimax,
    "volcengine": volcengine.query_volcengine,
    "zenmux": coding_plans.query_zenmux,
    "mimo": mimo.query_mimo,
    # 订阅只读类
    "claude_subscription": subscriptions.query_claude,
    "gemini_subscription": subscriptions.query_gemini,
    "grok_subscription": subscriptions.query_grok,
    "codex_subscription": subscriptions.query_codex,
    "copilot_subscription": subscriptions.query_copilot,
}


async def query_channel(channel: Channel) -> ChannelResult:
    fn = REGISTRY.get(channel.type)
    if fn is None:
        from ..models import fail

        return fail(
            "error",
            f"未知渠道类型: {channel.type}",
            id=channel.id,
            type=channel.type,
            name=channel.name,
            category="unknown",
        )
    try:
        return await fn(channel)
    except Exception as e:  # 网络层异常统一兜底
        from ..models import fail
        from ..net import friendly_error

        message = friendly_error(e)
        return fail(
            "error",
            message,
            id=channel.id,
            type=channel.type,
            name=channel.name,
            category=channel_category(channel.type),
        )


def channel_category(type_: str) -> str:
    from ..config import PROVIDERS

    return PROVIDERS.get(type_, {}).get("category", "unknown")
