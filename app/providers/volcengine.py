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

import asyncio
import dataclasses
import hashlib
import hmac as hmac_mod
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ..config import Channel
from ..credentials import CRED_NOT_FOUND, CRED_OK, Credential
from ..models import ChannelResult, fail, finite_float, ok, to_ts, window
from ..net import ParseError, friendly_error, request_text

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
    if not isinstance(body, dict):
        return None
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
        # 未配置 AK/SK：回退到官方 arkcli CLI 的 SSO 登录态（参考 CodexBar 的
        # Doubao provider——Auto 模式同样"配了 API 凭据优先，没配走 arkcli"）。
        # 同步 subprocess 包 to_thread，避免阻塞事件循环。
        return await asyncio.to_thread(_query_via_arkcli, base)
    region = channel.region or DEFAULT_REGION
    soft_errors: list[str] = []
    agent_windows: list = []
    coding_windows: list = []
    agent_plan_type: str | None = None

    # 1) Agent Plan
    try:
        body = await _openapi_call(region, channel.ak, channel.sk, "GetAFPUsage")
        if not isinstance(body, dict):
            soft_errors.append("Agent Plan: 响应不是对象结构")
            body = {}
        err = _error_of(body)
        if err:
            if _is_signature_error(err[0]):
                return fail("error", f"火山签名计算错误: {err[0]} {err[1]}", **base)
            if _is_auth_error(err[0]):
                return fail("expired", f"火山鉴权失败: {err[0]} {err[1]}", **base)
            soft_errors.append(f"Agent Plan: {err[0]} {err[1]}")
        else:
            result = body.get("Result") or body
            if isinstance(result, dict):
                agent_windows = _parse_afp_tiers(result)
                agent_plan_type = result.get("PlanType")
            else:
                soft_errors.append("Agent Plan: 响应中的 Result 格式错误")
    except Exception as e:
        # 必须捕获所有异常（不只是 ResponseError/ParseError）：_openapi_call →
        # request_text → client.stream 在 DNS 失败 / TLS 握手 / 连接超时 / 代理失败时
        # 抛的是 httpx.ConnectError/ReadTimeout 等，它们不是 ResponseError 的子类，
        # 窄 except 会让异常逃逸出本函数，最终被 query_channel 顶层兜底成整张卡 error，
        # 导致另一个正常的 plan 数据也一起丢失（双 plan 隔离失效）。friendly_error 把
        # 底层网络异常翻译成中文提示；ResponseError/ParseError 则原样保留可读文案。
        soft_errors.append(f"Agent Plan: {friendly_error(e)}")

    # 2) Coding Plan
    try:
        body = await _openapi_call(region, channel.ak, channel.sk, "GetCodingPlanUsage")
        if not isinstance(body, dict):
            soft_errors.append("Coding Plan: 响应不是对象结构")
            body = {}
        err = _error_of(body)
        if err:
            if _is_signature_error(err[0]):
                return fail("error", f"火山签名计算错误: {err[0]} {err[1]}", **base)
            if _is_auth_error(err[0]):
                return fail("expired", f"火山鉴权失败: {err[0]} {err[1]}", **base)
            soft_errors.append(f"Coding Plan: {err[0]} {err[1]}")
        else:
            result = body.get("Result") or body
            if isinstance(result, dict):
                coding_windows = _parse_coding_plan_tiers(result)
            else:
                soft_errors.append("Coding Plan: 响应中的 Result 格式错误")
    except Exception as e:
        soft_errors.append(f"Coding Plan: {friendly_error(e)}")

    windows, plan_name, plan_names = _merge_plans(agent_windows, coding_windows, agent_plan_type)
    if not windows:
        if soft_errors:
            return fail("error", "；".join(soft_errors), **base)
        return fail("error", "未检测到火山 Agent Plan 或 Coding Plan 订阅", **base)

    return ok(
        plan_name=plan_name,
        windows=windows,
        message="；".join(soft_errors) or None,
        extra=plan_names,
        **base,
    )


def _merge_plans(
    agent_windows: list, coding_windows: list, agent_plan_type: str | None
) -> tuple[list, str, dict[str, str]]:
    """合并 Agent Plan 与 Coding Plan 的窗口，并带出每个套餐各自的真实名称。

    两个 plan 的窗口 key 相同（five_hour/weekly/monthly）、label 也相同（如
    「每 5 小时」），合并后必须加来源前缀区分——key 加 plan 维度
    （agent_*/coding_*）让趋势图按 key 分线、前端按 key 分组渲染，label 加
    「Agent 」/「Coding 」前缀让展示可读。

    返回 (windows, plan_name, plan_names)：
    - plan_name：两个套餐名用 " · " 拼接的完整串（如 "火山 Agent Plan small ·
      火山 Coding Plan"），渠道没有被拆卡展示时直接当整体 plan_name 用；
    - plan_names：{"agent_plan_name": ..., "coding_plan_name": ...}（只在对应
      套餐确实查到数据时才有这个 key）。app/main.py 把火山渠道拆成 Agent/Coding
      两张独立卡片时，需要每张卡各自的真实套餐名——尤其 Agent Plan 的名字里带
      PlanType 档位（如 "火山 Agent Plan small"），不能像最初实现那样把两张卡的
      plan_name 硬编码成通用的 "Agent Plan"/"Coding Plan" 丢掉档位信息，也不能
      把这里拼接出的完整串塞给两张卡（那样两张卡会显示同一串、还各自带着对方
      套餐的名字，串味）。让 provider 层把结构化的单套餐名称通过这个返回值带
      出去，main.py 直接取用对应的 key，不用再从拼接串里反着解析——那样解析
      本身就是脆弱的（万一某个套餐名字里也出现" · "就解析错位了）。
    """
    windows = [dataclasses.replace(w, key=f"agent_{w.key}", label=f"Agent {w.label}") for w in agent_windows]
    windows += [dataclasses.replace(w, key=f"coding_{w.key}", label=f"Coding {w.label}") for w in coding_windows]
    plan_names: dict[str, str] = {}
    names = []
    if agent_windows:
        agent_name = f"火山 Agent Plan {agent_plan_type}" if agent_plan_type else "火山 Agent Plan"
        plan_names["agent_plan_name"] = agent_name
        names.append(agent_name)
    if coding_windows:
        coding_name = "火山 Coding Plan"
        plan_names["coding_plan_name"] = coding_name
        names.append(coding_name)
    return windows, " · ".join(names), plan_names


def _parse_afp_tiers(result: dict) -> list:
    """Agent Plan：Result.AFPFiveHour/AFPWeekly/AFPMonthly，字段 Quota/Used/ResetTime。"""
    if not isinstance(result, dict):
        return []
    windows = []
    for key, tier, label in [
        ("AFPFiveHour", "five_hour", "每 5 小时"),
        ("AFPWeekly", "weekly", "每周额度"),
        ("AFPMonthly", "monthly", "每月额度"),
    ]:
        item = result.get(key)
        if not isinstance(item, dict):
            continue
        quota = finite_float(item.get("Quota"), 0.0) or 0.0
        if quota <= 0:
            continue
        used = finite_float(item.get("Used"), 0.0) or 0.0
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
    if not isinstance(result, dict):
        return []
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
            # 火山 API 字段命名在不同版本间不稳定（本函数已为 QuotaUsage/Usages/
            # Details 三种字段名做兼容），未来若新增 daily 等档位，直接 continue 会
            # 静默丢弃，用户在 UI 上看不到这一项、也无任何提示。保留为 custom，
            # 用上游返回的原始 label 展示，至少让数据可见、可排查。
            tier, tier_label = "custom", label or "其他档位"
        # 已用百分比候选字段（火山不同版本字段名不统一）。用 is not None 而非 or 短路：
        # "完全没用过配额"时 Percent == 0 是合法值，`0 or UsedPercent` 会跳过 0 误取
        # 下一个 key（与 coding_plans.py 里 MiniMax 已修的同源 bug）。
        raw = next(
            (item.get(k) for k in ("Percent", "UsedPercent", "UsagePercent") if item.get(k) is not None),
            None,
        )
        used = finite_float(raw, 0.0) or 0.0
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


# ── arkcli CLI 回退（参考 CodexBar DoubaoUsageFetcher）─────────
#
# 官方 arkcli CLI 用 SSO 登录（arkcli auth login），不需要在 IAM 里创建 AK/SK。
# `arkcli usage plan --format json` 输出个人版/团队版 × Coding/Agent 四组产品，
# 每组 periods[] 含 label（5-hour/weekly/monthly 等）、percent（已用百分比）、
# reset_at（ISO 字符串或 epoch 秒/毫秒）。协议细节对照 CodExBar：
# - 二进制定位：ARKCLI_PATH 环境变量 → PATH → ~/.local/bin / /opt/homebrew/bin /
#   /usr/local/bin 三个常见安装位
# - 未登录的两种表现：viewer.auth_method == "none"，或非零退出 + stderr 含
#   "not logged in" 等关键词
# - 输出上限 256KB / 超时 15s

_ARKCLI_TIMEOUT = 15
_ARKCLI_MAX_OUTPUT = 256 * 1024

# CodExBar isArkcliAuthenticationError 的同款关键词（小写匹配）
_ARKCLI_AUTH_MARKERS = (
    "not logged in",
    "not authenticated",
    "authentication required",
    "login required",
    "please login",
    "please log in",
)

# product → (窗口 key 前缀, 展示 label 前缀)。key 必须保持 agent_/coding_ 开头，
# main.py 的火山拆卡逻辑（_split_multi_plan）按这两个前缀分桶。
_ARKCLI_PRODUCTS: dict[str, tuple[str, str]] = {
    "agent-plan": ("agent", "Agent "),
    "coding-plan": ("coding", "Coding "),
    "agent-plan-team": ("agent", "Agent "),
    "coding-plan-team": ("coding", "Coding "),
}


class ArkcliError(Exception):
    """arkcli 调用失败。status 取 ChannelResult 语义：not_found / expired / error。"""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _resolve_arkcli_path() -> str | None:
    """定位 arkcli：ARKCLI_PATH 覆盖 → PATH → 常见安装位（与 CodExBar 一致）。"""
    override = os.environ.get("ARKCLI_PATH")
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override
    from shutil import which

    found = which("arkcli")
    if found:
        return found
    for p in (
        Path.home() / ".local" / "bin" / "arkcli",
        Path("/opt/homebrew/bin/arkcli"),
        Path("/usr/local/bin/arkcli"),
    ):
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def run_arkcli_usage_plan() -> dict:
    """执行 `arkcli usage plan --format json` 并解析 stdout 为 dict。

    失败抛 ArkcliError（status 已按 ChannelResult 语义归类）。同步阻塞函数，
    调用方（async 上下文）需用 asyncio.to_thread 包裹。
    """
    path = _resolve_arkcli_path()
    if not path:
        raise ArkcliError(
            "not_found",
            "未找到 arkcli CLI。请安装 arkcli 并运行 `arkcli auth login`，"
            "或在渠道里配置火山 AK/SK",
        )
    try:
        result = subprocess.run(  # noqa: PLW1510 手动检查 returncode
            [path, "usage", "plan", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=_ARKCLI_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise ArkcliError("error", "arkcli usage plan 执行超时（15s），请检查 arkcli 登录状态") from None
    except OSError as e:
        raise ArkcliError("error", f"arkcli 启动失败: {e}") from e
    if len(result.stdout.encode("utf-8", errors="replace")) > _ARKCLI_MAX_OUTPUT:
        raise ArkcliError("error", "arkcli 输出过大（超过 256KB），请升级 arkcli 后重试")
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        if any(marker in message.lower() for marker in _ARKCLI_AUTH_MARKERS):
            raise ArkcliError(
                "expired", "arkcli 未登录，请运行 `arkcli auth login` 后重试（或在渠道里配置 AK/SK）"
            )
        raise ArkcliError("error", f"arkcli 执行失败 (exit {result.returncode}): {message[:200]}")
    try:
        parsed = json.loads(result.stdout)
    except ValueError as e:
        raise ArkcliError("error", f"arkcli 输出不是合法 JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ArkcliError("error", "arkcli 输出结构异常（顶层不是对象）")
    return parsed


def parse_arkcli_usage(data: dict) -> list:
    """解析 arkcli usage plan JSON → 窗口列表。

    percent 是「已用百分比」（CodExBar rateWindow 按 usedPercent 消费，与
    AK/SK 路径的 GetCodingPlanUsage 百分比语义一致）。团队版窗口 key 加
    _team_ 段（agent_team_five_hour），仍以 agent_/coding_ 开头保证 main.py
    的火山拆卡逻辑正常分桶；label 加「团队 ·」前缀区分。
    """
    viewer = data.get("viewer")
    auth_method = str(viewer.get("auth_method") or "").strip().lower() if isinstance(viewer, dict) else ""
    if auth_method == "none":
        raise ArkcliError("expired", "arkcli 未登录（auth_method=none），请运行 `arkcli auth login`")
    items = data.get("items")
    if not isinstance(items, list):
        raise ArkcliError("error", "arkcli 输出缺少 items 数组")

    windows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        product = str(item.get("product") or "").strip().lower()
        group = _ARKCLI_PRODUCTS.get(product)
        if group is None or item.get("subscribed") is False:
            continue
        key_prefix, label_prefix = group
        is_team = product.endswith("-team")
        for period in item.get("periods") or []:
            if not isinstance(period, dict):
                continue
            label = str(period.get("label") or "").lower()
            if any(k in label for k in ("session", "5-hour", "five_hour", "5h", "5 hour")):
                tier, tier_label = "five_hour", "每 5 小时"
            elif "week" in label:
                tier, tier_label = "weekly", "每周额度"
            elif "month" in label:
                tier, tier_label = "monthly", "每月额度"
            elif "day" in label:
                # arkcli 会输出 AFPDaily（每日窗口）；CodExBar 因 UI 没有槽位而跳过，
                # 我们的 custom 档可以直接显示
                tier, tier_label = "custom", "每日额度"
            else:
                tier, tier_label = "custom", str(period.get("label") or "其他档位")
            used = finite_float(period.get("percent"), None)
            if used is None:
                continue
            used = max(0.0, min(100.0, used))
            key = f"{key_prefix}_{'team_' if is_team else ''}{tier}"
            label = ("团队 · " if is_team else "") + label_prefix + tier_label
            windows.append(
                window(
                    key,
                    label,
                    used_percent=used,
                    remaining_percent=max(0.0, 100 - used),
                    reset_at=to_ts(period.get("reset_at")),
                )
            )
    if not windows:
        # 没有任何已订阅产品的窗口：带出 arkcli 报的具体 error（如有）
        first_error = next(
            (
                str(item.get("error")).strip()
                for item in items
                if isinstance(item, dict)
                and str(item.get("product") or "").lower() in _ARKCLI_PRODUCTS
                and str(item.get("error") or "").strip()
            ),
            None,
        )
        raise ArkcliError("error", first_error or "arkcli 未返回任何已订阅的火山 Plan 用量")
    return windows


def _query_via_arkcli(base: dict) -> ChannelResult:
    """无 AK/SK 时的查询路径：arkcli → 窗口 → 与 AK/SK 路径同构的结果。"""
    try:
        windows = parse_arkcli_usage(run_arkcli_usage_plan())
    except ArkcliError as e:
        return fail(e.status, e.message, **base)
    plan_names: dict[str, str] = {}
    if any(w.key.startswith("agent") for w in windows):
        plan_names["agent_plan_name"] = "火山 Agent Plan" + (
            "（含团队版）" if any(w.key.startswith("agent_team_") for w in windows) else ""
        )
    if any(w.key.startswith("coding") for w in windows):
        plan_names["coding_plan_name"] = "火山 Coding Plan" + (
            "（含团队版）" if any(w.key.startswith("coding_team_") for w in windows) else ""
        )
    name = " · ".join(v for v in plan_names.values()) or "火山方舟 Plan"
    return ok(plan_name=name, windows=windows, source="arkcli usage plan", extra=plan_names, **base)


def read_arkcli_credentials() -> Credential:
    """启动自动探测用：arkcli 已安装、已登录且至少有一个订阅产品 → ok。

    供 main.py 的 _SUBSCRIPTION_DETECTORS 使用——arkcli 登录过的机器开箱
    自动建火山渠道（与 Claude/Cursor 的 CLI 登录态探测同模式）。
    """
    try:
        parse_arkcli_usage(run_arkcli_usage_plan())
    except ArkcliError as e:
        status = {"not_found": CRED_NOT_FOUND, "expired": "expired"}.get(e.status, "error")
        return Credential("", status, "arkcli", e.message)
    return Credential("arkcli", CRED_OK, "arkcli usage plan")
