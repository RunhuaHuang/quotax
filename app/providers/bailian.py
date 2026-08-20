"""阿里云百炼 Token Plan（个人版）额度查询：Cookie 方式调用 OneConsole 网关。

百炼控制台没有公开的 JSON API，额度数据（滚动 5 小时 / 周两个窗口的已用百分比
+ 重置时间）由控制台前端通过阿里云 OneConsole 数据网关下发。参考 CodExBar 的
AlibabaTokenPlanUsageFetcher / QwenCloudTokenPlanAPIClient 逆向的请求协议：

- 网关：POST {gateway}/data/api.json?action=<action>&product=sfm_bailian
        &api=zeldaHttp.apikeyMgr./tokenplan/personal/api/v2/usage
- 请求体：application/x-www-form-urlencoded，字段 product / action / region /
  language / params（JSON 信封，内含 cornerstoneParam 控制台元数据）/ sec_token
- 认证：用户浏览器 Cookie（登录 bailian.console.aliyun.com 后整段复制），CSRF
  头取 Cookie 里 login_aliyunid_csrf 的值；sec_token 尽力解析（部分账号不带
  也能过，CodExBar 注释明确说明 cookie-only 对部分账号可用）
- 响应：嵌套 JSON（部分字段是内嵌 JSON 字符串），递归展开后找含
  per5HourPercentage / per1WeekPercentage 的对象；百分比值是 0-1 的比例。

遵循本项目的「只读凭据」原则：Cookie 存本地 config.json（权限 600），只读查询、
绝不刷新、绝不写回。区域支持 cn（默认，中国大陆个人版）/ intl（国际版
Model Studio 个人版）。
"""

from __future__ import annotations

import json
import re
import uuid

from ..config import Channel
from ..models import ChannelResult, fail, finite_float, ok, to_ts, window
from ..net import ParseError, ResponseError, request_json, request_text

# 个人版（Solo）Token Plan 的数据网关与控制台常量（对照 CodExBar
# AlibabaTokenPlanAPIRegion 的 chinaMainlandPersonal / internationalPersonal）
_REGIONS: dict[str, dict] = {
    "cn": {
        "gateway": "https://bailian-cs.console.aliyun.com",
        "console": "https://bailian.console.aliyun.com",
        "dashboard": "https://bailian.console.aliyun.com/cn-beijing?tab=plan#/efm/subscription/token-plan/personal",
        "action": "BroadScopeAspnGateway",
        "region_id": "cn-beijing",
        "console_site": "BAILIAN_ALIYUN",
        "language": "zh-CN",
    },
    "intl": {
        "gateway": "https://bailian-singapore-cs.alibabacloud.com",
        "console": "https://modelstudio.console.alibabacloud.com",
        "dashboard": "https://modelstudio.console.alibabacloud.com/ap-southeast-1/?tab=plan#/efm/subscription/token-plan/personal",
        "action": "IntlBroadScopeAspnGateway",
        "region_id": "ap-southeast-1",
        # 阿里国际版控制台的既有拼写（含历史性拼写错误），必须原样发送
        "console_site": "MODELSTUDIO_ALBABACLOUD",
        "language": "en-US",
    },
}

_USAGE_API = "zeldaHttp.apikeyMgr./tokenplan/personal/api/v2/usage"

# OneConsole 把 sec_token 以内联 JS 常量注入控制台页面 HTML；兜底模式覆盖
# sec_token / secToken / csrfToken 三种命名（与 CodExBar 一致）
_SEC_TOKEN_HTML_RE = re.compile(
    r"""(?:sec_token|secToken|csrfToken)['"]?\s*[:=]\s*['"]([A-Za-z0-9._-]{8,})['"]"""
)


def _cookie_pairs(cookie_header: str) -> dict[str, str]:
    """把浏览器复制的整段 Cookie 头解析成 {name: value}（保持最后一个重复项）。"""
    pairs: dict[str, str] = {}
    for part in cookie_header.split(";"):
        name, sep, value = part.partition("=")
        if sep:
            pairs[name.strip()] = value.strip()
    return pairs


def _ratio_to_percent(value) -> float | None:
    """百炼百分比字段是 0-1 的比例（CodExBar percentagePoints 同款换算）；
    兼容个别接口直接返回百分数的形态。返回 0-100 的已用百分比。"""
    v = finite_float(value, None)
    if v is None:
        return None
    pct = v * 100 if 0 <= v <= 1 else v
    return max(0.0, min(100.0, pct))


async def _resolve_sec_token(cookie_header: str, region: dict) -> str | None:
    """尽力解析 OneConsole 的 sec_token：先看 Cookie 里有没有现成的同名项，
    没有再抓控制台页面 HTML 从内联 JS 常量里提取。两条路都失败返回 None——
    部分账号不带 sec_token 也能过网关（CodExBar 注释），由调用方裸试。"""
    pairs = _cookie_pairs(cookie_header)
    direct = pairs.get("sec_token") or pairs.get("SecToken")
    if direct:
        return direct
    try:
        html = await request_text(
            "GET", region["dashboard"], headers={"Cookie": cookie_header, "Accept": "text/html"}
        )
    except Exception:
        return None
    m = _SEC_TOKEN_HTML_RE.search(html)
    return m.group(1) if m else None


def _expand_embedded(value, depth: int = 0):
    """递归展开 OneConsole 响应里内嵌为字符串的 JSON（部分字段的值是序列化
    过的 JSON 文本）。深度限制防畸形数据自嵌套。"""
    if depth > 6:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return _expand_embedded(json.loads(stripped), depth + 1)
            except (ValueError, TypeError):
                return value
        return value
    if isinstance(value, dict):
        return {k: _expand_embedded(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_embedded(v, depth + 1) for v in value]
    return value


def _find_usage_object(obj, depth: int = 0) -> dict | None:
    """在（已展开的）响应里递归找含 per5HourPercentage / per1WeekPercentage
    的对象——OneConsole 的包裹层级随产品演进变动，按特征字段定位最稳。"""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        if "per5HourPercentage" in obj or "per1WeekPercentage" in obj:
            return obj
        for v in obj.values():
            found = _find_usage_object(v, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_usage_object(v, depth + 1)
            if found is not None:
                return found
    return None


def parse_bailian_usage(data) -> dict:
    """解析 zelda usage 响应 → {"five_hour_used": float|None, "weekly_used": float|None,
    "five_hour_reset_ms": int|None, "weekly_reset_ms": int|None}。

    百分比字段是「已用」语义（CodExBar fiveHourUsedPercent 同名映射）。
    抛 ValueError 表示响应里没有可识别的用量数据。
    """
    expanded = _expand_embedded(data)
    usage = _find_usage_object(expanded)
    if usage is None:
        raise ValueError("响应中未找到 per5HourPercentage / per1WeekPercentage 用量数据")
    out = {
        "five_hour_used": _ratio_to_percent(usage.get("per5HourPercentage")),
        "weekly_used": _ratio_to_percent(usage.get("per1WeekPercentage")),
        "five_hour_reset_ms": to_ts(usage.get("per5HourResetTime")),
        "weekly_reset_ms": to_ts(usage.get("per1WeekResetTime")),
    }
    if out["five_hour_used"] is None and out["weekly_used"] is None:
        raise ValueError("用量字段存在但均为空值")
    return out


async def query_bailian(channel: Channel) -> ChannelResult:
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "coding_plan",
    }
    cookie = (channel.api_key or "").strip()
    if not cookie:
        return fail(
            "not_found",
            "未配置 Cookie（请登录 bailian.console.aliyun.com 后，从浏览器复制完整 Cookie 填入）",
            **base,
        )
    region_key = (channel.region or "cn").strip().lower()
    region = _REGIONS.get(region_key)
    if region is None:
        return fail("error", f"未知区域: {channel.region}（支持 cn / intl）", **base)

    pairs = _cookie_pairs(cookie)
    headers = {
        "Cookie": cookie,
        "Accept": "application/json, text/plain, */*",
        "Origin": region["console"],
        "Referer": region["dashboard"],
        "X-Requested-With": "XMLHttpRequest",
    }
    # CSRF 头取 Cookie 里的 login_aliyunid_csrf（国际版用 csrf）——两个头都带，
    # 与控制台前端行为一致
    csrf = pairs.get("login_aliyunid_csrf") or pairs.get("csrf")
    if csrf:
        headers["x-xsrf-token"] = csrf
        headers["x-csrf-token"] = csrf

    # sec_token 尽力解析；解析失败不带裸试（部分账号 cookie-only 可用）
    sec_token = await _resolve_sec_token(cookie, region)

    params = json.dumps(
        {
            "Api": _USAGE_API,
            "V": "1.0",
            "Data": {
                "cornerstoneParam": {
                    "feTraceId": str(uuid.uuid4()),
                    "feURL": region["dashboard"],
                    "protocol": "V2",
                    "console": "ONE_CONSOLE",
                    "productCode": "p_efm",
                    "domain": region["console"].split("://", 1)[-1],
                    "consoleSite": region["console_site"],
                    "userNickName": "",
                    "userPrincipalName": "",
                    "xsp_lang": region["language"],
                }
            },
        },
        ensure_ascii=False,
    )
    form: dict[str, str] = {
        "product": "sfm_bailian",
        "action": region["action"],
        "region": region["region_id"],
        "language": region["language"],
        "params": params,
    }
    if sec_token:
        form["sec_token"] = sec_token

    url = (
        f"{region['gateway']}/data/api.json?action={region['action']}"
        f"&product=sfm_bailian&api={_USAGE_API}&_v=undefined"
    )
    try:
        data = await request_json("POST", url, headers=headers, form_body=form)
    except ResponseError as e:
        if e.status in (401, 403):
            return fail(
                "expired",
                f"Cookie 已失效或无权限 (HTTP {e.status})，请重新登录阿里云百炼控制台后复制新 Cookie",
                **base,
            )
        return fail("error", f"接口返回错误 (HTTP {e.status}): {e.body[:200]}", **base)
    except ParseError as e:
        return fail("error", str(e), **base)
    except Exception as e:
        from ..net import friendly_error

        return fail("error", friendly_error(e), **base)

    try:
        parsed = parse_bailian_usage(data)
    except ValueError as e:
        return fail("error", f"百炼用量解析失败: {e}（Cookie 可能缺少必要的登录态字段）", **base)

    windows = []
    if parsed["five_hour_used"] is not None:
        used = parsed["five_hour_used"]
        windows.append(
            window(
                "five_hour",
                "每 5 小时",
                used_percent=used,
                remaining_percent=max(0.0, 100 - used),
                reset_at=parsed["five_hour_reset_ms"],
            )
        )
    if parsed["weekly_used"] is not None:
        used = parsed["weekly_used"]
        windows.append(
            window(
                "weekly",
                "每周额度",
                used_percent=used,
                remaining_percent=max(0.0, 100 - used),
                reset_at=parsed["weekly_reset_ms"],
            )
        )
    plan_name = "百炼 Token Plan" + ("（国际版）" if region_key == "intl" else "（个人版）")
    return ok(plan_name=plan_name, windows=windows, source=region["gateway"], **base)
