"""app/config.py 的单测：mask_secret / is_masked_secret / 原子写 / 损坏恢复 /
upsert_channel 的字段保留逻辑（P0 级 bug 的回归测试）。

所有测试都通过 isolated_config fixture 把 CONFIG_PATH 指到 tmp_path，绝不碰
项目根目录的真实 config.json。
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from app import config as config_store

# ── mask_secret / is_masked_secret ──────────────────────────────


def test_mask_secret_empty():
    assert config_store.mask_secret("") == ""
    assert config_store.mask_secret(None) == ""


def test_mask_secret_short():
    assert config_store.mask_secret("abc") == "***"
    assert config_store.mask_secret("12345678") == "********"  # 恰好 8 位


def test_mask_secret_long():
    assert config_store.mask_secret("sk-1234567890ABCD") == "sk-1********ABCD"


def test_is_masked_secret_detects_short_form():
    assert config_store.is_masked_secret("***") is True
    assert config_store.is_masked_secret("*") is True


def test_is_masked_secret_detects_long_form():
    assert config_store.is_masked_secret("sk-1********ABCD") is True


def test_is_masked_secret_rejects_real_values():
    assert config_store.is_masked_secret("") is False
    assert config_store.is_masked_secret(None) is False
    assert config_store.is_masked_secret("abc") is False  # 短，但不全是 *
    assert config_store.is_masked_secret("sk-TESTKEY1234567890ABCD") is False  # 真实长 key


def test_mask_secret_output_always_recognized_as_masked():
    """mask_secret 的输出必须总能被 is_masked_secret 识别——这是保护逻辑成立的前提。"""
    for real in ("a", "12345678", "sk-TESTKEY1234567890ABCD", "x" * 100):
        masked = config_store.mask_secret(real)
        assert config_store.is_masked_secret(masked) is True


# ── upsert_channel：打码回传保护（P0 第 1 条的回归测试）──────────


def test_upsert_channel_preserves_real_key_when_masked_value_echoed_back(
    isolated_config,
):
    created = config_store.upsert_channel(
        {"type": "deepseek", "name": "测试渠道", "api_key": "sk-TESTKEY1234567890ABCD"},
        provided_fields={"type", "name", "api_key"},
    )
    masked = config_store.mask_secret(created.api_key)

    # 模拟一个"编辑时原样回填打码值"的前端：POST 回来的 api_key 就是打码串
    updated = config_store.upsert_channel(
        {"id": created.id, "type": "deepseek", "name": "测试渠道", "api_key": masked},
        provided_fields={"id", "type", "name", "api_key"},
    )
    assert updated.api_key == "sk-TESTKEY1234567890ABCD", "真实密钥被打码串覆盖丢失了"

    # 从磁盘读回的原始存储（secret=True 落盘）也必须是真实密钥，不是打码串
    raw = json.loads(config_store.CONFIG_PATH.read_text(encoding="utf-8"))
    stored = next(c for c in raw["channels"] if c["id"] == created.id)
    assert stored["api_key"] == "sk-TESTKEY1234567890ABCD"


def test_upsert_channel_preserves_real_key_when_field_omitted(isolated_config):
    """密钥字段完全不出现在请求里（不在 provided_fields 里）也必须保留旧值。"""
    created = config_store.upsert_channel(
        {"type": "deepseek", "api_key": "sk-realkey12345678"},
        provided_fields={"type", "api_key"},
    )
    updated = config_store.upsert_channel(
        {"id": created.id, "type": "deepseek"},
        provided_fields={"id", "type"},
    )
    assert updated.api_key == "sk-realkey12345678"


# ── upsert_channel：按字段是否出现在请求里合并（协调者追加的那条硬要求）──


def test_upsert_channel_minimal_payload_preserves_untouched_fields(isolated_config):
    """前端"启用/停用"这种最小 payload（只有 id/type/enabled）不能清空其它字段，
    也不能让 name 回退成 PROVIDERS 的 default_name。"""
    created = config_store.upsert_channel(
        {
            "type": "volcengine",
            "name": "我的自定义名称",
            "ak": "AKtest1234",
            "sk": "SKtest1234",
            "region": "cn-shanghai",
        },
        provided_fields={"type", "name", "ak", "sk", "region"},
    )

    toggled = config_store.upsert_channel(
        {"id": created.id, "type": "volcengine", "enabled": False},
        provided_fields={"id", "type", "enabled"},
    )

    assert toggled.enabled is False
    assert toggled.name == "我的自定义名称"
    assert toggled.region == "cn-shanghai"
    assert toggled.ak == "AKtest1234"
    assert toggled.sk == "SKtest1234"


def test_upsert_channel_explicit_empty_value_clears_optional_field(isolated_config):
    """字段显式出现在请求里、值是空字符串——视为用户想清空，必须真的清空，
    不能被当成"没提供"而沿用旧值。"""
    created = config_store.upsert_channel(
        {
            "type": "volcengine",
            "name": "我的自定义名称",
            "ak": "AKtest1234",
            "sk": "SKtest1234",
            "region": "cn-shanghai",
        },
        provided_fields={"type", "name", "ak", "sk", "region"},
    )

    cleared = config_store.upsert_channel(
        {"id": created.id, "type": "volcengine", "region": ""},
        provided_fields={"id", "type", "region"},
    )

    assert not cleared.region  # 显式清空生效
    assert cleared.name == "我的自定义名称"  # 没提到的字段依然保留


def test_upsert_channel_without_provided_fields_falls_back_to_legacy_behavior(
    isolated_config,
):
    """provided_fields=None（未指定）时退化为旧行为：只保护三个密钥字段，
    其余缺失字段仍走 Channel.from_dict 的默认处理——保证旧调用方不受影响。"""
    created = config_store.upsert_channel(
        {"type": "deepseek", "name": "旧行为渠道", "api_key": "sk-old-behavior"},
    )
    updated = config_store.upsert_channel({"id": created.id, "type": "deepseek"})
    assert updated.api_key == "sk-old-behavior"  # 密钥字段始终受保护
    assert updated.name == "DeepSeek 账户余额"  # 未受保护字段按旧行为回退成默认名


# ── 原子写 / 损坏恢复 ──────────────────────────────────────────


def test_save_and_load_roundtrip(isolated_config):
    data = {"channels": [{"id": "ch_1", "type": "deepseek", "name": "x", "enabled": True}]}
    config_store._save_raw(data)
    assert config_store._load_raw() == data


def test_save_raw_sets_permissions_600(isolated_config):
    config_store._save_raw({"channels": []})
    mode = stat.S_IMODE(os.stat(config_store.CONFIG_PATH).st_mode)
    assert mode == 0o600


def test_save_raw_leaves_no_tmp_file_behind(isolated_config):
    config_store._save_raw({"channels": []})
    leftovers = list(config_store.CONFIG_PATH.parent.glob(f".{config_store.CONFIG_PATH.name}.*.tmp"))
    assert leftovers == []


def test_load_raw_corrupted_backs_up_and_raises(isolated_config):
    config_store.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_store.CONFIG_PATH.write_text("{not valid json!!", encoding="utf-8")

    with pytest.raises(config_store.ConfigCorruptedError):
        config_store._load_raw()

    # 坏文件必须被复制备份（不是移走——移走后下次 _load_raw 就找不到文件，
    # 回退成"空配置"，用户一保存就用空配置覆盖，密钥全丢）。
    backups = list(config_store.CONFIG_PATH.parent.glob(f"{config_store.CONFIG_PATH.name}.corrupted.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not valid json!!"
    # 原文件必须仍在原地，让错误持续暴露，直到用户手动修复
    assert config_store.CONFIG_PATH.exists()
    assert config_store.CONFIG_PATH.read_text(encoding="utf-8") == "{not valid json!!"


def test_load_raw_corrupted_does_not_silently_return_empty(isolated_config):
    """核心诉求：损坏配置绝不能假装"没有渠道"，否则用户一保存就会把坏文件
    彻底覆盖，密钥全丢。"""
    config_store.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_store.CONFIG_PATH.write_text("not json at all", encoding="utf-8")
    with pytest.raises(config_store.ConfigCorruptedError):
        config_store.list_channels()


def test_load_raw_corrupted_persists_across_reads(isolated_config):
    """损坏错误必须持续暴露，不能只闪一次。坏文件留在原地，每次 _load_raw
    都应重新报错——这是防止"错误消失后用户不知情地保存导致覆盖"的关键。"""
    config_store.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_store.CONFIG_PATH.write_text("still broken", encoding="utf-8")
    # 第一次读取：报错
    with pytest.raises(config_store.ConfigCorruptedError):
        config_store._load_raw()
    # 第二次读取：仍然报错（没有回退成空配置）
    with pytest.raises(config_store.ConfigCorruptedError):
        config_store._load_raw()
    # 坏文件仍在原地
    assert config_store.CONFIG_PATH.exists()


def test_load_raw_missing_file_returns_empty_channels(isolated_config):
    """文件压根不存在（还没保存过任何渠道）是正常的空状态，不是损坏。"""
    assert config_store._load_raw() == {"channels": []}


# ── extra 字段序列化（P2 第 13 条）────────────────────────────


def test_extra_field_round_trips(isolated_config):
    created = config_store.upsert_channel(
        {"type": "deepseek", "api_key": "sk-x", "extra": {"note": "hello"}},
        provided_fields={"type", "api_key", "extra"},
    )
    assert created.extra == {"note": "hello"}
    raw = json.loads(config_store.CONFIG_PATH.read_text(encoding="utf-8"))
    stored = next(c for c in raw["channels"] if c["id"] == created.id)
    assert stored.get("extra") == {"note": "hello"}

    reloaded = config_store.get_channel(created.id)
    assert reloaded.extra == {"note": "hello"}


# ── 配置导入/导出 ──────────────────────────────────────────────


def test_export_includes_secrets_when_requested(isolated_config):
    config_store.upsert_channel(
        {"type": "deepseek", "name": "DS", "api_key": "sk-secret1234567890"},
        provided_fields={"type", "name", "api_key"},
    )
    data = config_store.export_config(include_secrets=True)
    assert data["version"] == 1
    assert len(data["channels"]) == 1
    assert data["channels"][0]["api_key"] == "sk-secret1234567890"


def test_export_strips_secrets_when_safe(isolated_config):
    config_store.upsert_channel(
        {"type": "deepseek", "name": "DS", "api_key": "sk-secret1234567890"},
        provided_fields={"type", "name", "api_key"},
    )
    data = config_store.export_config(include_secrets=False)
    assert "api_key" not in data["channels"][0]
    assert data["channels"][0]["name"] == "DS"  # 非密钥字段保留


def test_import_merge_appends_and_preserves_existing_secrets(isolated_config):
    """merge 模式：导入数据没带密钥时，沿用现有同 id 渠道的密钥。"""
    config_store.upsert_channel(
        {"id": "ch_1", "type": "deepseek", "name": "原DS", "api_key": "sk-realsecret1234"},
        provided_fields={"id", "type", "name", "api_key"},
    )
    payload = {
        "version": 1,
        "channels": [{"id": "ch_1", "type": "deepseek", "name": "改名DS"}],  # 没带 api_key
    }
    result = config_store.import_config(payload, mode="merge")
    assert result["count"] == 1
    reloaded = config_store.get_channel("ch_1")
    assert reloaded.name == "改名DS"
    assert reloaded.api_key == "sk-realsecret1234"  # 密钥沿用


def test_import_replace_clears_all(isolated_config):
    config_store.upsert_channel({"type": "deepseek", "api_key": "sk-old1"})
    payload = {"version": 1, "channels": [{"type": "kimi_api", "api_key": "sk-new1", "base_url": "https://x.com"}]}
    result = config_store.import_config(payload, mode="replace")
    assert result["count"] == 1
    channels = config_store.list_channels()
    assert len(channels) == 1
    assert channels[0].type == "kimi_api"


def test_import_rejects_unknown_type(isolated_config):
    payload = {"version": 1, "channels": [{"type": "nonexistent_provider", "api_key": "x"}]}
    with pytest.raises(config_store.ImportConfigError):
        config_store.import_config(payload)


def test_import_rejects_non_list_channels(isolated_config):
    with pytest.raises(config_store.ImportConfigError):
        config_store.import_config({"channels": "not a list"})
