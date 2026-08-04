"""Coding Plan 类渠道：Kimi / 智谱 / MiniMax / ZenMux 的订阅额度查询。"""

from __future__ import annotations

from ..config import Channel
from ..models import ChannelResult, fail, ok, window
from ..net import ParseError, ResponseError, request_json


def _to_ts(value) -> int | None:
    """兼容秒/毫秒/ISO 字符串 → epoch 毫秒。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and value > 0:
        ms = value * 1000 if value < 10_000_000_000 else value
        return int(ms)
    if isinstance(value, str):
        from datetime import datetime

        try:
            dt = datetime.fromisoformat(value)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None
    return None


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, round(v, 1)))


def _error_result(base: dict, e: Exception) -> ChannelResult:
    if isinstance(e, ResponseError):
        if e.status in (401, 403):
            return fail("expired", f"API Key 无效或无权限 (HTTP {e.status})", **base)
        return fail("error", f"接口返回错误 (HTTP {e.status}): {e.body[:200]}", **base)
    if isinstance(e, ParseError):
        return fail("error", str(e), **base)
    # 其余网络异常（DNS 解析失败 / TLS 握手被中断 / 连接超时等）统一翻译成中文
    from ..net import friendly_error

    return fail("error", friendly_error(e), **base)


# ── Kimi For Coding ─────────────────────────────────────────


async def query_kimi_coding(channel: Channel) -> ChannelResult:
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "coding_plan",
    }
    headers = {
        "Authorization": f"Bearer {channel.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        data = await request_json("GET", "https://api.kimi.com/coding/v1/usages", headers=headers)
    except Exception as e:
        return _error_result(base, e)
    if not isinstance(data, dict):
        return fail("error", "Kimi 响应格式错误", **base)
    if data.get("code"):
        return fail("error", f"Kimi 额度查询失败: {data['code']}", **base)

    windows = []
    usage = data.get("usage") or {}
    if isinstance(usage, dict):
        remaining = float(usage.get("remaining") or 0)
        used = float(usage.get("used") or (100 - remaining))
        windows.append(
            window(
                "weekly",
                "每周额度",
                used_percent=used,
                remaining_percent=100 - used,
                reset_at=_to_ts(usage.get("resetTime")),
            )
        )

    for item in data.get("limits") or []:
        detail = item.get("detail") or {}
        if not isinstance(detail, dict):
            continue
        remaining = float(detail.get("remaining") or 0)
        used = float(detail.get("used") or (100 - remaining))
        if remaining <= 1 and used <= 1:
            # 兼容绝对额度（limit/remaining 是请求数而非百分比）
            limit = float(detail.get("limit") or 0)
            if limit > 0:
                remaining = remaining / limit * 100
                used = max(0.0, 100 - remaining)
        win_obj = item.get("window") or {}
        duration = win_obj.get("duration")
        unit = str(win_obj.get("timeUnit") or "")
        is_5h = (duration == 5 and unit == "TIME_UNIT_HOUR") or (duration == 300 and unit == "TIME_UNIT_MINUTE")
        label = (
            "每 5 小时"
            if is_5h
            else f"{duration} {'小时' if unit == 'TIME_UNIT_HOUR' else '分钟' if unit == 'TIME_UNIT_MINUTE' else '天' if unit == 'TIME_UNIT_DAY' else '月' if unit == 'TIME_UNIT_MONTH' else unit}"
        )
        windows.append(
            window(
                "five_hour" if is_5h else "custom",
                label,
                used_percent=used,
                remaining_percent=100 - used,
                reset_at=_to_ts(detail.get("resetTime")),
            )
        )

    if not windows:
        return fail("error", "Kimi 未返回订阅额度数据", **base)
    return ok(plan_name="Kimi For Coding", windows=windows, **base)


# ── 智谱 GLM Coding Plan ────────────────────────────────────


async def query_zhipu(channel: Channel) -> ChannelResult:
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "coding_plan",
    }
    # 固定顺序的 tuple（原来是 {...} 字面量，也就是 set——Python 字符串哈希每进程
    # 随机，"先试 bigmodel.cn 再试 api.z.ai" 这个优先级在不同进程里会随机失效）。
    urls = (
        "https://bigmodel.cn/api/monitor/usage/quota/limit",
        "https://bigmodel.cn/api/monitor/usage/quota/limit?type=1",
        "https://api.z.ai/api/monitor/usage/quota/limit",
        "https://api.z.ai/api/monitor/usage/quota/limit?type=1",
    )
    headers = {
        # 智谱特殊：Authorization 直接放 API Key，不带 Bearer
        "Authorization": channel.api_key or "",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    last_error: Exception | None = None
    for url in urls:
        try:
            data = await request_json("GET", url, headers=headers)
        except Exception as e:
            last_error = e
            continue
        if not isinstance(data, dict):
            # 响应结构不是预期的对象（可能是错误网关/域名返回了非预期内容），
            # 换下一个候选继续试。
            last_error = ParseError(f"响应不是合法的对象结构: {type(data).__name__}")
            continue
        # 只要拿到了结构合法的响应——哪怕业务上是"无套餐"——就不再继续打后面
        # 的候选域名，避免账号正常但无套餐时把 4 个 URL 全打一遍。
        return _parse_zhipu_data(data, "GLM Coding Plan", base)
    if last_error is not None:
        return _error_result(base, last_error)
    return fail("error", "智谱 Coding Plan 未返回窗口额度数据", **base)


async def query_zhipu_team(channel: Channel) -> ChannelResult:
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "coding_plan",
    }
    headers = {
        "Authorization": channel.api_key or "",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "set-language": "zh",
        "Referer": "https://bigmodel.cn/coding-plan/team/usage-stats",
    }
    if channel.organization:
        headers["bigmodel-organization"] = channel.organization
    if channel.project:
        headers["bigmodel-project"] = channel.project
    try:
        data = await request_json(
            "GET",
            "https://bigmodel.cn/api/monitor/usage/quota/limit?type=2",
            headers=headers,
        )
    except Exception as e:
        return _error_result(base, e)
    return _parse_zhipu_data(data, "GLM Coding Plan 团队版", base)


def _parse_zhipu_data(data, plan_name: str, base: dict) -> ChannelResult:
    if not isinstance(data, dict):
        return fail("error", "智谱响应格式错误", **base)
    if not data.get("success") or data.get("code") != 200:
        return fail("error", str(data.get("msg") or "智谱 Coding Plan 额度查询失败"), **base)
    limits = (data.get("data") or {}).get("limits") or []
    windows = []
    token_limits = sorted(
        [item for item in limits if item.get("type") == "TOKENS_LIMIT"],
        key=lambda item: _to_ts(item.get("nextResetTime")) or 0,
    )
    for i, item in enumerate(token_limits):
        used = float(item.get("percentage") or 0)
        unit = item.get("unit")
        if unit == 3 or (unit is None and i == 0):
            key, label = "five_hour", "每 5 小时"
        elif unit == 6 or (unit is None and i == 1):
            key, label = "weekly", "每周额度"
        else:
            continue
        windows.append(
            window(
                key,
                label,
                used_percent=used,
                remaining_percent=100 - used,
                reset_at=_to_ts(item.get("nextResetTime")),
            )
        )
    time_limit = next((item for item in limits if item.get("type") == "TIME_LIMIT"), None)
    if time_limit:
        remaining_count = float(time_limit.get("remaining") or 0)
        total_count = float(time_limit.get("usage") or 0)
        used_count = float(time_limit.get("currentValue") or (total_count - remaining_count if total_count > 0 else 0))
        total = total_count if total_count > 0 else remaining_count + used_count
        remaining_pct = remaining_count / total * 100 if total > 0 else 0
        windows.append(
            window(
                "custom",
                "MCP 每月",
                used_percent=100 - remaining_pct,
                remaining_percent=remaining_pct,
                used_label=f"{used_count:,.0f} 次",
                max_label=f"{total:,.0f} 次",
            )
        )
    if not windows:
        return fail("error", "智谱 Coding Plan 未返回窗口额度数据", **base)
    return ok(plan_name=plan_name, windows=windows, **base)


# ── MiniMax Token Plan ──────────────────────────────────────


async def query_minimax(channel: Channel) -> ChannelResult:
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "coding_plan",
    }
    url = "https://www.minimaxi.com/v1/token_plan/remains"
    if channel.base_url and "minimax.io" in channel.base_url:
        url = url.replace(".minimaxi.com", ".minimax.io")
    headers = {
        "Authorization": f"Bearer {channel.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        data = await request_json("GET", url, headers=headers)
    except Exception as e:
        return _error_result(base, e)
    if not isinstance(data, dict):
        return fail("error", "MiniMax 响应格式错误", **base)
    resp = data.get("base_resp") or {}
    if resp.get("status_code") not in (None, 0):
        return fail(
            "error",
            str(resp.get("status_msg") or "MiniMax Token Plan 额度查询失败"),
            **base,
        )
    general = [item for item in (data.get("model_remains") or []) if item.get("model_name") == "general"]
    if not general:
        return fail("error", "MiniMax Token Plan 未返回通用额度数据", **base)
    windows = []
    for item in general:
        interval = float(item.get("current_interval_remaining_percent") or 100)
        windows.append(
            window(
                "five_hour",
                "每 5 小时",
                used_percent=100 - interval,
                remaining_percent=interval,
                reset_at=_to_ts(item.get("end_time")),
            )
        )
        # 周额度判断用 remaining_percent 是否为空，而不是 current_weekly_total_count
        # 的 truthy——实测 total_count 为 0 的套餐（按百分比计额，不按次数）仍会
        # 返回有效的 current_weekly_remaining_percent，truthy 判断会把 0 误当成
        # "没有周额度"而整条跳过，导致只显示 5 小时窗口。
        # 月额度：上游 token_plan/remains 目前只返回 5 小时 + 每周两档，没有月字段。
        if item.get("current_weekly_remaining_percent") is not None:
            weekly = float(item.get("current_weekly_remaining_percent") or 100)
            windows.append(
                window(
                    "weekly",
                    "每周额度",
                    used_percent=100 - weekly,
                    remaining_percent=weekly,
                    reset_at=_to_ts(item.get("weekly_end_time")),
                )
            )
    return ok(plan_name="MiniMax Token Plan", windows=windows, **base)


# ── ZenMux ──────────────────────────────────────────────────


async def query_zenmux(channel: Channel) -> ChannelResult:
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "coding_plan",
    }
    headers = {
        "Authorization": f"Bearer {channel.api_key}",
        "Accept": "application/json",
    }
    try:
        data = await request_json("GET", channel.base_url, headers=headers)
    except Exception as e:
        return _error_result(base, e)
    if not isinstance(data, dict) or data.get("success") is not True:
        message = (data or {}).get("message") if isinstance(data, dict) else ""
        return fail("error", f"ZenMux API 错误: {message or '未知'}", **base)
    info = data.get("data") or {}
    windows = []
    for key, label, tier in [
        ("quota_5_hour", "每 5 小时", "five_hour"),
        ("quota_7_day", "每周额度", "weekly"),
        ("quota_30_day", "每月额度", "monthly"),
    ]:
        item = info.get(key)
        if not isinstance(item, dict):
            continue
        # usage_percentage 形如 0.123（0-1 小数比例，见 ZenMux 文档示例）。
        # 上游偶发返回 >1 的脏值（已观察到 1.0~100 的异常情况）——直接 ×100 会
        # 把 50 这种"其实代表 50%"的值放大成 5000%，整张卡的 remaining 被算成负数。
        # 这里用 _clamp 兜底：无论上游传 0-1、0-100 还是脏值，都归一到 0-100 的合法百分比。
        raw_pct = float(item.get("usage_percentage") or 0) * 100
        used_pct = _clamp(raw_pct)
        used_usd = item.get("used_value_usd")
        max_usd = item.get("max_value_usd")
        windows.append(
            window(
                tier,
                label,
                used_percent=used_pct,
                remaining_percent=max(0.0, 100 - used_pct),
                used_label=f"${float(used_usd):,.2f}" if used_usd is not None else None,
                max_label=f"${float(max_usd):,.2f}" if max_usd is not None else None,
                reset_at=_to_ts(item.get("resets_at")),
            )
        )
    if not windows:
        return fail("error", "ZenMux 未返回额度窗口数据", **base)
    plan = (info.get("plan") or {}).get("tier") if isinstance(info.get("plan"), dict) else None
    return ok(plan_name=f"ZenMux {plan}" if plan else "ZenMux", windows=windows, **base)
