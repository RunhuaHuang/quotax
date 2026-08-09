"""usage.parsers 单测：Claude / Codex 解析（用临时日志文件 + 隔离 DB）。

所有测试通过 monkeypatch 把 DB_PATH 指到 tmp_path、把各解析器的目录常量指到临时
目录，绝不碰用户真实 ~/.claude 或 usage.db。
"""

from __future__ import annotations

import json
import sqlite3


def _setup_db(monkeypatch, tmp_path):
    """把 usage.store 的 DB 指向临时文件，并重置连接。"""
    from app.usage import store

    db = tmp_path / "test_usage.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    store.reset_for_test()
    return store


def test_claude_parser_parses_assistant_usage(monkeypatch, tmp_path):
    """Claude Code JSONL：type=assistant 行的 message.usage 被正确提取。"""
    store = _setup_db(monkeypatch, tmp_path)
    from app.usage.parsers import claude as claude_mod

    proj = tmp_path / "proj"
    proj.mkdir()
    session = proj / "abc123.jsonl"
    entry = {
        "type": "assistant",
        "timestamp": "2026-08-01T10:00:00.000Z",
        "message": {
            "id": "msg-1",
            "model": "claude-sonnet-5-20260101",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 200,
                "cache_creation_input_tokens": 50,
                "cache_read_input_tokens": 300,
            },
            "stop_reason": "end_turn",
        },
    }
    # 一行 assistant + 一行 user（应被忽略）
    session.write_text(json.dumps(entry) + "\n" + json.dumps({"type": "user"}) + "\n", encoding="utf-8")

    monkeypatch.setattr(claude_mod, "CLAUDE_PROJECTS_DIR", tmp_path)
    parser = claude_mod.ClaudeParser()
    result = parser.collect_incremental()

    assert len(result.records) == 1
    rec = result.records[0]
    assert rec.model == "claude-sonnet-5-20260101"
    assert rec.input_tokens == 100
    assert rec.output_tokens == 200
    assert rec.cache_read == 300
    assert rec.stop_reason == "end_turn"
    # 游标已记录
    assert store.get_cursor("claude_code", str(session)) is not None


def test_claude_parser_incremental_skips_unchanged(monkeypatch, tmp_path):
    """mtime 未变时，第二次 collect 直接跳过（游标命中）。"""
    _setup_db(monkeypatch, tmp_path)
    from app.usage.parsers import claude as claude_mod

    proj = tmp_path / "p"
    proj.mkdir()
    session = proj / "s.jsonl"
    entry = {
        "type": "assistant",
        "timestamp": "2026-08-01T10:00:00.000Z",
        "message": {"id": "m1", "model": "claude-sonnet-5", "usage": {"input_tokens": 10, "output_tokens": 5}},
    }
    session.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    monkeypatch.setattr(claude_mod, "CLAUDE_PROJECTS_DIR", tmp_path)

    parser = claude_mod.ClaudeParser()
    first = parser.collect_incremental()
    assert len(first.records) == 1
    second = parser.collect_incremental()  # mtime 没变
    assert len(second.records) == 0


def test_codex_parser_event_msg_token_count(monkeypatch, tmp_path):
    """Codex：token 在 event_msg + payload.type=token_count 的 last_token_usage 里。

    真实日志结构（2026 版 Codex CLI）：
    - turn_context.payload.model 带模型名（可能含 provider:: 前缀）
    - token_count 的 last_token_usage 是本轮增量，直接用
    - input 含 cache（cached_input_tokens），需减去得新鲜 input
    - reasoning_output_tokens 并入 output
    """
    _setup_db(monkeypatch, tmp_path)
    from app.usage.parsers import codex as codex_mod

    sessions = tmp_path / "sessions" / "2026" / "08" / "01"
    sessions.mkdir(parents=True)
    f = sessions / "rollout-1.jsonl"
    lines = [
        # turn_context 先出现，带 provider:: 前缀的模型名
        {"timestamp": "2026-08-01T10:00:00.000Z", "type": "turn_context",
         "payload": {"turn_id": "t1", "model": "aimami_relay_xxx::gpt-5.6-sol"}},
        # token_count 事件：input=24440 含 cache_read=3456，output=313 + reasoning=40
        {"timestamp": "2026-08-01T10:00:05.000Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "total_token_usage": {"input_tokens": 24440, "cached_input_tokens": 3456,
                                   "cache_write_input_tokens": 100, "output_tokens": 313,
                                   "reasoning_output_tokens": 40, "total_tokens": 28223},
             "last_token_usage": {"input_tokens": 24440, "cached_input_tokens": 3456,
                                  "cache_write_input_tokens": 100, "output_tokens": 313,
                                  "reasoning_output_tokens": 40, "total_tokens": 28223}}}},
    ]
    f.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(codex_mod, "CODEX_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(codex_mod, "CODEX_ARCHIVED_DIR", tmp_path / "archived")

    parser = codex_mod.CodexParser()
    result = parser.collect_incremental()
    assert len(result.records) == 1
    rec = result.records[0]
    # 模型名去掉 provider:: 前缀
    assert rec.model == "gpt-5.6-sol"
    # fresh_input = 24440 - 3456 = 20984
    assert rec.input_tokens == 20984
    assert rec.cache_read == 3456
    assert rec.cache_creation == 100
    # output = 313 + 40(reasoning)
    assert rec.output_tokens == 353


def test_opencode_parser_from_session_table(monkeypatch, tmp_path):
    """OpenCode：新版 session 表带聚合列，按水位线增量。"""
    _setup_db(monkeypatch, tmp_path)
    from app.usage.parsers import opencode as oc_mod

    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE session (
            id TEXT, model TEXT, time_updated INTEGER,
            time_archived INTEGER,
            tokens_input INTEGER, tokens_output INTEGER,
            tokens_reasoning INTEGER, tokens_cache_read INTEGER, tokens_cache_write INTEGER
        );
        INSERT INTO session VALUES ('s1','{"id":"llama","providerID":"ollama"}', 1722507600000, NULL, 100,200,0,50,10);
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(oc_mod, "_db_path", lambda: db)

    parser = oc_mod.OpenCodeParser()
    result = parser.collect_incremental()
    assert len(result.records) == 1
    rec = result.records[0]
    assert rec.input_tokens == 100
    assert rec.cache_read == 50
    assert "llama" in rec.model


def test_store_upsert_and_aggregate(monkeypatch, tmp_path):
    """store：入库去重 + 总览聚合（缓存命中率）。"""
    import time

    store = _setup_db(monkeypatch, tmp_path)
    now_ms = int(time.time() * 1000)
    records = [
        {
            "uuid": "u1", "timestamp_ms": now_ms, "model": "claude-sonnet-5",
            "session_id": "s1", "input_tokens": 800, "output_tokens": 200,
            "cache_creation": 100, "cache_read": 700, "stop_reason": "end_turn",
            "pricing_model": "claude-sonnet-5",
            "input_cost_usd": "0", "output_cost_usd": "0", "cache_read_cost": "0",
            "cache_create_cost": "0", "total_cost_usd": "0", "has_cost": False,
        },
    ]
    n = store.upsert_records("claude_code", records)
    assert n == 1
    # 重复入库（同 uuid）被忽略
    assert store.upsert_records("claude_code", records) == 0

    overview = store.query_overview(days=30)
    # 缓存命中率 = cache_read/(input+cache_creation+cache_read) = 700/(800+100+700)
    assert overview["cache_hit_rate"] == round(700 / 1600, 4)
    assert overview["requests"] == 1
