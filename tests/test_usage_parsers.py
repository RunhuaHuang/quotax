"""usage.parsers 单测：Claude / Codex 解析（用临时日志文件 + 隔离 DB）。

所有测试通过 monkeypatch 把 DB_PATH 指到 tmp_path、把各解析器的目录常量指到临时
目录，绝不碰用户真实 ~/.claude 或 usage.db。
"""

from __future__ import annotations

import json
import os
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


def test_claude_parser_scans_append_when_mtime_is_unchanged(monkeypatch, tmp_path):
    """文件系统 mtime 精度较粗时，追加的新行不能因 mtime 未变而漏采。"""
    store = _setup_db(monkeypatch, tmp_path)
    from app.usage.parsers import claude as claude_mod

    proj = tmp_path / "p"
    proj.mkdir()
    session = proj / "s.jsonl"
    first = {
        "type": "assistant",
        "timestamp": "2026-08-01T10:00:00.000Z",
        "message": {"id": "m1", "model": "claude-sonnet-5", "usage": {"input_tokens": 10, "output_tokens": 5}},
    }
    second = {
        "type": "assistant",
        "timestamp": "2026-08-01T10:01:00.000Z",
        "message": {"id": "m2", "model": "claude-sonnet-5", "usage": {"input_tokens": 20, "output_tokens": 6}},
    }
    session.write_text(json.dumps(first) + "\n", encoding="utf-8")
    monkeypatch.setattr(claude_mod, "CLAUDE_PROJECTS_DIR", tmp_path)
    parser = claude_mod.ClaudeParser()
    assert len(parser.collect_incremental().records) == 1
    cursor = store.get_cursor("claude_code", str(session))
    assert cursor is not None

    original_mtime = cursor["last_modified_ns"]
    with session.open("a", encoding="utf-8") as f:
        f.write(json.dumps(second) + "\n")
    # 模拟粗粒度 mtime：内容变长，但 mtime 与旧游标保持一致。
    os.utime(session, ns=(original_mtime, original_mtime))

    result = parser.collect_incremental()
    assert [record.uuid for record in result.records] == ["claude:m2"]


def test_claude_parser_rewinds_cursor_after_same_mtime_truncation(monkeypatch, tmp_path):
    """日志被重写且 mtime 恰好未变时，越界游标也必须回到文件开头。"""
    store = _setup_db(monkeypatch, tmp_path)
    from app.usage.parsers import claude as claude_mod

    proj = tmp_path / "p"
    proj.mkdir()
    session = proj / "s.jsonl"
    first = {
        "type": "assistant",
        "timestamp": "2026-08-01T10:00:00.000Z",
        "message": {"id": "old", "model": "m", "usage": {"input_tokens": 10, "output_tokens": 5}},
        "padding": "x" * 500,
    }
    session.write_text(json.dumps(first) + "\n", encoding="utf-8")
    monkeypatch.setattr(claude_mod, "CLAUDE_PROJECTS_DIR", tmp_path)
    parser = claude_mod.ClaudeParser()
    assert len(parser.collect_incremental().records) == 1
    cursor = store.get_cursor("claude_code", str(session))
    assert cursor is not None
    original_mtime = cursor["last_modified_ns"]

    rewritten = {
        "type": "assistant",
        "timestamp": "2026-08-01T10:01:00.000Z",
        "message": {"id": "new", "model": "m", "usage": {"input_tokens": 20, "output_tokens": 6}},
    }
    session.write_text(json.dumps(rewritten) + "\n", encoding="utf-8")
    os.utime(session, ns=(original_mtime, original_mtime))

    result = parser.collect_incremental()
    assert [r.uuid for r in result.records] == ["claude:new"]


def test_claude_fallback_uuid_includes_line_offset(monkeypatch, tmp_path):
    """旧版无 message.id 的同毫秒回复不能互相覆盖。"""
    _setup_db(monkeypatch, tmp_path)
    from app.usage.parsers import claude as claude_mod

    project = tmp_path / "p"
    project.mkdir()
    session = project / "s.jsonl"
    base = {
        "type": "assistant",
        "timestamp": "2026-08-01T10:00:00Z",
        "message": {"model": "claude-sonnet-5", "usage": {"input_tokens": 1, "output_tokens": 2}},
    }
    session.write_text(json.dumps(base) + "\n" + json.dumps(base) + "\n", encoding="utf-8")
    monkeypatch.setattr(claude_mod, "CLAUDE_PROJECTS_DIR", tmp_path)

    records = claude_mod.ClaudeParser().collect_incremental().records
    assert len(records) == 2
    assert len({r.uuid for r in records}) == 2


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


def test_codex_incremental_scan_restores_model_context(monkeypatch, tmp_path):
    """追加 token_count 没有新的 turn_context 时仍沿用上一轮模型。"""
    store = _setup_db(monkeypatch, tmp_path)
    from app.usage.parsers import codex as codex_mod

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    f = sessions / "rollout-context.jsonl"

    def token(ts: str, value: int) -> dict:
        usage = {
            "input_tokens": value,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 1,
            "reasoning_output_tokens": 0,
        }
        return {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"last_token_usage": usage}},
        }

    f.write_text(
        json.dumps({"type": "turn_context", "payload": {"model": "provider::gpt-special"}})
        + "\n"
        + json.dumps(token("2026-08-01T10:00:00Z", 10))
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_mod, "CODEX_SESSIONS_DIR", sessions)
    monkeypatch.setattr(codex_mod, "CODEX_ARCHIVED_DIR", tmp_path / "archived")
    parser = codex_mod.CodexParser()
    assert parser.collect_incremental().records[0].model == "gpt-special"
    cursor = store.get_cursor("codex", str(f))
    assert cursor["last_model"] == "gpt-special"

    with f.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(token("2026-08-01T10:00:01Z", 20)) + "\n")
    appended = parser.collect_incremental().records
    assert len(appended) == 1
    assert appended[0].model == "gpt-special"


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


def test_opencode_message_rowid_cursor_handles_same_timestamp_and_numeric_string(monkeypatch, tmp_path):
    """旧 message.data 版本按 rowid 增量，不漏同毫秒新增消息。"""
    store = _setup_db(monkeypatch, tmp_path)
    from app.usage.parsers import opencode as oc_mod

    db = tmp_path / "opencode-old.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, time_updated TEXT);
        CREATE TABLE message (session_id TEXT, data TEXT);
        INSERT INTO session VALUES ('s1', '1722507600');
        """
    )
    payload = {
        "role": "assistant",
        "modelID": "gpt-old",
        "tokens": {"input": 10, "output": 2, "cache": {"read": 1, "write": 0}},
        "time": {"created": "1722507600"},
    }
    conn.execute("INSERT INTO message VALUES (?, ?)", ("s1", json.dumps(payload)))
    conn.execute("INSERT INTO message VALUES (?, ?)", ("s1", json.dumps(payload)))
    conn.commit()
    conn.close()
    monkeypatch.setattr(oc_mod, "_db_path", lambda: db)

    parser = oc_mod.OpenCodeParser()
    first = parser.collect_incremental().records
    assert len(first) == 2
    assert len({r.uuid for r in first}) == 2
    assert first[0].timestamp_ms == 1722507600000
    cursor = store.get_cursor("opencode", str(db))
    assert cursor["watermark_rowid"] == 2

    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO message VALUES (?, ?)", ("s1", json.dumps(payload)))
    conn.commit()
    conn.close()
    second = parser.collect_incremental().records
    assert len(second) == 1
    assert second[0].uuid.endswith(":3")


def test_grok_fallback_uuid_includes_line_offset(monkeypatch, tmp_path):
    """旧版 Grok 事件缺 prompt_id 时，同时间戳记录也要分别保留。"""
    _setup_db(monkeypatch, tmp_path)
    from app.usage.parsers import grok as grok_mod

    session_dir = tmp_path / "sessions" / "encoded-cwd" / "session-1"
    session_dir.mkdir(parents=True)
    (session_dir / "summary.json").write_text(json.dumps({"model": "grok-4"}), encoding="utf-8")
    event = {
        "method": "_x.ai/session/update",
        "params": {
            "sessionUpdate": "turn_completed",
            "timestamp": "2026-08-01T10:00:00Z",
            "usage": {"inputTokens": 10, "cachedReadTokens": 2, "outputTokens": 3},
        },
    }
    updates = session_dir / "updates.jsonl"
    updates.write_text(json.dumps(event) + "\n" + json.dumps(event) + "\n", encoding="utf-8")
    monkeypatch.setattr(grok_mod, "GROK_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(grok_mod, "GROK_ARCHIVED_DIR", tmp_path / "archived")

    records = grok_mod.GrokParser().collect_incremental().records
    assert len(records) == 2
    assert len({r.uuid for r in records}) == 2


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
