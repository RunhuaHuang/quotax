"""app/local_usage.py 的单测：opencode 的 _parse_message_data，新增的 Claude Code
transcript 聚合，以及 opencode 两条聚合路径（新版 session 聚合列 / 老版
message.data JSON）的正确性——尤其是第 11 条列出的几个 bug 的回归测试：
messages 语义错误、days 硬编码、message 级时间过滤不精确。

不发起任何网络请求；sqlite 数据库全部用 tmp_path 现造，不读用户真实的
~/.local/share/opencode/opencode.db；transcript 目录用 monkeypatch 指到 tmp_path，
不读用户真实的 ~/.claude/projects。
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta

from app import local_usage

# ── _parse_message_data（opencode）──────────────────────────────


def test_parse_message_data_valid_assistant_message():
    data = {
        "role": "assistant",
        "cost": 0.01,
        "tokens": {
            "input": 100,
            "output": 50,
            "reasoning": 5,
            "cache": {"read": 20, "write": 3},
        },
        "modelID": "deepseek-v4-flash-free",
        "time": {"created": 1785741028621},
    }
    parsed = local_usage._parse_message_data(json.dumps(data))
    assert parsed == {
        "input": 100,
        "output": 50,
        "reasoning": 5,
        "cache_read": 20,
        "cache_write": 3,
        "cost": 0.01,
        "model": "deepseek-v4-flash-free",
        "created": 1785741028621,
    }


def test_parse_message_data_skips_user_role():
    data = {"role": "user", "time": {"created": 1}}
    assert local_usage._parse_message_data(json.dumps(data)) is None


def test_parse_message_data_handles_bad_json():
    assert local_usage._parse_message_data("not json") is None
    assert local_usage._parse_message_data(None) is None


# ── Claude Code transcript 聚合 ──────────────────────────────────


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _write_jsonl(path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


def test_claude_code_usage_dedup_and_time_window(tmp_path, monkeypatch):
    projects_dir = tmp_path / "claude_projects"
    monkeypatch.setattr(local_usage, "CLAUDE_PROJECTS_DIR", projects_dir)

    now = datetime.now(UTC)
    recent = _iso(now - timedelta(hours=1))
    old = _iso(now - timedelta(days=100))

    session_file = projects_dir / "-Users-x-proj-a" / "11111111-1111-1111-1111-111111111111.jsonl"
    _write_jsonl(
        session_file,
        [
            # 正常一条
            {
                "type": "assistant",
                "timestamp": recent,
                "message": {
                    "id": "msg-1",
                    "model": "claude-opus-5",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "cache_creation_input_tokens": 5,
                        "cache_read_input_tokens": 3,
                    },
                },
            },
            # 同一条消息在续接会话里重复出现——必须按 message.id 去重，不能重复计数
            {
                "type": "assistant",
                "timestamp": recent,
                "message": {
                    "id": "msg-1",
                    "model": "claude-opus-5",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "cache_creation_input_tokens": 5,
                        "cache_read_input_tokens": 3,
                    },
                },
            },
            # 非 assistant 行必须跳过
            {
                "type": "user",
                "timestamp": recent,
                "message": {"model": "claude-opus-5"},
            },
            # 时间在窗口之外，必须被过滤掉（哪怕 message.id 不同）
            {
                "type": "assistant",
                "timestamp": old,
                "message": {
                    "id": "msg-old",
                    "model": "claude-opus-5",
                    "usage": {
                        "input_tokens": 999,
                        "output_tokens": 999,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            },
            # 没有 message.id，退化为 (timestamp, model, output_tokens) 去重
            {
                "type": "assistant",
                "timestamp": recent,
                "message": {
                    "model": "claude-sonnet-5",
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            },
            {
                "type": "assistant",
                "timestamp": recent,
                "message": {
                    "model": "claude-sonnet-5",
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            },
        ],
    )

    result = local_usage.get_claude_code_usage(days=14)
    assert result["available"] is True
    assert result["key"] == "claude_code"

    totals = result["totals"]
    assert totals["messages"] == 2  # msg-1（去重后 1 条）+ 无 id 的那条（去重后 1 条）
    assert totals["input"] == 10 + 1
    assert totals["output"] == 20 + 2
    assert totals["cache_read"] == 3
    assert totals["cache_write"] == 5
    assert totals["cost"] == 0.0
    assert totals["has_cost"] is False  # transcript 没有费用字段，绝不编造

    models = {m["model"]: m for m in result["model_stats"]}
    assert models["claude-opus-5"]["messages"] == 1
    assert models["claude-sonnet-5"]["messages"] == 1
    assert "msg-old" not in json.dumps(result)  # old 那条的痕迹不该出现


def test_claude_code_usage_missing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(local_usage, "CLAUDE_PROJECTS_DIR", tmp_path / "does_not_exist")
    result = local_usage.get_claude_code_usage(days=14)
    assert result["available"] is False
    assert result["model_stats"] == []
    assert result["totals"] == {}
    assert result["message"]


def test_claude_code_usage_skips_rows_without_usage_dict(tmp_path, monkeypatch):
    """message.usage 缺失或不是 dict 时不应该崩溃，也不应该计入统计。"""
    projects_dir = tmp_path / "claude_projects"
    monkeypatch.setattr(local_usage, "CLAUDE_PROJECTS_DIR", projects_dir)
    recent = _iso(datetime.now(UTC))
    _write_jsonl(
        projects_dir / "proj" / "22222222-2222-2222-2222-222222222222.jsonl",
        [
            {
                "type": "assistant",
                "timestamp": recent,
                "message": {"id": "m1", "model": "claude-opus-5"},
            },  # 无 usage
            {
                "type": "assistant",
                "timestamp": recent,
                "message": {
                    "id": "m2",
                    "model": "claude-opus-5",
                    "usage": "not-a-dict",
                },
            },
        ],
    )
    result = local_usage.get_claude_code_usage(days=14)
    assert result["available"] is True
    assert result["totals"] == dict(local_usage.EMPTY_TOTALS)


# ── OpenCode：新版 session 聚合列路径 ─────────────────────────────


def _make_db(tmp_path, name="opencode.db"):
    path = tmp_path / name
    conn = sqlite3.connect(str(path))
    return path, conn


def test_opencode_new_schema_messages_is_real_count_not_session_count(tmp_path, monkeypatch):
    """回归测试：totals['messages'] 之前被算成"各模型的会话数之和"，语义等价于
    sessions，现在必须是 message 表里真实的 assistant 消息条数。"""
    db_path, conn = _make_db(tmp_path)
    now_ms = int(time.time() * 1000)
    old_ms = now_ms - 200 * 86400 * 1000

    conn.execute(
        "CREATE TABLE session (id TEXT PRIMARY KEY, model TEXT, cost REAL, tokens_input INTEGER, "
        "tokens_output INTEGER, tokens_reasoning INTEGER, tokens_cache_read INTEGER, "
        "tokens_cache_write INTEGER, time_updated INTEGER, time_archived INTEGER)"
    )
    conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT)")

    model_json = json.dumps({"id": "deepseek-v4-flash", "providerID": "opencode-go", "variant": "high"})
    conn.execute(
        "INSERT INTO session VALUES ('ses_a', ?, 0.01, 100, 50, 10, 20, 5, ?, NULL)",
        (model_json, now_ms),
    )
    # 会话早在窗口之外，必须被完全排除
    conn.execute(
        "INSERT INTO session VALUES ('ses_old', ?, 99.0, 9999, 9999, 0, 0, 0, ?, NULL)",
        (model_json, old_ms),
    )
    for i in range(2):
        conn.execute(
            "INSERT INTO message VALUES (?, 'ses_a', ?)",
            (
                f"msg_{i}",
                json.dumps({"role": "assistant", "modelID": "deepseek-v4-flash"}),
            ),
        )
    conn.execute(
        "INSERT INTO message VALUES ('msg_user', 'ses_a', ?)",
        (json.dumps({"role": "user"}),),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("OPENCODE_DB", str(db_path))
    result = local_usage.get_opencode_usage(days=14)

    assert result["available"] is True
    assert result["days"] == 14  # 之前硬编码 None 的 bug
    assert result["totals"]["sessions"] == 1
    assert result["totals"]["messages"] == 2  # 真实消息数，不是会话数（1）
    assert result["totals"]["input"] == 100
    assert result["totals"]["output"] == 50
    assert result["totals"]["cache_read"] == 20
    assert result["totals"]["cache_write"] == 5
    assert result["model_stats"][0]["messages"] == 2
    assert result["model_stats"][0]["sessions"] == 1


def test_opencode_old_schema_filters_by_message_own_timestamp(tmp_path, monkeypatch):
    """回归测试：_aggregate_from_messages 之前只按 session.time_updated 粗筛，
    会把该会话全部历史消息都计入。这里构造一个"最近被更新，但混着一条很久以前
    的历史消息"的会话，验证历史消息不会被计入。"""
    db_path, conn = _make_db(tmp_path)
    now_ms = int(time.time() * 1000)
    old_created_ms = now_ms - 200 * 86400 * 1000

    # 老版 schema：session 表没有 tokens_input 列
    conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, time_updated INTEGER)")
    conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT)")
    conn.execute("INSERT INTO session VALUES ('ses_c', ?)", (now_ms,))  # 会话本身最近被更新过

    recent_msg = {
        "role": "assistant",
        "cost": 0.01,
        "tokens": {"input": 100, "output": 50, "cache": {"read": 0, "write": 0}},
        "modelID": "m1",
        "time": {"created": now_ms},
    }
    old_msg = {
        # 这条消息自己的时间很久以前，尽管所属会话最近被更新过——不该被计入
        "role": "assistant",
        "cost": 99.0,
        "tokens": {"input": 9999, "output": 9999, "cache": {"read": 0, "write": 0}},
        "modelID": "m1",
        "time": {"created": old_created_ms},
    }
    conn.execute("INSERT INTO message VALUES ('m_recent', 'ses_c', ?)", (json.dumps(recent_msg),))
    conn.execute("INSERT INTO message VALUES ('m_old', 'ses_c', ?)", (json.dumps(old_msg),))
    conn.commit()
    conn.close()

    monkeypatch.setenv("OPENCODE_DB", str(db_path))
    result = local_usage.get_opencode_usage(days=14)

    assert result["available"] is True
    assert result["totals"]["messages"] == 1
    assert result["totals"]["input"] == 100  # 不是 100 + 9999
    assert result["totals"]["output"] == 50
    assert result["totals"]["cost"] == 0.01


def test_opencode_usage_unavailable_shape_is_consistent(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "does_not_exist.db"))
    result = local_usage.get_opencode_usage(days=7)
    assert result["available"] is False
    assert result["model_stats"] == []
    assert result["totals"] == {}
    assert result["days"] == 7


# ── 统一多数据源接口 ─────────────────────────────────────────────


def test_get_local_usage_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(local_usage, "CLAUDE_PROJECTS_DIR", tmp_path / "no_claude_here")
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "no_opencode_here.db"))

    result = local_usage.get_local_usage(days=5)
    assert result["days"] == 5
    keys = {s["key"] for s in result["sources"]}
    assert keys == {"claude_code", "opencode"}
    for source in result["sources"]:
        assert set(source.keys()) == {
            "key",
            "label",
            "available",
            "message",
            "path",
            "model_stats",
            "totals",
        }
        assert source["available"] is False
        assert source["model_stats"] == []
        assert source["totals"] == {}
