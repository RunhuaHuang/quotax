"""Optional shared-admin HTTP Basic authentication; remote access is denied by default."""
from __future__ import annotations

import base64
import binascii
import ipaddress
import os
import secrets
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse


def _local_peer(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "testclient"  # in-process Starlette transport, never an IP from a socket


def check_access(request: Request):
    if request.url.path == "/healthz":
        return None  # Liveness only; no paths, accounts, configuration or database details.
    password = os.environ.get("QUOTAX_ACCESS_PASSWORD", "")
    if password:
        valid = False
        try:
            scheme, encoded = request.headers.get("authorization", "").split(" ", 1)
            user, supplied = base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
            valid = (scheme.lower() == "basic"
                     and secrets.compare_digest(user.encode(), b"quotax")
                     and secrets.compare_digest(supplied.encode(), password.encode()))
        except (ValueError, UnicodeError, binascii.Error):
            pass
        if not valid:
            return JSONResponse({"detail": "请使用用户名 quotax 和访问密码登录"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="QuotaX", charset="UTF-8"',
                                         "Cache-Control": "no-store"})
    else:
        local_host = request.url.hostname in {"localhost", "127.0.0.1", "::1", "testserver"}
        peer = request.client.host if request.client else ""
        if not local_host or not _local_peer(peer):
            return JSONResponse({"detail": "远程访问需要设置 QUOTAX_ACCESS_PASSWORD，并使用 HTTPS 反向代理"},
                                status_code=403)
    # Basic credentials can be attached automatically by browsers. Validate Origin
    # for mutations (and reject cross-site fetches) to prevent authenticated CSRF.
    origin = request.headers.get("origin")
    cross_site = request.headers.get("sec-fetch-site") == "cross-site"
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if origin:
            parsed = urlsplit(origin)
            if parsed.netloc.lower() != request.headers.get("host", "").lower():
                cross_site = True
        if cross_site:
            return JSONResponse({"detail": "拒绝跨站修改请求"}, status_code=403)
    elif cross_site and request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "拒绝跨站 API 请求"}, status_code=403)
    return None
