"""app/history.py 的单测：趋势记录的追加、同日去重、按天读取、删除清理。

所有测试通过 monkeypatch 把 config_store.HISTORY_DIR 指到 tmp_path，
绝不碰用户真实的 history/ 目录。
"""

from __future__ import annotations

import concurrent.futures
import json
import time

import pytest

from app import config as config_store
from app import history


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store, "HISTORY_DIR", tmp_path / "history")
    return config_store.HISTORY_DIR


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """history 的 get_history 依赖 config_store.list_channels() 判断哪些渠道
    是"已配置"的——必须同时隔离 CONFIG_PATH，否则会读到项目根目录的真实 config.json，
    导致测试里的 ch_test 被当成未配置渠道过滤掉。"""
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "config.json")
    config_store.upsert_channel(
        {"id": "ch_test", "type": "deepseek", "name": "test", "api_key": "sk-test1234567890"},
        provided_fields={"id", "type", "name", "api_key"},
    )


def _result(status="ok", amount_value=50.0, windows=None):
    return {
        "id": "ch_test",
        "type": "deepseek",
        "name": "test",
        "category": "balance",
        "status": status,
        "amount": {"value": amount_value, "currency": "CNY", "label": "¥50.00"},
        "windows": windows or [{"key": "balance", "label": "账户余额", "used_percent": 50, "remaining_percent": 50}],
    }


def test_record_result_only_stores_ok():
    history.record_result("ch_test", _result(status="error"))
    data = history.get_history(["ch_test"], days=30)
    assert data["channels"]["ch_test"] == []


def test_record_result_appends_across_days():
    """不同天（UTC）的记录各自保留一条，不会被同日去重合并。"""
    # 第一条：手动写一条昨天（UTC 跨日）的记录，模拟历史
    config_store.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = config_store.HISTORY_DIR / "ch_test.jsonl"
    yesterday_ts = int((time.time() - 86400 * 2) * 1000)
    with path.open("w") as f:
        f.write(
            json.dumps(
                {"ts": yesterday_ts, "status": "ok", "amount": {"value": 80.0, "currency": "CNY"}, "windows": []}
            )
            + "\n"
        )
    # 第二条：今天的新记录
    history.record_result("ch_test", _result(amount_value=70.0))
    data = history.get_history(["ch_test"], days=30)
    recs = data["channels"]["ch_test"]
    assert len(recs) == 2
    assert recs[0]["amount"]["value"] == 80.0  # 昨天
    assert recs[1]["amount"]["value"] == 70.0  # 今天


def test_record_result_same_day_dedup():
    """同一天（UTC）多次记录只保留最后一条。"""
    history.record_result("ch_test", _result(amount_value=80.0))
    history.record_result("ch_test", _result(amount_value=30.0))
    history.record_result("ch_test", _result(amount_value=50.0))
    data = history.get_history(["ch_test"], days=30)
    recs = data["channels"]["ch_test"]
    assert len(recs) == 1
    assert recs[0]["amount"]["value"] == 50.0


def test_record_result_filters_by_days():
    """超过 days 窗口的记录不返回。"""
    # 先手动写一条 100 天前的记录（在 record_result 之前，避免被覆盖）
    config_store.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = config_store.HISTORY_DIR / "ch_test.jsonl"
    old_ts = int((time.time() - 100 * 86400) * 1000)
    with path.open("w") as f:
        f.write(
            json.dumps({"ts": old_ts, "status": "ok", "amount": {"value": 1.0, "currency": "CNY"}, "windows": []})
            + "\n"
        )
    # 再记录一条今天的
    history.record_result("ch_test", _result(amount_value=100.0))
    # 查最近 30 天：100 天前那条应被过滤，只剩今天的
    data = history.get_history(["ch_test"], days=30)
    recs = data["channels"]["ch_test"]
    assert len(recs) == 1
    assert recs[0]["amount"]["value"] == 100.0


def test_record_result_drops_non_ok_fields():
    """ok 结果只保留画图要用的字段（丢 message/source/plan_name 等）。"""
    result = {
        "id": "ch_test",
        "type": "deepseek",
        "status": "ok",
        "name": "test",
        "message": "should be dropped",
        "source": "/some/path",
        "plan_name": "should be dropped",
        "amount": {"value": 50, "currency": "CNY", "label": "¥50.00"},
        "windows": [{"key": "balance", "label": "余额", "used_percent": 50, "remaining_percent": 50}],
    }
    history.record_result("ch_test", result)
    data = history.get_history(["ch_test"], days=30)
    rec = data["channels"]["ch_test"][0]
    assert "message" not in rec
    assert "source" not in rec
    assert "plan_name" not in rec
    assert rec["amount"] == {"value": 50, "currency": "CNY"}
    assert rec["windows"][0]["used_percent"] == 50


def test_get_history_only_returns_configured_channels():
    """已删除渠道的孤儿 JSONL 不应被返回。"""
    # ch_test 已由 autouse fixture 创建；ch_dead 没有对应渠道配置
    history.record_result("ch_test", _result())
    history.record_result("ch_dead", _result())
    data = history.get_history(days=30)
    assert "ch_test" in data["channels"]
    assert "ch_dead" not in data["channels"]


def test_delete_channel_history_removes_file():
    history.record_result("ch_test", _result())
    path = config_store.HISTORY_DIR / "ch_test.jsonl"
    assert path.exists()
    history.delete_channel_history("ch_test")
    assert not path.exists()


def test_unsafe_channel_ids_use_distinct_history_files():
    """a/b 与 ab 不能因旧 sanitize 规则共用一份趋势数据。"""
    result = _result()
    history.record_result("a/b", result)
    history.record_result("ab", result)
    assert history._channel_history_path("a/b") != history._channel_history_path("ab")
    assert history._channel_history_path("a/b").exists()
    assert history._channel_history_path("ab").exists()


def test_concurrent_same_day_history_writes_do_not_lose_records(monkeypatch):
    """并发记录同一渠道时，固定临时文件/读改写竞态不能造成异常或空文件。"""
    original = history._write_channel_history

    def slow_write(path, records):
        time.sleep(0.01)
        original(path, records)

    monkeypatch.setattr(history, "_write_channel_history", slow_write)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: history.record_result("ch_test", _result(amount_value=float(i))), range(8)))
    records = history.get_history(["ch_test"], days=30)["channels"]["ch_test"]
    assert len(records) == 1
    assert records[0]["amount"]["value"] in {float(i) for i in range(8)}


def test_record_result_failure_does_not_raise():
    """记录失败（如目录不可写）绝不能抛异常影响主查询流程。"""
    # 这里的 monkeypatch 已经把 HISTORY_DIR 指到 tmp_path，正常情况下不会失败；
    # 这个测试主要验证即便 record 内部 catch 住了，函数本身也不抛。
    history.record_result("ch_test", _result())
    history.record_result("ch_test", _result())  # 第二次不抛
