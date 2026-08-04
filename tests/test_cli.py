"""app/cli.py 的命令行单测。

全部通过 monkeypatch 隔离：query_channel 打桩成假函数（不发任何真实网络请求），
CONFIG_PATH 用 isolated_config fixture 指向 tmp_path。断言退出码 + capsys 输出。
"""

from __future__ import annotations

import pytest

from app import cli
from app import config as config_store
from app.models import amount, fail, ok, window


async def _fake_channel_result(channel):
    """默认假查询：余额类返回 ok，订阅类返回 not_found。"""
    if channel.type == "deepseek":
        return ok(
            id=channel.id,
            type=channel.type,
            name=channel.name,
            category="balance",
            amount=amount(12.34, "CNY"),
            windows=[window("balance", "账户余额", remaining_percent=61.7)],
        )
    return fail(
        "not_found",
        "未检测到本机登录",
        id=channel.id,
        type=channel.type,
        name=channel.name,
        category="subscription",
    )


def _make_channel(**kwargs) -> config_store.Channel:
    """直接写一条渠道进临时 config（不经 CLI）。"""
    data = {"id": "ch1", "type": "deepseek", "name": "DeepSeek 测试", "api_key": "sk-OLDKEY1234567890"}
    data.update(kwargs)
    return config_store.upsert_channel(
        data,
        provided_fields=set(data) | {"id"},
    )


def _run(monkeypatch, capsys, argv, query=None):
    """执行 CLI 并返回 (退出码, stdout, stderr)。"""
    monkeypatch.setattr(cli, "query_channel", query or _fake_channel_result)
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_quota_text_ok(monkeypatch, capsys, isolated_config):
    _make_channel()
    code, out, _ = _run(monkeypatch, capsys, ["quota"])
    assert code == 0
    assert "DeepSeek 测试" in out
    assert "✓" in out
    assert "12.34 CNY" in out
    assert "61.7%" in out


def test_quota_json_structure(monkeypatch, capsys, isolated_config):
    _make_channel()
    code, out, _ = _run(monkeypatch, capsys, ["quota", "--json"])
    assert code == 0
    import json

    body = json.loads(out)
    assert "generated_at" in body
    assert len(body["channels"]) == 1
    ch = body["channels"][0]
    assert ch["id"] == "ch1"
    assert ch["status"] == "ok"
    assert ch["amount"]["value"] == 12.34
    assert ch["windows"][0]["remaining_percent"] == 61.7


def test_quota_brief_single_line(monkeypatch, capsys, isolated_config):
    _make_channel()
    _make_channel(id="ch2", name="另一个渠道", type="claude_subscription")
    code, out, _ = _run(monkeypatch, capsys, ["quota", "--brief"])
    assert code == 1  # 存在 not_found（未登录）渠道
    lines = out.strip().splitlines()
    assert len(lines) == 1
    assert "DeepSeek 测试 61.7%" in out
    assert "另一个渠道 ○" in out  # not_found 用 ○ 符号


def test_quota_error_exit_code(monkeypatch, capsys, isolated_config):
    _make_channel()

    async def broken(channel):
        return fail(
            "error",
            "HTTP 500",
            id=channel.id,
            type=channel.type,
            name=channel.name,
            category="balance",
        )

    code, out, _ = _run(monkeypatch, capsys, ["quota"], query=broken)
    assert code == 1
    assert "✗" in out
    assert "HTTP 500" in out


def test_quota_disabled_not_queried(monkeypatch, capsys, isolated_config):
    _make_channel(enabled=False)
    queried = []

    async def spy(channel):
        queried.append(channel.id)
        return _fake_channel_result(channel)

    code, out, _ = _run(monkeypatch, capsys, ["quota"], query=spy)
    assert code == 0  # disabled 不算失败
    assert queried == []  # 停用渠道不发起查询
    assert "已停用" in out


def test_quota_ids_filter(monkeypatch, capsys, isolated_config):
    _make_channel()
    _make_channel(id="ch2", name="另一个渠道")
    queried = []

    async def spy(channel):
        queried.append(channel.id)
        return await _fake_channel_result(channel)

    code, out, _ = _run(monkeypatch, capsys, ["quota", "--ids", "ch2"], query=spy)
    assert code == 0
    assert queried == ["ch2"]
    assert "另一个渠道" in out
    assert "DeepSeek 测试" not in out


def test_quota_json_and_brief_mutually_exclusive(monkeypatch, capsys, isolated_config):
    _make_channel()
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, capsys, ["quota", "--json", "--brief"])
    assert exc.value.code == 2  # argparse 用法错误


def test_channels_lists_masked(monkeypatch, capsys, isolated_config):
    _make_channel(api_key="sk-ABCDEFGHIJKLMNOP")
    code, out, _ = _run(monkeypatch, capsys, ["channels"])
    assert code == 0
    assert "ch1" in out
    assert "sk-ABCDEFGHIJKLMNOP" not in out
    assert "sk-A********MNOP" in out


def test_channels_json(monkeypatch, capsys, isolated_config):
    _make_channel()
    code, out, _ = _run(monkeypatch, capsys, ["channels", "--json"])
    assert code == 0
    import json

    items = json.loads(out)
    assert items[0]["id"] == "ch1"
    assert "api_key_masked" in items[0]
    assert "api_key" not in items[0]  # 绝不输出明文密钥


def test_set_api_key_updates_and_masks(monkeypatch, capsys, isolated_config):
    _make_channel()
    code, out, _ = _run(
        monkeypatch, capsys, ["config", "set-api-key", "--channel", "ch1", "--key", "sk-NEWKEY1234567890"]
    )
    assert code == 0
    assert "sk-N********7890" in out
    # 真实密钥确实写进了 config，且旧密钥被替换
    assert config_store.get_channel("ch1").api_key == "sk-NEWKEY1234567890"


def test_set_api_key_preserves_other_fields(monkeypatch, capsys, isolated_config):
    _make_channel(name="原名", enabled=False)
    _run(monkeypatch, capsys, ["config", "set-api-key", "--channel", "ch1", "--key", "sk-NEWKEY1234567890"])
    updated = config_store.get_channel("ch1")
    assert updated.name == "原名"  # 最小 payload 不覆盖其它字段
    assert updated.enabled is False


def test_set_api_key_missing_channel(monkeypatch, capsys, isolated_config):
    code, _, err = _run(monkeypatch, capsys, ["config", "set-api-key", "--channel", "nope", "--key", "sk-X"])
    assert code == 1
    assert "渠道不存在" in err


def test_set_api_key_subscription_type_rejected(monkeypatch, capsys, isolated_config):
    _make_channel(type="claude_subscription", name="Claude 订阅")
    code, _, err = _run(monkeypatch, capsys, ["config", "set-api-key", "--channel", "ch1", "--key", "sk-X"])
    assert code == 1
    assert "不存储 API Key" in err


def test_cost_text(monkeypatch, capsys, isolated_config, tmp_path):
    fake = {
        "days": 14,
        "sources": [
            {
                "key": "claude_code",
                "label": "Claude Code 本地已用统计",
                "available": True,
                "message": None,
                "path": str(tmp_path),
                "model_stats": [],
                "totals": {"sessions": 5, "messages": 60, "input": 100, "output": 200, "cost": 0.0, "has_cost": False},
            }
        ],
    }
    monkeypatch.setattr(cli.local_usage, "get_local_usage", lambda days: fake)
    code, out, _ = _run(monkeypatch, capsys, ["cost"])
    assert code == 0
    assert "5 会话" in out
    assert "无费用数据" in out


def test_cost_unavailable_source(monkeypatch, capsys, isolated_config):
    fake = {
        "days": 14,
        "sources": [
            {
                "key": "claude_code",
                "label": "Claude Code 本地已用统计",
                "available": False,
                "message": "目录不存在",
                "path": None,
                "model_stats": [],
                "totals": {},
            }
        ],
    }
    monkeypatch.setattr(cli.local_usage, "get_local_usage", lambda days: fake)
    code, out, _ = _run(monkeypatch, capsys, ["cost"])
    assert code == 0
    assert "不可用" in out
    assert "目录不存在" in out


def test_config_corrupted_exit_code_2(monkeypatch, capsys, isolated_config):
    isolated_config.write_text("{ 这不是合法 JSON", encoding="utf-8")
    code, _, err = _run(monkeypatch, capsys, ["quota"])
    assert code == 2
    assert "配置" in err
