"""HTTP 网络层：全局 httpx 客户端 + 超时 + 响应读取。"""

from __future__ import annotations

import socket
import ssl
import urllib.request

import httpx

TIMEOUT_SECONDS = 15.0

_client: httpx.AsyncClient | None = None


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
    global _client
    if _client is None or _client.is_closed:
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
    return _client


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
    method: str, url: str, *, headers: dict | None = None, json_body: dict | None = None
) -> dict | list:
    """发送请求并解析 JSON；非 2xx 抛 ResponseError，JSON 非法抛 ParseError。"""
    client = get_client()
    response = await client.request(method, url, headers=headers, json=json_body)
    if response.status_code < 200 or response.status_code >= 300:
        raise ResponseError(response.status_code, response.text[:500])
    try:
        return response.json()
    except ValueError as e:
        raise ParseError(f"响应不是合法 JSON: {e}") from e


async def request_text(method: str, url: str, *, headers: dict | None = None, json_body: dict | None = None) -> str:
    """发送请求并返回文本；非 2xx 抛 ResponseError。"""
    client = get_client()
    response = await client.request(method, url, headers=headers, json=json_body)
    if response.status_code < 200 or response.status_code >= 300:
        raise ResponseError(response.status_code, response.text[:500])
    return response.text
