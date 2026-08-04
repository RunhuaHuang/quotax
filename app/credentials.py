"""只读读取各 CLI 的 OAuth 凭据文件 / macOS Keychain。

设计原则（对齐 cc-switch）：**只读、绝不刷新、绝不写入**。
- 只读取用户已有的登录态（Claude Code / Gemini CLI / Grok CLI / Codex CLI / Copilot），
  与这些 agent 共享同一份凭据，互不干扰；
- token 过期时只提示"请到对应 CLI 重新登录"，不做 OAuth refresh（避免刷新令牌
  轮换导致 agent 侧失效的并发问题）。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

CRED_NOT_FOUND = "not_found"
CRED_OK = "ok"
CRED_EXPIRED = "expired"
CRED_PARSE_ERROR = "parse_error"
CRED_NO_TOKEN = "no_token"  # 已登录，但本机未存储可用的 access token（如 Claude Code 新版钥匙串留空）


@dataclass
class Credential:
    token: str
    status: str = CRED_OK
    source: str = ""
    message: str | None = None
    extra: dict = None  # 各渠道附加字段（account_id / user_id 等）

    def __post_init__(self):
        if self.extra is None:
            self.extra = {}


def _run_security(args: list[str]) -> str | None:
    """调 macOS `security` CLI 读 Keychain（只读操作）。"""
    try:
        result = subprocess.run(  # noqa: PLW1510 我们手动检查 returncode，不要它自动抛 CalledProcessError
            ["security", *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _expired(ts: float | None) -> bool:
    """毫秒/秒时间戳是否已过期。"""
    if not ts:
        return False
    ms = ts * 1000 if ts < 10_000_000_000 else ts
    return ms < datetime.now(UTC).timestamp() * 1000


# ── Claude Code ─────────────────────────────────────────────

# Claude Keychain 里 OAuth 条目的两种键名（新版本用驼峰 claudeAiOauth，
# 旧版本/文件格式用 claude.ai_oauth）
CLAUDE_OAUTH_KEYS = ("claude.ai_oauth", "claudeAiOauth")


def read_claude_credentials() -> Credential:
    """来源：macOS Keychain `Claude Code-credentials`，或 ~/.claude/.credentials.json。

    格式：{"claude.ai_oauth" 或 "claudeAiOauth": {"accessToken", "expiresAt", ...}}

    注意：较新版本的 Claude Code 可能已登录（钥匙串里有 `claudeAiOauth` 条目，
    `subscriptionType` 等元信息齐全），但 `accessToken`/`refreshToken` 为空字符串——
    本机钥匙串没有存明文 access token。这不是"未登录"，调用方应识别 CRED_NO_TOKEN
    并展示本地 transcript 统计作为替代，而不是提示"请重新登录"（用户并未登出）。

    之前版本这里还有一段扫描 `Claude Code-credentials-<hex>` 后缀条目的回退逻辑，
    实测这些条目里只有 `mcpOAuth`（MCP 服务器凭据），从不包含账号 token，纯属无效
    的 `security dump-keychain` 全量扫描（还可能触发钥匙串授权弹窗），已删除。
    """
    source = ""
    content: str | None = None

    keychain = _run_security(["find-generic-password", "-s", "Claude Code-credentials", "-w"])
    if keychain:
        content, source = keychain, "macOS Keychain (Claude Code-credentials)"
    else:
        path = Path.home() / ".claude" / ".credentials.json"
        if path.exists():
            try:
                content, source = path.read_text(encoding="utf-8"), str(path)
            except OSError:
                return Credential("", CRED_PARSE_ERROR, str(path), "凭据文件读取失败")

    if not content:
        return Credential(
            "",
            CRED_NOT_FOUND,
            "Claude Code 未登录",
            "未找到 Claude Code 登录凭据（请先运行 claude 登录）",
        )

    return _parse_claude_json(content, source)


def _parse_claude_json(content: str, source: str) -> Credential:
    """解析 Claude 凭据 JSON。"""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        return Credential("", CRED_PARSE_ERROR, source, f"凭据 JSON 解析失败: {e}")

    entry = None
    if isinstance(parsed, dict):
        for key in CLAUDE_OAUTH_KEYS:
            candidate = parsed.get(key)
            if isinstance(candidate, dict):
                entry = candidate
                break
    if not isinstance(entry, dict):
        return Credential(
            "",
            CRED_NOT_FOUND,
            source,
            "Claude 登录凭据中未找到有效结构（请运行 claude 登录）",
        )

    token = entry.get("accessToken") or entry.get("access_token")
    subscription_type = entry.get("subscriptionType") or entry.get("subscription_type")
    rate_limit_tier = entry.get("rateLimitTier") or entry.get("rate_limit_tier")
    scopes = entry.get("scopes")

    if not token:
        # 已登录（有 subscriptionType 等元信息），但本机没有存明文 access token。
        # 这主要发生在 macOS：新版 Claude Code 把凭据存进 Keychain，但只留元信息、
        # 不留明文 accessToken（系统安全策略限制第三方读取）。Windows/Linux 版把
        # 凭据写成 ~/.claude/.credentials.json 明文文件，accessToken 齐全，可正常
        # 查询官方用量——所以这个"查不到"是 macOS 专属现象，不是账号问题。
        is_mac_keychain = source.startswith("macOS Keychain")
        where = "本机钥匙串" if is_mac_keychain else "本机凭据文件"
        plan_hint = f"（{subscription_type}）" if subscription_type else ""
        if is_mac_keychain:
            message = (
                f"已检测到 Claude Code 登录{plan_hint}，但 macOS 钥匙串未存储可读取的 "
                "access token（新版 Claude Code 的安全策略），无法查询官方用量窗口。"
                "下方展示本地 transcript 统计作为替代。"
                "（Windows / Linux 版 Claude Code 凭据为明文文件，可正常查询官方用量。）"
            )
        else:
            message = (
                f"已检测到 Claude Code 登录{plan_hint}，但{where}未存储可用的 access token，"
                "无法查询官方用量窗口；下方展示本地 transcript 统计。"
            )
        return Credential(
            "",
            CRED_NO_TOKEN,
            source,
            message,
            extra={
                "subscription_type": subscription_type,
                "rate_limit_tier": rate_limit_tier,
                "scopes": scopes,
            },
        )

    if _expired(entry.get("expiresAt") or entry.get("expires_at")):
        return Credential(token, CRED_EXPIRED, source, "Claude 登录已过期，请重新运行 claude 登录")
    return Credential(token, CRED_OK, source)


# ── Gemini CLI ──────────────────────────────────────────────


def read_gemini_credentials() -> Credential:
    """来源：macOS Keychain `gemini-cli-oauth` / main-account，或 ~/.gemini/oauth_creds.json。

    Keychain 格式（keytar）：{"token": {"accessToken", "refreshToken", "expiresAt(ms)"}}
    文件格式：{"access_token", "refresh_token", "expiry_date(ms)"}
    """
    source = ""
    content: str | None = None

    keychain = _run_security(["find-generic-password", "-s", "gemini-cli-oauth", "-a", "main-account", "-w"])
    if keychain:
        content, source = keychain, "macOS Keychain (gemini-cli-oauth)"
    else:
        path = Path.home() / ".gemini" / "oauth_creds.json"
        if path.exists():
            try:
                content, source = path.read_text(encoding="utf-8"), str(path)
            except OSError:
                return Credential("", CRED_PARSE_ERROR, str(path), "凭据文件读取失败")

    if not content:
        return Credential(
            "",
            CRED_NOT_FOUND,
            "Gemini CLI 未登录",
            "未找到 Gemini 登录凭据（请先运行 gemini login）",
        )

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        return Credential("", CRED_PARSE_ERROR, source, f"凭据 JSON 解析失败: {e}")

    # Keychain 的 keytar 包装
    token_obj = parsed.get("token") if isinstance(parsed, dict) else None
    if isinstance(token_obj, dict) and token_obj.get("accessToken"):
        token = token_obj["accessToken"]
        if _expired(token_obj.get("expiresAt")):
            return Credential(
                token,
                CRED_EXPIRED,
                source,
                "Gemini 登录已过期，请重新运行 gemini login",
            )
        return Credential(token, CRED_OK, source)

    # 文件格式
    token = parsed.get("access_token") if isinstance(parsed, dict) else None
    if not token:
        return Credential("", CRED_PARSE_ERROR, source, "凭据中未找到 access token")
    if _expired(parsed.get("expiry_date")):
        return Credential(token, CRED_EXPIRED, source, "Gemini 登录已过期，请重新运行 gemini login")
    return Credential(token, CRED_OK, source)


# ── Grok CLI ────────────────────────────────────────────────


def read_grok_credentials() -> Credential:
    """来源：~/.grok/auth.json（Grok CLI / grok-build 的登录凭据）。

    GrokAuth 结构：{"key", "user_id", "auth_mode", "refresh_token", "expires_at", ...}
    文件可能有多层包装（如 scopes.default），递归找含 key+user_id 的对象。
    """
    path = Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok"))) / "auth.json"
    if not path.exists():
        return Credential("", CRED_NOT_FOUND, str(path), "未找到 Grok 登录凭据（请先运行 grok login）")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return Credential("", CRED_PARSE_ERROR, str(path), f"凭据 JSON 解析失败: {e}")

    found: dict | None = None

    def walk(obj):
        nonlocal found
        if found is not None:
            return
        if isinstance(obj, dict):
            if isinstance(obj.get("key"), str) and isinstance(obj.get("user_id"), str) and obj.get("key"):
                found = obj
                return
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(parsed)
    if found is None:
        return Credential("", CRED_PARSE_ERROR, str(path), "凭据中未找到 key + user_id")

    token = found["key"]
    expires = found.get("expires_at")
    if isinstance(expires, str):
        try:
            expires = datetime.fromisoformat(expires).timestamp()
        except ValueError:
            expires = None
    if _expired(expires):
        return Credential(token, CRED_EXPIRED, str(path), "Grok 登录已过期，请重新运行 grok login")
    return Credential(token, CRED_OK, str(path), extra={"user_id": found.get("user_id", "")})


# ── Codex CLI ───────────────────────────────────────────────


def read_codex_credentials() -> Credential:
    """来源：macOS Keychain `Codex Auth`，或 ~/.codex/auth.json。

    仅 auth_mode == "chatgpt"（OAuth 登录）可用；API Key 模式无订阅额度。
    格式：{"auth_mode": "chatgpt", "tokens": {"access_token", "account_id"}}
    """
    source = ""
    content: str | None = None

    keychain = _run_security(["find-generic-password", "-s", "Codex Auth", "-w"])
    if keychain:
        content, source = keychain, "macOS Keychain (Codex Auth)"
    else:
        path = Path.home() / ".codex" / "auth.json"
        if path.exists():
            try:
                content, source = path.read_text(encoding="utf-8"), str(path)
            except OSError:
                return Credential("", CRED_PARSE_ERROR, str(path), "凭据文件读取失败")

    if not content:
        return Credential(
            "",
            CRED_NOT_FOUND,
            "Codex CLI 未登录",
            "未找到 Codex 登录凭据（请先运行 codex login）",
        )

    return parse_codex_credentials(content, source)


def read_codex_credentials_from_file(path: Path) -> Credential:
    """从用户上传/指定的 auth.json 文件读取 Codex 凭据（多账号场景）。

    与 read_codex_credentials 共享同一套解析与状态判定；文件由渠道配置的
    extra.codex_auth_file 指向（Web 上传后存在 config 同目录 credentials/ 下）。
    """
    if not path.exists():
        return Credential("", CRED_NOT_FOUND, str(path), "Codex 凭据文件不存在（可能已被删除，请重新上传）")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return Credential("", CRED_PARSE_ERROR, str(path), "凭据文件读取失败")
    return parse_codex_credentials(content, str(path))


def parse_codex_credentials(content: str, source: str) -> Credential:
    """解析 Codex 凭据 JSON（Keychain / ~/.codex/auth.json / 用户上传文件共用）。"""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        return Credential("", CRED_PARSE_ERROR, source, f"凭据 JSON 解析失败: {e}")

    if isinstance(parsed, dict) and parsed.get("auth_mode") not in (None, "chatgpt"):
        return Credential("", CRED_NOT_FOUND, source, "Codex 使用 API Key 模式，无订阅额度可查")

    tokens = parsed.get("tokens") if isinstance(parsed, dict) else None
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        # 注：曾有一个"顶层 OPENAI_API_KEY"兼容分支，但在 auth_mode == "apikey" 时
        # 已被上面的判断提前 return，且即便命中也没有意义——ChatGPT 订阅用量端点
        # （chatgpt.com/backend-api/wham/usage）只认 ChatGPT OAuth token，塞一个
        # OpenAI API Key 进 Authorization 头只会拿到 401，误导成"登录已过期"。
        # 直接归类为「无可用 ChatGPT OAuth 凭据」。
        return Credential(
            "",
            CRED_NOT_FOUND,
            source,
            "Codex 凭据中未找到 ChatGPT OAuth token（API Key 模式无订阅额度可查）",
        )

    extra = {}
    if tokens.get("account_id"):
        extra["account_id"] = tokens["account_id"]
    return Credential(tokens["access_token"], CRED_OK, source, extra=extra)


# ── GitHub Copilot ──────────────────────────────────────────


def read_copilot_credentials() -> Credential:
    """来源：~/.config/github-copilot/hosts.json（VS Code / Copilot CLI 通用）。

    格式：{"github.com": {"oauth_token": "ghu_...", "user": "..."}}
    """
    candidates = [
        Path.home() / ".config" / "github-copilot" / "hosts.json",
        Path.home() / "Library" / "Application Support" / "github-copilot" / "hosts.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            return Credential("", CRED_PARSE_ERROR, str(path), f"凭据 JSON 解析失败: {e}")
        for key, entry in parsed.items() if isinstance(parsed, dict) else []:
            if isinstance(entry, dict) and entry.get("oauth_token"):
                extra = {}
                if entry.get("user"):
                    extra["user"] = entry["user"]
                return Credential(entry["oauth_token"], CRED_OK, str(path), extra=extra)
        break
    return Credential(
        "",
        CRED_NOT_FOUND,
        "GitHub Copilot 未登录",
        "未找到 Copilot 登录凭据（请先安装并登录 Copilot）",
    )
