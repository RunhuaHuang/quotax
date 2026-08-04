"""小米 MiMo Coding Plan 用量查询。

与其它 API Key 直查的渠道不同，MiMo 开放平台的用量查询端点
（platform.xiaomimimo.com/api/v1/tokenPlan/usage）只接受小米账号登录后的
**Cookie**（session），不提供基于 API Key 的余额查询。

实现对照 Mimo-Usage（github.com/0xtbug/Mimo-Usage）的上游接口：
- GET /api/v1/tokenPlan/usage  → monthUsage / usage 两组，各含 percent + items[]
- GET /api/v1/tokenPlan/detail → planName / currentPeriodEnd / expired

用户需要从浏览器（登录 platform.xiaomimimo.com 后）复制 Cookie 填进来。本工具
只读查询，绝不刷新 Cookie、绝不写回。
"""

from __future__ import annotations

from ..config import Channel
from ..models import ChannelResult, fail, ok, to_ts, window
from ..net import ParseError, ResponseError, request_json

BASE_URL = "https://platform.xiaomimimo.com/api/v1"

# 与 Mimo-Usage 一致的请求头（上游对 User-Agent / Accept-Language 敏感，缺了可能 403）
_COMMON_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


def _error_result(base: dict, e: Exception) -> ChannelResult:
    if isinstance(e, ResponseError):
        if e.status in (401, 403):
            return fail(
                "expired",
                f"Cookie 已失效或无权限 (HTTP {e.status})，请重新登录 platform.xiaomimimo.com 后复制新 Cookie",
                **base,
            )
        return fail("error", f"接口返回错误 (HTTP {e.status}): {e.body[:200]}", **base)
    if isinstance(e, ParseError):
        return fail("error", str(e), **base)
    # 其余网络异常（DNS 解析失败 / TLS 握手被中断 / 连接超时等）统一翻译成中文
    from ..net import friendly_error

    return fail("error", friendly_error(e), **base)


async def query_mimo(channel: Channel) -> ChannelResult:
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "coding_plan",
    }
    cookie = (channel.extra or {}).get("cookie") or channel.api_key
    if not cookie:
        return fail(
            "error",
            "未配置 Cookie（请登录 platform.xiaomimimo.com 后，从浏览器复制完整 Cookie 填入）",
            **base,
        )

    headers = dict(_COMMON_HEADERS)
    headers["Cookie"] = cookie

    # 1) 用量
    try:
        data = await request_json("GET", f"{BASE_URL}/tokenPlan/usage", headers=headers)
    except Exception as e:
        return _error_result(base, e)
    if not isinstance(data, dict) or data.get("code") not in (None, 0):
        return fail("error", str(data.get("message") or "MiMo 用量查询失败"), **base)

    windows: list = []
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    for key, label in (("monthUsage", "每月额度"), ("usage", "当前周期")):
        group = payload.get(key) if isinstance(payload, dict) else None
        if not isinstance(group, dict):
            continue
        percent = group.get("percent")
        if isinstance(percent, (int, float)):
            used_pct = max(0.0, min(100.0, float(percent)))
            windows.append(
                window(
                    "monthly" if key == "monthUsage" else "custom",
                    label,
                    used_percent=used_pct,
                    remaining_percent=max(0.0, 100 - used_pct),
                )
            )
        # items 细分（各模型/维度的 used/limit）
        for item in group.get("items") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            used = item.get("used")
            limit = item.get("limit")
            item_pct = item.get("percent")
            if item_pct is None and isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
                item_pct = float(used) / float(limit) * 100
            if item_pct is None:
                continue
            item_pct = max(0.0, min(100.0, float(item_pct)))
            used_label = f"{float(used):,.0f}" if isinstance(used, (int, float)) else None
            max_label = f"{float(limit):,.0f}" if isinstance(limit, (int, float)) else None
            windows.append(
                window(
                    "custom",
                    f"{label} · {name}",
                    used_percent=item_pct,
                    remaining_percent=max(0.0, 100 - item_pct),
                    used_label=used_label,
                    max_label=max_label,
                )
            )

    # 2) 套餐详情（失败不影响用量展示）
    plan_name = "小米 MiMo Coding Plan"
    reset_at = None
    expired = False
    try:
        detail = await request_json("GET", f"{BASE_URL}/tokenPlan/detail", headers=headers)
        d = detail.get("data") if isinstance(detail, dict) and isinstance(detail.get("data"), dict) else detail
        if isinstance(d, dict):
            if d.get("planName"):
                plan_name = str(d["planName"])
            reset_at = to_ts(d.get("currentPeriodEnd"))
            expired = bool(d.get("expired"))
    except Exception:  # noqa: S110 — detail 是附加信息，失败不影响 usage 主数据
        pass

    # 周期结束时间挂到各额度窗口上（reset_at 是 QuotaWindow 的字段，ChannelResult
    # 上没有这个字段——直接传给 ok()/ChannelResult() 会 TypeError）。只填给本身
    # 没有重置时间的窗口，不覆盖上游已经给出的更精确的值。
    if reset_at is not None:
        for w in windows:
            if w.reset_at is None:
                w.reset_at = reset_at

    if expired:
        # 套餐已过期（可能仍返回历史用量）——标 info 而不是 error：这不是故障，
        # 只是没有有效额度可用。放在 windows 空判断之前，避免把"套餐过期"误报成
        # "Cookie 无效或未订阅"。
        return ChannelResult(
            status="info",
            message="MiMo 套餐已过期，以下为历史用量数据",
            plan_name=plan_name,
            windows=windows,
            **base,
        )
    if not windows:
        return fail("error", "MiMo 未返回用量数据（可能 Cookie 无效或未订阅套餐）", **base)
    return ok(plan_name=plan_name, windows=windows, **base)
