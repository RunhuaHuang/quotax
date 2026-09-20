"""HTTP 网络层：全局 httpx 客户端 + 超时 + 响应读取。"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import threading
import urllib.request

import httpx

from .config import assert_public_http_url_async

TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_client: httpx.AsyncClient | None = None
_client_proxy: str | None = None  # 创建当前 _client 时生效的系统代理
_client_lock = threading.Lock()


def _system_proxy() -> str | None:
    """读取 macOS 系统代理设置（用户开了 ClashX / Clash 等代理工具后生效）。

    直连 ChatGPT / Anthropic / Google 等境外接口时，本机 TLS 握手可能被网络
    环境干扰而失败；走系统代理（代理工具通常自带分流规则，国内接口直连）是
    标准解法。urllib.request.getproxies() 在 macOS 上读系统偏好设置，返回的
    值可能不带 scheme，补全后交给 httpx。
    """
    proxies = urllib.request.getproxies()
    url = proxies.get("https") or proxies.get("http")
    if not url:
        return None
    if "://" not in url:
        url = f"http://{url}"
    return url


def friendly_error(e: Exception) -> str:
    """把底层网络异常翻译成用户能看懂的中文提示。

    provider 查询函数的兜底逻辑直接拿 str(e) 当错误文案，而 httpx 抛出的底层
    异常（如 socket.gaierror）是 "[Errno 8] nodename nor servname provided,
    or not known" 这种纯英文技术串——用户完全看不懂，也无从判断该做什么。
    这里把最常见的几类网络错误统一翻译，其余异常原样返回。
    """
    # httpx 的 ConnectError 会包装原始异常（DNS 失败时 cause 是 socket.gaierror），
    # 先看 cause 能给出更精确的提示
    cause = getattr(e, "__cause__", None) or e
    if isinstance(cause, socket.gaierror):
        return "网络错误：无法解析服务器域名（DNS 解析失败），请检查网络连接后重试"
    if isinstance(cause, ssl.SSLError):
        return "网络错误：与服务器建立安全连接失败（TLS 握手被中断，可能是网络环境限制，可尝试开启系统代理后重试）"
    if isinstance(e, httpx.ConnectTimeout):
        return "网络错误：连接服务器超时，请检查网络后重试"
    if isinstance(e, httpx.ReadTimeout):
        return "网络错误：读取响应超时（上游响应太慢），请稍后重试"
    if isinstance(e, httpx.ProxyError):
        return "网络错误：代理连接失败，请检查系统代理设置"
    if isinstance(e, httpx.ConnectError):
        return "网络错误：无法连接到服务器，请检查网络/DNS 后重试"
    return str(e) or e.__class__.__name__


def get_client() -> httpx.AsyncClient:
    # 系统代理与创建时不一致（用户开/关了代理工具，或从系统代理切到 TUN 模式）
    # 必须重建 client：旧 client 固化的代理端口可能已不再监听，继续复用会让
    # 所有渠道的请求都以 ConnectError 失败——包括境内在配置上本可直连的接口。
    if _client is None or _client.is_closed or _client_proxy != _system_proxy():
        with _client_lock:
            # 双重检查：避免多个协程同时进入时创建出多个 client
            if _client is None or _client.is_closed or _client_proxy != _system_proxy():
                _rebuild_client_locked()
    return _client


def _rebuild_client_locked() -> None:
    """按当前系统代理重建全局 client（调用方必须已持有 _client_lock）。"""
    global _client, _client_proxy
    old = _client
    if old is not None and not old.is_closed:
        # AsyncClient 只能异步关闭。get_client 只在事件循环线程内被调用，把
        # 旧 client 的收尾丢回循环执行；万一没有运行中的循环就交给 GC。
        try:
            asyncio.get_running_loop().create_task(old.aclose())
        except RuntimeError:
            pass
    _client = None  # 构造中途抛异常时，下次调用会重新走创建路径
    kwargs: dict = {
        "timeout": httpx.Timeout(TIMEOUT_SECONDS),
        # 默认不跟随重定向：base_url 是用户自填的（new-api/one-api 中转站、
        # Kimi API、ZenMux 等），如果开着 follow_redirects，一个恶意或配置
        # 错误的 base_url 可以 3xx 跳转到任意主机，httpx 会把 Authorization
        # 头也带过去——相当于把用户的 API Key 泄露给跳转目标。所有渠道的
        # 官方接口地址都是写死的 https 直连域名，本来就不需要重定向。
        "follow_redirects": False,
        "headers": {"User-Agent": "quota-board/1.0"},
    }
    # 系统代理：用户开代理工具后，境外接口（chatgpt.com 等）的 TLS 握手可能
    # 被网络环境干扰，走系统代理可恢复；没有代理时与之前行为完全一致。
    proxy = _system_proxy()
    if proxy:
        kwargs["proxy"] = proxy
    _client = httpx.AsyncClient(**kwargs)
    _client_proxy = proxy


async def aclose() -> None:
    # 只读取模块级 _client（不做重新赋值），无需 global 声明
    if _client is not None and not _client.is_closed:
        await _client.aclose()


class ResponseError(Exception):
    """带 HTTP 状态码的请求错误（确定性的 4xx/5xx）。"""

    def __init__(self, status: int, body: str = ""):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


class ParseError(Exception):
    """响应体 JSON 解析失败。"""


async def request_json(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    json_body: dict | None = None,
    form_body: dict | None = None,
) -> dict | list:
    """发送请求并解析 JSON；非 2xx 抛 ResponseError，JSON 非法抛 ParseError。

    json_body 与 form_body 互斥：前者发 JSON，后者发 application/x-www-form-
    urlencoded（阿里云 OneConsole 网关等要求表单编码的接口用）。
    """
    await assert_public_http_url_async(url, field_name="请求 URL")
    client = get_client()
    status_code, text = await _request_text_bounded(
        client, method, url, headers=headers, json_body=json_body, form_body=form_body
    )
    if status_code < 200 or status_code >= 300:
        raise ResponseError(status_code, text[:500])
    try:
        return json.loads(text)
    except ValueError as e:
        raise ParseError(f"响应不是合法 JSON: {e}") from e


async def request_text(
    method: str, url: str, *, headers: dict | None = None, json_body: dict | None = None
) -> str:
    """发送请求并返回文本；非 2xx 抛 ResponseError。"""
    await assert_public_http_url_async(url, field_name="请求 URL")
    client = get_client()
    status_code, text = await _request_text_bounded(client, method, url, headers=headers, json_body=json_body)
    if status_code < 200 or status_code >= 300:
        raise ResponseError(status_code, text[:500])
    return text


async def _request_text_bounded(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict | None,
    json_body: dict | None,
    form_body: dict | None = None,
) -> tuple[int, str]:
    """流式读取响应并在超过上限时立即中止。

    ``AsyncClient.request()`` 会先把完整响应读入内存，之后再检查长度；恶意或异常
    上游返回超大 HTML/JSON 时，事后检查仍可能造成内存峰值。使用 stream + bytearray
    把峰值限制在 ``MAX_RESPONSE_BYTES`` 附近，再交给上层做状态码/JSON 处理。
    """
    async with client.stream(
        method, url, headers=headers, json=json_body, data=form_body
    ) as response:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                raise ResponseError(response.status_code, f"响应体过大（超过 {MAX_RESPONSE_BYTES} 字节）")
            body.extend(chunk)
        encoding = response.encoding or "utf-8"
        return response.status_code, bytes(body).decode(encoding, errors="replace")
