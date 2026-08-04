"""providers 包的纯函数单测：智谱/火山的响应解析、Copilot quota_snapshots 解析、
Gemini 的 camelCase/snake_case 兼容 helper。不发起任何真实网络请求——全部用
写死的 fixture JSON 喂给解析函数。
"""

from __future__ import annotations

from app.providers import coding_plans, volcengine
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
    assert {"five_hour", "weekly", "custom"} <= keys
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
    assert five_hour.used_label == "25 次"
    assert five_hour.max_label == "100 次"


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
    assert keys == {"five_hour", "weekly", "monthly"}  # 未识别的桶被跳过


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


def test_query_volcengine_agent_only(monkeypatch):
    result = _run_volcengine(
        monkeypatch,
        {"GetAFPUsage": _AFP_RESP, "GetCodingPlanUsage": {"Result": {"QuotaUsage": []}}},
    )
    assert result.status == "ok"
    assert {w.label for w in result.windows} == {"Agent 每 5 小时", "Agent 每周额度"}
    assert result.plan_name == "火山 Agent Plan small"
    assert "Coding Plan" not in result.plan_name


def test_query_volcengine_coding_only(monkeypatch):
    result = _run_volcengine(
        monkeypatch,
        {"GetAFPUsage": {"Result": {}}, "GetCodingPlanUsage": _CODING_RESP},
    )
    assert result.status == "ok"
    assert {w.label for w in result.windows} == {"Coding 每周额度"}
    assert result.plan_name == "火山 Coding Plan"


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
    assert "Coding Plan (GetCodingPlanUsage)" in result.message


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
