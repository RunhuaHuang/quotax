"""后台监控告警、冷却、恢复和状态持久化测试（不发送真实通知）。"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import config as config_store
from app.monitoring import MonitorService


def _quota(
    remaining: float, *, channel_id: str = "ch_a", channel_type: str = "deepseek"
) -> dict:
    return {
        "id": channel_id,
        "type": channel_type,
        "name": "DeepSeek",
        "category": "balance",
        "status": "ok",
        "windows": [
            {
                "key": "credits",
                "label": "账户额度",
                "used_percent": 100 - remaining,
                "remaining_percent": remaining,
                "reset_at": None,
            }
        ],
    }


@pytest.mark.asyncio
async def test_monitor_alert_cooldown_and_recovery(isolated_config):
    config_store.update_settings(
        {"monitor": {"thresholds": {"ch_a": 20}, "cooldown_seconds": 3600}}
    )
    current = [_quota(10)]
    events = []

    async def query(_force):
        return current

    async def notify(_settings, event):
        events.append(event)
        return [{"backend": "test", "ok": True}]

    service = MonitorService(query, notifier=notify)
    first = await service.run_once(reason="manual")
    assert first["active_alerts"] == 1
    assert first["notifications_sent"] == 1
    assert [e["type"] for e in events] == ["alert"]

    second = await service.run_once(reason="manual")
    assert second["active_alerts"] == 1
    assert second["notifications_sent"] == 0  # 冷却期内不重复打扰
    assert len(events) == 1

    current = [_quota(35)]
    third = await service.run_once(reason="manual")
    assert third["active_alerts"] == 0
    assert third["recoveries"] == 1
    assert [e["type"] for e in events] == ["alert", "recovered"]


@pytest.mark.asyncio
async def test_monitor_state_survives_service_restart(isolated_config):
    config_store.update_settings(
        {"monitor": {"thresholds": {"ch_a": 20}, "cooldown_seconds": 3600}}
    )
    calls = []

    async def query(_force):
        return [_quota(5)]

    async def notify(_settings, event):
        calls.append(event)
        return [{"backend": "test", "ok": True}]

    first_service = MonitorService(query, notifier=notify)
    await first_service.run_once()
    assert len(calls) == 1

    state_path = isolated_config.with_name("monitor-state.json")
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["alerts"]["ch_a:quota"]["active"] is True

    second_service = MonitorService(query, notifier=notify)
    result = await second_service.run_once()
    assert result["notifications_sent"] == 0
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_manual_monitor_run_works_while_scheduler_disabled(isolated_config):
    config_store.update_settings({"monitor": {"enabled": False, "thresholds": {"ch_a": 20}}})
    queried = []

    async def query(force):
        queried.append(force)
        return [_quota(80)]

    async def notify(_settings, _event):
        raise AssertionError("没有越过阈值，不应通知")

    service = MonitorService(query, notifier=notify)
    result = await service.run_once(force=True)
    assert queried == [True]
    assert result["evaluated_channels"] == 1
    assert result["active_alerts"] == 0
    assert (await service.status())["enabled"] is False


@pytest.mark.asyncio
async def test_provider_error_does_not_create_false_recovery(isolated_config):
    config_store.update_settings({"monitor": {"thresholds": {"ch_a": 20}}})
    current = [_quota(5)]
    events = []

    async def query(_force):
        return current

    async def notify(_settings, event):
        events.append(event)
        return []

    service = MonitorService(query, notifier=notify)
    await service.run_once()
    current = [{**_quota(80), "status": "error", "message": "上游超时"}]
    result = await service.run_once()
    assert result["active_alerts"] == 1
    assert [e["type"] for e in events] == ["alert"]


@pytest.mark.asyncio
async def test_reconfigure_during_running_check_is_not_lost(isolated_config):
    settings = {
        "monitor": {
            "enabled": True,
            "interval_seconds": 60,
            "cooldown_seconds": 3600,
            "thresholds": {},
        }
    }
    query_started = asyncio.Event()
    release_query = asyncio.Event()

    async def query(_force):
        query_started.set()
        await release_query.wait()
        return []

    service = MonitorService(query, get_settings=lambda: settings)
    await service.start()
    try:
        await asyncio.wait_for(query_started.wait(), timeout=1)
        settings["monitor"]["enabled"] = False
        service.reconfigure()
        release_query.set()
        await asyncio.sleep(0.1)
        status = await service.status()
        assert status["last_finished_at"] is not None
        assert status["next_run_at"] is None
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_suffix_threshold_fallback_is_volcengine_only(isolated_config):
    config_store.update_settings({"monitor": {"thresholds": {"team": 20, "volc": 20}}})

    async def query(_force):
        return [
            _quota(5, channel_id="team_agent", channel_type="deepseek"),
            _quota(5, channel_id="volc_agent", channel_type="volcengine"),
        ]

    events = []

    async def notify(_settings, event):
        events.append(event)
        return []

    result = await MonitorService(query, notifier=notify).run_once()
    assert result["evaluated_channels"] == 1
    assert [event["channel_id"] for event in events] == ["volc_agent"]


@pytest.mark.asyncio
async def test_clear_channel_does_not_remove_unrelated_prefixed_channel(isolated_config):
    async def query(_force):
        return []

    service = MonitorService(query, get_settings=lambda: {"monitor": {"thresholds": {}}})
    state = {
        "foo:quota": {"channel_id": "foo", "channel_type": "deepseek", "active": True},
        "foobar:quota": {"channel_id": "foobar", "channel_type": "deepseek", "active": True},
        "foo_agent:quota": {"channel_id": "foo_agent", "channel_type": "volcengine", "active": True},
    }
    state_path = isolated_config.with_name("monitor-state.json")
    state_path.write_text(json.dumps({"version": 1, "alerts": state}), encoding="utf-8")
    removed = await service.clear_channel("foo")
    assert removed == 2
    assert "foo:quota" not in service._state["alerts"]
    assert "foo_agent:quota" not in service._state["alerts"]
    assert "foobar:quota" in service._state["alerts"]


@pytest.mark.asyncio
async def test_notification_exception_does_not_abort_monitor_round(isolated_config):
    config_store.update_settings({"monitor": {"thresholds": {"ch_a": 20}}})

    async def query(_force):
        return [_quota(5)]

    async def broken_notifier(_settings, _event):
        raise RuntimeError("https://hooks.example.test/secret-token")

    result = await MonitorService(query, notifier=broken_notifier).run_once()
    assert result["active_alerts"] == 1
    backend = result["notification_results"][0]["backends"][0]
    assert backend["ok"] is False
    assert "secret-token" not in backend["error"]


def test_service_can_restart_across_event_loops(isolated_config):
    settings = {"monitor": {"enabled": False}}

    async def query(_force):
        return []

    service = MonitorService(query, get_settings=lambda: settings)

    async def cycle():
        await service.start()
        await asyncio.sleep(0)
        await service.stop()

    asyncio.run(cycle())
    asyncio.run(cycle())


@pytest.mark.asyncio
async def test_no_notification_destination_does_not_start_cooldown(isolated_config):
    config_store.update_settings({"monitor": {"thresholds": {"ch_a": 20}}})
    calls = 0

    async def query(_force):
        return [_quota(5)]

    async def notify(_settings, _event):
        nonlocal calls
        calls += 1
        return [] if calls == 1 else [{"backend": "test", "ok": True}]

    service = MonitorService(query, notifier=notify)
    first = await service.run_once()
    second = await service.run_once()
    assert first["notifications_sent"] == 0
    assert second["notifications_sent"] == 1
    assert calls == 2


@pytest.mark.asyncio
async def test_all_notification_backends_failing_does_not_start_cooldown(isolated_config):
    """桌面/Webhook 都返回失败时，下一轮仍应重试，不能假装已通知成功。"""
    config_store.update_settings({"monitor": {"thresholds": {"ch_a": 20}}})
    calls = 0

    async def query(_force):
        return [_quota(5)]

    async def notify(_settings, _event):
        nonlocal calls
        calls += 1
        return [
            {"backend": "desktop", "ok": False, "error": "unavailable"},
            {"backend": "webhook", "ok": False, "error": "HTTP 503"},
        ]

    service = MonitorService(query, notifier=notify)
    first = await service.run_once()
    second = await service.run_once()
    assert first["notifications_sent"] == 0
    assert second["notifications_sent"] == 0
    assert calls == 2


@pytest.mark.asyncio
async def test_non_finite_window_is_ignored(isolated_config):
    config_store.update_settings({"monitor": {"thresholds": {"ch_a": 20}}})

    async def query(_force):
        return [_quota(float("nan"))]

    async def notify(_settings, _event):
        raise AssertionError("非有限百分比不应触发告警")

    result = await MonitorService(query, notifier=notify).run_once()
    assert result["evaluated_channels"] == 0
