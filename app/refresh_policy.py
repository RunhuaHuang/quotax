"""Per-query retry metadata, including Retry-After even when providers handle errors."""
from __future__ import annotations

import time
from contextvars import ContextVar
from datetime import UTC
from email.utils import parsedate_to_datetime

upstream_error: ContextVar[dict | None] = ContextVar("upstream_error", default=None)


def record_http_error(status: int, retry_after: str | None = None) -> None:
    previous = upstream_error.get() or {}
    # A provider can try several endpoints: never lose an observed rate limit.
    if previous.get("status") == 429 and status != 429:
        return
    retry_at = previous.get("retry_at") if previous.get("status") == status else None
    if retry_after:
        try:
            seconds = float(retry_after)
            if seconds >= 0 and seconds < float("inf"):
                retry_at = int((time.time() + seconds) * 1000)
        except (ValueError, OverflowError):
            try:
                date = parsedate_to_datetime(retry_after)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                retry_at = int(date.timestamp() * 1000)
            except (ValueError, TypeError, OverflowError):
                pass
    upstream_error.set({"status": status, "retry_at": retry_at})


def retry_delay_ms(status: str, failures: int, error: dict | None, now: int) -> int:
    error = error or {}
    if error.get("status") == 429:
        retry_at = error.get("retry_at")
        return max(1000, retry_at - now) if retry_at is not None else 900_000
    if status in {"expired", "not_found", "info"} or error.get("status") in {401, 403}:
        return 300_000
    return min(300_000, 30_000 * 2 ** min(max(failures - 1, 0), 4))
