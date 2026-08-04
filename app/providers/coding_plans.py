"""Coding Plan 类渠道：Kimi / 智谱 / MiniMax / ZenMux 的订阅额度查询。"""

from __future__ import annotations

from ..config import Channel
from ..models import ChannelResult, fail, ok, to_ts, window
from ..net import ParseError, ResponseError, request_json
from ._common import _require


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
    # 缺 api_key 时不校验直接拼 Authorization 头，会发出裸的 "Bearer None"——
    # 上游大概率回 401，但错误文案对用户毫无意义，这里提前挡住给出中文提示。
    if (err := _require(channel.api_key, "API Key", base)) is not None:
        return err
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
                reset_at=to_ts(usage.get("resetTime")),
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
                reset_at=to_ts(detail.get("resetTime")),
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
    # 智谱的 Authorization 头直接放 API Key（不带 Bearer 前缀，见下方 headers）；
    # 缺失时不挡住的话，四个候选 URL 会各打一遍空鉴权请求，最后拼出一堆无意义的
    # HTTP 错误堆叠在一起，不如提前给出清晰的中文提示。
    if (err := _require(channel.api_key, "API Key", base)) is not None:
        return err
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
    if (err := _require(channel.api_key, "API Key", base)) is not None:
        return err
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
        key=lambda item: to_ts(item.get("nextResetTime")) or 0,
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
                reset_at=to_ts(item.get("nextResetTime")),
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
    if (err := _require(channel.api_key, "API Key", base)) is not None:
        return err
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
        # remaining_percent == 0（配额用尽）是有效值，绝不能用 `or 100` 兜底——
        # `0 or 100` 会求值成 100，把"已耗尽"误报成"完全没用过"。字段缺失才默认满额。
        interval_raw = item.get("current_interval_remaining_percent")
        interval = float(interval_raw) if interval_raw is not None else 100.0
        windows.append(
            window(
                "five_hour",
                "每 5 小时",
                used_percent=100 - interval,
                remaining_percent=interval,
                reset_at=to_ts(item.get("end_time")),
            )
        )
        # 周额度判断用 remaining_percent 是否为空（is not None），而不是
        # current_weekly_total_count 的 truthy——实测 total_count 为 0 的套餐
        # （按百分比计额，不按次数）仍会返回有效的 current_weekly_remaining_percent，
        # truthy 判断会把 0 误当成"没有周额度"而整条跳过，导致只显示 5 小时窗口。
        # 取值时同理：is not None 已挡住了缺失，进入分支后值必非 None，不能再叠加
        # `or 100`——那会再次把 0（配额用尽）篡改成 100。月额度：上游目前只返回
        # 5 小时 + 每周两档，没有月字段。
        if item.get("current_weekly_remaining_percent") is not None:
            weekly = float(item.get("current_weekly_remaining_percent"))
            windows.append(
                window(
                    "weekly",
                    "每周额度",
                    used_percent=100 - weekly,
                    remaining_percent=weekly,
                    reset_at=to_ts(item.get("weekly_end_time")),
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
    # ZenMux 的 base_url 直接就是完整的请求 URL（不是拼接前缀），为 None 时传给
    # request_json 会被 httpx 当成非法 URL 抛出内部异常，而不是一个能看懂的中文
    # 提示——必须提前挡住。api_key 同样是必填字段（PROVIDERS["zenmux"]["fields"]），
    # 缺失只会拼出裸的 "Bearer None"，一并校验。
    if (err := _require(channel.base_url, "Base URL", base)) is not None:
        return err
    if (err := _require(channel.api_key, "API Key", base)) is not None:
        return err
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
                reset_at=to_ts(item.get("resets_at")),
            )
        )
    if not windows:
        return fail("error", "ZenMux 未返回额度窗口数据", **base)
    plan = (info.get("plan") or {}).get("tier") if isinstance(info.get("plan"), dict) else None
    return ok(plan_name=f"ZenMux {plan}" if plan else "ZenMux", windows=windows, **base)
