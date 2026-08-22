"""余额类渠道：API Key 直查各家官方余额接口（全部为只读 GET）。"""

from __future__ import annotations

from urllib.parse import urlparse

from ..config import Channel
from ..models import ChannelResult, amount, fail, finite_float, ok, window
from ..net import ParseError, ResponseError, request_json
from ._common import _require


def _origin(base_url: str | None, fallback: str) -> str:
    if base_url:
        parsed = urlparse(base_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return fallback


def _status_for_error(e: Exception) -> tuple[str, str]:
    if isinstance(e, ResponseError) and e.status in (401, 403):
        return "expired", f"API Key 无效或无权限 (HTTP {e.status})"
    if isinstance(e, ResponseError):
        return "error", f"接口返回错误 (HTTP {e.status}): {e.body[:200]}"
    if isinstance(e, ParseError):
        return "error", str(e)
    # 底层网络异常（DNS 解析失败 / TLS 握手被中断 / 连接超时等）：httpx 抛出的是
    # 英文技术串，用户看不懂。翻译成中文，和 coding_plans / mimo 的兜底行为对齐。
    from ..net import friendly_error

    return "error", friendly_error(e)


# ── DeepSeek ────────────────────────────────────────────────


async def query_deepseek(channel: Channel) -> ChannelResult:
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "balance",
    }
    if (err := _require(channel.api_key, "API Key", base)) is not None:
        return err
    url = f"{_origin(channel.base_url, 'https://api.deepseek.com')}/user/balance"
    try:
        data = await request_json(
            "GET",
            url,
            headers={
                "Authorization": f"Bearer {channel.api_key}",
                "Accept": "application/json",
            },
        )
    except Exception as e:
        status, message = _status_for_error(e)
        return fail(status, message, **base)

    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return fail("error", "DeepSeek 错误: " + str(data["error"].get("message", "")), **base)
    infos = data.get("balance_infos") if isinstance(data, dict) else None
    if not isinstance(infos, list) or not infos:
        return fail("error", "DeepSeek 未返回余额数据", **base)
    infos = [item for item in infos if isinstance(item, dict)]
    if not infos:
        return fail("error", "DeepSeek 余额数据格式错误", **base)

    preferred = (
        next((i for i in infos if str(i.get("currency", "")).upper() == "CNY"), None)
        or next((i for i in infos if (finite_float(i.get("total_balance"), 0.0) or 0.0) > 0), None)
        or infos[0]
    )
    currency = str(preferred.get("currency") or "CNY")
    total = finite_float(preferred.get("total_balance"), 0.0) or 0.0
    symbol = "¥" if currency.upper() in ("CNY", "RMB") else ("$" if currency.upper() == "USD" else "")

    is_available = data.get("is_available") if isinstance(data, dict) else None
    return ok(
        plan_name="DeepSeek 账户余额",
        amount=amount(total, currency, symbol),
        # 余额没有"百分比"概念——直接显示金额（前端渲染成无百分比空环，
        # 金额在 sub 行；不传 used/remaining_percent，避免出现没意义的 100%）
        windows=[window("balance", "账户余额", max_label=f"{symbol}{total:,.2f}")],
        message="DeepSeek 账户余额不可用" if is_available is False else None,
        **base,
    )


# ── 阶跃星辰 StepFun ────────────────────────────────────────


async def query_stepfun(channel: Channel) -> ChannelResult:
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "balance",
    }
    if (err := _require(channel.api_key, "API Key", base)) is not None:
        return err
    try:
        data = await request_json(
            "GET",
            "https://api.stepfun.com/v1/accounts",
            headers={
                "Authorization": f"Bearer {channel.api_key}",
                "Accept": "application/json",
            },
        )
    except Exception as e:
        status, message = _status_for_error(e)
        return fail(status, message, **base)
    if not isinstance(data, dict) or "balance" not in data:
        return fail("error", "StepFun 余额响应格式错误", **base)
    balance = max(0.0, finite_float(data.get("balance"), 0.0) or 0.0)
    return ok(
        plan_name="StepFun 账户",
        amount=amount(balance, "CNY", "¥"),
        windows=[window("balance", "账户余额", max_label=f"¥{balance:,.2f}")],
        **base,
    )


# ── 硅基流动 SiliconFlow ────────────────────────────────────


async def query_siliconflow(channel: Channel) -> ChannelResult:
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "balance",
    }
    if (err := _require(channel.api_key, "API Key", base)) is not None:
        return err
    # base_url 只用于区分国际站（.com）/国内站（.cn）——不是请求目标。判断必须看
    # host 而不是整串 endswith(".com")：用户填 https://api.siliconflow.com/v1
    # （带路径）时 endswith 会误判成国内站，国际站 Key 全部误报「API Key 无效」。
    base_host = urlparse(channel.base_url or "").hostname or ""
    domain = "api.siliconflow.com" if ".com" in base_host else "api.siliconflow.cn"
    try:
        data = await request_json(
            "GET",
            f"https://{domain}/v1/user/info",
            headers={
                "Authorization": f"Bearer {channel.api_key}",
                "Accept": "application/json",
            },
        )
    except Exception as e:
        status, message = _status_for_error(e)
        return fail(status, message, **base)
    info = data.get("data") if isinstance(data, dict) else None
    if not isinstance(info, dict) or "totalBalance" not in info:
        return fail("error", "硅基流动余额响应格式错误", **base)
    total = max(0.0, finite_float(info.get("totalBalance"), 0.0) or 0.0)
    currency = "USD" if ".com" in domain else "CNY"
    symbol = "$" if currency == "USD" else "¥"
    return ok(
        plan_name="硅基流动 账户",
        amount=amount(total, currency, symbol),
        windows=[window("balance", "账户余额", max_label=f"{symbol}{total:,.2f}")],
        **base,
    )


# ── OpenRouter ──────────────────────────────────────────────


async def query_openrouter(channel: Channel) -> ChannelResult:
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "balance",
    }
    if (err := _require(channel.api_key, "API Key", base)) is not None:
        return err
    try:
        data = await request_json(
            "GET",
            "https://openrouter.ai/api/v1/credits",
            headers={
                "Authorization": f"Bearer {channel.api_key}",
                "Accept": "application/json",
            },
        )
    except Exception as e:
        status, message = _status_for_error(e)
        return fail(status, message, **base)
    if not isinstance(data, dict) or ("credits" not in data and "total_usage" not in data):
        return fail("error", "OpenRouter 余额响应格式错误", **base)
    credits = max(0.0, finite_float(data.get("credits"), 0.0) or 0.0)
    used = max(0.0, finite_float(data.get("total_usage"), 0.0) or 0.0)
    total = credits + used
    windows = []
    if total > 0:
        windows.append(
            window(
                "custom",
                "已用比例",
                used_percent=used / total * 100,
                remaining_percent=credits / total * 100,
                used_label=f"${used:,.2f}",
                max_label=f"${total:,.2f}",
            )
        )
    return ok(
        plan_name="OpenRouter Credits",
        amount=amount(credits, "USD", "$"),
        windows=windows,
        **base,
    )


# ── Novita ──────────────────────────────────────────────────


async def query_novita(channel: Channel) -> ChannelResult:
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "balance",
    }
    if (err := _require(channel.api_key, "API Key", base)) is not None:
        return err
    try:
        data = await request_json(
            "GET",
            "https://api.novita.ai/v3/user/balance",
            headers={
                "Authorization": f"Bearer {channel.api_key}",
                "Accept": "application/json",
            },
        )
    except Exception as e:
        status, message = _status_for_error(e)
        return fail(status, message, **base)
    if not isinstance(data, dict) or "balance" not in data:
        return fail("error", "Novita 余额响应格式错误", **base)
    balance = max(0.0, finite_float(data.get("balance"), 0.0) or 0.0)
    return ok(
        plan_name="Novita 账户",
        amount=amount(balance, "USD", "$"),
        windows=[window("balance", "账户余额", max_label=f"${balance:,.2f}")],
        **base,
    )


# ── Kimi API (Moonshot) ─────────────────────────────────────


async def query_kimi_api(channel: Channel) -> ChannelResult:
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "balance",
    }
    if (err := _require(channel.api_key, "API Key", base)) is not None:
        return err
    url = f"{_origin(channel.base_url, 'https://api.moonshot.cn')}/v1/users/me/balance"
    try:
        data = await request_json(
            "GET",
            url,
            headers={
                "Authorization": f"Bearer {channel.api_key}",
                "Accept": "application/json",
            },
        )
    except Exception as e:
        status, message = _status_for_error(e)
        return fail(status, message, **base)
    data_body = data.get("data") if isinstance(data, dict) else None
    balance = data_body.get("available_balance") if isinstance(data_body, dict) else None
    if not isinstance(balance, dict) or "total_balance" not in balance:
        return fail("error", "Kimi API 余额响应格式错误", **base)
    total = max(0.0, finite_float(balance.get("total_balance"), 0.0) or 0.0)
    currency = str(balance.get("currency") or "CNY")
    symbol = "¥" if currency.upper() in ("CNY", "RMB") else "$"
    return ok(
        plan_name="Kimi API 账户",
        amount=amount(total, currency, symbol),
        windows=[window("balance", "账户余额", max_label=f"{symbol}{total:,.2f}")],
        **base,
    )


# ── 智谱 API 余额 ────────────────────────────────────────────


async def query_zhipu_balance(channel: Channel) -> ChannelResult:
    """智谱开放平台（bigmodel.cn）按量付费账户余额。

    端点是控制台网页端的内部接口（官方文档没有公开的余额 API，社区验证可用）：
    GET https://www.bigmodel.cn/api/biz/account/query-customer-account-report，
    认证与智谱其它接口一致——Authorization 头直接放 API Key（不带 Bearer）。
    响应的 balance 对象含 balance / availableBalance / rechargeAmount /
    giveAmount / totalSpendAmount / frozenBalance（单位：元）。
    """
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "balance",
    }
    if (err := _require(channel.api_key, "API Key", base)) is not None:
        return err
    try:
        data = await request_json(
            "GET",
            "https://www.bigmodel.cn/api/biz/account/query-customer-account-report",
            headers={
                # 智谱风格：Authorization 直放 API Key，不带 Bearer 前缀
                "Authorization": channel.api_key,
                "Accept": "application/json",
            },
        )
    except Exception as e:
        status, message = _status_for_error(e)
        return fail(status, message, **base)

    if not isinstance(data, dict):
        return fail("error", "智谱余额响应格式错误", **base)
    # 兼容两种包裹：{"data": {...}} 与直接平铺。业务失败时智谱返回 success=false
    # / code!=200 + msg。
    if data.get("success") is False or (data.get("code") not in (None, 200) and data.get("code") != 200):
        return fail("error", str(data.get("msg") or "智谱余额查询失败"), **base)
    outer = data.get("data") if isinstance(data.get("data"), dict) else data
    # 实测（2026-08）响应是 data.balance 直接给金额数字、其余字段平铺在 data 里；
    # 同时兼容 balance 本身是对象的旧形态（linux.do 帖子的示例是 balance.balance）。
    raw_balance = outer.get("balance") if isinstance(outer, dict) else None
    if isinstance(raw_balance, dict):
        holder = raw_balance
    elif raw_balance is not None and finite_float(raw_balance, None) is not None:
        holder = outer
    else:
        return fail("error", "智谱未返回余额数据（响应里没有 balance 字段）", **base)

    available = finite_float(holder.get("availableBalance"), None)
    current = finite_float(holder.get("balance"), None)
    total = available if available is not None else (current or 0.0)
    total = max(0.0, total)
    # 摘要行：累计充值 / 赠送 / 累计消费 / 冻结——字段缺失就跳过，不硬凑
    parts = []
    for key, label in (
        ("rechargeAmount", "累计充值"),
        ("giveAmount", "赠送"),
        ("totalSpendAmount", "累计消费"),
        ("frozenBalance", "冻结"),
    ):
        v = finite_float(holder.get(key), None)
        if v is not None:
            parts.append(f"{label} ¥{v:,.2f}")
    return ok(
        plan_name="智谱 API 账户余额",
        amount=amount(total, "CNY", "¥"),
        windows=[window("balance", "账户余额", max_label=f"¥{total:,.2f}")],
        message=" · ".join(parts) or None,
        **base,
    )


# ── new-api / one-api 中转站 ────────────────────────────────


async def query_newapi(channel: Channel) -> ChannelResult:
    """new-api / one-api 中转站余额查询，两条路都试，哪条通用哪条：

    1. new-api 原生端点 GET {base}/api/user/self（quota 单位 $1 = 500000）——但
       这个端点在很多 new-api 部署里需要"系统访问令牌 + New-API-User: <用户 id>
       请求头"，而不是普通的 sk- 业务 key，业务 key 调用时可能拿不到数据；
    2. 回退到 OpenAI 兼容的 /v1/dashboard/billing/subscription（额度上限）+
       /v1/dashboard/billing/usage（已用量，单位 cent），一般 new-api/one-api
       都会实现这两个兼容端点，且用业务 sk- key 就能访问。
    """
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "balance",
    }
    if (err := _require(channel.base_url, "Base URL", base)) is not None:
        return err
    if (err := _require(channel.api_key, "API Key", base)) is not None:
        return err

    headers = {
        "Authorization": f"Bearer {channel.api_key}",
        "Accept": "application/json",
    }
    if channel.user_id:
        headers["New-API-User"] = channel.user_id

    errors: list[str] = []

    # 1) new-api 原生端点
    try:
        data = await request_json("GET", f"{channel.base_url}/api/user/self", headers=headers)
    except Exception as e:
        _, message = _status_for_error(e)
        errors.append(f"/api/user/self: {message}")
    else:
        info = data.get("data") if isinstance(data, dict) else None
        if isinstance(info, dict) and (info.get("quota") is not None or info.get("used_quota") is not None):
            quota = (finite_float(info.get("quota"), 0.0) or 0.0) / 500000
            used = (finite_float(info.get("used_quota"), 0.0) or 0.0) / 500000
            windows = []
            if quota + used > 0:
                total = quota + used
                windows.append(
                    window(
                        "custom",
                        "已用比例",
                        used_percent=used / total * 100,
                        remaining_percent=quota / total * 100,
                        used_label=f"${used:,.2f}",
                        max_label=f"${total:,.2f}",
                    )
                )
            return ok(
                plan_name=info.get("username") or "中转站额度",
                amount=amount(quota, "USD", "$"),
                windows=windows,
                message=f"请求数 {info.get('request_count', '-')}" if info.get("request_count") is not None else None,
                **base,
            )
        errors.append(
            "/api/user/self 未返回可识别的用户数据（该端点常需要系统访问令牌 + New-API-User 头，而非业务 sk- key）"
        )

    # 2) 回退：OpenAI 兼容 dashboard billing 端点
    try:
        sub = await request_json(
            "GET",
            f"{channel.base_url}/v1/dashboard/billing/subscription",
            headers=headers,
        )
    except Exception as e:
        _, message = _status_for_error(e)
        errors.append(f"/v1/dashboard/billing/subscription: {message}")
    else:
        if not isinstance(sub, dict):
            errors.append("/v1/dashboard/billing/subscription 响应格式不是对象")
        else:
            limit = finite_float(
                sub.get("hard_limit_usd") or sub.get("system_hard_limit_usd") or sub.get("soft_limit_usd"),
                0.0,
            ) or 0.0
            used = None
            try:
                usage = await request_json(
                    "GET",
                    f"{channel.base_url}/v1/dashboard/billing/usage",
                    headers=headers,
                )
                if isinstance(usage, dict) and usage.get("total_usage") is not None:
                    used = (finite_float(usage["total_usage"], 0.0) or 0.0) / 100  # OpenAI 该端点单位是 cent
            except (ResponseError, ParseError, ValueError, TypeError):
                pass  # /usage 很多中转站没实现，缺失不影响 subscription 的额度上限数据

            windows = []
            note = None
            if limit > 0 and used is not None:
                windows.append(
                    window(
                        "custom",
                        "已用比例",
                        used_percent=used / limit * 100,
                        remaining_percent=max(0.0, 100 - used / limit * 100),
                        used_label=f"${used:,.2f}",
                        max_label=f"${limit:,.2f}",
                    )
                )
            elif used is None:
                note = "/v1/dashboard/billing/usage 不可用，只能显示总额度，无法显示已用量"
            remaining = max(0.0, limit - used) if used is not None else limit
            return ok(
                plan_name="中转站额度（OpenAI 兼容账单）",
                amount=amount(remaining, "USD", "$"),
                windows=windows,
                message=note,
                **base,
            )

    return fail("error", "；".join(errors) or "中转站未返回可用的余额数据", **base)
