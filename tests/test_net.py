"""app/net.py 的 friendly_error 单测：底层网络异常要翻译成用户能看懂的中文。

回归背景：DNS 解析失败时 httpx 会抛 "[Errno 8] nodename nor servname provided,
or not known" 这种纯英文技术串，直接当错误文案展示给用户完全看不懂——必须
翻译成「无法解析服务器域名」这类可行动的提示。
"""

from __future__ import annotations

import socket
import urllib.request

import httpx

from app.net import friendly_error


def test_dns_error_translated():
    err = socket.gaierror(8, "nodename nor servname provided, or not known")
    assert "DNS" in friendly_error(err)
    assert "Errno" not in friendly_error(err)


def test_httpx_connect_error_wrapping_dns_error():
    # httpx 通常把 DNS 失败包装成 ConnectError，cause 是原始 gaierror
    err = socket.gaierror(8, "nodename nor servname provided, or not known")
    wrapped = httpx.ConnectError(str(err))
    wrapped.__cause__ = err
    msg = friendly_error(wrapped)
    assert "DNS" in msg
    assert "Errno" not in msg


def test_connect_timeout_translated():
    msg = friendly_error(httpx.ConnectTimeout("connect timed out"))
    assert "超时" in msg


def test_read_timeout_translated():
    msg = friendly_error(httpx.ReadTimeout("read timed out"))
    assert "超时" in msg


def test_proxy_error_translated():
    msg = friendly_error(httpx.ProxyError("proxy failed"))
    assert "代理" in msg


def test_plain_connect_error_translated():
    msg = friendly_error(httpx.ConnectError("connection refused"))
    assert "无法连接" in msg


def test_ssl_error_translated():
    import ssl

    err = ssl.SSLError(1, "UNEXPECTED_EOF_WHILE_READING")
    wrapped = httpx.ConnectError(str(err))
    wrapped.__cause__ = err
    msg = friendly_error(wrapped)
    assert "TLS" in msg
    assert "代理" in msg  # 提示可尝试开启代理


def test_system_proxy_reads_macos_settings(monkeypatch):
    from app import net

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "127.0.0.1:7890"})
    assert net._system_proxy() == "http://127.0.0.1:7890"  # 无 scheme 时补全

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "http://127.0.0.1:7890"})
    assert net._system_proxy() == "http://127.0.0.1:7890"  # 已有 scheme 不重复

    monkeypatch.setattr(urllib.request, "getproxies", dict)
    assert net._system_proxy() is None  # 无代理时不配置，行为与之前一致


def test_unknown_error_passthrough():
    assert friendly_error(ValueError("自定义错误")) == "自定义错误"
    assert friendly_error(RuntimeError()) == "RuntimeError"
