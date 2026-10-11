"""Refresh behavior tests use fake providers and an isolated clock; no real accounts."""
import asyncio
from datetime import UTC, datetime
from email.utils import format_datetime

import pytest

from app import main as m
from app.config import Channel
from app.models import fail, ok, window
from app.refresh_policy import record_http_error, retry_delay_ms, upstream_error


@pytest.fixture
def setup_refresh(monkeypatch, isolated_config):
    m._cache.clear()
    m._inflight.clear()
    monkeypatch.setattr(m.config_store, "HISTORY_DIR", isolated_config.parent / "history")
    clock = [1_000_000.0]
    monkeypatch.setattr(m.time, "time", lambda: clock[0])
    channel = Channel(id="a", type="deepseek", name="A")
    return channel, clock


def success(c, value=80):
    return ok(id=c.id, type=c.type, name=c.name, category="balance",
              windows=[window("quota", "quota", remaining_percent=value)])


def failure(c):
    return fail("error", "offline", id=c.id, type=c.type, name=c.name, category="balance")


async def test_short_refresh_interval_actually_queries_when_due(setup_refresh, monkeypatch):
    c, clock = setup_refresh
    calls = []

    async def query(c):
        calls.append(c.id)
        return success(c)

    monkeypatch.setattr(m, "query_channel", query)
    await m._get_channel_result(c, False, 30)
    clock[0] += 29
    assert (await m._get_channel_result(c, False, 30))[1]
    clock[0] += 1
    assert not (await m._get_channel_result(c, False, 30))[1]
    assert len(calls) == 2


async def test_failure_keeps_good_values_backs_off_and_recovers(setup_refresh, monkeypatch):
    c, clock = setup_refresh
    responses = [success(c), failure(c), failure(c), success(c, 50)]

    async def query(_):
        return responses.pop(0)

    monkeypatch.setattr(m, "query_channel", query)
    good, _ = await m._get_channel_result(c, True)
    clock[0] += 1
    stale, _ = await m._get_channel_result(c, True)
    assert stale["status"] == "stale"
    assert stale["updated_at"] == good["updated_at"]
    assert stale["windows"] == good["windows"]
    assert stale["refresh"]["next_retry_at"] == clock[0] * 1000 + 30_000
    clock[0] += 29
    assert (await m._get_channel_result(c, False))[1]
    clock[0] += 1
    stale, _ = await m._get_channel_result(c, False)
    assert stale["refresh"]["next_retry_at"] == clock[0] * 1000 + 60_000
    clock[0] += 60
    recovered, _ = await m._get_channel_result(c, False)
    assert recovered["status"] == "ok"
    assert recovered["refresh"]["failures"] == 0
    assert recovered["windows"][0]["remaining_percent"] == 50


async def test_429_deadline_cannot_be_bypassed_by_manual_refresh(setup_refresh, monkeypatch):
    c, clock = setup_refresh
    count = 0

    async def query(c):
        nonlocal count
        count += 1
        record_http_error(429, "120")
        return failure(c)

    monkeypatch.setattr(m, "query_channel", query)
    result, _ = await m._get_channel_result(c, True)
    assert result["refresh"]["upstream_status"] == 429
    clock[0] += 119
    assert (await m._get_channel_result(c, True))[1]
    assert count == 1
    clock[0] += 1
    assert not (await m._get_channel_result(c, True))[1]
    assert count == 2


async def test_total_channel_timeout_releases_inflight(setup_refresh, monkeypatch):
    c, _ = setup_refresh

    async def never(_):
        await asyncio.Event().wait()

    monkeypatch.setattr(m, "query_channel", never)
    monkeypatch.setattr(m, "CHANNEL_TIMEOUT_SECONDS", .01)
    result, _ = await m._get_channel_result(c, False)
    assert result["status"] == "error"
    assert "超时" in result["message"]
    assert c.id not in m._inflight


async def test_concurrent_callers_share_query_even_after_one_disconnects(setup_refresh, monkeypatch):
    c, _ = setup_refresh
    started, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def query(c):
        nonlocal count
        count += 1
        started.set()
        await release.wait()
        return success(c)

    monkeypatch.setattr(m, "query_channel", query)
    a = asyncio.create_task(m._get_channel_result(c, True))
    await started.wait()
    b = asyncio.create_task(m._get_channel_result(c, True))
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    release.set()
    assert (await b)[0]["status"] == "ok"
    assert count == 1


def test_retry_after_http_date_and_invalid_header():
    token = upstream_error.set(None)
    try:
        future = datetime.now(UTC).timestamp() + 120
        record_http_error(429, format_datetime(datetime.fromtimestamp(future, UTC)))
        assert abs(upstream_error.get()["retry_at"] - future * 1000) < 1000
        upstream_error.set(None)
        record_http_error(429, "invalid")
        assert retry_delay_ms("error", 1, upstream_error.get(), 0) == 900_000
    finally:
        upstream_error.reset(token)


async def test_monitor_uses_configured_interval(monkeypatch):
    observed = []
    monkeypatch.setattr(m.config_store, "get_settings", lambda: {"monitor": {"interval_seconds": 60}})

    async def quotas(**kwargs):
        observed.append(kwargs)
        return {"channels": []}

    monkeypatch.setattr(m, "quotas", quotas)
    await m._monitor_query_results(False)
    assert observed == [{"force": False, "ids": None, "max_age_seconds": 60}]


async def test_usage_collection_callers_share_one_scan(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def fake_thread(fn):
        nonlocal count
        count += 1
        started.set()
        await release.wait()
        return {"total_new": 3}

    monkeypatch.setattr(m, "_usage_collection_task", None)
    monkeypatch.setattr(m.asyncio, "to_thread", fake_thread)
    a = asyncio.create_task(m._collect_usage_shared())
    await started.wait()
    b = asyncio.create_task(m._collect_usage_shared())
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    release.set()
    assert await b == {"total_new": 3}
    assert count == 1


async def test_global_provider_concurrency_is_bounded(setup_refresh, monkeypatch):
    active = peak = 0

    async def query(c):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(.01)
        active -= 1
        return success(c)

    monkeypatch.setattr(m, "query_channel", query)
    channels = [Channel(id=str(i), type="deepseek", name=str(i)) for i in range(10)]
    await asyncio.gather(*(m._get_channel_result(c, True) for c in channels))
    assert peak == m.MAX_QUERY_CONCURRENCY


async def test_stale_values_do_not_resolve_an_active_alert(isolated_config):
    from app.monitoring import MonitorService
    service = MonitorService(lambda _: None)
    service._state["alerts"] = {"a:quota": {"active": True, "channel_id": "a", "channel_type": "deepseek"}}
    result = success(Channel(id="a", type="deepseek", name="a"), 90).to_dict()
    result["status"] = "stale"
    summary = await service._evaluate([result], {"thresholds": {"a": 20}}, 1_000_000, "test")
    assert summary["evaluated_channels"] == 0
    assert summary["recoveries"] == 0
    assert summary["active_alerts"] == 1


async def test_retry_after_survives_network_and_provider_error_handling(monkeypatch):
    import httpx

    from app import net

    token = upstream_error.set(None)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "120"})))

    async def allow(*args, **kwargs):
        return None

    monkeypatch.setattr(net, "assert_public_http_url_async", allow)
    monkeypatch.setattr(net, "get_client", lambda: client)
    try:
        with pytest.raises(net.ResponseError):
            await net.request_json("GET", "https://example.test")
        info = upstream_error.get()
        assert info["status"] == 429
        assert 118_000 < info["retry_at"] - m.time.time() * 1000 <= 120_000
    finally:
        await client.aclose()
        upstream_error.reset(token)


async def test_success_with_partial_429_retries_at_deadline_not_normal_ttl(setup_refresh, monkeypatch):
    c, clock = setup_refresh
    calls = 0

    async def query(c):
        nonlocal calls
        calls += 1
        if calls == 1:
            record_http_error(429, "30")
        return success(c)

    monkeypatch.setattr(m, "query_channel", query)
    await m._get_channel_result(c, False, 300)
    clock[0] += 30
    _, cached = await m._get_channel_result(c, False, 300)
    assert not cached
    assert calls == 2


async def test_shutdown_cancels_superseded_shared_queries(setup_refresh, monkeypatch):
    c, _ = setup_refresh
    started = asyncio.Queue()
    cancelled = []

    async def query(c):
        task = asyncio.current_task()
        started.put_nowait(task)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(task)

    monkeypatch.setattr(m, "query_channel", query)
    old = asyncio.create_task(m._get_channel_result(c, True))
    old_query = await started.get()
    await m._invalidate_cache(c.id)
    new = asyncio.create_task(m._get_channel_result(c, True))
    new_query = await started.get()
    try:
        await m._cancel_inflight_queries()
        assert old_query.cancelled()
        assert new_query.cancelled()
        assert set(cancelled) == {old_query, new_query}
    finally:
        for task in (old_query, new_query, old, new):
            task.cancel()
        await asyncio.gather(old_query, new_query, old, new, return_exceptions=True)


async def test_shutdown_drains_collection_before_closing_database(setup_refresh, monkeypatch):
    import threading

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    closed = []

    def collect():
        started.set()
        assert release.wait(3)
        finished.set()
        return {}

    async def noop():
        pass

    def close():
        closed.append(finished.is_set())

    monkeypatch.setattr(m, "_usage_collection_task", None)
    monkeypatch.setattr(m, "_auto_detect_subscription_channels", dict)
    monkeypatch.setattr(m.usage_collect, "collect_all", collect)
    monkeypatch.setattr(m.usage_store, "close", close)
    monkeypatch.setattr(m.usage_store, "cleanup_old_records", lambda _: {})
    monkeypatch.setattr(m.config_store, "get_settings", lambda: {"monitor": {"retention_days": 365}})
    monkeypatch.setattr(m.monitor_service, "start", noop)
    monkeypatch.setattr(m.monitor_service, "stop", noop)
    monkeypatch.setattr(m, "aclose", noop)
    lifespan = m.lifespan(m.app)
    await lifespan.__aenter__()
    assert await asyncio.to_thread(started.wait, 1)
    shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
    try:
        await asyncio.sleep(0.02)
        assert not shutdown.done()
        assert not closed
    finally:
        release.set()
        await shutdown
    assert closed == [True]
