"""用量采集编排层的并发回归测试。"""

from __future__ import annotations

import threading

from app.usage import collect as usage_collect
from app.usage.parsers import CollectResult


def test_collect_all_serializes_concurrent_runs(monkeypatch):
    """多标签页同时采集时不得并发读取/回写同一批增量游标。"""
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    calls_lock = threading.Lock()
    calls = 0

    class FakeParser:
        source = "fake"

        def collect_incremental(self):
            nonlocal calls
            with calls_lock:
                calls += 1
                call_no = calls
            if call_no == 1:
                first_entered.set()
                assert release_first.wait(timeout=2)
            else:
                second_entered.set()
            return CollectResult()

    monkeypatch.setattr(usage_collect, "all_parsers", lambda: [FakeParser()])

    first = threading.Thread(target=usage_collect.collect_all)
    second = threading.Thread(target=usage_collect.collect_all)
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()

    # 第一轮未释放前，第二轮必须停在采集锁外，不能进入解析器。
    assert not second_entered.wait(timeout=0.1)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()
    assert calls == 2
    status = usage_collect.get_status()
    assert status["running"] is False
    assert status["last_started_at"] is not None
    assert status["last_finished_at"] >= status["last_started_at"]
    assert status["last_result"]["sources"][0]["source"] == "fake"
