"""Claude Code PTY 用量探测：用伪终端启动 claude CLI，发 /usage，解析终端文本。

为什么需要这个模块：macOS 新版 Claude Code 把 OAuth token 存进 Keychain，但
accessToken 是空字符串（只留 subscriptionType 等元信息），第三方读不到明文
token，无法直接调 api.anthropic.com/api/oauth/usage。但 Claude CLI 自己有
Keychain 访问权限——它在交互式终端里能正常显示用量。所以我们用 PTY 模拟终端，
让 claude 自己执行 /usage 把用量面板「画」出来，再解析终端文本拿到百分比。

参考 steipete/CodexBar 的 ClaudeStatusProbe：
- openpty 创建伪终端，窗口 50×160
- 启动参数 --allowed-tools "" --strict-mcp-config（避免触发 MCP / 工具调用）
- 清理 ANTHROPIC_* 环境变量、设 DISABLE_AUTOUPDATER=1
- 等 CLI 初始化后发送 /usage + 回车
- 收集输出，剥离 ANSI 转义码，正则提取 "Current session" / "Current week" 标签后的百分比

所有函数都是同步阻塞 I/O（PTY + subprocess），调用方需用 asyncio.to_thread 包裹。
仅在 macOS（有 claude CLI + Keychain）下可用；其他平台返回 None。
"""

from __future__ import annotations

import os
import re
import select
import signal
import subprocess
import time
from dataclasses import dataclass


@dataclass
class ClaudePTYUsage:
    """PTY /usage 解析结果。百分比值为「剩余百分比」（与 QuotaWindow.remaining_percent 语义一致）。"""

    session_percent_left: int | None = None
    weekly_percent_left: int | None = None
    weekly_opus_percent_left: int | None = None
    session_reset_text: str | None = None
    weekly_reset_text: str | None = None
    raw_text: str = ""  # 原始输出（调试用）


# ANSI 转义码剥离：颜色、光标移动、清屏等。PTY 输出满是这些，不剥离无法正则匹配。
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[=>]|\x1b\][^\x07]*\x07|\r")
# 百分比数字：匹配 "45%" / "45.0%" / " 45 %" / "42%used"
_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


def strip_ansi(text: str) -> str:
    """剥离 PTY 输出里的 ANSI 转义码和回车符。"""
    return _ANSI_RE.sub("", text)


def _normalize_for_label(text: str) -> str:
    """小写 + 只保留字母数字，用于容忍 TUI 重绘时丢失空格/字符的标签匹配。

    Claude 的 /usage 是重绘式 TUI，同一行可能被捕获成 "Curret session"（丢 n）
    或 "Currentweek(allmodels)"（丢空格）——普通 `in` 匹配会漏掉。
    """
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein 编辑距离（输入是短标签 token，朴素单行实现足够快）。"""
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _fuzzy_contains(haystack: str, needle: str, max_dist: int = 2) -> bool:
    """needle 是否以 ≤ max_dist 编辑距离出现在 haystack 中（滑动窗口）。"""
    n = len(needle)
    if n == 0:
        return False
    for i in range(len(haystack) - n + 1):
        if _edit_distance(haystack[i : i + n], needle) <= max_dist:
            return True
    return False


def _label_matches(line: str, label: str) -> bool:
    """标签匹配：先精确（去空格小写），再容忍 1~2 字符错位的模糊匹配。

    参考 CodexBar 的 normalizedForLabelSearch + editDistance：真实捕获里
    "Current session" 会被重绘成 "Curretsession"（丢 n），精确匹配会漏。
    """
    norm_line = _normalize_for_label(line)
    norm_label = _normalize_for_label(label)
    if not norm_label:
        return False
    if norm_label in norm_line:
        return True
    if len(norm_label) >= 6:
        return _fuzzy_contains(norm_line, norm_label, max_dist=2)
    return False


def _percent_left_from_line(line: str) -> int | None:
    """从一行提取百分比，并按 used/remaining 语义返回「剩余百分比」。

    Claude 2.x 显示 "X% used"（剩余 = 100 - X）；旧版显示 "X% remaining/left"
    （剩余 = X）。语义不明（如状态栏的 0%）时返回 None，宁可丢弃也不猜。
    """
    m = _PERCENT_RE.search(line)
    if not m:
        return None
    try:
        pct = int(float(m.group(1)))
    except ValueError:
        return None
    pct = max(0, min(100, pct))
    lower = line.lower()
    if any(k in lower for k in ("used", "spent", "consumed")):
        return 100 - pct
    if any(k in lower for k in ("left", "remaining", "available")):
        return pct
    return None


def _find_percent_after_label(lines: list[str], label: str, window: int = 14) -> int | None:
    """在 label 所在行向下 window 行内找第一个「剩余百分比」。

    Claude 的 /usage 面板形如：
        Current session
        45% remaining · resets in 3h 22m   （旧版）
    或（2.x）：
        Curretsession0%used                 （TUI 重绘打乱 + used 语义）
    标签和百分比可能不在同一行，且标签可能被重绘打乱，所以要模糊匹配 + 扫窗口。
    """
    for i, line in enumerate(lines):
        if _label_matches(line, label):
            for candidate in lines[i : i + window]:
                pct = _percent_left_from_line(candidate)
                if pct is not None:
                    return pct
            break
    return None


def _all_percent_left(lines: list[str]) -> list[int]:
    """按出现顺序收集所有「剩余百分比」，用于标签被打乱时的顺序兜底。"""
    out = []
    for line in lines:
        pct = _percent_left_from_line(line)
        if pct is not None:
            out.append(pct)
    return out


def _find_reset_after_label(lines: list[str], label: str, window: int = 14) -> str | None:
    """在 label 附近找重置时间文案（如 "resets in 3h 22m" / "Resets Aug 22 at 6:59pm"）。"""
    reset_re = re.compile(r"(resets?\s+(?:in\s+)?[^.\n]+)", re.IGNORECASE)
    for i, line in enumerate(lines):
        if _label_matches(line, label):
            for candidate in lines[i : i + window]:
                m = reset_re.search(candidate)
                if m:
                    return m.group(1).strip()
            break
    return None


def parse_usage_text(text: str) -> ClaudePTYUsage:
    """解析 claude CLI /usage 的终端文本输出。

    输入是已剥离 ANSI 的纯文本。返回 ClaudePTYUsage，百分比统一为「剩余」语义：
    Claude 2.x 的 "X% used" 会换算成 100 - X，旧版 "X% remaining" 直接取 X。
    如果文本里没有订阅用量标签（如 CLI 未登录，只有本地 session 统计），
    返回的各字段均为 None——调用方据此判断 PTY 路径不可用。
    """
    lines = text.split("\n")
    result = ClaudePTYUsage(raw_text=text)

    # 订阅用量面板的关键标签（出现这些说明 CLI 已登录、返回了真实额度）
    result.session_percent_left = _find_percent_after_label(lines, "Current session")
    if result.session_percent_left is None:
        # 顺序兜底：标签被 TUI 重绘彻底打乱时，真实用量面板里第一个百分比即
        # session（参考 CodexBar 的 allPercents 顺序回退；仅在确认处于用量面板
        # 内才启用，避免把状态栏 "0%" 误当 session）。
        norm = _normalize_for_label(text)
        if "currentsession" in norm or "currentweek" in norm:
            ordered = _all_percent_left(lines)
            if ordered:
                result.session_percent_left = ordered[0]

    result.weekly_percent_left = _find_percent_after_label(lines, "Current week (all models)")

    # 分模型周额度（Opus / Sonnet）——标签可能是任意一种
    for opus_label in ("Current week (Opus)", "Current week (Sonnet only)", "Current week (Sonnet)"):
        pct = _find_percent_after_label(lines, opus_label)
        if pct is not None:
            result.weekly_opus_percent_left = pct
            break

    result.session_reset_text = _find_reset_after_label(lines, "Current session")
    result.weekly_reset_text = _find_reset_after_label(lines, "Current week")

    return result


def is_claude_cli_available() -> bool:
    """检测 claude CLI 是否安装且可执行。"""
    from shutil import which

    return which("claude") is not None


def fetch_usage_via_pty(timeout: float = 20) -> ClaudePTYUsage | None:
    """用 PTY 启动 claude CLI，发送 /usage，解析终端输出。

    返回 ClaudePTYUsage（即使没解析到订阅用量也会返回，各字段为 None）；
    claude CLI 不存在或 PTY 启动失败时返回 None。

    流程（对照 CodexBar ClaudeStatusProbe）：
    1. openpty 创建伪终端（50×160）
    2. 启动 claude --allowed-tools "" --strict-mcp-config（不触发 MCP/工具）
    3. 等 3 秒让 CLI 初始化（TUI 启动需要时间，太早发命令会被丢弃）
    4. 发送 /usage + 回车；再过 2 秒发回车促发面板渲染
    5. 收集输出直到超时或收集到足够数据
    6. 杀进程组清理，剥离 ANSI，解析百分比
    """
    if not is_claude_cli_available():
        return None

    # pty / fcntl / termios 都是 Unix 专属模块，Windows 上 import 直接抛
    # ModuleNotFoundError。模块 docstring 承诺"其他平台返回 None"，所以必须在
    # 函数内 import 并在 ImportError 时优雅降级——模块级 import 会让 Windows
    # 用户连导入本模块都直接崩溃，和承诺行为不符。
    try:
        import fcntl
        import pty
        import struct
        import termios
    except ImportError:
        return None

    try:
        master, slave = pty.openpty()
    except (OSError, AttributeError):
        return None

    # 设窗口大小 50 行 × 160 列（足够渲染用量面板，和 CodexBar 一致）
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
    except OSError:
        pass

    # 清理环境：去掉 ANTHROPIC_ 前缀变量（避免干扰 CLI 自己的认证逻辑），
    # 关闭自动更新和遥测（探测不该触发这些副作用）。
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    env["DISABLE_AUTOUPDATER"] = "1"
    env["DISABLE_TELEMETRY"] = "1"

    try:
        # start_new_session=True 在子进程里调 setsid()，创建新进程组——
        # 线程安全（preexec_fn 在多线程下不安全），且能确保后续 killpg 干净杀掉
        # claude 及其 spawn 的子进程。
        proc = subprocess.Popen(
            ["claude", "--allowed-tools", "", "--strict-mcp-config"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
        )
    except (OSError, FileNotFoundError):
        try:
            os.close(slave)
            os.close(master)
        except OSError:
            pass
        return None
    finally:
        try:
            os.close(slave)
        except OSError:
            pass

    output = b""
    start = time.time()
    sent_usage = False
    sent_enter = False

    try:
        while time.time() - start < timeout:
            r, _, _ = select.select([master], [], [], 0.3)
            if r:
                try:
                    data = os.read(master, 65536)
                    if data:
                        output += data
                    else:
                        break
                except OSError:
                    break

            elapsed = time.time() - start
            # 3 秒后发 /usage（TUI 初始化需要时间，太早发会被丢弃）
            if not sent_usage and elapsed > 3:
                try:
                    os.write(master, b"/usage\r")
                    sent_usage = True
                except OSError:
                    break
            # /usage 后 2 秒发回车（促发面板渲染——某些版本需要额外确认）
            if sent_usage and not sent_enter and elapsed > 5:
                try:
                    os.write(master, b"\r")
                    sent_enter = True
                except OSError:
                    break
            # 收集到订阅用量标签后可以提前结束（不用等满超时）
            if sent_enter and elapsed > 7:
                text_check = strip_ansi(output.decode("utf-8", errors="replace"))
                if "Current session" in text_check or "Current week" in text_check:
                    # 再等最多 0.5 秒收面板尾部数据。必须 select 等到可读再读：
                    # 面板渲染完成后 claude TUI 静止等待用户输入，PTY 缓冲区不会
                    # 再有数据，此刻裸 os.read 会永久阻塞在系统调用上——而清理
                    # 用的 killpg 在 finally 里、位于这次 read 之后，永远执行不到，
                    # 查询线程与 claude 子进程会一起挂死，拖住整个 /api/quotas。
                    r, _, _ = select.select([master], [], [], 0.5)
                    if r:
                        try:
                            output += os.read(master, 65536)
                        except OSError:
                            pass
                    break
    finally:
        # 杀整个进程组（claude 可能 spawn 子进程）
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        try:
            os.close(master)
        except OSError:
            pass

    text = strip_ansi(output.decode("utf-8", errors="replace"))
    return parse_usage_text(text)
