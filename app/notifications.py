"""后台监控通知后端。

桌面通知全部通过参数数组调用系统命令，不经过 shell；Webhook 使用短超时、禁止
自动重定向。调用方会把每个后端的成功/失败写进监控状态，单个通知失败不会让额度
采集失败。
"""

from __future__ import annotations

import asyncio
import platform
import shutil
import subprocess
from typing import Any

import httpx

from .config import assert_public_http_url


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").replace("\x00", "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _apple_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")


def _powershell_escape(value: str) -> str:
    return value.replace("'", "''").replace("\r", " ").replace("\n", " ")


def _desktop_command(title: str, message: str) -> list[str] | None:
    system = platform.system()
    if system == "Darwin" and shutil.which("osascript"):
        script = (
            f'display notification "{_apple_escape(message)}" '
            f'with title "{_apple_escape(title)}"'
        )
        return ["osascript", "-e", script]
    if system == "Linux" and shutil.which("notify-send"):
        return ["notify-send", "--app-name=QuotaX", "--", title, message]
    if system == "Windows":
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell:
            safe_title = _powershell_escape(title)
            safe_message = _powershell_escape(message)
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$n=New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon=[System.Drawing.SystemIcons]::Warning; "
                "$n.Visible=$true; "
                f"$n.ShowBalloonTip(8000,'{safe_title}','{safe_message}',"
                "[System.Windows.Forms.ToolTipIcon]::Warning); Start-Sleep -Milliseconds 8500; $n.Dispose()"
            )
            return [powershell, "-NoProfile", "-NonInteractive", "-Command", script]
    return None


async def send_desktop(title: str, message: str) -> dict:
    """发送本机桌面通知；平台不支持时返回 unavailable，而不是抛异常。"""
    title = _clip(title, 120)
    message = _clip(message, 500)
    command = _desktop_command(title, message)
    if command is None:
        return {"backend": "desktop", "ok": False, "error": "当前系统没有可用的桌面通知后端"}

    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(command, capture_output=True, text=True, timeout=12, check=False)

    try:
        completed = await asyncio.to_thread(_run)
    except (OSError, subprocess.SubprocessError) as e:
        return {"backend": "desktop", "ok": False, "error": _clip(e, 300)}
    if completed.returncode != 0:
        error = completed.stderr.strip() or completed.stdout.strip() or f"退出码 {completed.returncode}"
        return {"backend": "desktop", "ok": False, "error": _clip(error, 300)}
    return {"backend": "desktop", "ok": True}


async def send_webhook(url: str, event: dict) -> dict:
    """POST 标准 JSON 告警事件。Webhook URL 可能含签名，结果中永不回显 URL。"""
    payload = {"source": "QuotaX", "version": 1, "event": event}
    try:
        assert_public_http_url(url, field_name="Webhook URL")
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(url, json=payload, headers={"User-Agent": "QuotaX/0.1"})
        response.raise_for_status()
    except ValueError as e:
        return {"backend": "webhook", "ok": False, "error": str(e)}
    except httpx.HTTPStatusError as e:
        # httpx 异常字符串可能包含完整的签名 URL；状态里只记录状态码和异常类型。
        return {
            "backend": "webhook",
            "ok": False,
            "error": f"HTTP {e.response.status_code} ({type(e).__name__})",
        }
    except httpx.HTTPError as e:
        return {"backend": "webhook", "ok": False, "error": type(e).__name__}
    return {"backend": "webhook", "ok": True, "status_code": response.status_code}


async def dispatch(settings: dict, event: dict) -> list[dict]:
    """向已启用的通知后端并行分发一个告警/恢复事件。"""
    title = event.get("title") or "QuotaX 额度提醒"
    message = event.get("message") or "额度状态发生变化"
    tasks = []
    if settings.get("desktop_notifications"):
        tasks.append(send_desktop(title, message))
    webhook_url = str(settings.get("webhook_url") or "").strip()
    if webhook_url:
        tasks.append(send_webhook(webhook_url, event))
    if not tasks:
        return []
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    results = []
    for result in raw_results:
        if isinstance(result, BaseException):
            results.append(
                {"backend": "notification", "ok": False, "error": type(result).__name__}
            )
        else:
            results.append(result)
    return results
