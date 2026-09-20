"""Cursor 订阅用量：自动读本机 Cursor 登录态，调 cursor.com 网页端用量接口。

参考 CodExBar 的 CursorAppAuth + CursorStatusProbe：
- 凭据：state.vscdb 里的 cursorAuth/accessToken（JWT），由 credentials.read_cursor_
  credentials 只读获取；JWT payload 的 sub 是用户 ID。
- 会话：把 token 拼成 cursor.com 的 Web 会话 Cookie（WorkosCursorSessionToken=
  <sub>%3A%3A<accessToken>，:: URL 编码为 %3A%3A），等价于浏览器登录态。
- 接口：GET https://cursor.com/api/usage-summary（Accept: JSON + Cookie），
  individualUsage.plan 里是本计费周期的已用比例（totalPercentUsed 已是百分比
  单位）与 used/limit/remaining（单位：美分）；membershipType 标识套餐档位。

遵循「只读凭据」原则：不写回 state.vscdb、不刷新 token。Cursor 里的登录态
过期时接口回 401/403，提示用户打开 Cursor 重新登录即可。
"""

from __future__ import annotations

import asyncio

from ..config import Channel
from ..credentials import CRED_NOT_FOUND, CRED_OK, read_cursor_credentials
from ..models import ChannelResult, fail, finite_float, ok, to_ts, window
from ..net import ParseError, ResponseError, request_json

_MEMBERSHIP_LABELS = {
    "free": "Free",
    "pro": "Pro",
    "pro_plus_ultra": "Pro+ / Ultra",
    "ultra": "Ultra",
    "business": "Business",
    "teams": "Teams",
    "enterprise": "Enterprise",
}


def _cents_to_usd(value) -> float | None:
    v = finite_float(value, None)
    return None if v is None else v / 100.0


def parse_cursor_usage_summary(data: dict) -> dict:
    """解析 /api/usage-summary 响应的 individualUsage 部分。

    返回 {"membership", "plan_used_percent", "plan_used_usd", "plan_limit_usd",
    "plan_remaining_usd", "cycle_end_ms", "on_demand_used_usd", "on_demand_limit_usd"}。
    plan 缺失时（企业/团队成员的个人上限）回退 individualUsage.overall。
    抛 ValueError 表示响应里没有任何可用用量数据。
    """
    individual = data.get("individualUsage")
    if not isinstance(individual, dict):
        individual = {}
    plan = individual.get("plan") if isinstance(individual.get("plan"), dict) else None
    source = plan
    if source is None and isinstance(individual.get("overall"), dict):
        # 企业/团队成员：individualUsage.overall 是个人配额上限（单位同 plan）
        source = individual["overall"]

    out = {
        "membership": data.get("membershipType"),
        "cycle_end_ms": to_ts(data.get("billingCycleEnd")),
        "on_demand_used_usd": None,
        "on_demand_limit_usd": None,
    }
    if source is not None:
        # CodExBar 明确注释：这些 percent 字段已是百分比单位（即使是小数）
        out["plan_used_percent"] = finite_float(source.get("totalPercentUsed"), None)
        if out["plan_used_percent"] is None:
            auto = finite_float(source.get("autoPercentUsed"), None)
            api = finite_float(source.get("apiPercentUsed"), None)
            if auto is not None and api is not None:
                out["plan_used_percent"] = max(0.0, min(100.0, (auto + api) / 2))
        out["plan_used_usd"] = _cents_to_usd(source.get("used"))
        out["plan_limit_usd"] = _cents_to_usd(source.get("limit"))
        out["plan_remaining_usd"] = _cents_to_usd(source.get("remaining"))
    else:
        out["plan_used_percent"] = None
        out["plan_used_usd"] = None
        out["plan_limit_usd"] = None
        out["plan_remaining_usd"] = None

    on_demand = individual.get("onDemand") if isinstance(individual.get("onDemand"), dict) else None
    if on_demand is not None and on_demand.get("enabled") is True:
        out["on_demand_used_usd"] = _cents_to_usd(on_demand.get("used"))
        out["on_demand_limit_usd"] = _cents_to_usd(on_demand.get("limit"))

    if out["plan_used_percent"] is None and out["on_demand_used_usd"] is None:
        raise ValueError("响应里没有 plan / overall / onDemand 用量数据")
    return out


async def query_cursor(channel: Channel) -> ChannelResult:
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "subscription",
    }
    # read_cursor_credentials 是同步阻塞 I/O（SQLite 读 + 文件探测），包 to_thread
    cred = await asyncio.to_thread(read_cursor_credentials)
    if cred.status != CRED_OK:
        status = "not_found" if cred.status == CRED_NOT_FOUND else "error"
        return fail(status, cred.message or "Cursor 凭据不可用", source=cred.source, **base)

    sub = (cred.extra or {}).get("sub")
    if not sub:
        # JWT payload 里没有 sub 就没法构造会话 Cookie（CodExBar 的同款依赖）
        return fail("error", "Cursor token 中缺少用户 ID（sub），请打开 Cursor 重新登录", **base)
    cookie = f"WorkosCursorSessionToken={sub}%3A%3A{cred.token}"
    try:
        data = await request_json(
            "GET",
            "https://cursor.com/api/usage-summary",
            headers={"Accept": "application/json", "Cookie": cookie},
        )
    except ResponseError as e:
        if e.status in (401, 403):
            return fail(
                "expired",
                f"Cursor 登录已失效 (HTTP {e.status})，请打开 Cursor 重新登录后再刷新",
                **base,
            )
        return fail("error", f"接口返回错误 (HTTP {e.status}): {e.body[:200]}", **base)
    except ParseError as e:
        return fail("error", str(e), **base)
    except Exception as e:
        from ..net import friendly_error

        return fail("error", friendly_error(e), **base)

    if not isinstance(data, dict):
        return fail("error", "Cursor 用量响应格式错误", **base)
    try:
        parsed = parse_cursor_usage_summary(data)
    except ValueError as e:
        return fail("error", f"Cursor 用量解析失败: {e}", **base)

    membership = parsed.get("membership")
    membership_key = str(membership).lower() if membership else ""
    label = _MEMBERSHIP_LABELS.get(membership_key)
    if label:
        plan_label = f"Cursor {label}"
    elif membership:
        plan_label = f"Cursor {membership}"
    else:
        plan_label = "Cursor 订阅"
    windows = []
    used_pct = parsed.get("plan_used_percent")
    if used_pct is not None:
        used_pct = max(0.0, min(100.0, used_pct))
        used_usd = parsed.get("plan_used_usd")
        limit_usd = parsed.get("plan_limit_usd")
        windows.append(
            window(
                "monthly",
                "本计费周期",
                used_percent=used_pct,
                remaining_percent=max(0.0, 100 - used_pct),
                used_label=f"${used_usd:,.2f}" if used_usd is not None else None,
                max_label=f"${limit_usd:,.2f}" if limit_usd is not None else None,
                reset_at=parsed.get("cycle_end_ms"),
            )
        )
    on_demand_used = parsed.get("on_demand_used_usd")
    on_demand_limit = parsed.get("on_demand_limit_usd")
    message = None
    if on_demand_used is not None:
        if on_demand_limit and on_demand_limit > 0:
            pct = max(0.0, min(100.0, on_demand_used / on_demand_limit * 100))
            windows.append(
                window(
                    "monthly",
                    "按量计费（超额）",
                    used_percent=pct,
                    remaining_percent=max(0.0, 100 - pct),
                    used_label=f"${on_demand_used:,.2f}",
                    max_label=f"${on_demand_limit:,.2f}",
                )
            )
        else:
            message = f"按量计费已用 ${on_demand_used:,.2f}"

    email = (cred.extra or {}).get("email")
    if email:
        message = f"{email} · {message}" if message else email
    return ok(plan_name=plan_label, windows=windows, message=message, source=cred.source, **base)
