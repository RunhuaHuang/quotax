"""OpenCode (opencode.ai) Zen/Go 订阅额度查询。

实现参考 Doueen/opencode-usage-extension 浏览器扩展的反向工程结果：
opencode 官网是 SSR（服务端渲染）页面，没有公开的 JSON API。额度数据以内嵌
的 React Server Component 序列化字符串形式写在 HTML 里，形如
``rollingUsage:$R[123]={status:"active",resetInSec:86400,usagePercent:42}``，
用正则提取即可得到「滚动 / 周 / 月」三个周期各自的已用百分比和重置倒计时。

与 MiMo 渠道同源的认证模式：opencode.ai 的额度页面（/workspace/<id>/go）
需要登录态，未登录访问会被 302 重定向到 /auth/authorize（已实测确认）。本服务
是后端进程，无法像浏览器扩展那样自动复用浏览器 cookie，所以需要用户从浏览器
（登录 opencode.ai 后）手动复制完整 Cookie 填入。外加一个工作区 ID（wrk_xxx，
从浏览器地址栏 opencode.ai/workspace/wrk_xxx/... 取得）。本工具只读查询，
绝不刷新 Cookie、绝不写回。
"""

from __future__ import annotations

import re
import time

from ..config import Channel
from ..models import ChannelResult, fail, ok, window
from ..net import ResponseError, request_text
from ._common import _require

OPENCODE_BASE = "https://opencode.ai"

# 浏览器伪装头：opencode 官网是 SSR + Cloudflare，对请求指纹敏感。
# 实测缺 Accept / Sec-Fetch-* 等头时，/zen 页面的 SSR 分支不会内嵌 workspace ID
# （只返回空壳 SPA），完整模拟浏览器导航请求才能拿到带数据的 HTML。
_BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}


def _normalize_cookie(cookie: str) -> str:
    """cookie 值规范化：裸值（Fe26.2**...）补上 auth= 前缀。

    用户从浏览器复制的 `auth` cookie 值不含 `name=value` 的 name 部分。HTTP Cookie
    头必须是 `auth=<value>` 才能被服务端识别——裸值发过去会被当成无名的 cookie，
    服务端拿不到 auth session，返回 302 登录。已含 `=` 的（完整 `auth=xxx` 或多个
    `k=v; k2=v2`）原样使用。
    """
    return cookie if "=" in cookie else f"auth={cookie}"


async def _discover_workspace_id(cookie_header: str) -> str | None:
    """用 Cookie 访问 /zen 页面，从 SSR HTML 里正则提取 wrk_xxx 工作区 ID。

    opencode 的 /zen 是 SolidStart SSR 页面，登录态下会把当前账号的 workspace ID
    内嵌在 HTML 里（RSC 数据流）。用户只需填 Cookie，不必手动找 workspace ID。

    返回找到的第一个 wrk_xxx（单账号通常只有一个活跃 workspace），找不到返回 None。
    需要完整的 _BROWSER_HEADERS（Sec-Fetch 等），否则 SSR 不内挂数据。
    """
    headers = dict(_BROWSER_HEADERS)
    headers["Referer"] = f"{OPENCODE_BASE}/"
    headers["Cookie"] = cookie_header
    try:
        html = await request_text("GET", f"{OPENCODE_BASE}/zen", headers=headers)
    except ResponseError:
        # 未登录访问 /zen 会被 302 到 /auth/authorize（follow_redirects=False）。
        # 这不是「找不到工作区 ID」——必须原样上抛让调用方经 _error_result 翻译成
        # expired，否则用户会拿着「去地址栏找 wrk_xxx」的错误指引白忙一场。
        raise
    except Exception:
        return None
    m = re.search(r"(wrk_[A-Za-z0-9]+)", html)
    return m.group(1) if m else None

# 提取 SSR 页面内嵌的额度序列化字符串。resetInSec 是「距离重置还剩多少秒」，
# usagePercent 是「已用百分比」（整数 0-100）。status 字段（如 "ok"）这里不
# 捕获——只关心数字。$R[N] 是 RSC 的引用占位，数字 N 每次请求可能不同。
# 实测真实页面片段：rollingUsage:$R[31]={status:"ok",resetInSec:18000,usagePercent:0}
# 注意 $R 前面没有反斜杠（之前误写成 \$R 导致完全不匹配）。
_USAGE_RE = re.compile(
    r'(\w+)Usage:\$R\[\d+\]=\{status:"[a-zA-Z]+",resetInSec:(\d+),usagePercent:(\d+)\}'
)

# 周期 → (QuotaWindow.key, 展示标签)
# rolling 的 resetInSec 实测是 18000 秒（= 5 小时），就是 5 小时额度窗口——
# 映射成 five_hour 而非 custom，这样前端「每 5 小时」槽位能正确显示，
# 不会出现「每 5 小时：未提供」+ 一个孤立的「滚动周期」的错配。
_PERIOD_MAP = {
    "rolling": ("five_hour", "每 5 小时"),
    "weekly": ("weekly", "每周额度"),
    "monthly": ("monthly", "每月额度"),
}


def _error_result(base: dict, e: Exception) -> ChannelResult:
    """把网络/响应异常翻译成 ChannelResult。

    opencode 的额度页面未登录时会 302 跳转 /auth/authorize——但 request_text 默认
    follow_redirects=False（见 net.py，出于防止 base_url 跳转泄露 Authorization 的
    安全考量），所以未登录会拿到 302 而不是登录页 HTML。这里把 302/401/403 统一
    归为「登录已失效」，提示用户重新复制 Cookie。
    """
    if isinstance(e, ResponseError):
        if e.status in (301, 302, 303, 307, 308, 401, 403):
            return fail(
                "expired",
                "OpenCode 登录已失效或未登录（Cookie 过期），请登录 opencode.ai 后重新复制 Cookie",
                **base,
            )
        return fail("error", f"接口返回错误 (HTTP {e.status}): {e.body[:200]}", **base)
    # 其余网络异常（DNS 解析失败 / TLS 握手被中断 / 连接超时等）统一翻译成中文
    from ..net import friendly_error

    return fail("error", friendly_error(e), **base)


def parse_opencode_usage(html: str, fetched_at_ms: int) -> list:
    """从 SSR 页面 HTML 中正则提取三个周期的额度窗口。

    fetched_at_ms：本次抓取的时间戳（epoch 毫秒）。页面里只有 resetInSec（距离
    重置还剩多少秒），没有绝对重置时刻——参考扩展的做法，用「抓取时刻 +
    resetInSec」换算出绝对重置时间戳 reset_at，前端再据此逐秒递减倒计时。

    返回 QuotaWindow 列表；页面里没匹配到任何周期时返回空列表（调用方据此报错）。
    """
    windows = []
    seen_kinds: set[str] = set()
    for m in _USAGE_RE.finditer(html):
        kind, reset_in_sec, used_percent = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        # 同一个周期可能因页面结构出现多次（如 rollingUsage 在不同区块各嵌一次），
        # 只取第一次匹配，避免重复窗口。
        if kind in seen_kinds:
            continue
        seen_kinds.add(kind)
        key, label = _PERIOD_MAP.get(kind, (None, None))
        if key is None:
            continue
        used_pct = max(0.0, min(100.0, float(used_percent)))
        reset_at = fetched_at_ms + reset_in_sec * 1000
        windows.append(
            window(
                key,
                label,
                used_percent=used_pct,
                remaining_percent=max(0.0, 100 - used_pct),
                reset_at=reset_at,
            )
        )
    return windows


async def query_opencode(channel: Channel) -> ChannelResult:
    base = {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "category": "subscription",
    }
    cookie = (channel.extra or {}).get("cookie") or channel.api_key
    if (
        err := _require(
            cookie,
            "Cookie",
            base,
            message="未配置 Cookie（请登录 opencode.ai 后，从浏览器复制完整 Cookie 填入）",
        )
    ) is not None:
        return err
    cookie_header = _normalize_cookie(cookie)

    # workspace_id 可选：用户没填时自动用 Cookie 从 /zen 页面探测（wrk_xxx）。
    # 这样用户只需复制一个 Cookie，不用自己去地址栏找 workspace ID。
    workspace_id = channel.workspace_id or (channel.extra or {}).get("workspace_id")
    if workspace_id:
        # 用户填了但可能不是标准 wrk_ 前缀（误填整条 URL 等），尝试从中抠出 wrk_ 部分
        if not workspace_id.startswith("wrk_"):
            m = re.search(r"(wrk_[A-Za-z0-9]+)", workspace_id)
            workspace_id = m.group(1) if m else workspace_id
    else:
        try:
            workspace_id = await _discover_workspace_id(cookie_header)
        except ResponseError as e:
            # /zen 探测时发现登录态失效（302/401/403）——按 expired 报，与主查询
            # 页面的 _error_result 语义一致
            return _error_result(base, e)
        if not workspace_id:
            return fail(
                "error",
                "未能自动找到工作区 ID（wrk_xxx）。请登录 opencode.ai 后从地址栏复制工作区 ID 手动填入",
                **base,
            )

    url = f"{OPENCODE_BASE}/workspace/{workspace_id}/go"
    headers = dict(_BROWSER_HEADERS)
    headers["Referer"] = f"{OPENCODE_BASE}/zen"
    headers["Cookie"] = cookie_header

    fetched_at_ms = int(time.time() * 1000)
    try:
        html = await request_text("GET", url, headers=headers)
    except Exception as e:
        return _error_result(base, e)

    # 登录态校验：未登录时虽然会 302，但稳妥起见也检查 HTML 是否包含登录入口文案
    # （某些情况下服务端可能返回 200 的登录引导页而非 302）。
    if "Continue with GitHub" in html or "Continue with Google" in html or "/auth/authorize" in html[:2000]:
        return fail(
            "expired",
            "OpenCode 登录已失效或未登录（Cookie 过期），请登录 opencode.ai 后重新复制 Cookie",
            **base,
        )

    windows = parse_opencode_usage(html, fetched_at_ms)
    if not windows:
        return fail(
            "error",
            "OpenCode 页面未解析到额度数据（页面结构可能已变更，或该工作区无 Go 订阅）",
            **base,
        )
    return ok(plan_name="OpenCode (Zen/Go) 订阅", windows=windows, **base)
