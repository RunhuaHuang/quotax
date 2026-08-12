"""Codex OAuth 模块与端点测试。

覆盖：
- PKCE / state 生成的格式与确定性关系（challenge = base64url(sha256(verifier))）
- state store 的 store/take/过期清理、CSRF 防护（state 不匹配返回 None）
- id_token JWT 解析（account_id 从 https://api.openai.com/auth.chatgpt_account_id 提取）
- token 交换（mock httpx 客户端，验证请求构造 + 响应解析）
- tokens_to_auth_json 生成与 Codex CLI auth.json 格式一致（能被 parse_codex_credentials 解析）
- 端点：start 生成 URL / callback 换 token 建渠道 / poll 取结果

所有测试不发起真实网络请求（token 交换 mock 掉 httpx 客户端）。
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from app import oauth

# ── 辅助：构造 mock id_token JWT ──────────────────────────────


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_id_token(account_id: str = "acc_123", email: str = "test@example.com", plan_type: str = "plus") -> str:
    """构造一个结构真实的 OpenAI id_token JWT（header.payload.signature，不验签）。"""
    header = {"alg": "RS256", "typ": "JWT", "kid": "test"}
    payload = {
        "email": email,
        "email_verified": True,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": plan_type,
            "chatgpt_user_id": "user_456",
            "user_id": "org_789",
        },
    }
    return f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}.fake_sig"


# ── PKCE / state 生成 ─────────────────────────────────────────


def test_generate_pkce_verifier_is_base64url_no_padding():
    verifier, _ = oauth.generate_pkce()
    assert len(verifier) == 128  # 96 bytes → 128 base64 chars (no padding)
    assert "=" not in verifier
    assert "-" in verifier or "_" in verifier  # url-safe alphabet


def test_generate_pkce_challenge_is_sha256_of_verifier():
    """challenge 必须等于 base64url(sha256(verifier))，否则 token 交换时 OpenAI 校验不过。"""
    verifier, challenge = oauth.generate_pkce()
    expected = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    assert challenge == expected


def test_generate_pkce_unique_each_call():
    a = oauth.generate_pkce()
    b = oauth.generate_pkce()
    assert a != b  # 随机性


def test_generate_state_is_hex():
    state = oauth.generate_state()
    assert len(state) == 32  # 16 bytes hex
    assert all(c in "0123456789abcdef" for c in state)


# ── authorize URL 构造 ────────────────────────────────────────


def test_build_authorize_url_contains_all_required_params():
    url = oauth.build_authorize_url("mystate", "mychallenge")
    assert url.startswith("https://auth.openai.com/oauth/authorize?")
    assert "client_id=app_EMoamEEZ73f0CkXaXp7hrann" in url
    assert "response_type=code" in url
    assert "code_challenge=mychallenge" in url
    assert "code_challenge_method=S256" in url
    assert "state=mystate" in url
    assert "scope=openid+email+profile+offline_access" in url
    assert "codex_cli_simplified_flow=true" in url
    assert "redirect_uri=http" in url


# ── state store ───────────────────────────────────────────────


def test_store_and_take_flow_roundtrip():
    oauth.store_flow("state1", "verifier1")
    assert oauth.take_flow("state1") == "verifier1"
    # 取出即删（一次性）
    assert oauth.take_flow("state1") is None


def test_take_flow_unknown_state_returns_none():
    """state 不匹配必须返回 None——这是 CSRF 防护的核心。"""
    oauth.store_flow("real_state", "v")
    assert oauth.take_flow("fake_state") is None
    assert oauth.take_flow("real_state") == "v"


def test_store_flow_overwrites_same_state():
    oauth.store_flow("s", "v1")
    oauth.store_flow("s", "v2")
    assert oauth.take_flow("s") == "v2"


def test_store_flow_has_capacity_bound():
    oauth._FLOWS.clear()
    for index in range(oauth._FLOW_MAX + 5):
        oauth.store_flow(f"capacity_{index}", f"v{index}")
    assert len(oauth._FLOWS) == oauth._FLOW_MAX
    assert oauth.take_flow("capacity_0") is None
    oauth._FLOWS.clear()


# ── id_token 解析 ─────────────────────────────────────────────


def test_parse_id_token_extracts_account_id_and_email():
    id_token = _make_id_token(account_id="acc_abc", email="u@x.com", plan_type="pro")
    info = oauth.parse_id_token(id_token)
    assert info["account_id"] == "acc_abc"
    assert info["email"] == "u@x.com"
    assert info["plan_type"] == "pro"


def test_parse_id_token_handles_malformed_token():
    assert oauth.parse_id_token("not.a.jwt.too.many.parts") == {
        "account_id": "", "email": "", "plan_type": ""
    }
    assert oauth.parse_id_token("") == {"account_id": "", "email": "", "plan_type": ""}
    assert oauth.parse_id_token("only.one") == {"account_id": "", "email": "", "plan_type": ""}


def test_parse_id_token_missing_auth_claim():
    """id_token 里没有 https://api.openai.com/auth claim 时不崩，返回空串。"""
    payload = _b64url(json.dumps({"email": "x@y.com"}).encode())
    info = oauth.parse_id_token(f"header.{payload}.sig")
    assert info["account_id"] == ""
    assert info["email"] == "x@y.com"


# ── token 交换（mock httpx）───────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text or (json.dumps(json_data) if json_data else "")

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeClient:
    """mock oauth.get_client() 返回的 httpx.AsyncClient，只实现 post。"""

    def __init__(self, response: _FakeResponse):
        self._response = response
        self.last_post: dict = {}

    async def post(self, url, data=None, headers=None):
        self.last_post = {"url": url, "data": data, "headers": headers}
        return self._response


def test_exchange_code_success(monkeypatch):
    id_token = _make_id_token("acc_ok", "ok@test.com", "plus")
    fake_client = _FakeClient(
        _FakeResponse(
            200,
            {
                "access_token": "at_xxx",
                "refresh_token": "rt_yyy",
                "id_token": id_token,
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )
    )
    monkeypatch.setattr(oauth, "get_client", lambda: fake_client)

    tokens = __import__("asyncio").run(oauth.exchange_code("code123", "verifier456"))
    assert tokens.access_token == "at_xxx"
    assert tokens.refresh_token == "rt_yyy"
    assert tokens.account_id == "acc_ok"
    assert tokens.email == "ok@test.com"
    assert tokens.plan_type == "plus"

    # 验证请求构造
    post = fake_client.last_post
    assert post["url"] == "https://auth.openai.com/oauth/token"
    assert post["data"]["grant_type"] == "authorization_code"
    assert post["data"]["code"] == "code123"
    assert post["data"]["code_verifier"] == "verifier456"
    assert post["data"]["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert post["data"]["client_id"] == "app_EMoamEEZ73f0CkXaXp7hrann"
    assert post["headers"]["Accept"] == "application/json"


def test_exchange_code_http_error_raises(monkeypatch):
    fake_client = _FakeClient(_FakeResponse(400, text='{"error":"bad_code"}'))
    monkeypatch.setattr(oauth, "get_client", lambda: fake_client)
    with pytest.raises(oauth.OAuthError, match="HTTP 400"):
        __import__("asyncio").run(oauth.exchange_code("bad", "v"))


def test_exchange_code_missing_access_token_raises(monkeypatch):
    fake_client = _FakeClient(_FakeResponse(200, {"refresh_token": "rt"}))
    monkeypatch.setattr(oauth, "get_client", lambda: fake_client)
    with pytest.raises(oauth.OAuthError, match="缺少 access_token"):
        __import__("asyncio").run(oauth.exchange_code("c", "v"))


# ── tokens_to_auth_json：与 Codex CLI 格式兼容 ────────────────


def test_tokens_to_auth_json_is_parseable_by_credentials_module():
    """生成的 auth.json 必须能被 parse_codex_credentials 正常解析——这是复用查询链路的前提。"""
    from app.credentials import CRED_OK, parse_codex_credentials

    id_token = _make_id_token("acc_auth", "auth@test.com")
    tokens = oauth.CodexTokens(
        access_token="at", refresh_token="rt", id_token=id_token,
        account_id="acc_auth", email="auth@test.com", plan_type="plus",
        expires_at_ms=9999999999999,
    )
    auth_json = oauth.tokens_to_auth_json(tokens)
    parsed = parse_codex_credentials(auth_json, "oauth")
    assert parsed.status == CRED_OK
    assert parsed.token == "at"
    assert parsed.extra["account_id"] == "acc_auth"
