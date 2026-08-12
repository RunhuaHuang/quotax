"""通知后端安全调用与空配置行为测试（不发送真实通知/Webhook）。"""

from __future__ import annotations

import subprocess

import httpx
import pytest

from app import notifications


def test_macos_desktop_command_uses_argv_and_escapes_applescript(monkeypatch):
    monkeypatch.setattr(notifications.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(notifications.shutil, "which", lambda name: f"/usr/bin/{name}")
    command = notifications._desktop_command('Quota "X"', "line1\nline2\\end")
    assert command[:2] == ["osascript", "-e"]
    assert '\\"X\\"' in command[2]
    assert "line1 line2\\\\end" in command[2]


@pytest.mark.asyncio
async def test_send_desktop_reports_success_without_shell(monkeypatch):
    monkeypatch.setattr(notifications, "_desktop_command", lambda _title, _message: ["fake-notifier", "hello"])
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(notifications.subprocess, "run", fake_run)
    result = await notifications.send_desktop("Title", "Body")
    assert result == {"backend": "desktop", "ok": True}
    assert captured["command"] == ["fake-notifier", "hello"]
    assert "shell" not in captured["kwargs"]


@pytest.mark.asyncio
async def test_dispatch_with_no_enabled_destinations_is_noop():
    assert await notifications.dispatch(
        {"desktop_notifications": False, "webhook_url": ""},
        {"title": "QuotaX", "message": "test"},
    ) == []


def test_linux_desktop_command_stops_option_parsing(monkeypatch):
    monkeypatch.setattr(notifications.platform, "system", lambda: "Linux")
    monkeypatch.setattr(notifications.shutil, "which", lambda _name: "/usr/bin/notify-send")
    command = notifications._desktop_command("--expire-time=0", "--help")
    assert command == [
        "notify-send",
        "--app-name=QuotaX",
        "--",
        "--expire-time=0",
        "--help",
    ]


@pytest.mark.asyncio
async def test_webhook_error_never_leaks_signed_url(monkeypatch):
    secret_url = "https://hooks.example.test/path?token=TOP-SECRET"

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **_kwargs):
            request = httpx.Request("POST", url)
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError(
                f"request failed for {url}", request=request, response=response
            )

    monkeypatch.setattr(notifications.httpx, "AsyncClient", FakeClient)
    result = await notifications.send_webhook(secret_url, {"type": "alert"})
    assert result["ok"] is False
    assert result["error"] == "HTTP 403 (HTTPStatusError)"
    assert "TOP-SECRET" not in str(result)


@pytest.mark.asyncio
async def test_dispatch_isolates_unexpected_backend_exception(monkeypatch):
    async def broken_desktop(_title, _message):
        raise RuntimeError("desktop exploded")

    async def successful_webhook(_url, _event):
        return {"backend": "webhook", "ok": True}

    monkeypatch.setattr(notifications, "send_desktop", broken_desktop)
    monkeypatch.setattr(notifications, "send_webhook", successful_webhook)
    results = await notifications.dispatch(
        {"desktop_notifications": True, "webhook_url": "https://example.test/hook"},
        {"title": "QuotaX", "message": "test"},
    )
    assert results == [
        {"backend": "notification", "ok": False, "error": "RuntimeError"},
        {"backend": "webhook", "ok": True},
    ]
