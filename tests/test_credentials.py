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


def test_claude_access_token_expired_but_session_valid_is_no_token():
    """access token 过期 ≠ 登录过期：refresh token 未过期就仍是登录态。

    真实场景：钥匙串里 accessToken 的 expiresAt 停在 CLI 上次写回时（CLI 用
    refresh token 续期后通常不写回钥匙串），但 refreshTokenExpiresAt 还在未来。
    此时必须走 CRED_NO_TOKEN（调用方改走 PTY 探测），不能误报 CRED_EXPIRED。
    """
    past_ms = 1_000_000_000_000  # 2001 年，早就过期
    future_ms = 9_999_999_999_999  # 远未来
    content = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-oat-real",
                "expiresAt": past_ms,
                "refreshTokenExpiresAt": future_ms,
                "subscriptionType": "pro",
            }
        }
    )
    cred = _parse_claude_json(content, "macOS Keychain (Claude Code-credentials)")
    assert cred.status == CRED_NO_TOKEN
    assert cred.token == ""  # 过期 token 不再交给调用方直接调用量 API
    assert cred.extra["subscription_type"] == "pro"


def test_claude_access_token_expired_without_session_field_is_no_token():
    """没有 refreshTokenExpiresAt 字段时，access token 过期也不能判「登录过期」
    ——会话状态未知，交给 PTY 探测，而不是拿短命 token 的 expiresAt 误判。"""
    past_ms = 1_000_000_000_000
    content = json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-real", "expiresAt": past_ms}})
    cred = _parse_claude_json(content, "macOS Keychain (Claude Code-credentials)")
    assert cred.status == CRED_NO_TOKEN


def test_claude_expired_when_refresh_token_expires():
    """refresh token 过期才是真正的「登录过期」，需要重新登录。"""
    past_ms = 1_000_000_000_000
    future_ms = 9_999_999_999_999
    content = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-oat-real",
                "expiresAt": future_ms,
                "refreshTokenExpiresAt": past_ms,
            }
        }
    )
    cred = _parse_claude_json(content, "macOS Keychain (Claude Code-credentials)")
    assert cred.status == CRED_EXPIRED
    assert cred.token == "sk-ant-oat-real"


def test_malformed_optional_expiry_does_not_crash_credential_parsing():
    content = json.dumps({"claudeAiOauth": {"accessToken": "token", "expiresAt": "unknown"}})
    cred = _parse_claude_json(content, "test")
    assert cred.status == CRED_OK
    assert cred.token == "token"


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


def test_claude_no_token_message_guides_user_to_login():
    """CRED_NO_TOKEN 的文案应提醒用户去终端登录（PTY fallback 依赖 CLI 登录态），
    而不是说"无法查询"——因为有 PTY 探测，登录后即可自动获取用量。"""
    content = json.dumps({"claudeAiOauth": {"accessToken": "", "subscriptionType": "max"}})
    cred = _parse_claude_json(content, "/Users/x/.claude/.credentials.json")
    assert cred.status == CRED_NO_TOKEN
    assert "claude" in cred.message.lower()  # 提醒运行 claude 登录
    assert "登录" in cred.message  # 包含登录指引


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


# ── Claude CLI 权威登录态（claude auth status --json）──────────────


def test_claude_cli_logged_out_short_circuits_before_keychain(monkeypatch):
    """CLI 明确报告未登录时，读凭据应直接短路为 not_found，不再读钥匙串——
    避免钥匙串里残留的旧 token 被误判为可用。"""
    monkeypatch.setattr(credentials, "_claude_cli_auth_status", lambda: {"loggedIn": False})

    def _forbidden_keychain_read(*_args, **_kwargs):
        raise AssertionError("CLI 已报告未登录，不应再读钥匙串")

    monkeypatch.setattr(credentials, "_run_security", _forbidden_keychain_read)
    cred = credentials.read_claude_credentials()
    assert cred.status == CRED_NOT_FOUND
    assert "未登录" in (cred.message or "")


def test_claude_cli_logged_in_falls_through_to_keychain(monkeypatch, tmp_path):
    """CLI 报告已登录时继续走钥匙串路径（这里钥匙串无内容 → not_found）。"""
    monkeypatch.setattr(credentials, "_claude_cli_auth_status", lambda: {"loggedIn": True})
    monkeypatch.setattr(credentials, "_run_security", lambda *_a, **_k: None)
    monkeypatch.setattr(credentials.Path, "home", lambda: tmp_path)
    cred = credentials.read_claude_credentials()
    assert cred.status == CRED_NOT_FOUND


def test_claude_cli_auth_status_unavailable_does_not_short_circuit(monkeypatch, tmp_path):
    """CLI 不存在/命令失败（返回 None）时，退回读钥匙串的原有逻辑。"""
    monkeypatch.setattr(credentials, "_claude_cli_auth_status", lambda: None)
    monkeypatch.setattr(credentials, "_run_security", lambda *_a, **_k: None)
    monkeypatch.setattr(credentials.Path, "home", lambda: tmp_path)
    cred = credentials.read_claude_credentials()
    assert cred.status == CRED_NOT_FOUND
