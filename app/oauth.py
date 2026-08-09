"""OAuth 在线授权：目前支持 ChatGPT (Codex) 订阅。

让用户在 WebUI 点一下「通过 ChatGPT 登录」，浏览器打开 OpenAI 授权页完成登录，
回调到本服务后自动用授权码换 token、解析出 account_id，生成与 Codex CLI 格式
一致的 auth.json，自动创建一个 codex_subscription 渠道——省掉手动上传 auth.json。

实现参考 cliproxyapi（github.com/router-for-me/CLIProxyAPI）的 internal/auth/codex，
复用 OpenAI Codex CLI 的公开 OAuth 配置（client_id / endpoints / PKCE / scope）。

安全要点：
- authorize 必须由前端浏览器打开（auth.openai.com 有 Cloudflare 挑战，服务端
  请求必被拦）；本模块只负责生成 authorize URL 和后续 token 交换。
- PKCE（code_verifier / code_challenge）+ state 防 CSRF：state 随机生成，发起时
  记录、回调时校验，不匹配一律拒绝。
- token endpoint（auth.openai.com/oauth/token）是纯 API，不受 Cloudflare 保护，
  后端可直接 POST 换 token（已实测确认）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from .net import get_client

# ── OpenAI Codex OAuth 配置常量（与 Codex CLI / cliproxyapi 完全一致）────────
# client_id 是 Codex CLI 的公开常量，非机密——PKCE flow 下它本身不提供安全性，
# 安全性来自 code_verifier（只有发起方知道，OpenAI 在 token 交换时校验）。
AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
SCOPE = "openid email profile offline_access"
# redirect_uri 必须与 Codex CLI 注册的一致（http://localhost:1455/auth/callback）。
# OpenAI 对这个 client_id 做精确 redirect_uri 白名单校验，不接受其他端口/路径。
# 因此 OAuth 回调不走主服务端口，而是在 1455 端口起一个临时 HTTP 服务器接收。
CALLBACK_PORT = 1455
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/auth/callback"

# authorize 请求附加的 Codex 专用参数（逐字对齐 cliproxyapi 的 GenerateAuthURL），
# 缺了可能导致 OpenAI 走非 Codex 的标准 OAuth 分支，拿不到订阅额度所需的 claim。
_EXTRA_AUTHZ_PARAMS = {
    "prompt": "login",
    "id_token_add_organizations": "true",
    "codex_cli_simplified_flow": "true",
}


# ── PKCE / state 生成 ─────────────────────────────────────────────────────


def generate_pkce() -> tuple[str, str]:
    """生成 PKCE code_verifier + code_challenge（RFC 7636，method=S256）。

    返回 (code_verifier, code_challenge)。verifier 是 96 字节随机数的 URL-safe
    base64（无 padding，128 字符）；challenge = BASE64URL_NOPAD(SHA256(verifier))。
    verifier 由本服务在发起时记录、token 交换时回传给 OpenAI 校验——绝不发给前端、
    绝不出现在 authorize URL 里。
    """
    verifier_bytes = secrets.token_bytes(96)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
    challenge_bytes = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(challenge_bytes).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def generate_state() -> str:
    """生成 CSRF 防护用的随机 state（16 字节 hex）。"""
    return secrets.token_hex(16)


def build_authorize_url(state: str, code_challenge: str) -> str:
    """构造 OpenAI 授权页 URL。这个 URL 交给前端浏览器打开（不是后端请求）。

    redirect_uri 固定用 REDIRECT_URI（localhost:1455），不接收参数——OpenAI 对
    client_id 做精确 redirect_uri 白名单校验，必须用注册的那个。
    """
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        **_EXTRA_AUTHZ_PARAMS,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


# ── in-process state store（发起 ↔ 回调之间的临时状态）─────────────────────
#
# OAuth 是跨请求流程：start 端点发起、callback 端点收尾，中间隔着浏览器跳转。
# 两者要共享 (state, code_verifier)，回调时才能校验 state 防 CSRF、用 verifier
# 换 token。这里用一个进程内 dict + 过期清理做临时存储——本工具是单进程本机
# 服务，没有多实例共享问题；token 交换完成后条目立即删除，不会长期留存。
# TTL 兜底 10 分钟：用户可能在授权页停留较久，但不应无限期保留未完成的流程。


@dataclass
class _PendingFlow:
    state: str
    code_verifier: str
    created_at: float


_FLOWS: dict[str, _PendingFlow] = {}
_FLOW_TTL = 600  # 秒


def store_flow(state: str, code_verifier: str) -> None:
    """记录一次发起的 OAuth 流程（state + verifier）。"""
    _purge_expired()
    _FLOWS[state] = _PendingFlow(state=state, code_verifier=code_verifier, created_at=time.time())


def take_flow(state: str) -> str | None:
    """取出并删除一次流程的 code_verifier。state 不匹配返回 None（CSRF 防护）。

    取出即删：OAuth flow 是一次性的，code 只能换一次 token，verifier 用完即弃。
    """
    _purge_expired()
    flow = _FLOWS.pop(state, None)
    return flow.code_verifier if flow else None


def _purge_expired() -> None:
    """清理超过 TTL 的残留流程，防止内存缓慢增长（用户发起后从不回调的场景）。"""
    cutoff = time.time() - _FLOW_TTL
    expired = [k for k, v in _FLOWS.items() if v.created_at < cutoff]
    for k in expired:
        _FLOWS.pop(k, None)


# ── ID token (JWT) 解析：提取 account_id / email ──────────────────────────


def parse_id_token(id_token: str) -> dict:
    """解析 OpenAI id_token JWT 的 claims（不验签，只读 payload）。

    account_id 不在 token 响应顶层，而是嵌在 id_token 的 claims 里：
    claims["https://api.openai.com/auth"]["chatgpt_account_id"]。
    返回 {"account_id": str, "email": str, "plan_type": str}（缺失字段为空串）。

    不验签：id_token 是 OpenAI 的 token endpoint 直接下发的（TLS 通道已保证来源
    可信），验签对「本地读取自己刚换到的 token」没有额外安全价值，且需要拉 JWKS
    公钥、处理 ES256——开销不值得。这与 cliproxyapi 的 ParseJWTToken 一致。
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        return {"account_id": "", "email": "", "plan_type": ""}
    try:
        payload = parts[1]
        # JWT payload 是 base64url 无 padding，补齐后 decode
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {"account_id": "", "email": "", "plan_type": ""}
    if not isinstance(claims, dict):
        return {"account_id": "", "email": "", "plan_type": ""}
    auth_info = claims.get("https://api.openai.com/auth")
    if not isinstance(auth_info, dict):
        auth_info = {}
    return {
        "account_id": str(auth_info.get("chatgpt_account_id") or ""),
        "email": str(claims.get("email") or ""),
        "plan_type": str(auth_info.get("chatgpt_plan_type") or ""),
    }


# ── token 交换 ────────────────────────────────────────────────────────────


@dataclass
class CodexTokens:
    """token endpoint 换回的完整凭据（已解析出 account_id/email）。"""

    access_token: str
    refresh_token: str
    id_token: str
    account_id: str
    email: str
    plan_type: str
    expires_at_ms: int  # 绝对过期时间（epoch 毫秒）


class OAuthError(Exception):
    """OAuth 流程错误（token 交换失败 / state 不匹配 / 响应缺字段等）。"""


async def exchange_code(code: str, code_verifier: str) -> CodexTokens:
    """用授权码换 token（POST auth.openai.com/oauth/token）。

    返回 CodexTokens（含从 id_token 解析出的 account_id）。token endpoint 不受
    Cloudflare 保护，后端可直接调用。失败抛 OAuthError。redirect_uri 固定用
    REDIRECT_URI——必须与 authorize 时一致，否则 OpenAI 拒绝换 token。
    """
    client = get_client()
    body = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": code_verifier,
    }
    resp = await client.post(
        TOKEN_URL,
        data=body,
        headers={"Accept": "application/json"},
    )
    if resp.status_code != 200:
        raise OAuthError(f"OpenAI token 交换失败 (HTTP {resp.status_code}): {resp.text[:300]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise OAuthError(f"OpenAI token 响应不是合法 JSON: {e}") from e
    if not isinstance(data, dict) or not data.get("access_token"):
        raise OAuthError(f"OpenAI token 响应缺少 access_token: {str(data)[:200]}")

    id_token = data.get("id_token") or ""
    info = parse_id_token(id_token) if id_token else {"account_id": "", "email": "", "plan_type": ""}
    expires_in = data.get("expires_in") or 3600
    return CodexTokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token") or "",
        id_token=id_token,
        account_id=info["account_id"],
        email=info["email"],
        plan_type=info["plan_type"],
        expires_at_ms=int(time.time() * 1000) + int(expires_in) * 1000,
    )


def tokens_to_auth_json(tokens: CodexTokens) -> str:
    """把换到的 token 序列化成与 ~/.codex/auth.json 完全一致的结构。

    这样 query_codex 能直接走 read_codex_credentials_from_file → parse_codex_credentials，
    无缝复用现有的「上传 auth.json」查询链路，无需为 OAuth 路径单独写查询逻辑。
    auth_mode 固定 "chatgpt"（OAuth 登录），与 Codex CLI 登录后落盘的格式一致。
    """
    import time as _time

    auth = {
        "OPENAI_API_KEY": None,  # OAuth 模式无 API key（与 Codex CLI auth.json 结构对齐）
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "id_token": tokens.id_token,
            "account_id": tokens.account_id,
        },
        "last_refresh": int(_time.time() * 1000),
    }
    return json.dumps(auth, ensure_ascii=False, indent=2)
