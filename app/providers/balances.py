"""余额类渠道：API Key 直查各家官方余额接口（全部为只读 GET）。"""

from __future__ import annotations

from urllib.parse import urlparse

from ..config import Channel
from ..models import ChannelResult, amount, fail, ok, window
from ..net import ParseError, ResponseError, request_json


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
    return "error", str(e) or e.__class__.__name__


def _require(value: str | None, field_label: str, base: dict) -> ChannelResult | None:
    """必填字段缺失时的统一友好报错：缺失时返回一个 error 结果，否则返回 None。

    Pydantic 层（app/main.py）已经会在新建渠道时挡掉必填字段缺失的情况，但这里
    仍然兜底一层——避免 base_url=None 时拼出 "None/api/xxx" 这种丑陋 URL，或者
    sk=None 时报 "'NoneType' object has no attribute 'encode'" 这种没法排查的
    Python 内部异常。
    """
    if not value:
        return fail("error", f"未配置 {field_label}", **base)
    return None


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

    preferred = (
        next((i for i in infos if str(i.get("currency", "")).upper() == "CNY"), None)
        or next((i for i in infos if float(i.get("total_balance") or 0) > 0), None)
        or infos[0]
    )
    currency = str(preferred.get("currency") or "CNY")
    total = float(preferred.get("total_balance") or 0)
    symbol = "¥" if currency.upper() in ("CNY", "RMB") else ("$" if currency.upper() == "USD" else "")

    return ok(
        plan_name="DeepSeek 账户余额",
        amount=amount(total, currency, symbol),
        # 余额没有"百分比"概念——直接显示金额（前端渲染成无百分比空环，
        # 金额在 sub 行；不传 used/remaining_percent，避免出现没意义的 100%）
        windows=[window("balance", "账户余额", max_label=f"{symbol}{total:,.2f}")],
        message="DeepSeek 账户余额不可用" if data.get("is_available") is False else None,
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
    balance = float(data.get("balance") or 0) if isinstance(data, dict) else 0
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
    domain = "api.siliconflow.com" if (channel.base_url or "").endswith(".com") else "api.siliconflow.cn"
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
    total = float(info.get("totalBalance") or 0) if isinstance(info, dict) else 0
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
    credits = float(data.get("credits") or 0) if isinstance(data, dict) else 0
    used = float(data.get("total_usage") or 0) if isinstance(data, dict) else 0
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
    balance = float(data.get("balance") or 0) if isinstance(data, dict) else 0
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
    balance = data.get("data", {}).get("available_balance", {}) if isinstance(data, dict) else {}
    total = float(balance.get("total_balance") or 0) if isinstance(balance, dict) else 0
    currency = str(balance.get("currency") or "CNY") if isinstance(balance, dict) else "CNY"
    symbol = "¥" if currency.upper() in ("CNY", "RMB") else "$"
    return ok(
        plan_name="Kimi API 账户",
        amount=amount(total, currency, symbol),
        windows=[window("balance", "账户余额", max_label=f"{symbol}{total:,.2f}")],
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
            quota = float(info.get("quota") or 0) / 500000
            used = float(info.get("used_quota") or 0) / 500000
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
            limit = float(
                sub.get("hard_limit_usd") or sub.get("system_hard_limit_usd") or sub.get("soft_limit_usd") or 0
            )
            used = None
            try:
                usage = await request_json(
                    "GET",
                    f"{channel.base_url}/v1/dashboard/billing/usage",
                    headers=headers,
                )
                if isinstance(usage, dict) and usage.get("total_usage") is not None:
                    used = float(usage["total_usage"]) / 100  # OpenAI 该端点单位是 cent
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
