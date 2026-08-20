"""用量数据库健康诊断与保留清理测试。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.usage import store


@pytest.fixture
def isolated_usage_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "usage.db")
    store.reset_for_test()
    yield tmp_path
    store.reset_for_test()


def _record(uuid: str, timestamp_ms: int) -> dict:
    return {
        "uuid": uuid,
        "timestamp_ms": timestamp_ms,
        "model": "test-model",
        "pricing_model": "test-model",
        "session_id": "session",
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_creation": 0,
        "cache_read": 0,
        "total_cost_usd": "0",
        "has_cost": False,
    }


def test_cleanup_deletes_expired_records_and_missing_file_cursors(isolated_usage_db):
    now_ms = int(datetime(2026, 8, 10, tzinfo=UTC).timestamp() * 1000)
    old_ms = now_ms - 400 * 86_400_000
    recent_ms = now_ms - 10 * 86_400_000
    assert store.upsert_records("test", [_record("old", old_ms), _record("recent", recent_ms)]) == 2

    existing = isolated_usage_db / "existing.jsonl"
    existing.write_text("{}\n", encoding="utf-8")
    missing = isolated_usage_db / "missing.jsonl"
    store.set_cursor("test", str(existing), last_line_offset=1)
    store.set_cursor("test", str(missing), last_line_offset=1)

    result = store.cleanup_old_records(365, force=True, now_ms=now_ms)
    assert result["deleted_records"] == 1
    assert result["deleted_cursors"] == 1
    status = store.get_database_status()
    assert status["ok"] is True
    assert status["records"] == 1
    assert status["cursors"] == 1
    assert status["oldest_record_at"] == recent_ms


def test_cleanup_runs_at_most_once_per_day_without_force(isolated_usage_db):
    now_ms = int(datetime(2026, 8, 10, tzinfo=UTC).timestamp() * 1000)
    first = store.cleanup_old_records(365, now_ms=now_ms)
    second = store.cleanup_old_records(365, now_ms=now_ms + 60_000)
    assert first["skipped"] is False
    assert second["skipped"] is True
    assert second["last_cleanup_at"] == now_ms


def test_database_status_does_not_create_missing_database(isolated_usage_db):
    db_path = isolated_usage_db / "usage.db"
    assert not db_path.exists()
    status = store.get_database_status()
    assert status["ok"] is True
    assert status["exists"] is False
    assert status["records"] == 0
    assert not db_path.exists()


def test_legacy_scan_progress_schema_is_migrated_and_partial_updates_preserve_cursor(isolated_usage_db):
    """旧版 usage.db 增加 rowid/model 游标后仍可读写。"""
    db_path = isolated_usage_db / "usage.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE scan_progress ("
        "source TEXT NOT NULL, file_path TEXT NOT NULL, last_modified_ns INTEGER, "
        "last_line_offset INTEGER, watermark_ms INTEGER, PRIMARY KEY(source, file_path))"
    )
    conn.commit()
    conn.close()

    store.get_conn()
    columns = {row[1] for row in store.get_conn().execute("PRAGMA table_info(scan_progress)")}
    assert {"watermark_rowid", "last_model"} <= columns

    store.set_cursor("codex", "session.jsonl", last_line_offset=42)
    store.set_cursor("codex", "session.jsonl", last_model="gpt-special")
    cursor = store.get_cursor("codex", "session.jsonl")
    assert cursor["last_line_offset"] == 42
    assert cursor["last_model"] == "gpt-special"


def test_query_trend_returns_exact_requested_number_of_calendar_days(isolated_usage_db):
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    assert store.upsert_records("test", [_record("recent", int(yesterday.timestamp() * 1000))]) == 1

    trend = store.query_trend(days=14)

    assert len(trend) == 14
    assert trend[0]["day"] == (now.date() - timedelta(days=13)).isoformat()
    assert trend[-1]["day"] == now.date().isoformat()


def test_usage_queries_share_calendar_day_window(isolated_usage_db):
    """总览、模型、日志和筛选都应与趋势使用同一 UTC 自然日窗口。"""
    now = datetime.now(UTC)
    today_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
    before_today = int((today_start - timedelta(milliseconds=1)).timestamp() * 1000)
    today = int((today_start + timedelta(hours=1)).timestamp() * 1000)
    records = [_record("before", before_today), _record("today", today)]
    records[0]["model"] = "before-model"
    records[1]["model"] = "today-model"
    assert store.upsert_records("test", records) == 2

    assert store.query_overview(days=1)["requests"] == 1
    assert [row["model"] for row in store.query_model_breakdown(days=1)] == ["today-model"]
    assert store.query_request_log(days=1)["rows"][0]["model"] == "today-model"
    assert store.query_models(days=1) == ["today-model"]


# ── rebill（只补 0 成本记录）─────────────────────────────────────


def _zero_cost_record(uuid: str, model: str, timestamp_ms: int) -> dict:
    return {
        "uuid": uuid,
        "timestamp_ms": timestamp_ms,
        "model": model,
        "pricing_model": model,
        "session_id": "session",
        "input_tokens": 1_000_000,
        "output_tokens": 500_000,
        "cache_creation": 0,
        "cache_read": 0,
        "total_cost_usd": "0.000000",  # 真实采集写入的无价记录是 6 位小数格式
        "has_cost": False,
    }


def test_rebill_recounts_zero_cost_records_and_keeps_unknown_zero(isolated_usage_db):
    from app.usage.pricing import PricingBook

    book = PricingBook()
    book.upsert("gpt-4o", 2.5, 10.0, 1.25, 5.0, is_builtin=False)
    now_ms = int(datetime(2026, 8, 10, tzinfo=UTC).timestamp() * 1000)
    assert store.upsert_records(
        "test",
        [
            _zero_cost_record("matched", "gpt-4o", now_ms),
            _zero_cost_record("unknown", "no-such-model", now_ms),
        ],
    ) == 2

    result = store.rebill_zero_cost(book)
    assert result["recounted"] == 1
    assert result["still_zero"] == 1

    rows = {r["uuid"]: r for r in store.get_conn().execute(
        "SELECT uuid, has_cost, total_cost_usd FROM usage_records"
    )}
    # 有价的补算成功：has_cost 置 1、成本不再是 0（1M input@2.5 + 0.5M output@10 = 7.5）
    assert rows["matched"]["has_cost"] == 1
    assert float(rows["matched"]["total_cost_usd"]) > 0
    # 单价表没匹配上的保持原样
    assert rows["unknown"]["has_cost"] == 0
    assert float(rows["unknown"]["total_cost_usd"]) == 0


def test_rebill_never_rewrites_already_priced_records(isolated_usage_db):
    from app.usage.pricing import PricingBook

    book = PricingBook()
    book.upsert("gpt-4o", 2.5, 10.0, 1.25, 5.0, is_builtin=False)
    now_ms = int(datetime(2026, 8, 10, tzinfo=UTC).timestamp() * 1000)
    priced = _zero_cost_record("priced", "gpt-4o", now_ms)
    priced["total_cost_usd"] = "9.990000"
    priced["has_cost"] = True
    assert store.upsert_records("test", [priced]) == 1

    result = store.rebill_zero_cost(book)
    assert result["recounted"] == 0

    row = store.get_conn().execute(
        "SELECT has_cost, total_cost_usd FROM usage_records WHERE uuid='priced'"
    ).fetchone()
    assert row["has_cost"] == 1
    assert row["total_cost_usd"] == "9.990000"


# ── close（退出时 checkpoint WAL 并关闭连接）─────────────────────


def test_close_then_reopen_keeps_data(isolated_usage_db):
    now_ms = int(datetime(2026, 8, 10, tzinfo=UTC).timestamp() * 1000)
    assert store.upsert_records("test", [_record("r1", now_ms)]) == 1
    store.close()
    # close 后全局连接可重建（惰性），数据仍然完整
    status = store.get_database_status()
    assert status["ok"] is True
    assert status["records"] == 1
    assert store.query_overview(days=365)["requests"] == 1
