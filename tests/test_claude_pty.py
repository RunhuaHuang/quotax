"""Claude PTY 用量探测测试。

parse_usage_text 是纯函数，用模拟的 claude CLI /usage 终端输出测试。
fetch_usage_via_pty 涉及真实 PTY + claude 进程，只测 is_claude_cli_available
和不可用时的 None 返回路径。
"""

from __future__ import annotations

from app.claude_pty import is_claude_cli_available, parse_usage_text, strip_ansi

# ── strip_ansi ────────────────────────────────────────────────


def test_strip_ansi_removes_color_codes():
    raw = "\x1b[32m绿色文字\x1b[0m\x1b[1;31m红色粗体\x1b[0m"
    assert strip_ansi(raw) == "绿色文字红色粗体"


def test_strip_ansi_removes_cursor_and_clear():
    raw = "\x1b[2J\x1b[H\x1b[?25l\x1b[6n文本\r\n第二行"
    assert strip_ansi(raw) == "文本\n第二行"


def test_strip_ansi_preserves_plain_text():
    assert strip_ansi("Current session\n45% remaining") == "Current session\n45% remaining"


# ── parse_usage_text ─────────────────────────────────────────
#
# 测试样本模拟 claude CLI /usage 面板渲染后的纯文本（已剥离 ANSI）。
# 真实面板形如（标签和百分比可能在不同行）：
#   Usage
#   ╰── Current session
#         45% remaining · resets in 3h 22m
#   ╰── Current week (all models)
#         67% remaining · resets in 2 days


_SAMPLE_WITH_USAGE = """
Usage

  Current session
    45% remaining · resets in 3h 22m

  Current week (all models)
    67% remaining · resets in 2 days

  Current week (Opus)
    80% remaining · resets in 2 days
"""


def test_parse_usage_text_extracts_session_and_weekly():
    result = parse_usage_text(_SAMPLE_WITH_USAGE)
    assert result.session_percent_left == 45
    assert result.weekly_percent_left == 67
    assert result.weekly_opus_percent_left == 80


def test_parse_usage_text_extracts_reset_text():
    result = parse_usage_text(_SAMPLE_WITH_USAGE)
    assert result.session_reset_text is not None
    assert "3h 22m" in result.session_reset_text
    assert result.weekly_reset_text is not None
    assert "2 days" in result.weekly_reset_text


def test_parse_usage_text_label_and_percent_same_line():
    """标签和百分比在同一行的布局也能解析。"""
    text = "Current session: 30% remaining\nCurrent week (all models): 50% remaining"
    result = parse_usage_text(text)
    assert result.session_percent_left == 30
    assert result.weekly_percent_left == 50


def test_parse_usage_text_no_usage_labels_returns_none():
    """CLI 未登录时 /usage 只返回本地统计（没有 Current session/week 标签）。
    各字段应为 None——调用方据此降级。"""
    text = """
Usage Stats

Total cost: $0.0000
Total duration (API): 0s
Usage: 0 input, 0 output

Not logged in · Run /login
"""
    result = parse_usage_text(text)
    assert result.session_percent_left is None
    assert result.weekly_percent_left is None
    assert result.weekly_opus_percent_left is None


def test_parse_usage_text_handles_sonnet_variant_label():
    """分模型周额度标签可能是 Sonnet 变体。"""
    text = "Current session\n10% remaining\nCurrent week (Sonnet only)\n90% remaining"
    result = parse_usage_text(text)
    assert result.session_percent_left == 10
    assert result.weekly_opus_percent_left == 90


def test_parse_usage_text_decimal_percent():
    """百分比可能是小数（45.0%）。"""
    text = "Current session\n45.0% remaining"
    result = parse_usage_text(text)
    assert result.session_percent_left == 45


# ── fetch_usage_via_pty 边界 ─────────────────────────────────


def test_fetch_usage_returns_none_when_no_claude_cli(monkeypatch):
    """claude CLI 不存在时返回 None，不崩。"""
    from app import claude_pty

    monkeypatch.setattr(claude_pty, "is_claude_cli_available", lambda: False)
    assert claude_pty.fetch_usage_via_pty() is None


def test_is_claude_cli_available_returns_bool():
    """is_claude_cli_available 应返回布尔值（不崩）。"""
    result = is_claude_cli_available()
    assert isinstance(result, bool)


# ── query_claude PTY fallback 集成 ────────────────────────────


def test_query_claude_falls_back_to_pty_when_no_token(monkeypatch):
    """CRED_NO_TOKEN 时应尝试 PTY fallback；PTY 成功则返回 ok + windows。"""
    import asyncio

    from app.config import Channel
    from app.credentials import CRED_NO_TOKEN, Credential
    from app.providers import subscriptions

    # mock read_claude_credentials 返回 NO_TOKEN
    monkeypatch.setattr(
        subscriptions,
        "read_claude_credentials",
        lambda: Credential("", CRED_NO_TOKEN, "test", extra={"subscription_type": "pro"}),
    )
    # mock PTY 返回成功结果
    monkeypatch.setattr(
        subscriptions,
        "_try_pty_usage",
        lambda base: subscriptions.ChannelResult(
            status="ok", plan_name="PTY", windows=[], id="ch", type="claude_subscription",
            name="C", category="subscription", source="pty",
        ),
    )

    result = asyncio.run(subscriptions.query_claude(Channel(id="ch", type="claude_subscription", name="C")))
    assert result.status == "ok"
    assert result.source == "pty"


def test_query_claude_pty_unavailable_falls_back_to_info(monkeypatch):
    """PTY 也不可用（CLI 未登录）时，降级为 info + 本地统计提示。"""
    import asyncio

    from app.config import Channel
    from app.credentials import CRED_NO_TOKEN, Credential
    from app.providers import subscriptions

    monkeypatch.setattr(
        subscriptions,
        "read_claude_credentials",
        lambda: Credential("", CRED_NO_TOKEN, "test", "无 token", extra={"subscription_type": "pro"}),
    )
    monkeypatch.setattr(subscriptions, "_try_pty_usage", lambda base: None)

    result = asyncio.run(subscriptions.query_claude(Channel(id="ch", type="claude_subscription", name="C")))
    assert result.status == "info"
    assert result.plan_name == "Claude Pro 订阅"
    assert result.windows == []
