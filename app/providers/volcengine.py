"""火山方舟 Agent Plan / Coding Plan 额度查询（OpenAPI + 火山版 SigV4）。

对照 cc-switch 的实现移植：
- 控制面网关 open.volcengineapi.com（不是数据面推理域名）
- 强制火山签名 V4（AK/SK），算法是 AWS SigV4 的火山变体：
  - canonical headers 固定顺序 host;x-date;x-content-sha256;content-type（不按字母序）
  - algorithm 串 HMAC-SHA256（无 AWS4 前缀），credential scope 结尾 request
  - kDate = HMAC(SK, date)，SK 不加 AWS4 前缀
- 先 GetAFPUsage（Agent Plan，Quota/Used 绝对值），未订阅再 GetCodingPlanUsage（百分比）
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac as hmac_mod
import json
from datetime import UTC, datetime

from ..config import Channel
from ..models import ChannelResult, fail, ok, to_ts, window
from ..net import ParseError, ResponseError, request_text

HOST = "open.volcengineapi.com"
API_VERSION = "2024-01-01"
DEFAULT_REGION = "cn-beijing"
SERVICE = "ark"
CONTENT_TYPE = "application/json; charset=utf-8"
# 火山签名 V4：SignedHeaders 必须按字典序排列（content-type < host < x-content-sha256
# < x-date），canonical headers 的行顺序也必须与此一致——之前写的是
# "host;x-date;x-content-sha256;content-type"，既不是字典序也和 canonical_headers
# 的行顺序绕在一起，会导致签名计算错误（SignatureDoesNotMatch）。
SIGNED_HEADERS = "content-type;host;x-content-sha256;x-date"


def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac_mod.new(key, data, hashlib.sha256).digest()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _uri_encode(value: str) -> str:
    out = []
    for byte in value.encode("utf-8"):
        if byte in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~":
            out.append(chr(byte))
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def _canonical_query(action: str, region: str) -> str:
    pairs = sorted([("Action", action), ("Region", region), ("Version", API_VERSION)])
    return "&".join(f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in pairs)


def _sign(ak: str, sk: str, region: str, action: str) -> tuple[str, str, str]:
    now = datetime.now(UTC)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = now.strftime("%Y%m%d")
    body = b""
    x_content_sha256 = _sha256_hex(body)

    # 行顺序必须和 SIGNED_HEADERS 里的字典序一致：content-type, host,
    # x-content-sha256, x-date。
    canonical_headers = (
        f"content-type:{CONTENT_TYPE}\nhost:{HOST}\nx-content-sha256:{x_content_sha256}\nx-date:{x_date}\n"
    )
    canonical_request = (
        f"POST\n/\n{_canonical_query(action, region)}\n{canonical_headers}\n{SIGNED_HEADERS}\n{x_content_sha256}"
    )
    credential_scope = f"{short_date}/{region}/{SERVICE}/request"
    string_to_sign = f"HMAC-SHA256\n{x_date}\n{credential_scope}\n{_sha256_hex(canonical_request.encode())}"
    k_date = _hmac_sha256(sk.encode(), short_date.encode())
    k_region = _hmac_sha256(k_date, region.encode())
    k_service = _hmac_sha256(k_region, SERVICE.encode())
    k_signing = _hmac_sha256(k_service, b"request")
    signature = _hmac_sha256(k_signing, string_to_sign.encode()).hex()

    authorization = (
        f"HMAC-SHA256 Credential={ak}/{credential_scope}, SignedHeaders={SIGNED_HEADERS}, Signature={signature}"
    )
    return authorization, x_date, x_content_sha256


async def _openapi_call(region: str, ak: str, sk: str, action: str) -> dict:
    authorization, x_date, x_content_sha256 = _sign(ak, sk, region, action)
    url = f"https://{HOST}/?{_canonical_query(action, region)}"
    text = await request_text(
        "POST",
        url,
        headers={
            "Authorization": authorization,
            "X-Date": x_date,
            "X-Content-Sha256": x_content_sha256,
            "Content-Type": CONTENT_TYPE,
            "Host": HOST,
        },
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(f"火山响应 JSON 解析失败: {e}") from e


def _is_signature_error(code: str) -> bool:
    """签名计算错误（SignatureDoesNotMatch 等）——通常是本工具的签名实现有 bug，
    不是用户的 AK/SK 配置问题，必须单独分类，不能和"请检查 AK/SK"混在一起提示，
    否则会把签名 bug 误报成用户配错密钥，用户排查半天也换不出正确的 AK/SK。"""
    return "signature" in code.lower()


def _is_auth_error(code: str) -> bool:
    """AK/SK 无效、权限不足等真正的鉴权问题（用户侧问题）。"""
    c = code.lower()
    return any(
        k in c
        for k in (
            "auth",
            "accessdenied",
            "denied",
            "unauthorized",
            "forbidden",
            "credential",
            "token",
        )
    )


def _error_of(body: dict) -> tuple[str, str] | None:
    err = (body.get("ResponseMetadata") or {}).get("Error") if isinstance(body.get("ResponseMetadata"), dict) else None
    if not isinstance(err, dict):
        err = body.get("Error")
    if not isinstance(err, dict):
        return None
    code = str(err.get("Code") or "")
    msg = str(err.get("Message") or "")
    if not code and not msg:
        return None
    return code, msg


async def query_volcengine(channel: Channel) -> ChannelResult:
    """自动判断并列出此账号下的所有 Plan (Agent Plan + Coding Plan)。"""
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "coding_plan",
    }
    if not channel.ak or not channel.sk:
        return fail("error", "未配置 AK/SK", **base)
    region = channel.region or DEFAULT_REGION
    soft_errors: list[str] = []
    agent_windows: list = []
    coding_windows: list = []
    agent_plan_type: str | None = None

    # 1) Agent Plan
    try:
        body = await _openapi_call(region, channel.ak, channel.sk, "GetAFPUsage")
        err = _error_of(body)
        if err:
            if _is_signature_error(err[0]):
                return fail("error", f"火山签名计算错误: {err[0]} {err[1]}", **base)
            if _is_auth_error(err[0]):
                return fail("expired", f"火山鉴权失败: {err[0]} {err[1]}", **base)
            soft_errors.append(f"Agent Plan: {err[0]} {err[1]}")
        else:
            result = body.get("Result") or body
            agent_windows = _parse_afp_tiers(result)
            agent_plan_type = result.get("PlanType")
    except (ResponseError, ParseError) as e:
        soft_errors.append(f"Agent Plan: {e}")

    # 2) Coding Plan
    try:
        body = await _openapi_call(region, channel.ak, channel.sk, "GetCodingPlanUsage")
        err = _error_of(body)
        if err:
            if _is_signature_error(err[0]):
                return fail("error", f"火山签名计算错误: {err[0]} {err[1]}", **base)
            if _is_auth_error(err[0]):
                return fail("expired", f"火山鉴权失败: {err[0]} {err[1]}", **base)
            soft_errors.append(f"Coding Plan: {err[0]} {err[1]}")
        else:
            result = body.get("Result") or body
            coding_windows = _parse_coding_plan_tiers(result)
    except (ResponseError, ParseError) as e:
        soft_errors.append(f"Coding Plan: {e}")

    windows, plan_name = _merge_plans(agent_windows, coding_windows, agent_plan_type)
    if not windows:
        if soft_errors:
            return fail("error", "；".join(soft_errors), **base)
        return fail("error", "未检测到火山 Agent Plan 或 Coding Plan 订阅", **base)

    return ok(plan_name=plan_name, windows=windows, message="；".join(soft_errors) or None, **base)


def _merge_plans(agent_windows: list, coding_windows: list, agent_plan_type: str | None) -> tuple[list, str]:
    """合并 Agent Plan 与 Coding Plan 的窗口。

    两个 plan 的窗口 key 相同（five_hour/weekly/monthly）、label 也相同（如
    「每 5 小时」），合并后必须加来源前缀区分——key 加 plan 维度
    （agent_*/coding_*）让趋势图按 key 分线、前端按 key 分组渲染，label 加
    「Agent 」/「Coding 」前缀让展示可读。返回 (windows, plan_name)。
    """
    windows = [dataclasses.replace(w, key=f"agent_{w.key}", label=f"Agent {w.label}") for w in agent_windows]
    windows += [dataclasses.replace(w, key=f"coding_{w.key}", label=f"Coding {w.label}") for w in coding_windows]
    names = []
    if agent_windows:
        names.append(f"火山 Agent Plan {agent_plan_type}" if agent_plan_type else "火山 Agent Plan")
    if coding_windows:
        names.append("火山 Coding Plan")
    return windows, " · ".join(names)


def _parse_afp_tiers(result: dict) -> list:
    """Agent Plan：Result.AFPFiveHour/AFPWeekly/AFPMonthly，字段 Quota/Used/ResetTime。"""
    windows = []
    for key, tier, label in [
        ("AFPFiveHour", "five_hour", "每 5 小时"),
        ("AFPWeekly", "weekly", "每周额度"),
        ("AFPMonthly", "monthly", "每月额度"),
    ]:
        item = result.get(key)
        if not isinstance(item, dict):
            continue
        quota = float(item.get("Quota") or 0)
        if quota <= 0:
            continue
        used = float(item.get("Used") or 0)
        used_pct = used / quota * 100
        windows.append(
            window(
                tier,
                label,
                used_percent=used_pct,
                remaining_percent=max(0.0, 100 - used_pct),
                used_label=f"{used:,.0f} APF",
                max_label=f"{quota:,.0f} APF",
                reset_at=to_ts(item.get("ResetTime")),
            )
        )
    return windows


def _parse_coding_plan_tiers(result: dict) -> list:
    """Coding Plan：Result.QuotaUsage[]（或 Usages/Details），只给百分比。"""
    items = result.get("QuotaUsage") or result.get("Usages") or result.get("Details")
    if not isinstance(items, list):
        return []
    windows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(
            item.get("Level") or item.get("Type") or item.get("Period") or item.get("Label") or item.get("Window") or ""
        ).lower()
        if "session" in label or "5h" in label or "five" in label:
            tier, tier_label = "five_hour", "每 5 小时"
        elif "week" in label or "7d" in label:
            tier, tier_label = "weekly", "每周额度"
        elif "month" in label:
            tier, tier_label = "monthly", "每月额度"
        else:
            continue
        used = float(item.get("Percent") or item.get("UsedPercent") or item.get("UsagePercent") or 0)
        windows.append(
            window(
                tier,
                tier_label,
                used_percent=used,
                remaining_percent=max(0.0, 100 - used),
                reset_at=to_ts(item.get("ResetTime") or item.get("ResetTimestamp")),
            )
        )
    return windows
