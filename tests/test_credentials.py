"""app/credentials.py 的单测：只测试纯函数 _parse_claude_json（不发起任何
subprocess / keychain / 网络调用）。

覆盖诊断出的真实场景：Claude Code 已登录但钥匙串里 accessToken 是空字符串
（CRED_NO_TOKEN），以及正常的 ok / expired / not_found / parse_error 分支。
"""

from __future__ import annotations

import json

from app.credentials import (
    CRED_EXPIRED,
    CRED_NO_TOKEN,
    CRED_NOT_FOUND,
    CRED_OK,
    CRED_PARSE_ERROR,
    _parse_claude_json,
)


def test_claude_ok_when_token_present_and_not_expired():
    future_ms = 9_999_999_999_999  # 远未来
    content = json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-real", "expiresAt": future_ms}})
    cred = _parse_claude_json(content, "macOS Keychain (Claude Code-credentials)")
    assert cred.status == CRED_OK
    assert cred.token == "sk-ant-oat-real"


def test_claude_expired_when_expires_at_in_past():
    past_ms = 1_000_000_000_000
    content = json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-real", "expiresAt": past_ms}})
    cred = _parse_claude_json(content, "macOS Keychain (Claude Code-credentials)")
    assert cred.status == CRED_EXPIRED


def test_claude_no_token_real_world_diagnosis_case():
    """诊断出的真实钥匙串结构：已登录（有 subscriptionType 等元信息），但
    accessToken/refreshToken 是空字符串——这不是"未登录"，必须是 CRED_NO_TOKEN
    而不是 CRED_NOT_FOUND/CRED_EXPIRED，message 也不能说"请重新登录"。"""
    content = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "",
                "refreshToken": "",
                "expiresAt": 0,
                "refreshTokenExpiresAt": 1785329083927,
                "scopes": ["user:inference", "user:profile"],
                "subscriptionType": "pro",
                "rateLimitTier": "default_claude_ai",
            },
            "mcpOAuth": {},
        }
    )
    cred = _parse_claude_json(content, "macOS Keychain (Claude Code-credentials)")
    assert cred.status == CRED_NO_TOKEN
    assert cred.token == ""
    assert cred.extra["subscription_type"] == "pro"
    assert cred.extra["rate_limit_tier"] == "default_claude_ai"
    assert cred.extra["scopes"] == ["user:inference", "user:profile"]
    assert "登录" in cred.message
    assert "重新" not in cred.message  # 不能误导成"请重新登录"——用户没有掉登录
    assert "重新运行" not in cred.message


def test_claude_no_token_message_mentions_source_kind():
    content = json.dumps({"claudeAiOauth": {"accessToken": "", "subscriptionType": "max"}})
    cred = _parse_claude_json(content, "/Users/x/.claude/.credentials.json")
    assert cred.status == CRED_NO_TOKEN
    assert "钥匙串" not in cred.message  # 来源是文件，措辞应该说"凭据文件"
    assert "凭据文件" in cred.message


def test_claude_not_found_when_no_oauth_key():
    content = json.dumps({"somethingElse": {}})
    cred = _parse_claude_json(content, "some source")
    assert cred.status == CRED_NOT_FOUND


def test_claude_supports_legacy_snake_case_key():
    content = json.dumps({"claude.ai_oauth": {"access_token": "legacy-token"}})
    cred = _parse_claude_json(content, "legacy file")
    assert cred.status == CRED_OK
    assert cred.token == "legacy-token"


def test_claude_parse_error_on_invalid_json():
    cred = _parse_claude_json("not json at all", "some source")
    assert cred.status == CRED_PARSE_ERROR
