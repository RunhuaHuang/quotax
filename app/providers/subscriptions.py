"""订阅只读类渠道：Claude / Gemini / Grok / Codex / Copilot。

全部遵循「只读凭据 + 绝不刷新」原则：直接读各 CLI 已有的登录凭据文件
或 macOS Keychain，查询官方订阅用量端点；token 过期只提示重新登录。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from ..config import Channel, resolve_codex_auth_file
from ..credentials import (
    CRED_EXPIRED,
    CRED_NO_TOKEN,
    CRED_NOT_FOUND,
    CRED_OK,
    CRED_PARSE_ERROR,
    read_claude_credentials,
    read_codex_credentials,
    read_codex_credentials_from_file,
    read_copilot_credentials,
    read_gemini_credentials,
    read_grok_credentials,
)
from ..models import ChannelResult, fail, ok, to_ts, window
from ..net import ParseError, ResponseError, request_json

USER_AGENT = "quota-board/1.0 (macOS; read-only usage checker)"


def _pick(d: dict, *keys, default=None):
    """按多个候选 key 依次取值——用于兼容同一个字段的 camelCase / snake_case 两种
    命名（不同网关/客户端库对同一个上游 JSON API 的字段命名转换习惯不一致）。"""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def _base(channel: Channel) -> dict:
    return {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "subscription",
    }


def _fail_from_error(base: dict, e: Exception, provider: str) -> ChannelResult:
    if isinstance(e, ResponseError):
        if e.status in (401, 403):
            return fail(
                "expired",
                f"{provider} 登录已失效 (HTTP {e.status})，请重新登录对应 CLI",
                **base,
            )
        return fail("error", f"接口返回错误 (HTTP {e.status}): {e.body[:200]}", **base)
    if isinstance(e, ParseError):
        return fail("error", str(e), **base)
    # 其余网络异常（DNS 解析失败 / TLS 握手被中断 / 连接超时等）统一翻译成中文
    # 提示——直接拼原始异常用户看不懂（如 "[Errno 8] ..." / SSL UNEXPECTED_EOF）
    from ..net import friendly_error

    return fail("error", friendly_error(e), **base)


def _cred_fail(cred, base: dict) -> ChannelResult:
    # no_token 目前只有 Claude 会产生，且 query_claude 会在调用这里之前就单独处理
    # 成 status="info"；这里仍兜底映射一下，避免以后有别的渠道复用 CRED_NO_TOKEN
    # 时被归类成语义更重的 "error"。用 credentials 模块导出的常量做 key（而不是
    # 重复写字符串字面量），避免两处的取值以后不小心走偏。
    status = {
        CRED_EXPIRED: "expired",
        CRED_NOT_FOUND: "not_found",
        CRED_PARSE_ERROR: "error",
        CRED_NO_TOKEN: "info",
    }.get(cred.status, "error")
    return fail(status, cred.message or "凭据不可用", source=cred.source, **base)


# ── Claude Pro / Max 订阅 ───────────────────────────────────


async def query_claude(channel: Channel) -> ChannelResult:
    base = _base(channel)
    # read_*_credentials 是同步阻塞 I/O（subprocess 调 security / 读文件），
    # 用 to_thread 包一下，不要在 async 函数里直接同步调用，否则会阻塞事件循环，
    # 让 asyncio.gather 并发查询各渠道的效果打折。
    cred = await asyncio.to_thread(read_claude_credentials)
    if cred.status == CRED_NO_TOKEN:
        # 已登录（钥匙串里有 claudeAiOauth 元信息），但本机没有存明文 access
        # token，查不了官方用量窗口——这不是错误，是"有数据但只能看本地统计"，
        # 所以用 status="info" 而不是 error/not_found（那两个会被前端当成异常）。
        subscription_type = (cred.extra or {}).get("subscription_type")
        plan_name = f"Claude {subscription_type.capitalize()} 订阅" if subscription_type else "Claude 订阅"
        return ChannelResult(
            status="info",
            message=cred.message,
            plan_name=plan_name,
            windows=[],
            source=cred.source,
            **base,
        )
    if cred.status != CRED_OK:
        return _cred_fail(cred, base)
    try:
        data = await request_json(
            "GET",
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {cred.token}",
                "anthropic-beta": "oauth-2025-04-20",
                "Accept": "application/json",
            },
        )
    except Exception as e:
        return _fail_from_error(base, e, "Claude")
    if not isinstance(data, dict):
        return fail("error", "Claude 用量响应格式错误", source=cred.source, **base)

    windows = []
    for key, label in [
        ("five_hour", "每 5 小时"),
        ("seven_day_opus", "每周（Opus）"),
        ("seven_day_sonnet", "每周（Sonnet）"),
        ("seven_day", "每周额度"),
    ]:
        item = data.get(key)
        if isinstance(item, dict) and item.get("utilization") is not None:
            used = float(item["utilization"]) * 100
            windows.append(
                window(
                    "five_hour" if key == "five_hour" else "weekly",
                    label,
                    used_percent=used,
                    remaining_percent=max(0.0, 100 - used),
                    reset_at=to_ts(item.get("resets_at")),
                )
            )
    # 未知窗口也展示（API 可能新增窗口类型）
    for key, item in data.items():
        if key in (
            "five_hour",
            "seven_day",
            "seven_day_opus",
            "seven_day_sonnet",
            "extra_usage",
        ):
            continue
        if isinstance(item, dict) and item.get("utilization") is not None:
            used = float(item["utilization"]) * 100
            windows.append(
                window(
                    "custom",
                    key.replace("_", " "),
                    used_percent=used,
                    remaining_percent=max(0.0, 100 - used),
                    reset_at=to_ts(item.get("resets_at")),
                )
            )

    plan_name = None
    extra = data.get("extra_usage")
    if isinstance(extra, dict):
        if extra.get("is_enabled") is True:
            limit = float(extra.get("monthly_limit") or 0)
            used_credits = float(extra.get("used_credits") or 0)
            if limit > 0:
                windows.append(
                    window(
                        "monthly",
                        "超额额度（本月）",
                        used_percent=used_credits / limit * 100,
                        remaining_percent=max(0.0, 100 - used_credits / limit * 100),
                        used_label=f"${used_credits:,.2f}",
                        max_label=f"${limit:,.2f}",
                        reset_at=to_ts(extra.get("reset_at")),
                    )
                )
        currency = extra.get("currency")
        plan_name = f"Claude 订阅（超额：{currency or 'USD'}）" if extra.get("is_enabled") else "Claude 订阅"

    if not windows:
        return fail(
            "error",
            "Claude 未返回用量窗口数据（可能未订阅）",
            source=cred.source,
            **base,
        )
    return ok(plan_name=plan_name, windows=windows, source=cred.source, **base)


# ── Gemini (AI Studio) 订阅 ─────────────────────────────────


async def query_gemini(channel: Channel) -> ChannelResult:
    base = _base(channel)
    cred = await asyncio.to_thread(read_gemini_credentials)
    if cred.status != CRED_OK:
        return _cred_fail(cred, base)
    headers = {
        "Authorization": f"Bearer {cred.token}",
        "Content-Type": "application/json",
    }
    try:
        load = await request_json(
            "POST",
            "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
            headers=headers,
            json_body={"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
        )
        project = None
        if isinstance(load, dict):
            # Google JSON API 默认返回 camelCase（cloudaicompanionProject），但个别
            # 网关/旧客户端库会转成 snake_case——两种都试。
            companion = _pick(load, "cloudaicompanionProject", "cloudaicompanion_project")
            if isinstance(companion, dict):
                project = _pick(companion, "project")
            else:
                project = companion
        quota_body = {"project": project} if project else {}
        data = await request_json(
            "POST",
            "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
            headers=headers,
            json_body=quota_body,
        )
    except Exception as e:
        return _fail_from_error(base, e, "Gemini")
    if not isinstance(data, dict):
        return fail("error", "Gemini 用量响应格式错误", source=cred.source, **base)

    # 按模型档位聚合（pro / flash / flash-lite），取各桶最小剩余比例
    # 同样兼容 camelCase（modelId/remainingFraction/resetTime）和 snake_case。
    buckets: list[dict] = data.get("buckets") or []
    categories: dict[str, dict] = {}
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        model_id = str(_pick(bucket, "modelId", "model_id", default="unknown"))
        category = _classify_gemini_model(model_id)
        remaining = float(_pick(bucket, "remainingFraction", "remaining_fraction", default=1.0))
        entry = categories.setdefault(category, {"remaining": 1.0, "reset": None, "models": set()})
        entry["models"].add(model_id)
        if remaining < entry["remaining"]:
            entry["remaining"] = remaining
            reset_time = _pick(bucket, "resetTime", "reset_time")
            if reset_time:
                entry["reset"] = reset_time

    label_map = {
        "gemini_pro": "Gemini Pro",
        "gemini_flash": "Gemini Flash",
        "gemini_flash_lite": "Gemini Flash-Lite",
        "other": "其他模型",
    }
    order = {"gemini_pro": 0, "gemini_flash": 1, "gemini_flash_lite": 2, "other": 3}
    windows = []
    for category in sorted(categories, key=lambda c: order.get(c, 9)):
        entry = categories[category]
        remaining = entry["remaining"] * 100
        windows.append(
            window(
                "custom",
                label_map.get(category, category),
                used_percent=100 - remaining,
                remaining_percent=remaining,
                reset_at=to_ts(entry["reset"]),
            )
        )
    if not windows:
        return fail("error", "Gemini 未返回用量数据（可能未订阅）", source=cred.source, **base)
    return ok(plan_name="Gemini (AI Studio) 订阅", windows=windows, source=cred.source, **base)


def _classify_gemini_model(model_id: str) -> str:
    lower = model_id.lower()
    if "pro" in lower:
        return "gemini_pro"
    if "flash-lite" in lower or "flash_lite" in lower:
        return "gemini_flash_lite"
    if "flash" in lower:
        return "gemini_flash"
    return "other"


# ── Grok (SuperGrok / X) 订阅 ───────────────────────────────

GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"


async def query_grok(channel: Channel) -> ChannelResult:
    base = _base(channel)
    cred = await asyncio.to_thread(read_grok_credentials)
    if cred.status != CRED_OK:
        return _cred_fail(cred, base)
    headers = {
        "Authorization": f"Bearer {cred.token}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-userid": cred.extra.get("user_id", ""),
        "x-grok-client-version": "0.2.64",
        "Accept": "application/json",
    }
    try:
        data = await request_json("GET", GROK_BILLING_URL, headers=headers)
    except Exception as e:
        return _fail_from_error(base, e, "Grok")
    if not isinstance(data, dict):
        return fail("error", "Grok 账单响应格式错误", source=cred.source, **base)

    config = data.get("config") or {}
    windows = []
    if isinstance(config, dict):
        used_pct = config.get("creditUsagePercent")
        if isinstance(used_pct, (int, float)):
            period = config.get("currentPeriod") or {}
            period_type = str(period.get("type") or "") if isinstance(period, dict) else ""
            key = "monthly" if "MONTH" in period_type.upper() else "weekly"
            label = "每月额度" if key == "monthly" else "每周额度"
            windows.append(
                window(
                    key,
                    label,
                    used_percent=float(used_pct),
                    remaining_percent=max(0.0, 100 - float(used_pct)),
                    reset_at=to_ts(period.get("end")),
                )
            )
        # 产品维度分解
        for product in config.get("productUsage") or []:
            if not isinstance(product, dict):
                continue
            name = str(product.get("product") or product.get("name") or "其他")
            pct = product.get("creditUsagePercent") or product.get("usagePercent") or product.get("usedPercent")
            if isinstance(pct, (int, float)):
                windows.append(
                    window(
                        "custom",
                        f"{name}",
                        used_percent=float(pct),
                        remaining_percent=max(0.0, 100 - float(pct)),
                    )
                )
        # 充值余额
        prepaid = config.get("prepaidBalance")
        if isinstance(prepaid, dict) and prepaid.get("val") is not None:
            cents = float(prepaid["val"])
            windows.append(
                window(
                    "credits",
                    "充值余额 (Credits)",
                    used_percent=None,
                    remaining_percent=None,
                    used_label=None,
                    max_label=f"${cents / 100:,.2f}",
                )
            )
        # 兼容遗留字段
        if not windows and config.get("monthlyLimit"):
            limit = float(config["monthlyLimit"])
            used = float(config.get("used") or 0)
            windows.append(
                window(
                    "monthly",
                    "每月额度",
                    used_percent=used / limit * 100 if limit else 0,
                    remaining_percent=(1 - used / limit) * 100 if limit else 0,
                    used_label=f"${used:,.2f}",
                    max_label=f"${limit:,.2f}",
                )
            )
    # 注：原来这里写的是 `data.get("subscriptionTier") or (config or {}).get("plan")
    # if isinstance(config, dict) else None`——三元表达式的优先级低于 `or`，实际
    # 等价于 `(A or B) if C else D`，而 config 在上面已经 `= data.get("config") or {}`
    # 保证是 dict 了，所以 `if isinstance(config, dict)` 几乎恒真，整个三元没有
    # 意义，只是让人误以为这里有条件判断。config 本来就保证是 dict，直接取即可。
    tier = data.get("subscriptionTier") or config.get("plan")
    if not windows:
        return fail("error", "Grok 未返回用量数据（可能未订阅）", source=cred.source, **base)
    plan_name = f"Grok {tier.capitalize()}" if tier else "Grok 订阅"
    return ok(plan_name=plan_name, windows=windows, source=cred.source, **base)


# ── ChatGPT (Codex) 订阅 ────────────────────────────────────


async def query_codex(channel: Channel) -> ChannelResult:
    base = _base(channel)
    # 渠道可以关联一份用户上传的 auth.json（多账号场景，extra.codex_auth_file
    # 指向 config 同目录 credentials/ 下的文件）；否则读本机 Codex CLI 登录态。
    # codex_auth_file 经 resolve_codex_auth_file 校验——extra 是用户可自由设置的
    # 开放字段，不校验的话 ../../ 或绝对路径会读本目录之外的任意文件（且删除渠道
    # 时会被 unlink 删掉）。
    auth_file = (channel.extra or {}).get("codex_auth_file")
    path = resolve_codex_auth_file(auth_file) if auth_file else None
    if path is not None:
        cred = await asyncio.to_thread(read_codex_credentials_from_file, path)
    else:
        cred = await asyncio.to_thread(read_codex_credentials)
    if cred.status != CRED_OK:
        return _cred_fail(cred, base)
    headers = {
        "Authorization": f"Bearer {cred.token}",
        "Accept": "application/json",
    }
    if cred.extra.get("account_id"):
        headers["ChatGPT-Account-Id"] = cred.extra["account_id"]
    try:
        data = await request_json("GET", "https://chatgpt.com/backend-api/wham/usage", headers=headers)
    except Exception as e:
        return _fail_from_error(base, e, "Codex")
    if not isinstance(data, dict):
        return fail("error", "Codex 额度响应格式错误", source=cred.source, **base)
    rate_limit = data.get("rate_limit") or {}
    windows = []
    for key in ("primary_window", "secondary_window"):
        item = rate_limit.get(key)
        if not isinstance(item, dict):
            continue
        used = item.get("used_percent")
        duration = item.get("limit_window_seconds")
        if used is None or not duration:
            continue
        used = float(used)
        duration = float(duration)
        if abs(duration - 5 * 3600) < 60:
            tier, label = "five_hour", "每 5 小时"
        elif abs(duration - 7 * 86400) < 60:
            tier, label = "weekly", "每周额度"
        elif abs(duration - 30 * 86400) < 60:
            tier, label = "monthly", "每月额度"
        else:
            tier, label = "custom", f"每 {duration / 3600:.1f} 小时"
        windows.append(
            window(
                tier,
                label,
                used_percent=used,
                remaining_percent=100 - used,
                reset_at=to_ts(item.get("reset_at")),
            )
        )
    if not windows:
        return fail("error", "ChatGPT 未返回 Codex 订阅额度数据", source=cred.source, **base)
    plan_type = data.get("plan_type")
    plan_name = f"ChatGPT {plan_type.capitalize()} (Codex)" if plan_type else "ChatGPT 订阅 (Codex)"
    return ok(plan_name=plan_name, windows=windows, source=cred.source, **base)


# ── GitHub Copilot ──────────────────────────────────────────


async def query_copilot(channel: Channel) -> ChannelResult:
    base = _base(channel)
    cred = await asyncio.to_thread(read_copilot_credentials)
    if cred.status != CRED_OK:
        return _cred_fail(cred, base)
    headers = {
        "Authorization": f"token {cred.token}",
        "Content-Type": "application/json",
        "editor-version": "vscode/1.90.0",
        "editor-plugin-version": "copilot-chat/0.17.0",
        "user-agent": "GitHubCopilot/1.0 quota-board",
        "x-github-api-version": "2023-07-07",
    }
    try:
        data = await request_json(
            "GET",
            "https://api.githubcopilot.com/copilot_internal/user",
            headers=headers,
        )
    except Exception as e:
        return _fail_from_error(base, e, "Copilot")
    if not isinstance(data, dict):
        return fail("error", "Copilot 用量响应格式错误", source=cred.source, **base)

    windows: list = []
    notes: list[str] = []
    plan = data.get("copilot_plan") or ""

    # 真实配额在 quota_snapshots 里（chat / completions / premium_interactions，
    # 各含 entitlement 总额、remaining 剩余、percent_remaining 剩余百分比、
    # unlimited 是否无限）。之前完全没解析它，只拿 copilot_plan 拼了个没有实际
    # 百分比的假窗口。
    snapshots = data.get("quota_snapshots")
    if isinstance(snapshots, dict) and snapshots:
        _parse_copilot_quota_snapshots(snapshots, windows)
    else:
        notes.append("响应中没有 quota_snapshots 字段（可能是企业版额度或接口已变更）")

    reset_date = data.get("quota_reset_date")
    reset_ts = None
    if isinstance(reset_date, str):
        try:
            reset_ts = int(datetime.fromisoformat(reset_date).timestamp() * 1000)
        except ValueError:
            pass
    if reset_ts is not None:
        # quota_reset_date 是全局下一次重置时间，补到还没有各自 reset_at 的窗口上
        for w in windows:
            if w.reset_at is None:
                w.reset_at = reset_ts

    # /usage 端点补充（旧版兼容路径：quota_snapshots 是主要数据来源，这里失败不
    # 应该整体判定为 error，但也不能像原来一样 except Exception: pass 完全吞掉，
    # 否则排查"为什么用量比预期少"时无从下手）。
    endpoints = data.get("endpoints")
    if isinstance(endpoints, dict) and endpoints.get("api"):
        try:
            usage = await request_json(
                "GET",
                f"{endpoints['api']}/usage",
                headers=dict(headers),
            )
            if isinstance(usage, dict):
                _merge_copilot_usage(usage, windows)
        except Exception as e:
            notes.append(f"/usage 端点查询失败（已忽略，不影响 quota_snapshots 数据）: {e}")

    message = "；".join(notes) if notes else None
    if not windows:
        return fail("error", message or "Copilot 未返回用量数据", source=cred.source, **base)
    return ok(
        plan_name=f"Copilot {plan}" if plan else "GitHub Copilot",
        windows=windows,
        message=message,
        source=cred.source,
        **base,
    )


COPILOT_QUOTA_LABELS = {
    "chat": "Chat 对话",
    "completions": "代码补全",
    "premium_interactions": "高级模型请求",
}


def _parse_copilot_quota_snapshots(snapshots: dict, windows: list) -> None:
    """解析 copilot_internal/user 响应里的 quota_snapshots 字段（真实配额数据）。

    形如 {"chat": {"entitlement": 300, "remaining": 120, "percent_remaining": 40.0,
    "unlimited": false}, "completions": {...}, "premium_interactions": {...}}。
    """
    for raw_key, item in snapshots.items():
        if not isinstance(item, dict):
            continue
        label = COPILOT_QUOTA_LABELS.get(raw_key, str(raw_key))
        if item.get("unlimited"):
            windows.append(
                window(
                    "custom",
                    label,
                    used_percent=0.0,
                    remaining_percent=100.0,
                    max_label="无限量",
                )
            )
            continue

        percent_remaining = item.get("percent_remaining")
        entitlement = item.get("entitlement")
        remaining = item.get("remaining")
        if percent_remaining is None and entitlement and remaining is not None:
            try:
                percent_remaining = float(remaining) / float(entitlement) * 100
            except (TypeError, ValueError, ZeroDivisionError):
                percent_remaining = None
        if percent_remaining is None:
            continue
        percent_remaining = max(0.0, min(100.0, float(percent_remaining)))

        used_label = max_label = None
        if entitlement is not None and remaining is not None:
            try:
                used_count = float(entitlement) - float(remaining)
                used_label = f"{used_count:,.0f}"
                max_label = f"{float(entitlement):,.0f}"
            except (TypeError, ValueError):
                pass

        windows.append(
            window(
                "custom",
                label,
                used_percent=100 - percent_remaining,
                remaining_percent=percent_remaining,
                used_label=used_label,
                max_label=max_label,
            )
        )


def _merge_copilot_usage(usage: dict, windows: list) -> None:
    for tier in ("chat", "code_review", "code_completion", "copilot_ide"):
        item = usage.get(tier)
        if not isinstance(item, dict):
            continue
        used = item.get("total_requests") or 0
        limit = item.get("limit") or 0
        if limit:
            label = {
                "chat": "Chat 请求",
                "code_review": "Code Review",
                "code_completion": "代码补全",
                "copilot_ide": "IDE 用量",
            }[tier]
            windows.append(
                window(
                    "custom",
                    label,
                    used_percent=float(used) / float(limit) * 100,
                    remaining_percent=(1 - float(used) / float(limit)) * 100,
                    used_label=f"{used:,}",
                    max_label=f"{limit:,}",
                )
            )
