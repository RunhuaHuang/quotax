"""只读读取各 CLI 的 OAuth 凭据文件 / macOS Keychain。

设计原则（对齐 cc-switch）：**只读、绝不刷新、绝不写入**。
- 只读取用户已有的登录态（Claude Code / Gemini CLI / Grok CLI / Codex CLI / Copilot），
  与这些 agent 共享同一份凭据，互不干扰；
- token 过期时只提示"请到对应 CLI 重新登录"，不做 OAuth refresh（避免刷新令牌
  轮换导致 agent 侧失效的并发问题）。
"""

from __future__ import annotations

import json
import math
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
    if ts in (None, "", 0, 0.0):
        return False
    try:
        value = float(ts)
    except (TypeError, ValueError):
        # 某些 CLI 版本会把 expiry 字段序列化成非数字文本；不能让一个坏的
        # 可选过期字段把整个凭据探测打成 500，按“未知过期时间”继续解析 token。
        return False
    if not math.isfinite(value) or value <= 0:
        return False
    ms = value * 1000 if value < 10_000_000_000 else value
    return ms < datetime.now(UTC).timestamp() * 1000


# ── Claude Code ─────────────────────────────────────────────

# Claude Keychain 里 OAuth 条目的两种键名（新版本用驼峰 claudeAiOauth，
# 旧版本/文件格式用 claude.ai_oauth）
CLAUDE_OAUTH_KEYS = ("claude.ai_oauth", "claudeAiOauth")


def _claude_cli_auth_status() -> dict | None:
    """用 `claude auth status --json` 拿 CLI 自己的登录态（权威来源）。

    参考 CodexBar 的 ClaudeCLIAuthStatusProbe：钥匙串里的 access token 会陈旧，
    而 CLI 自己最清楚登录有没有效。返回解析后的 JSON dict（含 loggedIn 字段）；
    CLI 不存在 / 命令失败 / 输出非法时返回 None，调用方退回读钥匙串。
    """
    from shutil import which

    if which("claude") is None:
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    env["DISABLE_AUTOUPDATER"] = "1"
    env["DISABLE_TELEMETRY"] = "1"
    try:
        result = subprocess.run(  # noqa: PLW1510
            ["claude", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


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

    # 先问 Claude CLI 自己登录没登录（权威，参考 CodexBar 的 ClaudeCLIAuthStatusProbe）。
    # CLI 明确报告未登录时直接短路，避免钥匙串里残留的旧 token 被误判为可用。
    cli_status = _claude_cli_auth_status()
    if cli_status is not None and cli_status.get("loggedIn") is False:
        return Credential(
            "",
            CRED_NOT_FOUND,
            "Claude Code 未登录",
            "Claude Code 未登录（claude CLI 报告），请先运行 claude 登录",
        )

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
        # 不留明文 accessToken（系统安全策略限制第三方读取）。此时本项目会自动尝试
        # PTY 探测（启动 claude CLI 执行 /usage）——但如果 CLI 本身也未登录（auth
        # token 为空），PTY 同样拿不到用量，才会走到这里展示本地统计作为替代。
        # 解决办法：在终端运行一次 claude 完成登录，PTY 即可自动获取实时用量。
        plan_hint = f"（{subscription_type}）" if subscription_type else ""
        message = (
            f"已检测到 Claude Code 登录{plan_hint}，但本机未存储可读取的 access token，"
            "PTY 探测也未获取到用量（Claude CLI 可能未登录）。"
            "请在终端运行一次 claude 完成登录，之后即可自动获取实时用量；"
            "下方暂时展示本地 transcript 统计作为替代。"
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

    # 会话寿命（refresh token）才是「登录是否过期」的唯一正确信号。access token
    # 是短命的（几十分钟到几小时），Claude CLI 用 refresh token 在后台自动续期，
    # 但通常不会把新 access token 写回钥匙串——钥匙串里的 expiresAt 早就过期，
    # 拿它判登录会误报「已过期」。参考 CodexBar：登录态看 refreshTokenExpiresAt。
    session_expires_at = entry.get("refreshTokenExpiresAt") or entry.get("refresh_token_expires_at")
    if _expired(session_expires_at):
        return Credential(token, CRED_EXPIRED, source, "Claude 登录已过期，请重新运行 claude 登录")

    # access token 已过期但会话仍有效：不是登出，只是这份 token 不能直接调用量
    # API。归入 CRED_NO_TOKEN（「无可用的 access token」），让调用方走 PTY 探测——
    # CLI 自己会用它手里的 refresh token 换新并显示实时用量（与 CodexBar 委托 CLI
    # 刷新的思路一致）。
    if _expired(entry.get("expiresAt") or entry.get("expires_at")):
        return Credential(
            "",
            CRED_NO_TOKEN,
            source,
            "Claude Code 登录仍有效，但钥匙串里的 access token 已过期"
            "（Claude CLI 刷新后通常不写回钥匙串）。QuotaX 将改用 Claude CLI 探测实时用量。",
            extra={
                "subscription_type": subscription_type,
                "rate_limit_tier": rate_limit_tier,
                "scopes": scopes,
            },
        )

    return Credential(
        token, CRED_OK, source,
        extra={"subscription_type": subscription_type, "rate_limit_tier": rate_limit_tier},
    )


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

    候选路径有两个（不同安装方式/平台落盘位置不同）。之前的实现里，只要第一个
    候选文件存在就 break 掉，不管里面有没有真的找到 oauth_token——如果这个文件
    存在但是空 {}、或者没有 oauth_token 字段（比如只装了 CLI 但还没登录，文件
    已经被创建出来），第二个候选路径即便真的有 token 也永远不会被尝试，被误判
    成"未登录"。正确逻辑应该是"这个候选文件存在但没找到可用 token，继续试下一个
    候选"，只有全部候选都试完仍找不到才返回 not_found。
    """
    candidates = [
        Path.home() / ".config" / "github-copilot" / "hosts.json",
        Path.home() / "Library" / "Application Support" / "github-copilot" / "hosts.json",
    ]
    last_error: Exception | None = None
    last_error_path: str | None = None
    for path in candidates:
        if not path.exists():
            continue
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            # 这个候选文件损坏/读不出，但后面可能还有正常文件——继续尝试，
            # 只在全部候选都失败时才报解析错误。记录出错的实际路径，避免
            # 循环结束后 `path` 被后面不存在的候选覆盖。
            last_error = e
            last_error_path = str(path)
            continue
        # entry 是 hosts.json 里每个 host（如 "github.com"）对应的值，key 本身
        # 用不到，只需要遍历 values（旧代码写的 `for key, entry in ...` 里 key
        # 从未被使用）。
        for entry in parsed.values() if isinstance(parsed, dict) else []:
            if isinstance(entry, dict) and entry.get("oauth_token"):
                extra = {}
                if entry.get("user"):
                    extra["user"] = entry["user"]
                return Credential(entry["oauth_token"], CRED_OK, str(path), extra=extra)
        # 这个候选文件存在，但没有找到可用 token——继续尝试下一个候选路径，
        # 不能在这里 break（旧 bug 正是在这里提前退出，漏掉了后面真正有 token
        # 的候选文件）。
    if last_error is not None:
        return Credential("", CRED_PARSE_ERROR, last_error_path or "", f"凭据 JSON 解析失败: {last_error}")
    return Credential(
        "",
        CRED_NOT_FOUND,
        "GitHub Copilot 未登录",
        "未找到 Copilot 登录凭据（请先安装并登录 Copilot）",
    )


# ── Cursor（IDE 本地登录态）─────────────────────────────────


def _jwt_payload(token: str) -> dict:
    """无验签地解出 JWT 的 payload 段（base64url）。只用来取 sub/email 等展示
    信息，不做任何安全判断——token 本身就是刚从本机 Cursor 数据库读出来的。"""
    import base64

    parts = token.split(".")
    if len(parts) < 2:
        return {}
    pad = "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(parts[1] + pad)
        parsed = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_cursor_credentials() -> Credential:
    """只读 Cursor 的本地登录态：state.vscdb（SQLite）ItemTable 里的
    cursorAuth/accessToken（JWT）。

    参考 CodExBar CursorAppAuth：macOS 在 ~/Library/Application Support/Cursor，
    Linux 在 ~/.config/Cursor。以只读 + immutable 模式打开，绝不写回（Cursor
    正在运行时 WAL 可能持有写锁，immutable 读避免互相干扰）。token 里的 sub
    （用户 ID）用于构造 cursor.com 的 Web 会话 Cookie。
    """
    import sqlite3

    home = Path.home()
    candidates = [
        home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb",
        home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb",
    ]
    for db_path in candidates:
        if not db_path.exists():
            continue
        token: str | None = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=2)
            try:
                row = conn.execute(
                    "SELECT value FROM ItemTable WHERE key = ? LIMIT 1",
                    ("cursorAuth/accessToken",),
                ).fetchone()
                if row and isinstance(row[0], str) and row[0].strip():
                    token = row[0].strip()
            finally:
                conn.close()
        except sqlite3.Error:
            # 数据库被锁 / 损坏：当作该候选不可用，继续下一个
            continue
        if not token:
            continue
        payload = _jwt_payload(token)
        sub = payload.get("sub")
        extra: dict = {}
        if sub:
            extra["sub"] = sub
        if payload.get("email"):
            extra["email"] = payload["email"]
        return Credential(token, CRED_OK, str(db_path), extra=extra)

    return Credential(
        "",
        CRED_NOT_FOUND,
        "Cursor 未登录",
        "未找到 Cursor 登录凭据（请安装并登录 Cursor 后重试探测）",
    )
