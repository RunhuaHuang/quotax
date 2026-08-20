"""浏览器无关的后台额度监控、告警去重与恢复检测。"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from . import config as config_store
from . import notifications

logger = logging.getLogger(__name__)

QueryResults = Callable[[bool], Awaitable[list[dict]]]
SettingsGetter = Callable[[], dict]
Notifier = Callable[[dict, dict], Awaitable[list[dict]]]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _default_state() -> dict:
    return {
        "version": 1,
        "alerts": {},
        "last_started_at": None,
        "last_finished_at": None,
        "last_error": None,
        "last_result": None,
    }


def _state_path() -> Path:
    # 动态从 CONFIG_PATH 派生，pytest monkeypatch 配置路径后不会误写真实目录。
    return config_store.CONFIG_PATH.with_name("monitor-state.json")


def _load_state(path: Path) -> dict:
    if not path.exists():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("监控状态文件读取失败，将从空状态继续: %s", e)
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()
    state = _default_state()
    state.update({k: data.get(k) for k in state if k in data})
    if not isinstance(state.get("alerts"), dict):
        state["alerts"] = {}
    return state


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(str(tmp_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass
        raise


def _threshold_for(result_id: str, channel_type: str | None, thresholds: dict) -> float | None:
    """精确 ID 优先；只有火山拆分子卡可继承原始渠道阈值。"""
    value = thresholds.get(result_id)
    if value is None and channel_type == "volcengine":
        for suffix in ("_agent", "_coding"):
            if result_id.endswith(suffix):
                value = thresholds.get(result_id[: -len(suffix)])
                if value is not None:
                    break
    return float(value) if value is not None else None


def _lowest_window(result: dict) -> dict | None:
    candidates = []
    for item in result.get("windows") or []:
        remaining = item.get("remaining_percent")
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
            continue
        remaining_value = float(remaining)
        if not math.isfinite(remaining_value):
            continue
        candidates.append((remaining_value, item))
    if not candidates:
        return None
    remaining, item = min(candidates, key=lambda pair: pair[0])
    return {
        "key": str(item.get("key") or "quota"),
        "label": str(item.get("label") or "额度"),
        "remaining_percent": round(remaining, 1),
        "reset_at": item.get("reset_at"),
    }


def _alert_event(result: dict, threshold: float, window: dict, event_type: str, now_ms: int) -> dict:
    name = str(result.get("name") or result.get("id") or "未知渠道")
    remaining = window["remaining_percent"]
    if event_type == "recovered":
        title = "QuotaX 额度已恢复"
        message = f"{name} · {window['label']}剩余 {remaining:.1f}%，已回到阈值 {threshold:.1f}% 以上"
    else:
        title = "QuotaX 低额度告警"
        message = f"{name} · {window['label']}仅剩 {remaining:.1f}%，低于阈值 {threshold:.1f}%"
    return {
        "type": event_type,
        "occurred_at": now_ms,
        "channel_id": result.get("id"),
        "channel_type": result.get("type"),
        "channel_name": name,
        "plan_name": result.get("plan_name"),
        "window": window,
        "threshold_percent": threshold,
        "title": title,
        "message": message,
    }


class MonitorService:
    """单进程后台调度器；同一时刻最多执行一轮检查。"""

    def __init__(
        self,
        query_results: QueryResults,
        get_settings: SettingsGetter | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self._query_results = query_results
        self._get_settings = get_settings or config_store.get_settings
        self._notifier = notifier or notifications.dispatch
        self._run_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._wake_event = asyncio.Event()
        self._wake_revision = 0
        self._stop_requested = False
        self._running = False
        self._next_run_at: int | None = None
        self._loaded_path: Path | None = None
        self._state = _default_state()

    def _ensure_state_loaded(self) -> Path:
        path = _state_path()
        if self._loaded_path != path:
            self._state = _load_state(path)
            self._loaded_path = path
        return path

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._ensure_loop_primitives()
        self._ensure_state_loaded()
        self._stop_requested = False
        self._task = asyncio.create_task(self._scheduler_loop(), name="quotax-monitor")

    async def stop(self) -> None:
        self._stop_requested = True
        self._wake_revision += 1
        self._wake_event.set()
        task = self._task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=15)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception:
                logger.exception("后台监控任务异常结束")
        self._task = None
        self._next_run_at = None

    def reconfigure(self) -> None:
        """设置写盘后唤醒循环，使开关/间隔立即生效。"""
        self._wake_revision += 1
        self._wake_event.set()

    def _ensure_loop_primitives(self) -> None:
        """允许同一个服务对象跨 TestClient/asyncio.run 生命周期安全重启。"""
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        if self._task is not None and not self._task.done():
            raise RuntimeError("监控服务仍在另一个事件循环中运行")
        self._loop = loop
        self._run_lock = asyncio.Lock()
        self._wake_event = asyncio.Event()

    async def _wait(self, timeout_seconds: float | None, observed_revision: int) -> None:
        # reconfigure 可能发生在一轮上游查询期间，或恰好落在 clear() 前后。
        # 用单调 revision 双检，保证任何一次配置变更都不会被 clear() 擦掉。
        if self._wake_revision != observed_revision:
            return
        self._wake_event.clear()
        if self._wake_revision != observed_revision:
            return
        try:
            if timeout_seconds is None:
                await self._wake_event.wait()
            else:
                await asyncio.wait_for(self._wake_event.wait(), timeout=max(timeout_seconds, 0.05))
        except TimeoutError:
            pass

    async def _scheduler_loop(self) -> None:
        while not self._stop_requested:
            observed_revision = self._wake_revision
            try:
                # get_settings 默认实现会同步读盘 + 反序列化 config.json，放线程里
                # 跑，避免阻塞事件循环（磁盘慢时整个服务会卡住）
                settings = (await asyncio.to_thread(self._get_settings))["monitor"]
            except Exception as e:
                self._state["last_error"] = f"读取监控设置失败: {e}"
                self._next_run_at = None
                await self._wait(60, observed_revision)
                continue

            if not settings.get("enabled"):
                self._next_run_at = None
                await self._wait(None, observed_revision)
                continue

            try:
                await self.run_once(force=False, reason="scheduled")
            except Exception:
                # run_once 已记录完整错误；调度循环必须继续存活，不能因为一次上游
                # 故障永久停止后续检查。
                logger.warning("本轮后台监控失败，将在下个周期重试")
            try:
                interval = int((await asyncio.to_thread(self._get_settings))["monitor"]["interval_seconds"])
            except Exception as e:
                self._state["last_error"] = f"读取监控设置失败: {e}"
                self._next_run_at = None
                await self._wait(60, observed_revision)
                continue
            self._next_run_at = _now_ms() + interval * 1000
            await self._wait(interval, observed_revision)

    async def run_once(self, *, force: bool = False, reason: str = "manual") -> dict:
        """执行一轮监控；手动检查即使总开关关闭也允许运行。"""
        self._ensure_loop_primitives()
        async with self._run_lock:
            path = self._ensure_state_loaded()
            now_ms = _now_ms()
            self._running = True
            self._state["last_started_at"] = now_ms
            self._state["last_error"] = None
            try:
                settings = (await asyncio.to_thread(self._get_settings))["monitor"]
                results = await self._query_results(force)
                summary = await self._evaluate(results, settings, now_ms, reason)
                self._state["last_result"] = summary
                return copy.deepcopy(summary)
            except Exception as e:
                self._state["last_error"] = f"{type(e).__name__}: {e}"
                logger.exception("后台额度监控执行失败")
                raise
            finally:
                self._running = False
                self._state["last_finished_at"] = _now_ms()
                try:
                    await asyncio.to_thread(_save_state, path, self._state)
                except Exception:
                    logger.exception("保存监控状态失败")

    async def _evaluate(self, results: list[dict], settings: dict, now_ms: int, reason: str) -> dict:
        thresholds = settings.get("thresholds") or {}
        cooldown_ms = int(settings.get("cooldown_seconds") or 3600) * 1000
        alerts: dict = self._state.setdefault("alerts", {})
        evaluated = 0
        breaches = 0
        notified = 0
        recovered = 0
        notification_results: list[dict] = []

        for result in results:
            result_id = str(result.get("id") or "")
            channel_type = str(result.get("type") or "")
            threshold = _threshold_for(result_id, channel_type, thresholds)
            if threshold is None or result.get("status") != "ok":
                continue
            lowest = _lowest_window(result)
            if lowest is None:
                continue
            evaluated += 1
            alert_key = f"{result_id}:quota"
            previous = alerts.get(alert_key) if isinstance(alerts.get(alert_key), dict) else {}
            is_breach = lowest["remaining_percent"] < threshold

            if is_breach:
                breaches += 1
                try:
                    last_notified = int(previous.get("last_notified_at") or 0)
                except (TypeError, ValueError, OverflowError):
                    last_notified = 0
                last_notified = max(last_notified, 0)
                should_notify = not previous.get("active") or now_ms - last_notified >= cooldown_ms
                entry = {
                    "active": True,
                    "channel_id": result_id,
                    "channel_type": channel_type,
                    "channel_name": result.get("name"),
                    "threshold_percent": threshold,
                    "remaining_percent": lowest["remaining_percent"],
                    "window_key": lowest["key"],
                    "window_label": lowest["label"],
                    "first_triggered_at": previous.get("first_triggered_at") or now_ms,
                    "last_seen_at": now_ms,
                    "last_notified_at": last_notified or None,
                }
                if should_notify:
                    event = _alert_event(result, threshold, lowest, "alert", now_ms)
                    backend_results = await self._notify_safely(settings, event)
                    # 没有启用任何通知目标时 dispatch 返回空列表；此时不启动冷却，
                    # 用户稍后启用桌面/Webhook 后应立即收到仍处于低额度的告警。
                    # 只有至少一个后端真正返回 ok=True 才启动冷却；桌面通知不可用、
                    # Webhook 4xx/超时等“返回了失败结果”的情况不能让后续检查静默。
                    if any(isinstance(item, dict) and item.get("ok") is True for item in backend_results):
                        entry["last_notified_at"] = now_ms
                        notified += 1
                    notification_results.append({"event": event, "backends": backend_results})
                alerts[alert_key] = entry
                continue

            if previous.get("active"):
                event = _alert_event(result, threshold, lowest, "recovered", now_ms)
                backend_results = await self._notify_safely(settings, event)
                recovered += 1
                notification_results.append({"event": event, "backends": backend_results})
            alerts[alert_key] = {
                "active": False,
                "channel_id": result_id,
                "channel_type": channel_type,
                "channel_name": result.get("name"),
                "threshold_percent": threshold,
                "remaining_percent": lowest["remaining_percent"],
                "window_key": lowest["key"],
                "window_label": lowest["label"],
                "last_seen_at": now_ms,
                "recovered_at": now_ms if previous.get("active") else previous.get("recovered_at"),
                "last_notified_at": previous.get("last_notified_at"),
            }

        # 阈值被删除的渠道不再属于监控范围，清理其旧状态；查询失败/过期的渠道没有
        # 参与本轮评估（status != "ok" 已在上方 continue 跳过），但仍有阈值，因此
        # 保留 active 状态，避免把网络错误误报成恢复。
        for key, entry in list(alerts.items()):
            channel_id = str((entry or {}).get("channel_id") or key.rsplit(":", 1)[0])
            channel_type = str((entry or {}).get("channel_type") or "")
            if _threshold_for(channel_id, channel_type, thresholds) is None:
                alerts.pop(key, None)

        active_alerts = [copy.deepcopy(v) for v in alerts.values() if isinstance(v, dict) and v.get("active")]
        return {
            "reason": reason,
            "checked_channels": len(results),
            "evaluated_channels": evaluated,
            "active_alerts": len(active_alerts),
            "breaches": breaches,
            "notifications_sent": notified,
            "recoveries": recovered,
            "notification_results": notification_results,
        }

    async def _notify_safely(self, settings: dict, event: dict) -> list[dict]:
        """通知实现的意外异常不得中断额度检查，也不持久化潜在秘密。"""
        try:
            return await self._notifier(settings, event)
        except Exception as exc:
            logger.exception("通知分发异常")
            return [
                {
                    "backend": "notification",
                    "ok": False,
                    "error": f"{type(exc).__name__}: 通知发送失败",
                }
            ]

    async def status(self) -> dict:
        self._ensure_state_loaded()
        settings = (await asyncio.to_thread(self._get_settings))["monitor"]
        alerts = self._state.get("alerts") or {}
        active_alerts = [copy.deepcopy(v) for v in alerts.values() if isinstance(v, dict) and v.get("active")]
        return {
            "enabled": bool(settings.get("enabled")),
            "running": self._running,
            "scheduler_running": self._task is not None and not self._task.done(),
            "interval_seconds": settings.get("interval_seconds"),
            "next_run_at": self._next_run_at,
            "last_started_at": self._state.get("last_started_at"),
            "last_finished_at": self._state.get("last_finished_at"),
            "last_error": self._state.get("last_error"),
            "last_result": copy.deepcopy(self._state.get("last_result")),
            "active_alerts": active_alerts,
        }

    async def clear_channel(self, channel_id: str) -> int:
        """删除渠道时同步清理持久化告警状态，避免幽灵活动告警残留。"""
        self._ensure_loop_primitives()
        path = self._ensure_state_loaded()
        removed = 0
        async with self._run_lock:
            alerts = self._state.setdefault("alerts", {})
            for key, entry in list(alerts.items()):
                entry_id = str((entry or {}).get("channel_id") or key.rsplit(":", 1)[0])
                entry_type = str((entry or {}).get("channel_type") or "")
                is_volc_child = entry_type == "volcengine" and entry_id in {
                    f"{channel_id}_agent",
                    f"{channel_id}_coding",
                }
                if entry_id == channel_id or is_volc_child:
                    alerts.pop(key, None)
                    removed += 1
            if removed:
                await asyncio.to_thread(_save_state, path, self._state)
        return removed
