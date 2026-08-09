"""providers 包的纯函数单测：智谱/火山的响应解析、Copilot quota_snapshots 解析、
Gemini 的 camelCase/snake_case 兼容 helper。不发起任何真实网络请求——全部用
写死的 fixture JSON 喂给解析函数。
"""

from __future__ import annotations

import asyncio

from app.config import Channel
from app.models import window as make_window
from app.providers import coding_plans, volcengine
from app.providers._common import _require
from app.providers.subscriptions import (
    _classify_gemini_model,
    _merge_copilot_usage,
    _parse_copilot_quota_snapshots,
    _pick,
)

# ── 智谱 _parse_zhipu_data ──────────────────────────────────────


def _base():
    return {
        "id": "ch_1",
        "type": "zhipu_coding",
        "name": "GLM",
        "category": "coding_plan",
    }


def test_parse_zhipu_data_success_with_five_hour_and_weekly():
    data = {
        "success": True,
        "code": 200,
        "data": {
            "limits": [
                {
                    "type": "TOKENS_LIMIT",
                    "unit": 3,
                    "percentage": 12.5,
                    "nextResetTime": "2026-08-03T18:00:00Z",
                },
                {
                    "type": "TOKENS_LIMIT",
                    "unit": 6,
                    "percentage": 40.0,
                    "nextResetTime": "2026-08-10T00:00:00Z",
                },
                # TIME_LIMIT（MCP 每月调用次数）已不再展示，这里故意带上确保它被忽略
                {
                    "type": "TIME_LIMIT",
                    "remaining": 80,
                    "usage": 100,
                    "currentValue": 20,
                },
            ]
        },
    }
    result = coding_plans._parse_zhipu_data(data, "GLM Coding Plan", _base())
    assert result.status == "ok"
    assert result.plan_name == "GLM Coding Plan"
    keys = {w.key for w in result.windows}
    assert {"five_hour", "weekly"} <= keys
    assert "custom" not in keys  # MCP 每月不再产生 custom 窗口
    five_hour = next(w for w in result.windows if w.key == "five_hour")
    assert five_hour.used_percent == 12.5
    assert five_hour.remaining_percent == 87.5


def test_parse_zhipu_data_business_failure():
    data = {"success": False, "code": 401, "msg": "无效的 API Key"}
    result = coding_plans._parse_zhipu_data(data, "GLM Coding Plan", _base())
    assert result.status == "error"
    assert "无效的 API Key" in result.message


def test_parse_zhipu_data_not_a_dict():
    result = coding_plans._parse_zhipu_data(["not", "a", "dict"], "GLM Coding Plan", _base())
    assert result.status == "error"


def test_parse_zhipu_data_no_limits_is_error():
    data = {"success": True, "code": 200, "data": {"limits": []}}
    result = coding_plans._parse_zhipu_data(data, "GLM Coding Plan", _base())
    assert result.status == "error"


# ── 火山 _parse_afp_tiers / _parse_coding_plan_tiers ────────────


def test_parse_afp_tiers_basic():
    result = {
        "AFPFiveHour": {"Quota": 100, "Used": 25, "ResetTime": "2026-08-03T18:00:00Z"},
        "AFPWeekly": {"Quota": 1000, "Used": 300},
        "PlanType": "Pro",
    }
    windows = volcengine._parse_afp_tiers(result)
    keys = {w.key for w in windows}
    assert keys == {"five_hour", "weekly"}
    five_hour = next(w for w in windows if w.key == "five_hour")
    assert five_hour.used_percent == 25.0
    assert five_hour.remaining_percent == 75.0
    # 火山 Agent Plan 的配额单位是 APF（Agent Plan tokens），不是"次"
    assert five_hour.used_label == "25 APF"
    assert five_hour.max_label == "100 APF"


def test_parse_afp_tiers_skips_zero_quota():
    result = {"AFPFiveHour": {"Quota": 0, "Used": 0}}
    windows = volcengine._parse_afp_tiers(result)
    assert windows == []


def test_parse_coding_plan_tiers_basic():
    result = {
        "QuotaUsage": [
            {"Level": "session_5h", "Percent": 10.0},
            {"Type": "week", "UsagePercent": 55.0},
            {"Period": "monthly", "UsedPercent": 5.0},
            {"Level": "unrecognized-bucket", "Percent": 99.0},
        ]
    }
    windows = volcengine._parse_coding_plan_tiers(result)
    keys = {w.key for w in windows}
    # 三个标准档位照常解析；未识别的桶（如火山未来新增的 daily 档位）不再静默丢弃，
    # 而是保留为 custom 让数据可见、可排查（而不是 UI 上悄悄消失）。
    assert {"five_hour", "weekly", "monthly"} <= keys
    custom = [w for w in windows if w.key == "custom"]
    assert len(custom) == 1
    assert custom[0].used_percent == 99.0


def test_parse_coding_plan_tiers_zero_percent_not_treated_as_missing():
    # 回归：Percent == 0（完全没用过配额）是合法值。旧代码用 `or` 短路，
    # `0 or UsedPercent` 会跳过 0 误取下一个 key（与 MiniMax 已修的同源 bug）。
    result = {
        "QuotaUsage": [
            {"Level": "session_5h", "Percent": 0, "UsedPercent": 37.0},  # 0 必须被采用，不能取 37
        ]
    }
    windows = volcengine._parse_coding_plan_tiers(result)
    assert len(windows) == 1
    assert windows[0].key == "five_hour"
    assert windows[0].used_percent == 0.0      # 不能被篡改成 37.0
    assert windows[0].remaining_percent == 100.0


def test_parse_coding_plan_tiers_not_a_list_returns_empty():
    assert volcengine._parse_coding_plan_tiers({"QuotaUsage": "nope"}) == []


# ── 火山 query_volcengine：Agent Plan 与 Coding Plan 合并 ────────
#
# 回归背景：同一账号可能同时开 Agent Plan 和 Coding Plan，旧的实现"查到
# Agent 就提前返回"，Coding Plan 永远不显示。现在两个都查、合并窗口，并且
# 同名窗口（每 5 小时/每周/每月）加 Agent/Coding 前缀区分。


def _run_volcengine(monkeypatch, responses):
    """用写死的 Action→响应 映射跑一遍 query_volcengine，不发真实网络请求。"""
    import asyncio

    from app.config import Channel
    from app.providers import volcengine

    async def fake_openapi(region, ak, sk, action):
        return responses[action]

    monkeypatch.setattr(volcengine, "_openapi_call", fake_openapi)
    channel = Channel(id="ch_v", type="volcengine", name="火山", ak="ak-1", sk="sk-1")
    return asyncio.run(volcengine.query_volcengine(channel))


_AFP_RESP = {
    "Result": {
        "AFPFiveHour": {"Quota": 100, "Used": 25},
        "AFPWeekly": {"Quota": 1000, "Used": 300},
        "PlanType": "small",
    }
}
_CODING_RESP = {"Result": {"QuotaUsage": [{"Level": "week", "Percent": 55.0}]}}


def test_query_volcengine_merges_agent_and_coding(monkeypatch):
    result = _run_volcengine(
        monkeypatch,
        {"GetAFPUsage": _AFP_RESP, "GetCodingPlanUsage": _CODING_RESP},
    )
    assert result.status == "ok"
    labels = {w.label for w in result.windows}
    assert labels == {"Agent 每 5 小时", "Agent 每周额度", "Coding 每周额度"}
    # key 带 plan 维度：趋势图按 key 分线、前端按 key 分组渲染都靠它
    keys = {w.key for w in result.windows}
    assert keys == {"agent_five_hour", "agent_weekly", "coding_weekly"}
    assert "火山 Agent Plan small" in result.plan_name
    assert "火山 Coding Plan" in result.plan_name
    assert result.message is None
    # 任务 3 回归：main.py 拆卡时需要这两个结构化字段各自还原真实套餐名（含
    # PlanType 档位），不能只依赖拼接后的 plan_name 反过来解析。
    assert result.extra["agent_plan_name"] == "火山 Agent Plan small"
    assert result.extra["coding_plan_name"] == "火山 Coding Plan"


def test_query_volcengine_agent_only(monkeypatch):
    result = _run_volcengine(
        monkeypatch,
        {"GetAFPUsage": _AFP_RESP, "GetCodingPlanUsage": {"Result": {"QuotaUsage": []}}},
    )
    assert result.status == "ok"
    assert {w.label for w in result.windows} == {"Agent 每 5 小时", "Agent 每周额度"}
    assert result.plan_name == "火山 Agent Plan small"
    assert "Coding Plan" not in result.plan_name
    assert result.extra == {"agent_plan_name": "火山 Agent Plan small"}


def test_query_volcengine_coding_only(monkeypatch):
    result = _run_volcengine(
        monkeypatch,
        {"GetAFPUsage": {"Result": {}}, "GetCodingPlanUsage": _CODING_RESP},
    )
    assert result.status == "ok"
    assert {w.label for w in result.windows} == {"Coding 每周额度"}
    assert result.plan_name == "火山 Coding Plan"
    assert result.extra == {"coding_plan_name": "火山 Coding Plan"}


# ── 火山 _merge_plans：直接单测结构化的 plan_names 返回值 ───────


def test_merge_plans_returns_structured_plan_names_for_both():
    agent_windows = [make_window("five_hour", "每 5 小时", used_percent=10, remaining_percent=90)]
    coding_windows = [make_window("weekly", "每周额度", used_percent=20, remaining_percent=80)]
    windows, plan_name, plan_names = volcengine._merge_plans(agent_windows, coding_windows, "small")
    assert plan_name == "火山 Agent Plan small · 火山 Coding Plan"
    assert plan_names == {
        "agent_plan_name": "火山 Agent Plan small",
        "coding_plan_name": "火山 Coding Plan",
    }
    assert len(windows) == 2


def test_merge_plans_agent_only_omits_coding_key():
    agent_windows = [make_window("five_hour", "每 5 小时", used_percent=10, remaining_percent=90)]
    _windows, plan_name, plan_names = volcengine._merge_plans(agent_windows, [], None)
    assert plan_name == "火山 Agent Plan"  # 没有 PlanType 时不带档位后缀
    assert plan_names == {"agent_plan_name": "火山 Agent Plan"}
    assert "coding_plan_name" not in plan_names


def test_merge_plans_neither_plan_returns_empty_names():
    windows, plan_name, plan_names = volcengine._merge_plans([], [], None)
    assert windows == []
    assert plan_name == ""
    assert plan_names == {}


def test_query_volcengine_one_plan_error_keeps_other(monkeypatch):
    result = _run_volcengine(
        monkeypatch,
        {
            "GetAFPUsage": _AFP_RESP,
            # Coding Plan 业务错误（非签名/鉴权）：不拖垮 Agent Plan 的展示
            "GetCodingPlanUsage": {
                "ResponseMetadata": {"Error": {"Code": "NotSubscribed", "Message": "未订阅 Coding Plan"}}
            },
        },
    )
    assert result.status == "ok"
    assert any(w.label.startswith("Agent ") for w in result.windows)
    # soft_errors 格式 "Coding Plan: <Code> <Message>"——包含错误码，比只给 Action
    # 名更有助于排查。这里验证 Coding Plan 失败信息确实附在了 message 上。
    assert "Coding Plan" in result.message
    assert "NotSubscribed" in result.message


def test_query_volcengine_neither_plan(monkeypatch):
    result = _run_volcengine(
        monkeypatch,
        {"GetAFPUsage": {"Result": {}}, "GetCodingPlanUsage": {"Result": {"QuotaUsage": []}}},
    )
    assert result.status == "error"
    assert "未检测到" in result.message


def test_query_volcengine_signature_error_short_circuits(monkeypatch):
    """签名错误必须立即返回，不能继续查第二个 plan（AK/SK 有问题时都查不了）。"""
    calls = []

    import asyncio

    from app.config import Channel
    from app.providers import volcengine

    async def fake_openapi(region, ak, sk, action):
        calls.append(action)
        return {"ResponseMetadata": {"Error": {"Code": "SignatureDoesNotMatch", "Message": "sig mismatch"}}}

    monkeypatch.setattr(volcengine, "_openapi_call", fake_openapi)
    channel = Channel(id="ch_v", type="volcengine", name="火山", ak="ak-1", sk="sk-1")
    result = asyncio.run(volcengine.query_volcengine(channel))
    assert result.status == "error"
    assert "签名" in result.message
    assert calls == ["GetAFPUsage"]  # 只查了一次


# ── Copilot quota_snapshots 解析 ────────────────────────────────


def test_parse_copilot_quota_snapshots_percent_remaining():
    windows = []
    snapshots = {
        "chat": {
            "entitlement": 300,
            "remaining": 120,
            "percent_remaining": 40.0,
            "unlimited": False,
        },
        "premium_interactions": {
            "entitlement": 0,
            "remaining": 0,
            "percent_remaining": 0.0,
            "unlimited": False,
        },
    }
    _parse_copilot_quota_snapshots(snapshots, windows)
    assert len(windows) == 2
    chat = next(w for w in windows if w.label == "Chat 对话")
    assert chat.remaining_percent == 40.0
    assert chat.used_percent == 60.0
    assert chat.used_label == "180"
    assert chat.max_label == "300"


def test_parse_copilot_quota_snapshots_unlimited():
    windows = []
    _parse_copilot_quota_snapshots({"completions": {"unlimited": True}}, windows)
    assert len(windows) == 1
    assert windows[0].remaining_percent == 100.0
    assert windows[0].max_label == "无限量"


def test_parse_copilot_quota_snapshots_derives_percent_when_missing():
    windows = []
    snapshots = {"chat": {"entitlement": 100, "remaining": 25}}  # 没给 percent_remaining，需要自己算
    _parse_copilot_quota_snapshots(snapshots, windows)
    assert windows[0].remaining_percent == 25.0
    assert windows[0].used_percent == 75.0


def test_parse_copilot_quota_snapshots_unknown_key_uses_raw_name_as_label():
    windows = []
    _parse_copilot_quota_snapshots({"some_future_quota": {"percent_remaining": 50.0}}, windows)
    assert windows[0].label == "some_future_quota"


def test_merge_copilot_usage_legacy_endpoint():
    windows = []
    usage = {"chat": {"total_requests": 30, "limit": 100}}
    _merge_copilot_usage(usage, windows)
    assert len(windows) == 1
    assert windows[0].used_percent == 30.0


# ── Gemini：camelCase / snake_case 兼容 ─────────────────────────


def test_pick_prefers_first_present_key():
    assert _pick({"modelId": "a", "model_id": "b"}, "modelId", "model_id") == "a"


def test_pick_falls_back_to_snake_case_when_camel_missing():
    assert _pick({"model_id": "b"}, "modelId", "model_id") == "b"


def test_pick_returns_default_when_absent():
    assert _pick({}, "modelId", "model_id", default="unknown") == "unknown"


def test_pick_skips_none_values():
    assert _pick({"modelId": None, "model_id": "b"}, "modelId", "model_id") == "b"


def test_classify_gemini_model():
    assert _classify_gemini_model("models/gemini-2.5-pro") == "gemini_pro"
    assert _classify_gemini_model("gemini-2.5-flash") == "gemini_flash"
    assert _classify_gemini_model("gemini-2.5-flash-lite") == "gemini_flash_lite"
    assert _classify_gemini_model("something-else") == "other"


# ── MiMo query_mimo ────────────────────────────────────────────
#
# 回归测试：reset_at 是 QuotaWindow 的字段，ChannelResult 上没有。曾经
# query_mimo 直接把 reset_at 传给 ok()/ChannelResult()，导致**每一次成功查询
# 都抛 TypeError**，被 query_channel 的兜底吞成一张写着 Python 报错的错误卡，
# 也就是说 MiMo 渠道从来没能真正工作过。这里覆盖成功 / 过期两条返回路径。


def _run_mimo(monkeypatch, usage_resp, detail_resp):
    """用写死的上游响应跑一遍 query_mimo，不发起任何真实网络请求。"""
    import asyncio

    from app.config import Channel
    from app.providers import mimo

    async def fake_request_json(method, url, *, headers=None, json_body=None):
        return usage_resp if url.endswith("/tokenPlan/usage") else detail_resp

    monkeypatch.setattr(mimo, "request_json", fake_request_json)
    channel = Channel(id="ch_m", type="mimo", name="MiMo", api_key="session=abc")
    return asyncio.run(mimo.query_mimo(channel))


def test_query_mimo_success_does_not_pass_reset_at_to_channel_result(monkeypatch):
    result = _run_mimo(
        monkeypatch,
        {"code": 0, "data": {"monthUsage": {"percent": 42.0, "items": [{"name": "总量", "used": 420, "limit": 1000}]}}},
        {"code": 0, "data": {"planName": "MiMo Pro", "currentPeriodEnd": "2026-08-31T23:59:59+08:00"}},
    )
    assert result.status == "ok"
    assert result.plan_name == "MiMo Pro"
    assert [w.used_percent for w in result.windows] == [42.0, 42.0]
    # 套餐周期结束时间应该落到窗口的 reset_at 上（而不是 ChannelResult 上）
    assert all(w.reset_at == 1788191999000 for w in result.windows)


def test_query_mimo_expired_plan_returns_info_not_error(monkeypatch):
    result = _run_mimo(
        monkeypatch,
        {"code": 0, "data": {"monthUsage": {"percent": 100.0}}},
        {"code": 0, "data": {"planName": "MiMo Pro", "expired": True}},
    )
    # 套餐过期不是故障：应标 info，且不能被误报成"Cookie 无效或未订阅"
    assert result.status == "info"
    assert "已过期" in result.message


def test_query_mimo_missing_cookie_is_reported_clearly(monkeypatch):
    import asyncio

    from app.config import Channel
    from app.providers import mimo

    result = asyncio.run(mimo.query_mimo(Channel(id="ch_m", type="mimo", name="MiMo")))
    assert result.status == "error"
    assert "Cookie" in result.message


# ── MiniMax query_minimax ────────────────────────────────────
#
# 回归背景：真实上游响应里 general 条目的 current_weekly_total_count 是 0
# （按百分比计额的套餐不返回次数总额），旧代码用 `if item.get(...)` 的 truthy
# 判断，把 0 误当成"没有周额度"整条跳过，导致只显示 5 小时窗口。


def _run_minimax(monkeypatch, resp):
    import asyncio

    from app.config import Channel
    from app.providers import coding_plans

    async def fake_request_json(method, url, *, headers=None, json_body=None):
        return resp

    monkeypatch.setattr(coding_plans, "request_json", fake_request_json)
    channel = Channel(id="ch_mm", type="minimax", name="MM", api_key="sk-x")
    return asyncio.run(coding_plans.query_minimax(channel))


def test_query_minimax_zero_weekly_total_count_still_shows_weekly(monkeypatch):
    resp = {
        "base_resp": {"status_code": 0, "status_msg": "success"},
        "model_remains": [
            {
                "model_name": "general",
                "current_interval_remaining_percent": 99,
                "current_weekly_total_count": 0,  # 0 也必须显示周额度
                "current_weekly_remaining_percent": 77,
                "end_time": 1785844800000,
                "weekly_end_time": 1786291200000,
            }
        ],
    }
    result = _run_minimax(monkeypatch, resp)
    assert result.status == "ok"
    keys = {w.key for w in result.windows}
    assert keys == {"five_hour", "weekly"}
    weekly = next(w for w in result.windows if w.key == "weekly")
    assert weekly.remaining_percent == 77.0
    assert weekly.reset_at == 1786291200000


def test_query_minimax_missing_weekly_percent_skips_weekly(monkeypatch):
    resp = {
        "base_resp": {"status_code": 0},
        "model_remains": [
            {
                "model_name": "general",
                "current_interval_remaining_percent": 50,
                "current_weekly_total_count": 5,
                # 没有 current_weekly_remaining_percent：真的没有周额度
            }
        ],
    }
    result = _run_minimax(monkeypatch, resp)
    assert result.status == "ok"
    assert {w.key for w in result.windows} == {"five_hour"}


def test_query_minimax_exhausted_quota_shows_zero_not_full(monkeypatch):
    # 回归：remaining_percent == 0（配额用尽）是有效值。旧代码用 `or 100` 兜底，
    # `0 or 100` → 100，把"已耗尽"误报成"完全没用过"。
    resp = {
        "base_resp": {"status_code": 0},
        "model_remains": [
            {
                "model_name": "general",
                "current_interval_remaining_percent": 0,   # 5 小时窗口已耗尽
                "current_weekly_total_count": 10,
                "current_weekly_remaining_percent": 0,      # 周窗口已耗尽
                "end_time": 1785844800000,
                "weekly_end_time": 1786291200000,
            }
        ],
    }
    result = _run_minimax(monkeypatch, resp)
    assert result.status == "ok"
    five_hour = next(w for w in result.windows if w.key == "five_hour")
    assert five_hour.remaining_percent == 0.0   # 不能被篡改成 100.0
    assert five_hour.used_percent == 100.0
    weekly = next(w for w in result.windows if w.key == "weekly")
    assert weekly.remaining_percent == 0.0      # 不能被篡改成 100.0
    assert weekly.used_percent == 100.0


# ── query_channel 兜底：底层网络异常翻译成中文 ──────────────────
#
# 回归背景：DNS 解析失败时 httpx 抛的 "[Errno 8] nodename nor servname provided,
# or not known" 会一路冒到 query_channel 的 except Exception 兜底，被当错误文案
# 展示。这里验证兜底把原始 socket 错误翻译成「DNS」开头的中文提示。


def test_query_channel_translates_dns_error_to_chinese(monkeypatch):
    import asyncio
    import socket

    import httpx

    from app import providers
    from app.config import Channel

    async def boom(channel):
        err = socket.gaierror(8, "nodename nor servname provided, or not known")
        raise httpx.ConnectError(str(err)) from err

    monkeypatch.setitem(providers.REGISTRY, "deepseek", boom)
    result = asyncio.run(providers.query_channel(Channel(id="ch_x", type="deepseek", name="X", api_key="sk-x")))
    assert result.status == "error"
    assert "DNS" in result.message
    assert "Errno" not in result.message


# ── 订阅类 _fail_from_error：SSL 握手失败翻译成中文 ─────────────
#
# 回归背景：chatgpt.com 的 TLS 握手被网络环境中断时，httpx 抛 SSL 相关异常，
# _fail_from_error 之前拼 "（网络或超时，可稍后重试）"，用户不知道真正原因。


def test_fail_from_error_translates_ssl_handshake_failure(monkeypatch):
    import ssl

    import httpx

    from app.providers import subscriptions

    err = ssl.SSLError(1, "UNEXPECTED_EOF_WHILE_READING")
    wrapped = httpx.ConnectError(str(err))
    wrapped.__cause__ = err
    result = subscriptions._fail_from_error(
        {"id": "ch_c", "type": "codex_subscription", "name": "C", "category": "subscription"}, wrapped, "Codex"
    )
    assert result.status == "error"
    assert "TLS" in result.message
    assert "代理" in result.message
    assert "网络或超时" not in result.message


# ── providers/_common._require：三个 provider 模块共用的必填字段校验 ────


def _base(**overrides):
    d = {"id": "ch_r", "type": "t", "name": "N", "category": "coding_plan"}
    d.update(overrides)
    return d


def test_require_returns_none_when_value_present():
    assert _require("sk-x", "API Key", _base()) is None


def test_require_returns_default_message_when_missing():
    result = _require(None, "API Key", _base())
    assert result.status == "error"
    assert result.message == "未配置 API Key"
    assert result.id == "ch_r"  # base 里的字段被透传


def test_require_treats_empty_string_as_missing():
    assert _require("", "API Key", _base()) is not None


def test_require_custom_message_overrides_default_template():
    result = _require(None, "Cookie", _base(), message="自定义提示文案")
    assert result.message == "自定义提示文案"


# ── 任务 4 回归：coding_plans.py 缺必填字段返回友好 error 而不是抛异常 ────
#
# 回归背景：query_zenmux 直接把 channel.base_url 传给 request_json 当 URL，
# 为 None 时会把 httpx 的内部异常当错误文案抛给用户；query_kimi_coding /
# query_minimax / query_zhipu / query_zhipu_team 都直接用 channel.api_key 拼
# Authorization 头，为空时会发出裸的 "Bearer None"。这里逐个覆盖缺字段场景，
# 确认现在都能返回带中文标签的友好 error。


def test_query_zenmux_missing_base_url_is_reported_clearly():
    channel = Channel(id="ch_z", type="zenmux", name="ZenMux", api_key="sk-x")  # 没填 base_url
    result = asyncio.run(coding_plans.query_zenmux(channel))
    assert result.status == "error"
    assert "Base URL" in result.message


def test_query_zenmux_missing_api_key_is_reported_clearly():
    channel = Channel(id="ch_z", type="zenmux", name="ZenMux", base_url="https://zenmux.example/api/usage")
    result = asyncio.run(coding_plans.query_zenmux(channel))
    assert result.status == "error"
    assert "API Key" in result.message


def test_query_kimi_coding_missing_api_key_is_reported_clearly():
    result = asyncio.run(coding_plans.query_kimi_coding(Channel(id="ch_k", type="kimi_coding", name="Kimi")))
    assert result.status == "error"
    assert "API Key" in result.message


def test_query_minimax_missing_api_key_is_reported_clearly():
    result = asyncio.run(coding_plans.query_minimax(Channel(id="ch_mm", type="minimax", name="MM")))
    assert result.status == "error"
    assert "API Key" in result.message


def test_query_zhipu_missing_api_key_is_reported_clearly():
    result = asyncio.run(coding_plans.query_zhipu(Channel(id="ch_zh", type="zhipu_coding", name="GLM")))
    assert result.status == "error"
    assert "API Key" in result.message


def test_query_zhipu_team_missing_api_key_is_reported_clearly():
    result = asyncio.run(coding_plans.query_zhipu_team(Channel(id="ch_zt", type="zhipu_team", name="GLM 团队")))
    assert result.status == "error"
    assert "API Key" in result.message


# ── OpenCode query_opencode ────────────────────────────────────
#
# OpenCode 官网是 SSR 页面（没有 JSON API），额度数据以内嵌的 RSC 序列化字符串
# 写在 HTML 里。测试覆盖：SSR HTML 成功解析三周期 / 未登录（302 + 登录页文案）/
# 缺 Cookie / 缺工作区 ID / 页面结构变更（解析不到数据）五条路径。所有测试均
# monkeypatch 模块级 request_text，不发起真实网络请求。

# 模拟 opencode.ai /go 页面里内嵌的真实 RSC 数据结构（节选关键片段）
_FAKE_OC_HTML = """
<!DOCTYPE html><html><head><title>opencode</title></head><body>
<script>self.$R=self.$R||[];</script>
<script>_$HY.r["rollingUsage[\"wrk_test\"]"]=$R[10]=r=>(r?"rollingUsage":null)</script>
rollingUsage:$R[123]={status:"active",resetInSec:86400,usagePercent:42}
weeklyUsage:$R[124]={status:"active",resetInSec:432000,usagePercent:67}
monthlyUsage:$R[125]={status:"active",resetInSec:2592000,usagePercent:35}
<script>_$HY.fe()</script>
</body></html>
"""


def _run_opencode(monkeypatch, html_response=None, exc=None):
    """用写死的上游 HTML 跑一遍 query_opencode，不发起任何真实网络请求。"""
    import asyncio

    from app.config import Channel
    from app.providers import opencode

    if exc is not None:
        async def fake_request_text(method, url, *, headers=None, json_body=None):
            raise exc
    else:
        async def fake_request_text(method, url, *, headers=None, json_body=None):
            return html_response

    monkeypatch.setattr(opencode, "request_text", fake_request_text)
    channel = Channel(
        id="ch_oc",
        type="opencode_subscription",
        name="OpenCode",
        api_key="auth=abc; session=xyz",
        workspace_id="wrk_test",
    )
    return asyncio.run(opencode.query_opencode(channel))


def test_query_opencode_parses_three_periods_from_ssr_html(monkeypatch):
    result = _run_opencode(monkeypatch, html_response=_FAKE_OC_HTML)
    assert result.status == "ok"
    assert result.plan_name == "OpenCode (Zen/Go) 订阅"
    by_label = {w.label: w for w in result.windows}
    # rolling 映射成 five_hour（resetInSec=18000=5h，就是 5 小时额度）
    assert set(by_label) == {"每 5 小时", "每周额度", "每月额度"}
    assert by_label["每 5 小时"].used_percent == 42.0
    assert by_label["每周额度"].used_percent == 67.0
    assert by_label["每月额度"].used_percent == 35.0
    # remaining_percent 应为 100 - used
    assert by_label["每 5 小时"].remaining_percent == 58.0
    # rolling 的 key 必须是 five_hour（而非 custom），否则前端 5h 槽位显示"未提供"
    by_key = {w.key: w for w in result.windows}
    assert "five_hour" in by_key
    assert by_key["five_hour"].used_percent == 42.0
    # reset_at 是「抓取时刻 + resetInSec」，这里只验证 resetInSec 的大小关系正确
    assert by_label["每 5 小时"].reset_at > by_label["每周额度"].reset_at - 432000 * 1000


def test_query_opencode_redirect_means_not_logged_in(monkeypatch):
    # 未登录访问 /go 会被 302 重定向到 /auth/authorize（实测确认）
    from app.net import ResponseError

    result = _run_opencode(monkeypatch, exc=ResponseError(302, ""))
    assert result.status == "expired"
    assert "Cookie" in result.message


def test_query_opencode_login_page_html_is_expired(monkeypatch):
    # 退化路径：服务端返回 200 但内容是登录引导页
    result = _run_opencode(
        monkeypatch,
        html_response="<html><body>Continue with GitHub Continue with Google</body></html>",
    )
    assert result.status == "expired"
    assert "Cookie" in result.message


def test_query_opencode_missing_cookie_is_reported(monkeypatch):
    import asyncio

    from app.config import Channel
    from app.providers import opencode

    result = asyncio.run(
        opencode.query_opencode(
            Channel(id="ch_oc", type="opencode_subscription", name="OC", workspace_id="wrk_test")
        )
    )
    assert result.status == "error"
    assert "Cookie" in result.message


def test_query_opencode_auto_discovers_workspace_id(monkeypatch):
    """workspace_id 未填时，自动从 /zen 页面探测 wrk_xxx，再用它查额度。"""
    import asyncio

    from app.config import Channel
    from app.providers import opencode

    async def fake_request_text(method, url, *, headers=None, json_body=None):
        if "/zen" in url:
            return '<html>workspace: wrk_AUTO_FOUND_123</html>'
        return _FAKE_OC_HTML  # /go 页面

    monkeypatch.setattr(opencode, "request_text", fake_request_text)
    # 不填 workspace_id，只填 Cookie
    channel = Channel(id="ch_oc", type="opencode_subscription", name="OC", api_key="Fe26.2**abc")
    result = asyncio.run(opencode.query_opencode(channel))
    assert result.status == "ok"
    assert result.plan_name == "OpenCode (Zen/Go) 订阅"


def test_query_opencode_auto_discover_fails_reports_helpful_error(monkeypatch):
    """workspace_id 未填 + 自动探测也找不到 wrk_ 时，给出明确的操作指引。"""
    import asyncio

    from app.config import Channel
    from app.providers import opencode

    async def fake_request_text(method, url, *, headers=None, json_body=None):
        if "/zen" in url:
            return "<html>no workspace id here</html>"
        return _FAKE_OC_HTML

    monkeypatch.setattr(opencode, "request_text", fake_request_text)
    channel = Channel(id="ch_oc", type="opencode_subscription", name="OC", api_key="Fe26.2**abc")
    result = asyncio.run(opencode.query_opencode(channel))
    assert result.status == "error"
    assert "工作区 ID" in result.message


def test_query_opencode_no_usage_data_in_html_is_error(monkeypatch):
    # 页面结构变更：HTML 里没有 *Usage:$R 序列化字符串
    result = _run_opencode(monkeypatch, html_response="<html><body>hello no quota here</body></html>")
    assert result.status == "error"
    assert "未解析到额度数据" in result.message


def test_parse_opencode_usage_extracts_absolute_reset_at():
    """解析函数用「抓取时刻 + resetInSec」换算绝对重置时间，而非直接存秒数。"""
    from app.providers.opencode import parse_opencode_usage

    fetched_at = 1_700_000_000_000  # 固定抓取时刻，避免依赖真实时钟
    windows = parse_opencode_usage(_FAKE_OC_HTML, fetched_at)
    rolling = next(w for w in windows if w.key == "five_hour")
    # rolling resetInSec=86400 → reset_at = fetched_at + 86400*1000
    assert rolling.reset_at == fetched_at + 86400 * 1000


def test_query_opencode_extracts_wrk_id_from_url(monkeypatch):
    """用户可能误填整条 URL，provider 应从中抠出 wrk_xxx 部分。"""
    import asyncio

    from app.config import Channel
    from app.providers import opencode

    captured = {}

    async def fake_request_text(method, url, *, headers=None, json_body=None):
        captured["url"] = url
        return _FAKE_OC_HTML

    monkeypatch.setattr(opencode, "request_text", fake_request_text)
    channel = Channel(
        id="ch_oc",
        type="opencode_subscription",
        name="OC",
        api_key="auth=abc",
        workspace_id="https://opencode.ai/workspace/wrk_abc123/go",
    )
    result = asyncio.run(opencode.query_opencode(channel))
    assert result.status == "ok"
    assert captured["url"] == "https://opencode.ai/workspace/wrk_abc123/go"


def test_query_opencode_bare_cookie_value_gets_auth_prefix(monkeypatch):
    """用户从浏览器复制的 auth cookie 值是裸串（Fe26.2**...），不含 `auth=` 前缀。
    provider 必须自动补上 `auth=`，否则服务端拿不到 session 返回 302 登录。
    回归背景：之前直接 headers["Cookie"] = cookie 把裸值发出去，每次都误报 expired。
    """
    import asyncio

    from app.config import Channel
    from app.providers import opencode

    captured = {}

    async def fake_request_text(method, url, *, headers=None, json_body=None):
        captured["cookie"] = (headers or {}).get("Cookie", "")
        return _FAKE_OC_HTML

    monkeypatch.setattr(opencode, "request_text", fake_request_text)
    # 裸 cookie 值（不带 auth= 前缀，模拟用户直接复制 cookie value）
    channel = Channel(
        id="ch_oc",
        type="opencode_subscription",
        name="OC",
        api_key="Fe26.2**abc*def*ghi",
        workspace_id="wrk_test",
    )
    result = asyncio.run(opencode.query_opencode(channel))
    assert result.status == "ok"
    # 裸值应被补成 auth=Fe26.2**abc*def*ghi
    assert captured["cookie"] == "auth=Fe26.2**abc*def*ghi"


def test_query_opencode_full_cookie_string_kept_as_is(monkeypatch):
    """用户若已填了完整的 `auth=xxx` 或多个 cookie，原样使用，不重复加前缀。"""
    import asyncio

    from app.config import Channel
    from app.providers import opencode

    captured = {}

    async def fake_request_text(method, url, *, headers=None, json_body=None):
        captured["cookie"] = (headers or {}).get("Cookie", "")
        return _FAKE_OC_HTML

    monkeypatch.setattr(opencode, "request_text", fake_request_text)
    channel = Channel(
        id="ch_oc",
        type="opencode_subscription",
        name="OC",
        api_key="auth=Fe26.2**abc; other=val",
        workspace_id="wrk_test",
    )
    result = asyncio.run(opencode.query_opencode(channel))
    assert result.status == "ok"
    assert captured["cookie"] == "auth=Fe26.2**abc; other=val"


# ── Claude utilization 语义：剩余比例，不是已用比例 ──────────────
#
# 回归背景：Claude 的 /api/oauth/usage 返回的 utilization 字段表示「剩余/可用
# 比例」（0=全用完, 1=全可用），不是「已用比例」。之前误当已用处理，导致
# 100%可用的窗口显示成 100%已用。交叉验证依据：utilization=1.0 时 limits 数组
# 对应条目的 severity=normal（充足），而不是 critical。


def _run_claude_usage(monkeypatch, api_response):
    """mock token + API 响应，跑 query_claude。"""
    from app.config import Channel
    from app.credentials import CRED_OK, Credential
    from app.providers import subscriptions

    monkeypatch.setattr(
        subscriptions, "read_claude_credentials",
        lambda: Credential("sk-test", CRED_OK, "test"),
    )

    async def fake_request_json(method, url, *, headers=None, json_body=None):
        return api_response

    monkeypatch.setattr(subscriptions, "request_json", fake_request_json)
    return asyncio.run(
        subscriptions.query_claude(Channel(id="ch_c", type="claude_subscription", name="Claude"))
    )


def test_claude_utilization_is_used(monkeypatch):
    """utilization 直接是「已用百分比」的数值（1.0 = 1%，不是 100%）。
    PTY 交叉验证：CLI 显示 "Current session 1% used" 对应 utilization=1.0；
    "Current week 0% used" 对应 utilization=0.0。"""
    resp = {
        "five_hour": {"utilization": 1.0, "resets_at": "2026-08-09T07:59:59+00:00"},
        "seven_day": {"utilization": 0.0, "resets_at": "2026-08-15T10:59:59+00:00"},
    }
    result = _run_claude_usage(monkeypatch, resp)
    assert result.status == "ok"
    by_label = {w.label: w for w in result.windows}
    # utilization=1.0 → 已用 1%（不是 100%！），剩余 99%
    assert by_label["每 5 小时"].used_percent == 1.0
    assert by_label["每 5 小时"].remaining_percent == 99.0
    # utilization=0.0 → 已用 0%，剩余 100%
    assert by_label["每周额度"].used_percent == 0.0
    assert by_label["每周额度"].remaining_percent == 100.0


def test_claude_utilization_partial(monkeypatch):
    """utilization=35.0 → 已用 35%，剩余 65%。"""
    resp = {
        "five_hour": {"utilization": 35.0, "resets_at": "2026-08-09T07:59:59+00:00"},
        "seven_day": {"utilization": 67.5, "resets_at": "2026-08-15T10:59:59+00:00"},
    }
    result = _run_claude_usage(monkeypatch, resp)
    by_label = {w.label: w for w in result.windows}
    assert by_label["每 5 小时"].used_percent == 35.0
    assert by_label["每 5 小时"].remaining_percent == 65.0
    assert by_label["每周额度"].used_percent == 67.5
    assert by_label["每周额度"].remaining_percent == 32.5
