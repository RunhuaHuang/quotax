"""app/credentials.py 的单测：主要测试纯函数 _parse_claude_json（不发起任何
subprocess / keychain / 网络调用），以及 read_copilot_credentials 的候选路径
回退逻辑（用 monkeypatch Path.home() 隔离，只读写 tmp_path，不碰真实文件）。

覆盖诊断出的真实场景：Claude Code 已登录但钥匙串里 accessToken 是空字符串
（CRED_NO_TOKEN），以及正常的 ok / expired / not_found / parse_error 分支。
"""

from __future__ import annotations

import json

from app import credentials
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


# ── 任务 7：Copilot 候选路径要全部试完才判定未登录 ────────────────
#
# 回归背景：read_copilot_credentials 的两个候选路径，旧实现里循环第一个
# **存在**的文件如果没有 oauth_token（比如是个空 {}），会直接 break，不再试
# 第二个候选路径——即便第二个候选文件里真的有 token，也会被误判成"未登录"。


def test_copilot_falls_back_to_second_candidate_when_first_has_no_token(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    first = fake_home / ".config" / "github-copilot" / "hosts.json"
    second = fake_home / "Library" / "Application Support" / "github-copilot" / "hosts.json"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("{}", encoding="utf-8")  # 第一个候选文件存在，但没有 token
    second.write_text(
        json.dumps({"github.com": {"oauth_token": "ghu_real_token", "user": "octocat"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(credentials.Path, "home", lambda: fake_home)
    cred = credentials.read_copilot_credentials()
    assert cred.status == CRED_OK
    assert cred.token == "ghu_real_token"
    assert cred.extra["user"] == "octocat"
    assert str(second) in cred.source  # 来源确实是第二个候选文件


def test_copilot_first_candidate_missing_oauth_token_key_also_falls_back(monkeypatch, tmp_path):
    """第一个候选文件存在、结构合法，但里面的 entry 没有 oauth_token 字段
    （不是完全空 {}，而是有 host 但没登录），同样要继续试第二个候选。"""
    fake_home = tmp_path / "home"
    first = fake_home / ".config" / "github-copilot" / "hosts.json"
    second = fake_home / "Library" / "Application Support" / "github-copilot" / "hosts.json"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(json.dumps({"github.com": {"user": "nobody"}}), encoding="utf-8")
    second.write_text(json.dumps({"github.com": {"oauth_token": "ghu_second"}}), encoding="utf-8")

    monkeypatch.setattr(credentials.Path, "home", lambda: fake_home)
    cred = credentials.read_copilot_credentials()
    assert cred.status == CRED_OK
    assert cred.token == "ghu_second"


def test_copilot_not_found_when_no_candidate_has_token(monkeypatch, tmp_path):
    """两个候选都存在但都没有 token 时，才真正判定为未登录（回归保护：不能
    因为这次修复变得"随便找到一个文件就返回 ok"）。"""
    fake_home = tmp_path / "home"
    first = fake_home / ".config" / "github-copilot" / "hosts.json"
    second = fake_home / "Library" / "Application Support" / "github-copilot" / "hosts.json"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(credentials.Path, "home", lambda: fake_home)
    cred = credentials.read_copilot_credentials()
    assert cred.status == CRED_NOT_FOUND


def test_copilot_not_found_when_neither_candidate_exists(monkeypatch, tmp_path):
    fake_home = tmp_path / "home_without_copilot"
    monkeypatch.setattr(credentials.Path, "home", lambda: fake_home)
    cred = credentials.read_copilot_credentials()
    assert cred.status == CRED_NOT_FOUND
