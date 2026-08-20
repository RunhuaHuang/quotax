"""app/config.py 的单测：mask_secret / is_masked_secret / 原子写 / 损坏恢复 /
upsert_channel 的字段保留逻辑（P0 级 bug 的回归测试）。

所有测试都通过 isolated_config fixture 把 CONFIG_PATH 指到 tmp_path，绝不碰
项目根目录的真实 config.json。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import threading

import pytest

from app import config as config_store

# ── assert_public_http_url_async（异步版 SSRF 校验，不阻塞事件循环）──────────────


def test_async_url_check_rejects_private_addresses():
    """字面私网/回环地址在 DNS 解析前就被拒绝，与网络状态无关。"""
    for url in (
        "http://127.0.0.1/x",
        "http://[::1]/x",
        "http://192.168.1.1/x",
        "http://localhost/x",
        "https://metadata.google.internal/",
    ):
        with pytest.raises(config_store.SettingsValidationError):
            asyncio.run(config_store.assert_public_http_url_async(url, field_name="测试 URL"))


def test_async_url_check_allows_fake_ip_proxy_range():
    """198.18.0.0/15 是 Clash TUN fake-ip 的回传段（被代理域名在本机一律解析成
    它），真实路由由代理管控——必须放行，否则 fake-ip 用户所有境外渠道全挂。"""
    loop = asyncio.new_event_loop()
    try:

        async def run():
            await config_store.assert_public_http_url_async(
                "http://198.18.1.203/x", field_name="测试 URL"
            )

        loop.run_until_complete(run())
    finally:
        loop.close()
    # 字面 127.0.0.1 仍拒绝（放行范围仅限 fake-ip 段）
    with pytest.raises(config_store.SettingsValidationError):
        asyncio.run(config_store.assert_public_http_url_async("http://127.0.0.1/x", field_name="测试 URL"))


def _public_addr(host, port):
    """构造一个解析到公网 IP 的 getaddrinfo 结果（不依赖真实 DNS/网络环境）。"""
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("93.184.216.34", port),
        )
    ]


def test_async_url_check_accepts_public_hostname(monkeypatch):
    """域名解析到公网 IP 时应通过；DNS 解析失败（gaierror）时也被吞掉、不抛异常。

    不依赖真实网络：getaddrinfo 用 monkeypatch 固定返回值，避免沙箱 / CI / 内网 DNS
    把公网域名解析到保留段（如 198.18.x.x）导致测试被环境状态左右。
    """
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(
        loop,
        "getaddrinfo",
        lambda *a, **kw: asyncio.sleep(0, result=_public_addr(*a[:2])),
    )

    async def run():
        await config_store.assert_public_http_url_async("https://example.com/x", field_name="测试 URL")

    loop.run_until_complete(run())
    loop.close()


def test_async_url_check_swallows_dns_failure(monkeypatch):
    """DNS 暂时解析失败不是 SSRF 风险，应静默通过；真正的网络错误由请求层呈现。"""

    async def _failing_getaddrinfo(*a, **kw):
        raise socket.gaierror("temporary failure in name resolution")

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _failing_getaddrinfo)

    async def run():
        await config_store.assert_public_http_url_async("https://example.com/x", field_name="测试 URL")

    loop.run_until_complete(run())
    loop.close()


def test_async_url_check_rejects_private_resolved_address(monkeypatch):
    """域名即使能解析，一旦解析到私网/回环地址也必须被拒绝（DNS rebinding 防护）。"""

    def _private_addr(*a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 443))]

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(
        loop,
        "getaddrinfo",
        lambda *a, **kw: asyncio.sleep(0, result=_private_addr(*a, **kw)),
    )

    async def run():
        with pytest.raises(config_store.SettingsValidationError):
            await config_store.assert_public_http_url_async("https://rebind.example/x", field_name="测试 URL")

    loop.run_until_complete(run())
    loop.close()


def test_async_url_check_rejects_malformed_url():
    with pytest.raises(config_store.SettingsValidationError):
        asyncio.run(config_store.assert_public_http_url_async("not-a-url", field_name="测试 URL"))
    with pytest.raises(config_store.SettingsValidationError):
        asyncio.run(config_store.assert_public_http_url_async("ftp://example.com/x", field_name="测试 URL"))

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


def test_concurrent_upserts_do_not_lose_channels(isolated_config, monkeypatch):
    """两个线程同时做读改写时必须串行，否则后写者会用旧快照覆盖先写者。"""
    original_save = config_store._save_raw
    first_save_entered = threading.Event()
    release_first_save = threading.Event()
    second_done = threading.Event()

    def controlled_save(data):
        if not first_save_entered.is_set():
            first_save_entered.set()
            assert release_first_save.wait(timeout=2)
        original_save(data)

    monkeypatch.setattr(config_store, "_save_raw", controlled_save)

    def add(channel_id):
        config_store.upsert_channel(
            {"id": channel_id, "type": "deepseek", "api_key": f"sk-{channel_id}"},
            provided_fields={"id", "type", "api_key"},
        )
        if channel_id == "ch_b":
            second_done.set()

    first = threading.Thread(target=add, args=("ch_a",))
    second = threading.Thread(target=add, args=("ch_b",))
    first.start()
    assert first_save_entered.wait(timeout=1)
    second.start()

    # 第一笔事务还停在写入前时，第二笔不能越过完整的读改写锁。
    assert not second_done.wait(timeout=0.1)
    release_first_save.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert {c.id for c in config_store.list_channels()} == {"ch_a", "ch_b"}


# ── 原子写 / 损坏恢复 ──────────────────────────────────────────


def test_save_and_load_roundtrip(isolated_config):
    data = {"channels": [{"id": "ch_1", "type": "deepseek", "name": "x", "enabled": True}]}
    config_store._save_raw(data)
    assert config_store._load_raw() == data


def test_opencode_workspace_id_roundtrip_and_preserved_on_minimal_edit(isolated_config):
    """OpenCode 渠道的 workspace_id 字段必须能持久化往返，且编辑渠道时的最小
    payload（只发 id/type/enabled）不能把它清空——和 name/region 等字段同一套
    `_MERGE_ON_UPDATE_FIELDS` 保留语义。"""
    created = config_store.upsert_channel(
        {
            "type": "opencode_subscription",
            "name": "OpenCode",
            "api_key": "auth=abc",
            "workspace_id": "wrk_test123",
        },
        provided_fields={"type", "name", "api_key", "workspace_id"},
    )
    assert created.workspace_id == "wrk_test123"

    # 持久化往返：save → load，workspace_id 不丢
    reloaded = config_store.get_channel(created.id)
    assert reloaded.workspace_id == "wrk_test123"

    # 最小 payload（启停开关）不能清掉 workspace_id
    toggled = config_store.upsert_channel(
        {"id": created.id, "type": "opencode_subscription", "enabled": False},
        provided_fields={"id", "type", "enabled"},
    )
    assert toggled.workspace_id == "wrk_test123"


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


@pytest.mark.parametrize("channels", [{"id": "not-a-list"}, ["not-an-object"]])
def test_load_raw_rejects_invalid_channels_structure(isolated_config, channels):
    isolated_config.write_text(json.dumps({"channels": channels}), encoding="utf-8")
    with pytest.raises(config_store.ConfigCorruptedError, match="channels"):
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


def test_safe_export_redacts_nested_sensitive_extra_values(isolated_config):
    """Cookie/Token 藏在嵌套 extra 中时，安全导出也不能泄露。"""
    config_store.upsert_channel(
        {
            "type": "deepseek",
            "api_key": "sk-real",
            "extra": {
                "profile": {"cookie": "auth=TOP-SECRET", "note": "keep"},
                "items": [{"access_token": "AT-SECRET", "refreshToken": "RT-SECRET"}, {"label": "ok"}],
                "codex_auth_file": "credentials/codex_ch1.json",
            },
        },
        provided_fields={"type", "api_key", "extra"},
    )
    safe = config_store.export_config(include_secrets=False)
    extra = safe["channels"][0]["extra"]
    assert extra["profile"]["cookie"] == "[REDACTED]"
    assert extra["items"][0]["access_token"] == "[REDACTED]"
    assert extra["items"][0]["refreshToken"] == "[REDACTED]"
    assert extra["profile"]["note"] == "keep"
    assert extra["codex_auth_file"] == "credentials/codex_ch1.json"


def test_upsert_channel_restores_redacted_extra_values(isolated_config):
    """GET 脱敏后的 extra（含 [REDACTED] 占位符）被原样 PUT 回来时，必须从旧值
    恢复真实 Cookie/Token，不能把占位符当真实凭据写入。这与 import_config 的
    _restore_redacted_values 行为一致，也与 api_key 的打码回传保护对称——之前
    upsert 只保护了 api_key/ak/sk 三个顶层密钥，漏了 extra 里的嵌套凭据。"""
    created = config_store.upsert_channel(
        {
            "type": "deepseek",
            "api_key": "sk-real1234567890",
            "extra": {
                "profile": {"cookie": "auth=TOP-SECRET", "note": "keep"},
                "items": [{"access_token": "AT-SECRET"}],
            },
        },
        provided_fields={"type", "api_key", "extra"},
    )

    # 模拟"GET 渠道 → 改某字段 → 把含 [REDACTED] 的 extra 原样 PUT 回来"的客户端
    updated = config_store.upsert_channel(
        {
            "id": created.id,
            "type": "deepseek",
            "name": "改名",
            "extra": {
                "profile": {"cookie": "[REDACTED]", "note": "keep"},
                "items": [{"access_token": "[REDACTED]"}],
            },
        },
        provided_fields={"id", "type", "name", "extra"},
    )
    # 真实 Cookie/Token 必须被保留，不能被字面量 [REDACTED] 覆盖
    assert updated.extra["profile"]["cookie"] == "auth=TOP-SECRET"
    assert updated.extra["items"][0]["access_token"] == "AT-SECRET"
    assert updated.extra["profile"]["note"] == "keep"

    # 落盘的也必须是真实值
    raw = json.loads(config_store.CONFIG_PATH.read_text(encoding="utf-8"))
    stored = next(c for c in raw["channels"] if c["id"] == created.id)
    assert stored["extra"]["profile"]["cookie"] == "auth=TOP-SECRET"
    assert stored["extra"]["items"][0]["access_token"] == "AT-SECRET"


# ── 配置导入/导出 ──────────────────────────────────────────────


def test_export_includes_secrets_when_requested(isolated_config):
    config_store.upsert_channel(
        {"type": "deepseek", "name": "DS", "api_key": "sk-secret1234567890"},
        provided_fields={"type", "name", "api_key"},
    )
    data = config_store.export_config(include_secrets=True)
    assert data["version"] == 2
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


def test_import_does_not_mutate_caller_payload(isolated_config):
    """导入时补 id/规整 URL 只能作用于内部副本，不能偷偷修改调用方对象。"""
    payload = {
        "version": 1,
        "channels": [{"type": "kimi_api", "api_key": "sk-new", "base_url": "https://x.com/"}],
    }
    before = json.loads(json.dumps(payload))
    config_store.import_config(payload, mode="merge")
    assert payload == before


def test_import_replace_clears_all(isolated_config):
    config_store.upsert_channel({"type": "deepseek", "api_key": "sk-old1"})
    payload = {"version": 1, "channels": [{"type": "kimi_api", "api_key": "sk-new1", "base_url": "https://x.com"}]}
    result = config_store.import_config(payload, mode="replace")
    assert result["count"] == 1
    channels = config_store.list_channels()
    assert len(channels) == 1
    assert channels[0].type == "kimi_api"


def test_import_replace_cleans_orphan_codex_credentials_but_keeps_referenced(isolated_config):
    """替换配置后，旧渠道的 refresh token 文件不能继续滞留在 credentials/。"""
    cred_dir = isolated_config.parent / "credentials"
    cred_dir.mkdir()
    keep = cred_dir / "codex_keep.json"
    orphan = cred_dir / "codex_orphan.json"
    nested_orphan = cred_dir / "nested" / "codex_nested.json"
    nested_orphan.parent.mkdir()
    keep.write_text("keep", encoding="utf-8")
    orphan.write_text("orphan", encoding="utf-8")
    nested_orphan.write_text("nested", encoding="utf-8")

    config_store.import_config(
        {
            "channels": [
                {
                    "id": "keep",
                    "type": "codex_subscription",
                    "extra": {"codex_auth_file": "credentials/codex_keep.json"},
                }
            ]
        },
        mode="replace",
    )

    assert keep.read_text(encoding="utf-8") == "keep"
    assert not orphan.exists()
    assert not nested_orphan.exists()


def test_import_merge_removes_old_codex_file_when_channel_reassociated(isolated_config):
    """merge 覆盖同一渠道的凭据关联时，旧文件应被回收。"""
    cred_dir = isolated_config.parent / "credentials"
    cred_dir.mkdir()
    old_path = cred_dir / "codex_old.json"
    new_path = cred_dir / "codex_new.json"
    old_path.write_text("old", encoding="utf-8")
    config_store.import_config(
        {
            "channels": [
                {
                    "id": "codex_channel",
                    "type": "codex_subscription",
                    "extra": {"codex_auth_file": "credentials/codex_old.json"},
                }
            ]
        },
        mode="replace",
    )
    new_path.write_text("new", encoding="utf-8")
    config_store.import_config(
        {
            "channels": [
                {
                    "id": "codex_channel",
                    "type": "codex_subscription",
                    "extra": {"codex_auth_file": "credentials/codex_new.json"},
                }
            ]
        },
        mode="merge",
    )
    assert not old_path.exists()
    assert new_path.exists()


def test_import_rejects_unknown_type(isolated_config):
    payload = {"version": 1, "channels": [{"type": "nonexistent_provider", "api_key": "x"}]}
    with pytest.raises(config_store.ImportConfigError):
        config_store.import_config(payload)


def test_import_rejects_non_list_channels(isolated_config):
    with pytest.raises(config_store.ImportConfigError):
        config_store.import_config({"channels": "not a list"})


def test_import_rejects_invalid_mode(isolated_config):
    with pytest.raises(config_store.ImportConfigError):
        config_store.import_config({"channels": []}, mode="invalid")


def test_import_rejects_unknown_monitor_field(isolated_config):
    with pytest.raises(config_store.ImportConfigError):
        config_store.import_config(
            {"channels": [], "settings": {"monitor": {"unexpected": True}}}
        )


def test_import_rejects_malformed_base_url(isolated_config):
    with pytest.raises(config_store.ImportConfigError):
        config_store.import_config(
            {"channels": [{"type": "kimi_api", "api_key": "x", "base_url": "http://"}]}
        )


def test_import_rejects_non_boolean_enabled_and_non_object_extra(isolated_config):
    with pytest.raises(config_store.ImportConfigError):
        config_store.import_config({"channels": [{"type": "deepseek", "enabled": "false"}]})
    with pytest.raises(config_store.ImportConfigError):
        config_store.import_config({"channels": [{"type": "deepseek", "extra": []}]})


def test_import_merge_masked_secret_preserves_existing_secret(isolated_config):
    config_store.upsert_channel(
        {"id": "ch_1", "type": "deepseek", "api_key": "sk-real-secret-1234"}
    )
    config_store.import_config(
        {
            "channels": [
                {"id": "ch_1", "type": "deepseek", "api_key": "sk-r********1234"}
            ]
        }
    )
    assert config_store.get_channel("ch_1").api_key == "sk-real-secret-1234"


def test_import_merge_nested_redacted_values_preserve_existing_secrets(isolated_config):
    config_store.upsert_channel(
        {
            "id": "ch_nested",
            "type": "opencode_subscription",
            "api_key": "cookie-top-secret",
            "extra": {
                "profile": {
                    "cookie": "cookie-nested-secret",
                    "note": "old note",
                },
                "items": [
                    {"access_token": "access-old", "label": "first"},
                    {"refreshToken": "refresh-old", "label": "second"},
                ],
            },
        }
    )
    config_store.import_config(
        {
            "channels": [
                {
                    "id": "ch_nested",
                    "type": "opencode_subscription",
                    "extra": {
                        "profile": {"cookie": "[REDACTED]", "note": "new note"},
                        "items": [
                            {"access_token": "[REDACTED]", "label": "first updated"},
                            {"refreshToken": "[REDACTED]", "label": "second updated"},
                        ],
                    },
                }
            ]
        },
        mode="merge",
    )
    channel = config_store.get_channel("ch_nested")
    assert channel is not None
    assert channel.extra == {
        "profile": {"cookie": "cookie-nested-secret", "note": "new note"},
        "items": [
            {"access_token": "access-old", "label": "first updated"},
            {"refreshToken": "refresh-old", "label": "second updated"},
        ],
    }


def test_import_replace_drops_unrecoverable_nested_redacted_values(isolated_config):
    config_store.import_config(
        {
            "channels": [
                {
                    "id": "ch_redacted",
                    "type": "deepseek",
                    "api_key": "[REDACTED]",
                    "extra": {
                        "cookie": "[REDACTED]",
                        "nested": {"token": "[REDACTED]", "label": "kept"},
                        "items": [{"accessToken": "[REDACTED]", "label": "kept"}],
                    },
                }
            ]
        },
        mode="replace",
    )
    channel = config_store.get_channel("ch_redacted")
    assert channel is not None
    assert channel.api_key is None
    assert channel.extra == {
        "nested": {"label": "kept"},
        "items": [{"label": "kept"}],
    }


# ── 持久化后台监控设置 ─────────────────────────────────────────


def test_settings_defaults_are_safe_and_monitoring_is_off(isolated_config):
    settings = config_store.get_settings()["monitor"]
    assert settings["enabled"] is False
    assert settings["interval_seconds"] == 300
    assert settings["thresholds"] == {}
    assert not isolated_config.exists()  # 单纯读取默认值不制造配置文件


def test_update_settings_merges_and_persists(isolated_config):
    updated = config_store.update_settings(
        {"monitor": {"enabled": True, "interval_seconds": 900, "thresholds": {"ch_a": 17.5}}}
    )
    assert updated["monitor"]["enabled"] is True
    assert updated["monitor"]["interval_seconds"] == 900
    assert updated["monitor"]["cooldown_seconds"] == 3600
    assert updated["monitor"]["thresholds"] == {"ch_a": 17.5}
    assert config_store.get_settings() == updated


@pytest.mark.parametrize(
    "patch",
    [
        {"interval_seconds": 10},
        {"cooldown_seconds": 0},
        {"retention_days": 0},
        {"thresholds": {"ch_a": 101}},
        {"webhook_url": "javascript:alert(1)"},
    ],
)
def test_update_settings_rejects_unsafe_values(isolated_config, patch):
    with pytest.raises(config_store.SettingsValidationError):
        config_store.update_settings({"monitor": patch})


@pytest.mark.parametrize(
    "patch",
    [
        {"interval_seconds": 60.5},
        {"interval_seconds": "60.5"},
        {"thresholds": {"ch_a": float("nan")}},
        {"thresholds": {"ch_a": float("inf")}},
        {"webhook_url": "http://"},
        {"webhook_url": "https://"},
        {"webhook_url": "https://example.test/bad path"},
        {"unexpected": True},
    ],
)
def test_update_settings_rejects_malformed_values(isolated_config, patch):
    with pytest.raises(config_store.SettingsValidationError):
        config_store.update_settings({"monitor": patch})


def test_safe_export_strips_webhook_secret_but_full_export_keeps_it(isolated_config):
    url = "https://hooks.example.test/path/secret-token"
    config_store.update_settings({"monitor": {"webhook_url": url}})
    assert config_store.export_config(include_secrets=False)["settings"]["monitor"]["webhook_url"] == ""
    assert config_store.export_config(include_secrets=True)["settings"]["monitor"]["webhook_url"] == url


def test_import_merge_settings_preserves_unmentioned_fields(isolated_config):
    config_store.update_settings(
        {"monitor": {"enabled": True, "interval_seconds": 600, "webhook_url": "https://example.test/hook"}}
    )
    result = config_store.import_config(
        {"version": 2, "channels": [], "settings": {"monitor": {"thresholds": {"ch_x": 20}}}},
        mode="merge",
    )
    assert result["settings_imported"] is True
    monitor = config_store.get_settings()["monitor"]
    assert monitor["enabled"] is True
    assert monitor["interval_seconds"] == 600
    assert monitor["webhook_url"] == "https://example.test/hook"
    assert monitor["thresholds"] == {"ch_x": 20.0}


def test_import_merge_redacted_settings_does_not_clear_existing_webhook(isolated_config):
    config_store.update_settings({"monitor": {"webhook_url": "https://example.test/real-secret"}})
    config_store.import_config(
        {"version": 2, "channels": [], "settings": {"monitor": {"webhook_url": "", "interval_seconds": 900}}},
        mode="merge",
    )
    monitor = config_store.get_settings()["monitor"]
    assert monitor["webhook_url"] == "https://example.test/real-secret"
    assert monitor["interval_seconds"] == 900


def test_import_replace_old_backup_resets_monitor_to_safe_default(isolated_config):
    config_store.update_settings({"monitor": {"enabled": True, "webhook_url": "https://example.test/hook"}})
    config_store.import_config({"version": 1, "channels": []}, mode="replace")
    monitor = config_store.get_settings()["monitor"]
    assert monitor["enabled"] is False
    assert monitor["webhook_url"] == ""


# ── resolve_codex_auth_file：路径穿越防护 ────────────────────────
#
# 回归：extra.codex_auth_file 是用户可通过 POST /api/channels 自由设置的开放字段。
# 不校验的话，"../../etc/passwd" 或绝对路径会让 query_codex 读 config 目录外任意
# 文件，删除渠道时还会 unlink 删除任意文件。resolve_codex_auth_file 必须把值限制
# 在 credentials/ 子目录内、且文件名匹配 codex_*.json。


def test_resolve_codex_auth_file_normal_path(isolated_config):
    path = config_store.resolve_codex_auth_file("credentials/codex_ch1.json")
    assert path is not None
    assert path.name == "codex_ch1.json"
    assert path.parent.name == "credentials"


def test_resolve_codex_auth_file_rejects_traversal(isolated_config):
    # 相对路径穿越：解析后落在 credentials/ 之外
    assert config_store.resolve_codex_auth_file("../../etc/passwd") is None
    assert config_store.resolve_codex_auth_file("credentials/../../../etc/passwd") is None


def test_resolve_codex_auth_file_rejects_absolute_path(isolated_config):
    # 绝对路径：Path / 操作符会丢弃左边直接用右边，必须被拒
    assert config_store.resolve_codex_auth_file("/etc/passwd") is None
    assert config_store.resolve_codex_auth_file("/Users/x/credentials/codex_a.json") is None


def test_resolve_codex_auth_file_rejects_wrong_filename_pattern(isolated_config):
    # 文件名必须匹配 codex_*.json，挡住 credentials/ 下放任意名字的文件
    assert config_store.resolve_codex_auth_file("credentials/secret.json") is None
    assert config_store.resolve_codex_auth_file("credentials/codex_a.txt") is None


def test_resolve_codex_auth_file_rejects_empty(isolated_config):
    assert config_store.resolve_codex_auth_file("") is None
    assert config_store.resolve_codex_auth_file(None) is None


def test_codex_credentials_symlink_outside_config_is_rejected(isolated_config):
    """credentials/ 不能通过目录符号链接把读写边界带到配置目录之外。"""
    outside = isolated_config.parent.parent / f"{isolated_config.parent.name}-outside_credentials"
    outside.mkdir()
    try:
        (isolated_config.parent / "credentials").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前平台不允许创建目录符号链接")
    assert config_store.resolve_codex_auth_file("credentials/codex_x.json") is None
    with pytest.raises(ValueError, match="credentials"):
        config_store.codex_credentials_path("ch_x")
