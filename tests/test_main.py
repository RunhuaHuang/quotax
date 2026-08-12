"""app/main.py 的端点级单测（用 FastAPI TestClient，同步 in-process，不发真实
网络请求）。全部通过 isolated_config fixture 隔离 CONFIG_PATH，绝不碰项目根目录
的真实 config.json。只覆盖不需要真实上游凭据的场景：校验失败、disabled 直通、
ids 过滤、404、config 损坏时的错误暴露。
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import config as config_store
from app import main as app_main
from app.models import ok, window


@pytest.fixture
def client(isolated_config, monkeypatch):
    # main.py 里的缓存字典是模块级全局，测试之间可能残留——每个用到 client 的
    # 测试开始前清空，避免相互干扰。
    app_main._cache.clear()
    app_main._inflight.clear()
    app_main._OAUTH_RESULTS.clear()
    app_main._OAUTH_RESULT_CREATED.clear()
    # config_store.HISTORY_DIR 是模块级全局，在 app/config.py 导入时基于
    # CONFIG_PATH 计算一次；isolated_config 只替换了 CONFIG_PATH，不会连带更新
    # 它。如果某个测试触发一次真正成功（status=="ok"）的查询，_query_and_cache
    # 会用 fire-and-forget 后台任务把结果写进 HISTORY_DIR——不隔离的话会写进
    # 项目根目录真实的 history/ 目录，污染用户数据。这里和 test_history.py 的
    # isolated_history fixture 做同样的隔离。
    monkeypatch.setattr(config_store, "HISTORY_DIR", isolated_config.parent / "history")
    monkeypatch.setattr(app_main.usage_store, "DB_PATH", isolated_config.parent / "usage.db")
    app_main.usage_store.reset_for_test()
    yield TestClient(app_main.app)
    app_main.usage_store.reset_for_test()


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["version"]
    assert body["config"]["ok"] is True
    assert body["database"]["ok"] is True
    assert "usage_collection" in body
    assert "monitor" in body


def test_settings_endpoint_persists_monitor_configuration(client):
    initial = client.get("/api/settings")
    assert initial.status_code == 200
    assert initial.headers["cache-control"] == "no-store"
    assert initial.json()["monitor"]["enabled"] is False

    updated = client.put(
        "/api/settings",
        json={"monitor": {"enabled": True, "interval_seconds": 600, "thresholds": {"ch_a": 15}}},
    )
    assert updated.status_code == 200
    assert updated.json()["monitor"]["enabled"] is True
    assert updated.json()["monitor"]["thresholds"] == {"ch_a": 15.0}
    assert client.get("/api/settings").json() == updated.json()


def test_settings_endpoint_rejects_too_frequent_background_polling(client):
    resp = client.put("/api/settings", json={"monitor": {"interval_seconds": 10}})
    assert resp.status_code == 400
    assert "interval_seconds" in resp.json()["detail"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_key", 123),
        ("input_per_million", -1),
        ("output_per_million", "NaN"),
        ("cache_read_per_million", "Infinity"),
        ("cache_creation_per_million", 1_000_001),
    ],
)
def test_pricing_endpoint_rejects_invalid_rates(client, field, value):
    payload = {
        "model_key": "custom-model",
        "input_per_million": 0,
        "output_per_million": 0,
        "cache_read_per_million": 0,
        "cache_creation_per_million": 0,
    }
    payload[field] = value
    resp = client.put("/api/usage/pricing", json=payload)
    assert resp.status_code == 400
    assert field in resp.json()["detail"]


@pytest.mark.asyncio
async def test_lifespan_does_not_wait_for_usage_collection(monkeypatch):
    """历史日志扫描可以在后台继续，但不能拖住 Web 服务开放。"""
    collection_started = threading.Event()
    release_collection = threading.Event()

    monkeypatch.setattr(app_main, "_auto_detect_subscription_channels", lambda: {"added": [], "skipped": []})

    def slow_collect():
        collection_started.set()
        assert release_collection.wait(timeout=2)
        return {"sources": [], "total_new": 0}

    async def no_close():
        return None

    async def no_monitor_lifecycle():
        return None

    monkeypatch.setattr(app_main.usage_collect, "collect_all", slow_collect)
    monkeypatch.setattr(app_main.usage_store, "cleanup_old_records", lambda _days: {"skipped": True})
    monkeypatch.setattr(app_main.config_store, "get_settings", lambda: {"monitor": {"retention_days": 365}})
    monkeypatch.setattr(app_main.monitor_service, "start", no_monitor_lifecycle)
    monkeypatch.setattr(app_main.monitor_service, "stop", no_monitor_lifecycle)
    monkeypatch.setattr(app_main, "aclose", no_close)

    async with app_main.lifespan(app_main.app):
        # 能进入上下文就说明 lifespan 已 yield；此时采集线程仍被我们故意阻塞。
        assert await asyncio.to_thread(collection_started.wait, 1)
        assert not release_collection.is_set()
        release_collection.set()

    # 让后台任务有机会完成并从强引用集合移除，避免影响后续测试。
    for _ in range(20):
        if not app_main._bg_tasks:
            break
        await asyncio.sleep(0.01)
    assert not app_main._bg_tasks


@pytest.mark.asyncio
async def test_shutdown_cancels_shielded_inflight_queries():
    started = asyncio.Event()

    async def never_finishes():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(never_finishes())
    app_main._inflight["ch_shutdown"] = (task, app_main._generation)
    await started.wait()
    await app_main._cancel_inflight_queries()
    assert task.cancelled()
    assert app_main._inflight == {}


def test_providers_endpoint_lists_newapi_with_user_id_field(client):
    resp = client.get("/api/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert "user_id" in body["providers"]["newapi"]["fields"]


def test_post_empty_payload_returns_400_not_500(client):
    resp = client.post("/api/channels", json={})
    assert resp.status_code == 400
    assert "detail" in resp.json()


def test_post_invalid_type_returns_400(client):
    resp = client.post("/api/channels", json={"type": "不存在的渠道", "api_key": "sk-x"})
    assert resp.status_code == 400


def test_post_missing_required_field_on_create_returns_400(client):
    resp = client.post("/api/channels", json={"type": "deepseek"})
    assert resp.status_code == 400
    assert "api_key" in resp.json()["detail"]


def test_post_and_get_channel_masks_secret(client):
    resp = client.post(
        "/api/channels",
        json={"type": "deepseek", "api_key": "sk-TESTKEY1234567890ABCD"},
    )
    assert resp.status_code == 200
    created = resp.json()
    assert created["api_key"] != "sk-TESTKEY1234567890ABCD"
    assert created["api_key"].startswith("sk-T")

    listed = client.get("/api/channels").json()
    assert listed[0]["api_key"] == created["api_key"]


def test_edit_with_minimal_payload_preserves_other_fields(client):
    """协调者追加的硬要求在 HTTP 层面的回归测试：启用/停用这种最小 payload
    不能清空 name/base_url。"""
    created = client.post(
        "/api/channels",
        json={
            "type": "kimi_api",
            "name": "我的 Kimi",
            "api_key": "sk-kimi-x",
            "base_url": "https://example.com",
        },
    ).json()

    toggled = client.post(
        "/api/channels",
        json={"id": created["id"], "type": "kimi_api", "enabled": False},
    ).json()
    assert toggled["enabled"] is False
    assert toggled["name"] == "我的 Kimi"
    assert toggled["base_url"] == "https://example.com"


def test_delete_nonexistent_channel_404(client):
    resp = client.delete("/api/channels/ch_does_not_exist")
    assert resp.status_code == 404


def test_quotas_empty_when_no_channels(client):
    resp = client.get("/api/quotas")
    assert resp.status_code == 200
    assert resp.json() == {"cached": False, "channels": []}


def test_quota_response_exposes_per_channel_freshness(client, monkeypatch):
    created = client.post(
        "/api/channels",
        json={"type": "deepseek", "name": "Freshness", "api_key": "sk-test"},
    ).json()

    async def query(channel):
        return ok(
            id=channel.id,
            type=channel.type,
            name=channel.name,
            category="balance",
            windows=[window("balance", "余额", remaining_percent=80)],
        )

    monkeypatch.setattr(app_main, "query_channel", query)
    live = client.get(f"/api/quotas?ids={created['id']}&force=true").json()["channels"][0]
    assert live["cache"]["hit"] is False
    assert live["cache"]["source"] == "live"
    assert live["cache"]["stale"] is False
    assert live["cache"]["age_ms"] >= 0

    cached_response = client.get(f"/api/quotas?ids={created['id']}").json()
    assert cached_response["cached"] is True
    cached = cached_response["channels"][0]
    assert cached["cache"]["hit"] is True
    assert cached["cache"]["source"] == "cache"


def test_quotas_disabled_channel_short_circuits_without_network(client):
    created = client.post(
        "/api/channels",
        json={
            "type": "deepseek",
            "name": "禁用测试",
            "api_key": "sk-x",
            "enabled": False,
        },
    ).json()

    resp = client.get("/api/quotas")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["channels"]) == 1
    assert body["channels"][0]["status"] == "disabled"
    assert body["channels"][0]["id"] == created["id"]


def test_quotas_ids_filter_only_returns_requested_channels(client):
    a = client.post(
        "/api/channels",
        json={"type": "deepseek", "name": "A", "api_key": "sk-a", "enabled": False},
    ).json()
    client.post(
        "/api/channels",
        json={"type": "deepseek", "name": "B", "api_key": "sk-b", "enabled": False},
    )

    resp = client.get(f"/api/quotas?ids={a['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["channels"]) == 1
    assert body["channels"][0]["id"] == a["id"]


def test_quotas_ids_filter_ignores_unknown_ids(client):
    a = client.post(
        "/api/channels",
        json={"type": "deepseek", "name": "A", "api_key": "sk-a", "enabled": False},
    ).json()
    resp = client.get(f"/api/quotas?ids={a['id']},ch_does_not_exist")
    assert resp.status_code == 200
    assert len(resp.json()["channels"]) == 1


def test_quotas_ids_strips_volcengine_plan_suffix(client):
    """回归：火山渠道在 /api/quotas 返回层被拆成 <id>_agent/<id>_coding 两张卡，
    前端"刷新此渠道"按钮发的 ids 带后缀。后端必须归一回 config id 才能匹配到
    渠道，否则单卡刷新火山子卡时返回空、卡上的数据永远更新不了。"""
    created = client.post(
        "/api/channels",
        json={"type": "volcengine", "name": "火山", "ak": "ak-x", "sk": "sk-x", "enabled": False},
    ).json()
    # 带 _agent 后缀（停用渠道直接返回 disabled，不发起网络请求，便于断言）
    resp = client.get(f"/api/quotas?ids={created['id']}_agent")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["channels"]) == 1
    # 停用渠道返回的 id 是原始 config id（无后缀）
    assert body["channels"][0]["id"] == created["id"]
    assert body["channels"][0]["status"] == "disabled"


def test_delete_volcengine_plan_suffix_removes_base_channel(client):
    """删除展示层火山子卡 ID 时，应删除底层共享的原始渠道。"""
    created = client.post(
        "/api/channels",
        json={"type": "volcengine", "name": "火山", "ak": "ak-x", "sk": "sk-x", "enabled": False},
    ).json()
    resp = client.delete(f"/api/channels/{created['id']}_coding")
    assert resp.status_code == 200
    assert client.get("/api/channels").json() == []


def test_quotas_no_ids_param_returns_everything(client):
    client.post(
        "/api/channels",
        json={"type": "deepseek", "name": "A", "api_key": "sk-a", "enabled": False},
    )
    client.post(
        "/api/channels",
        json={"type": "deepseek", "name": "B", "api_key": "sk-b", "enabled": False},
    )
    resp = client.get("/api/quotas")
    assert len(resp.json()["channels"]) == 2


def test_local_usage_endpoint_returns_two_sources(client, tmp_path, monkeypatch):
    from app import local_usage

    monkeypatch.setattr(local_usage, "CLAUDE_PROJECTS_DIR", tmp_path / "no_such_dir")
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "no_such_db.db"))
    resp = client.get("/api/local-usage?days=3")
    assert resp.status_code == 200
    body = resp.json()
    assert body["days"] == 3
    assert {s["key"] for s in body["sources"]} == {"claude_code", "opencode"}


def test_config_corrupted_returns_500_with_detail_not_bare_crash(client):
    config_store.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_store.CONFIG_PATH.write_text("{ broken", encoding="utf-8")
    resp = client.get("/api/channels")
    assert resp.status_code == 500
    assert "detail" in resp.json()


# ── 配置导入/导出端点 ─────────────────────────────────────────


def test_export_config_endpoint(client):
    client.post("/api/channels", json={"type": "deepseek", "name": "DS", "api_key": "sk-test1234567890ab"})
    # 含密钥
    full = client.get("/api/config/export?include_secrets=true").json()
    assert full["channels"][0]["api_key"] == "sk-test1234567890ab"
    # 脱敏
    safe = client.get("/api/config/export?include_secrets=false").json()
    assert "api_key" not in safe["channels"][0]


def test_import_config_endpoint_merge(client):
    client.post("/api/channels", json={"type": "deepseek", "api_key": "sk-original1234", "name": "原渠道"})
    resp = client.post(
        "/api/config/import?mode=merge",
        json={"version": 1, "channels": [{"type": "kimi_api", "api_key": "sk-new", "base_url": "https://x.com"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 2


def test_import_config_endpoint_rejects_bad_payload(client):
    resp = client.post("/api/config/import", json={"channels": "not a list"})
    assert resp.status_code == 400


# ── 历史趋势端点 ─────────────────────────────────────────────


def test_history_endpoint_empty_when_no_ok_results(client):
    """没有 ok 查询结果时，历史端点返回的每个渠道都是空列表。"""
    created = client.post("/api/channels", json={"type": "deepseek", "api_key": "sk-x", "enabled": False}).json()
    resp = client.get("/api/history?days=30")
    assert resp.status_code == 200
    body = resp.json()
    assert body["days"] == 30
    # 已配置渠道会出现，但其历史记录为空（没有成功的 ok 查询）
    assert body["channels"].get(created["id"], []) == []


def test_history_endpoint_clamps_days(client):
    resp = client.get("/api/history?days=99999")
    assert resp.json()["days"] == 365
    resp = client.get("/api/history?days=0")
    assert resp.json()["days"] == 1


def test_providers_endpoint_includes_mimo(client):
    resp = client.get("/api/providers")
    assert resp.status_code == 200
    assert "mimo" in resp.json()["providers"]


# ── 安全：export 默认脱敏 + no-store；import mode 收口 ──────────


def test_export_defaults_to_safe_no_secrets(client):
    """无参数 GET /api/config/export 必须默认脱敏——不能因为用户没加参数就吐明文密钥。"""
    client.post("/api/channels", json={"type": "deepseek", "name": "DS", "api_key": "sk-SUPERSECRET123456"})
    body = client.get("/api/config/export").json()
    assert "api_key" not in body["channels"][0]  # 默认脱敏


def test_export_has_no_store_cache_control(client):
    """含密钥导出的响应必须有 Cache-Control: no-store，防止明文密钥进浏览器缓存。"""
    client.post("/api/channels", json={"type": "deepseek", "api_key": "sk-secret1234567890"})
    resp = client.get("/api/config/export?include_secrets=true")
    assert "no-store" in resp.headers.get("cache-control", "")


def test_import_rejects_invalid_mode(client):
    """mode 不是 merge/replace 的值（如大写、拼写错误）必须被挡成 4xx，不能静默当 merge。"""
    resp = client.post(
        "/api/config/import?mode=Replace",
        json={"channels": [{"type": "deepseek", "api_key": "sk-x"}]},
    )
    assert resp.status_code in (400, 422)


# ── Codex auth.json 上传（多账号） ─────────────────────────────


def _make_codex_channel(client, **kwargs) -> dict:
    payload = {"type": "codex_subscription", "name": "Codex 账号"}
    payload.update(kwargs)
    resp = client.post("/api/channels", json=payload)
    assert resp.status_code == 200
    return resp.json()


VALID_AUTH_JSON = json.dumps(
    {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "eyJtest-token-123", "account_id": "user_abc"},
    }
)


def test_upload_codex_credentials_writes_file_and_links_channel(client, isolated_config):

    from app import config as config_store

    ch = _make_codex_channel(client)
    resp = client.post(f"/api/channels/{ch['id']}/codex-credentials", json={"content": VALID_AUTH_JSON})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["account_id"] == "user_abc"

    # 凭据文件真实写入 config 同目录 credentials/ 下，且权限 600
    path = isolated_config.parent / "credentials" / f"codex_{ch['id']}.json"
    assert path.exists()
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    assert (path.stat().st_mode & 0o777) == 0o600
    assert "eyJtest-token-123" in path.read_text(encoding="utf-8")

    # 渠道 extra 已关联相对路径
    channel = config_store.get_channel(ch["id"])
    assert channel.extra["codex_auth_file"] == f"credentials/codex_{ch['id']}.json"


def test_upload_codex_credentials_rejects_credentials_symlink_outside_config(client, isolated_config):
    outside = isolated_config.parent.parent / f"{isolated_config.parent.name}-outside-cred"
    outside.mkdir()
    try:
        (isolated_config.parent / "credentials").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前平台不允许创建目录符号链接")
    ch = _make_codex_channel(client)
    resp = client.post(f"/api/channels/{ch['id']}/codex-credentials", json={"content": VALID_AUTH_JSON})
    assert resp.status_code == 400
    assert "credentials" in resp.json()["detail"]


def test_upload_codex_credentials_rejects_invalid_json(client):
    ch = _make_codex_channel(client)
    resp = client.post(f"/api/channels/{ch['id']}/codex-credentials", json={"content": "{ 不是合法 JSON"})
    assert resp.status_code == 400
    assert "无效" in resp.json()["detail"]


def test_upload_codex_credentials_rejects_apikey_mode(client):
    ch = _make_codex_channel(client)
    resp = client.post(
        f"/api/channels/{ch['id']}/codex-credentials",
        json={"content": json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x"})},
    )
    assert resp.status_code == 400


def test_upload_codex_credentials_rejects_missing_token(client):
    ch = _make_codex_channel(client)
    resp = client.post(
        f"/api/channels/{ch['id']}/codex-credentials",
        json={"content": json.dumps({"auth_mode": "chatgpt", "tokens": {}})},
    )
    assert resp.status_code == 400


def test_upload_codex_credentials_only_for_codex_type(client):
    resp = client.post("/api/channels", json={"type": "deepseek", "api_key": "sk-x"})
    ch = resp.json()
    resp = client.post(f"/api/channels/{ch['id']}/codex-credentials", json={"content": VALID_AUTH_JSON})
    assert resp.status_code == 400
    assert "只有 Codex" in resp.json()["detail"]


def test_upload_codex_credentials_channel_missing(client):
    resp = client.post("/api/channels/ch_nope/codex-credentials", json={"content": VALID_AUTH_JSON})
    assert resp.status_code == 404


def test_delete_channel_removes_uploaded_credentials_file(client, isolated_config):
    ch = _make_codex_channel(client)
    client.post(f"/api/channels/{ch['id']}/codex-credentials", json={"content": VALID_AUTH_JSON})
    path = isolated_config.parent / "credentials" / f"codex_{ch['id']}.json"
    assert path.exists()
    client.delete(f"/api/channels/{ch['id']}")
    assert not path.exists()  # 渠道删除后凭据文件一并清理


# ── P1 任务 1：_canonical_channel_id 只在真的是火山渠道时才归一 ────
#
# 回归背景：旧实现无条件剥掉任何 channel id 结尾的 _agent/_coding 后缀，不看
# 渠道类型。真实破坏路径：导入一个 id 为 "team_agent" 的 deepseek 渠道后
# （import_config 会原样保留传入的 id），在卡片上点"停用"，前端发最小 payload
# {"id":"team_agent","type":"deepseek","enabled":false}——旧逻辑把它归一成不
# 存在的 "team"，被误判为"新建渠道"，因缺少必填的 api_key 而 400。这个渠道从此
# 在 UI 里彻底无法编辑/停用，GET /api/channels/{id}/secret 也会 404。


def test_toggle_channel_whose_id_ends_with_agent_suffix_is_not_new_upsert(client):
    """精确复现监工报告的破坏路径：非火山渠道的 id 恰好以 _agent 结尾时，
    "停用"这种最小 payload 必须被当成编辑已有渠道，而不是被误判成新建。"""
    created = client.post(
        "/api/channels",
        json={"id": "team_agent", "type": "deepseek", "name": "团队", "api_key": "sk-team-x"},
    ).json()
    assert created["id"] == "team_agent"  # id 原样保留，未被创建逻辑改写

    resp = client.post(
        "/api/channels",
        json={"id": "team_agent", "type": "deepseek", "enabled": False},
    )
    assert resp.status_code == 200, resp.json()  # 旧逻辑这里会 400 缺少 api_key
    body = resp.json()
    assert body["id"] == "team_agent"  # id 没有被误剥成 "team"
    assert body["enabled"] is False
    assert body["name"] == "团队"  # 其它字段未被清空

    # 渠道确实只有一条（没有被误建成一个新的 "team" 渠道）
    all_channels = client.get("/api/channels").json()
    assert [c["id"] for c in all_channels] == ["team_agent"]


def test_create_new_channel_with_coding_suffix_id_keeps_full_id(client):
    """POST 一个全新渠道、id 恰好起成 "my_coding"，必须原样存成 "my_coding"，
    不能被静默剥成 "my"（这个 "my" 渠道压根不存在）。"""
    created = client.post(
        "/api/channels",
        json={"id": "my_coding", "type": "deepseek", "name": "My", "api_key": "sk-my-x"},
    ).json()
    assert created["id"] == "my_coding"
    assert config_store.get_channel("my_coding") is not None
    assert config_store.get_channel("my") is None


def test_get_channel_secret_for_non_volcengine_id_with_agent_suffix(client):
    """get_channel_secret 同样不能对非火山渠道做后缀归一。"""
    client.post(
        "/api/channels",
        json={"id": "team_agent", "type": "deepseek", "name": "团队", "api_key": "sk-team-x"},
    )
    resp = client.get("/api/channels/team_agent/secret")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "team_agent"
    assert body["secret"]["api_key"] == "sk-team-x"


def test_get_channel_secret_still_strips_suffix_for_real_volcengine_channel(client):
    """真实火山渠道的子卡 id 仍然要能正确归一——这是任务 1 修复不能破坏的另一半。"""
    created = client.post(
        "/api/channels",
        json={"type": "volcengine", "name": "火山", "ak": "ak-real", "sk": "sk-real"},
    ).json()
    resp = client.get(f"/api/channels/{created['id']}_agent/secret")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == created["id"]  # 归一回不带后缀的真实 config id
    assert body["secret"]["ak"] == "ak-real"
    assert body["secret"]["sk"] == "sk-real"


def test_quotas_ids_filter_does_not_strip_suffix_from_non_volcengine_id(client):
    """/api/quotas?ids=... 对一个 id 恰好以 _agent 结尾的非火山渠道，必须按
    原样 id 匹配，不能被归一到一个不存在的 "更短" id 从而查不到任何结果。"""
    created = client.post(
        "/api/channels",
        json={"id": "team_agent", "type": "deepseek", "name": "团队", "api_key": "sk-x", "enabled": False},
    ).json()
    resp = client.get(f"/api/quotas?ids={created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["channels"]) == 1
    assert body["channels"][0]["id"] == "team_agent"
    assert body["channels"][0]["status"] == "disabled"


# ── P2 任务 2：/api/history 也要做 _agent/_coding 后缀归一 ──────────
#
# 回归背景：四个涉及 channel id 的端点（create_or_update_channel /
# get_channel_secret / quotas / history）里，history 是唯一没做后缀归一的——
# 目前前端历史下拉用的是不带后缀的配置 id 所以还没暴露，但这是个隐患，必须
# 和其它三处保持一致。


def test_history_endpoint_strips_volcengine_plan_suffix(client):
    created = client.post(
        "/api/channels",
        json={"type": "volcengine", "name": "火山", "ak": "ak-x", "sk": "sk-x"},
    ).json()
    resp = client.get(f"/api/history?ids={created['id']}_agent,{created['id']}_coding")
    assert resp.status_code == 200
    body = resp.json()
    # 两个带后缀的子卡 id 都应该归一到同一个真实 config id，返回该渠道的历史
    # （此时还没有任何 ok 查询结果，历史为空列表，但 key 必须是不带后缀的原始 id）
    assert list(body["channels"].keys()) == [created["id"]]
    assert body["channels"][created["id"]] == []


def test_history_endpoint_does_not_strip_suffix_from_non_volcengine_id(client):
    """和 quotas 保持一致：非火山渠道 id 恰好以 _agent 结尾时，history 也不能
    把它误归一成一个不存在的更短 id。"""
    created = client.post(
        "/api/channels",
        json={"id": "team_agent", "type": "deepseek", "name": "团队", "api_key": "sk-x"},
    ).json()
    resp = client.get(f"/api/history?ids={created['id']}")
    assert resp.status_code == 200
    assert list(resp.json()["channels"].keys()) == ["team_agent"]


# ── P2 任务 3：拆卡后每张卡保留各自真实的 plan_name（含 PlanType 档位）────


def test_quotas_splits_volcengine_cards_preserve_real_plan_names(client, monkeypatch):
    """回归：main.py 曾经把拆分出的 Agent/Coding 两张卡的 plan_name 硬编码成
    通用的 "Agent Plan"/"Coding Plan"，丢掉了 volcengine._merge_plans 算出来的
    真实套餐名（尤其 Agent Plan 名字里带 PlanType 档位）。拆卡后必须各自显示
    真实名称，互不串味，也不能是拼接后的完整串。"""
    from app.providers import volcengine

    async def fake_openapi(region, ak, sk, action):
        if action == "GetAFPUsage":
            return {
                "Result": {
                    "AFPFiveHour": {"Quota": 100, "Used": 25},
                    "PlanType": "small",
                }
            }
        return {"Result": {"QuotaUsage": [{"Level": "week", "Percent": 55.0}]}}

    monkeypatch.setattr(volcengine, "_openapi_call", fake_openapi)

    created = client.post(
        "/api/channels",
        json={"type": "volcengine", "name": "火山", "ak": "ak-x", "sk": "sk-x"},
    ).json()

    resp = client.get("/api/quotas")
    assert resp.status_code == 200
    channels = resp.json()["channels"]
    ids = {c["id"] for c in channels}
    assert ids == {f"{created['id']}_agent", f"{created['id']}_coding"}

    agent_card = next(c for c in channels if c["id"] == f"{created['id']}_agent")
    coding_card = next(c for c in channels if c["id"] == f"{created['id']}_coding")

    assert agent_card["plan_name"] == "火山 Agent Plan small"  # 含 PlanType 档位
    assert coding_card["plan_name"] == "火山 Coding Plan"
    assert agent_card["plan_name"] != coding_card["plan_name"]  # 不互相串味
    # 不能是 _merge_plans 拼接后的完整串
    assert " · " not in agent_card["plan_name"]
    assert " · " not in coding_card["plan_name"]


# ── P2 任务 5：单飞 task 的 shield 保护 ──────────────────────────
#
# 回归背景：_inflight 里的共享查询 task 会被多个并发请求 await。如果不用
# asyncio.shield 隔离，其中一个调用者的外层协程被取消（客户端断连、uvicorn
# 取消请求处理）时，取消会顺着 await 传播进共享的 task，把它也取消掉，连累
# 其它正在等同一个结果、客户端根本没断开的调用者。


async def test_get_channel_result_shield_protects_other_waiters_from_cancellation(isolated_config, monkeypatch):
    monkeypatch.setattr(config_store, "HISTORY_DIR", isolated_config.parent / "history")
    app_main._cache.clear()
    app_main._inflight.clear()

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_query(channel):
        started.set()
        await release.wait()
        return ok(
            id=channel.id,
            type=channel.type,
            name=channel.name,
            category="balance",
            windows=[window("balance", "余额", remaining_percent=50.0)],
        )

    monkeypatch.setattr(app_main, "query_channel", slow_query)
    channel = config_store.Channel(id="ch_shield", type="deepseek", name="X", api_key="sk-x")

    task_a = asyncio.create_task(app_main._get_channel_result(channel, False))
    await started.wait()  # 确保共享查询 task 已经真正发起、_inflight 已登记
    task_b = asyncio.create_task(app_main._get_channel_result(channel, False))
    await asyncio.sleep(0)  # 让 task_b 排到共享 task 的等待队列上

    task_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_a

    # task_a 被取消后，共享查询 task 必须还活着（还没跑完），_inflight 的登记
    # 也不该被 task_a 的 finally 提前摘掉——这正是 task.done() 判断要守住的。
    # _inflight 的值是 (task, 代际) 元组，取 task 本体来判断。
    shared_task, _shared_gen = app_main._inflight.get("ch_shield")
    assert not shared_task.done()

    release.set()  # 放行真正的查询逻辑
    result, from_cache = await task_b
    assert result["status"] == "ok"
    assert result["windows"][0]["remaining_percent"] == 50.0
    assert from_cache is False

    # 收尾：共享 task 完成后应该已经从 _inflight 里清理掉
    await asyncio.sleep(0)
    assert "ch_shield" not in app_main._inflight


# ── 配置代际：import 后在飞的旧查询结果不污染缓存 ──────────────────
#
# 回归背景：merge 导入覆盖某渠道密钥的瞬间，该渠道恰好有一次 in-flight 查询。
# 编辑渠道走 _invalidate_cache 会摘掉已完成的 task，但仍在运行的 task 被
# asyncio.shield 保护、无法摘除。这条用旧密钥发起的 task 跑完后原本会把旧密钥
# 结果写回 _cache，TTL 内展示旧额度。代际（_generation）校验让它在写缓存时
# 发现自己已"过时"，优雅丢弃结果。


async def test_import_invalidates_inflight_stale_result(isolated_config, monkeypatch):
    """导入配置后，导入前已在飞的旧查询完成时不应把结果写入缓存。"""
    monkeypatch.setattr(config_store, "HISTORY_DIR", isolated_config.parent / "history")
    app_main._cache.clear()
    app_main._inflight.clear()
    app_main._generation = 0

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_query(channel):
        started.set()
        await release.wait()
        # 用旧密钥查到的（模拟）结果
        return ok(
            id=channel.id,
            type=channel.type,
            name=channel.name,
            category="balance",
            windows=[window("balance", "余额", remaining_percent=99.0)],
        )

    monkeypatch.setattr(app_main, "query_channel", slow_query)
    # 先建一个渠道，再发起查询（此时 slow_query 会卡在 release.wait()）
    config_store.upsert_channel({"id": "ch_imp", "type": "deepseek", "name": "X", "api_key": "sk-old"})
    channel = config_store.get_channel("ch_imp")
    query_task = asyncio.create_task(app_main._get_channel_result(channel, False))
    await started.wait()  # 确保查询已经真正发起、卡在 in-flight

    # 查询在飞期间：走真实 import 端点 merge 覆盖这个渠道的密钥（端点内 bump 代际）
    await app_main.import_config(
        {"version": 1, "channels": [{"id": "ch_imp", "type": "deepseek", "api_key": "sk-new", "name": "X"}]},
        "merge",
    )
    assert app_main._generation > 0  # 代际已推进

    # 放行旧查询：它跑完写缓存时会发现代际已变，必须丢弃结果
    release.set()
    stale_result, _ = await query_task
    assert stale_result["windows"][0]["remaining_percent"] == 99.0  # 返回值本身仍是旧结果（调用者拿到的）
    # 关键断言：旧结果不应进入缓存（否则 TTL 内会展示旧密钥的额度）
    assert "ch_imp" not in app_main._cache


async def test_edit_channel_bumps_generation_and_invalidates_inflight(isolated_config, monkeypatch):
    """编辑渠道（POST /api/channels）同样 bump 代际，在飞的旧查询结果被丢弃。"""
    monkeypatch.setattr(config_store, "HISTORY_DIR", isolated_config.parent / "history")
    app_main._cache.clear()
    app_main._inflight.clear()
    app_main._generation = 0

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_query(channel):
        started.set()
        await release.wait()
        return ok(
            id=channel.id,
            type=channel.type,
            name=channel.name,
            category="balance",
            windows=[window("balance", "余额", remaining_percent=10.0)],
        )

    monkeypatch.setattr(app_main, "query_channel", slow_query)
    config_store.upsert_channel({"id": "ch_edit", "type": "deepseek", "name": "X", "api_key": "sk-old"})
    channel = config_store.get_channel("ch_edit")
    query_task = asyncio.create_task(app_main._get_channel_result(channel, False))
    await started.wait()

    # 查询在飞期间：编辑渠道（_invalidate_cache 会 bump 代际）
    await app_main._invalidate_cache("ch_edit")
    assert app_main._generation > 0

    release.set()
    await query_task
    assert "ch_edit" not in app_main._cache  # 旧结果被代际校验挡掉


async def test_fresh_query_after_generation_bump_is_cached(isolated_config, monkeypatch):
    """代际推进后发起的新查询（捕获新代际）结果正常写入缓存——代际校验不能误伤。"""
    monkeypatch.setattr(config_store, "HISTORY_DIR", isolated_config.parent / "history")
    app_main._cache.clear()
    app_main._inflight.clear()
    app_main._generation = 0

    async def fast_query(channel):
        return ok(
            id=channel.id,
            type=channel.type,
            name=channel.name,
            category="balance",
            windows=[window("balance", "余额", remaining_percent=50.0)],
        )

    monkeypatch.setattr(app_main, "query_channel", fast_query)
    config_store.upsert_channel({"id": "ch_fresh", "type": "deepseek", "name": "X", "api_key": "sk-x"})
    channel = config_store.get_channel("ch_fresh")

    # 先 bump 一次代际（模拟一次 import），再发起查询——查询捕获的是 bump 之后的代际
    await app_main._invalidate_cache("ch_fresh")
    gen_after_bump = app_main._generation
    await app_main._get_channel_result(channel, False)

    # 新代际下的查询结果应该正常进缓存
    assert "ch_fresh" in app_main._cache
    assert app_main._generation == gen_after_bump  # 期间没有再变动


async def test_import_then_query_does_not_reuse_stale_inflight_task(isolated_config, monkeypatch):
    """import 后的新请求不能复用旧代际的 in-flight task——否则会拿到旧密钥结果。

    这是代际机制的第二道防线（复用判断），与写缓存校验（第一道防线）互补：
    写缓存校验只挡住"旧结果不进缓存"，但如果新请求直接复用了旧代际 task，
    await 它拿到的返回值本身仍是旧结果（哪怕不进缓存，本次响应也是错的）。
    复用判断在 _get_channel_result 里比对 task 的发起代际，代际不一致就新建查询。
    """
    monkeypatch.setattr(config_store, "HISTORY_DIR", isolated_config.parent / "history")
    app_main._cache.clear()
    app_main._inflight.clear()
    app_main._generation = 0

    # 用 started 事件精确同步：第 1 次调用（旧代际）卡住等 release_old，第 2 次
    # （新代际）卡住等 release_new。两次都 set started 让测试能确认它们各被发起。
    started = asyncio.Event()
    release_old = asyncio.Event()
    release_new = asyncio.Event()
    call_count = {"n": 0}

    async def query(channel):
        call_count["n"] += 1
        n = call_count["n"]
        started.set()
        pct = 99.0 if n == 1 else 11.0  # 旧查询返回 99%、新查询返回 11%，便于区分
        await (release_old if n == 1 else release_new).wait()
        return ok(
            id=channel.id, type=channel.type, name=channel.name, category="balance",
            windows=[window("balance", "余额", remaining_percent=pct)],
        )

    monkeypatch.setattr(app_main, "query_channel", query)
    config_store.upsert_channel({"id": "ch_reuse", "type": "deepseek", "name": "X", "api_key": "sk-old"})
    channel = config_store.get_channel("ch_reuse")

    # 1) 旧代际发起查询，卡在 in-flight（task 仍 running）。started.wait() 确保它
    #    已经真正进入 query_channel 并登记进 _inflight，而不是停留在事件循环队列里。
    old_task = asyncio.create_task(app_main._get_channel_result(channel, False))
    await started.wait()
    assert call_count["n"] == 1
    started.clear()

    # 2) import 覆盖密钥 → 代际 bump。旧 task 仍 running、登记的代际为旧值。
    await app_main.import_config(
        {"version": 1, "channels": [{"id": "ch_reuse", "type": "deepseek", "api_key": "sk-new", "name": "X"}]},
        "merge",
    )
    assert app_main._generation > 0

    # 3) 新请求到来：复用判断应发现旧 task 代际不一致，新建查询（而非复用旧 task）。
    #    started 再次被 set 证明 query_channel 被调用了第 2 次——若复用了旧 task，
    #    call_count 仍是 1、started 不会被再次 set（测试会卡到超时失败）。
    new_task = asyncio.create_task(app_main._get_channel_result(channel, False))
    await started.wait()
    assert call_count["n"] == 2

    # 4) 放行两条查询，断言新请求拿到的是新代际的结果（11%），不是旧代际的（99%）
    release_old.set()
    release_new.set()
    old_result, _ = await old_task
    new_result, _ = await new_task
    assert old_result["windows"][0]["remaining_percent"] == 99.0  # 旧 task 返回旧结果
    assert new_result["windows"][0]["remaining_percent"] == 11.0  # 新请求拿到新结果，没有复用旧 task


# ── 任务 8：DNS rebinding 防护（Host 头白名单）──────────────────────


def test_host_whitelist_allows_default_testclient_host(client):
    """TestClient 默认发送 Host: testserver，必须放行——否则这个中间件会把
    现有的所有测试都拦成 403。"""
    resp = client.get("/api/health")
    assert resp.status_code == 200


def test_host_whitelist_rejects_evil_host(client):
    resp = client.get("/api/health", headers={"Host": "evil.com"})
    assert resp.status_code == 403
    assert "detail" in resp.json()


def test_host_whitelist_allows_127_0_0_1_with_arbitrary_port(client):
    """白名单只看主机部分，不管端口——服务可能起在任意端口。"""
    resp = client.get("/api/health", headers={"Host": "127.0.0.1:59999"})
    assert resp.status_code == 200


def test_host_whitelist_allows_localhost_with_port(client):
    resp = client.get("/api/health", headers={"Host": "localhost:8931"})
    assert resp.status_code == 200


def test_host_whitelist_allows_ipv6_loopback_with_port(client):
    resp = client.get("/api/health", headers={"Host": "[::1]:8900"})
    assert resp.status_code == 200


def test_host_whitelist_rejects_evil_host_even_with_plausible_port(client):
    """攻击者伪造一个"看起来像本机端口"的 Host 也不能通过——校验的是主机部分，
    不是"这个 Host 长得像不像本机"。"""
    resp = client.get("/api/health", headers={"Host": "evil.com:8900"})
    assert resp.status_code == 403


def test_host_whitelist_does_not_block_static_files(client):
    """中间件按 Host 判断、不看路径——合法 Host 下静态文件必须能正常访问。"""
    resp = client.get("/static/app.js")
    assert resp.status_code == 200


def test_host_whitelist_does_not_block_index_page(client):
    resp = client.get("/")
    assert resp.status_code == 200


# ── Codex OAuth 端点（start / callback / poll）─────────────────


def _make_fake_tokens():
    """构造一个 mock CodexTokens，避免真实网络 token 交换。"""
    from app import oauth

    return oauth.CodexTokens(
        access_token="at_fake",
        refresh_token="rt_fake",
        id_token="header.payload.sig",
        account_id="acc_oauth_test",
        email="oauth@test.com",
        plan_type="plus",
        expires_at_ms=9999999999999,
    )


async def _fake_exchange_ok(*a, **k):
    """async mock：exchange_code 成功路径（exchange_code 本身是 async）。"""
    return _make_fake_tokens()


def test_codex_oauth_start_returns_authorize_url(client, monkeypatch):
    """start 端点生成 authorize URL（mock 掉 1455 端口服务器，测试环境起不了）。"""
    monkeypatch.setattr(app_main, "_start_callback_server", lambda loop: None)
    resp = client.post("/api/auth/codex/start")
    assert resp.status_code == 200
    body = resp.json()
    assert body["authorize_url"].startswith("https://auth.openai.com/oauth/authorize?")
    assert "client_id=app_EMoamEEZ73f0CkXaXp7hrann" in body["authorize_url"]
    assert "code_challenge_method=S256" in body["authorize_url"]
    # redirect_uri 必须是固定的 localhost:1455
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback" in body["authorize_url"]
    assert len(body["state"]) == 32


def test_oauth_callback_server_stop_is_scheduled_off_handler_thread(monkeypatch):
    """HTTPServer.shutdown 不能在 serve_forever 所在线程内直接调用。"""
    stopped = threading.Event()
    caller_thread = threading.get_ident()
    stop_thread_ids = []

    def fake_stop():
        stop_thread_ids.append(threading.get_ident())
        stopped.set()

    monkeypatch.setattr(app_main, "_stop_callback_server", fake_stop)
    app_main._schedule_callback_server_stop()
    assert stopped.wait(timeout=1)
    assert len(stop_thread_ids) == 1
    assert stop_thread_ids[0] != caller_thread


def test_codex_oauth_callback_creates_channel(client, monkeypatch):
    """OAuth 回调核心逻辑：mock token 交换成功 → 自动创建 codex 渠道 + 写凭据文件。

    直接调用 _process_oauth_callback（1455 端口回调服务器内部就是调它），不经过
    HTTP 端点——回调走的是独立的 1455 服务器而非主服务的 FastAPI 路由。
    """
    from app import oauth

    monkeypatch.setattr(oauth, "exchange_code", _fake_exchange_ok)
    # 先登记一个合法 state（回调要用它校验 CSRF）
    state = "test_state_create"
    oauth.store_flow(state, "verifier_x")

    html = asyncio.run(app_main._process_oauth_callback("auth_code_xyz", state, None))
    assert "授权成功" in html

    # 渠道已自动创建
    channels = client.get("/api/channels").json()
    codex_channels = [c for c in channels if c["type"] == "codex_subscription"]
    assert len(codex_channels) == 1
    assert "oauth@test.com" in codex_channels[0]["name"]
    assert codex_channels[0]["extra"]["codex_auth_file"].startswith("credentials/codex_")

    # 结果已写入 _OAUTH_RESULTS（供 poll 取回）
    result = app_main._OAUTH_RESULTS.pop(state, None)
    assert result is not None
    assert result["status"] == "ok"
    assert result["email"] == "oauth@test.com"


def test_codex_oauth_callback_rejects_unknown_state(client, monkeypatch):
    """state 不匹配（CSRF 攻击 / 过期）必须拒绝，不能建渠道。"""
    from app import oauth

    monkeypatch.setattr(oauth, "exchange_code", _fake_exchange_ok)
    html = asyncio.run(app_main._process_oauth_callback("c", "nonexistent_state", None))
    assert "授权失败" in html
    assert client.get("/api/channels").json() == []
    assert "nonexistent_state" not in app_main._OAUTH_RESULTS


def test_codex_oauth_poll_rejects_oversized_state(client):
    state = "x" * 129
    resp = client.get(f"/api/auth/codex/poll?state={state}")
    assert resp.status_code == 400


def test_codex_oauth_callback_handles_openai_error(client):
    """OpenAI 回调带 error 参数（用户拒绝授权）时不应建渠道。"""
    html = asyncio.run(app_main._process_oauth_callback(None, "any", "access_denied"))
    assert "授权失败" in html
    assert client.get("/api/channels").json() == []


def test_codex_oauth_callback_token_exchange_failure(client, monkeypatch):
    """token 交换失败时回调返回失败页、不建渠道。"""
    from app import oauth

    async def fail_exchange(*a, **k):
        raise oauth.OAuthError("token exchange failed")

    monkeypatch.setattr(oauth, "exchange_code", fail_exchange)
    state = "test_state_fail"
    oauth.store_flow(state, "v")
    html = asyncio.run(app_main._process_oauth_callback("c", state, None))
    assert "授权失败" in html
    assert client.get("/api/channels").json() == []


def test_codex_oauth_callback_rolls_back_channel_when_credential_write_fails(client, monkeypatch):
    """凭据文件写入失败时不能留下没有 auth.json 的孤儿 Codex 渠道。"""
    from app import oauth

    monkeypatch.setattr(oauth, "exchange_code", _fake_exchange_ok)

    def fail_write(_path, _content):
        raise OSError("disk full")

    monkeypatch.setattr(app_main, "_write_private_text_atomic", fail_write)
    state = "test_state_write_failure"
    oauth.store_flow(state, "v")
    html = asyncio.run(app_main._process_oauth_callback("c", state, None))
    assert "授权失败" in html
    assert client.get("/api/channels").json() == []


def test_codex_oauth_poll_returns_pending_then_result(client, monkeypatch):
    """poll 端点：授权完成前返回 pending，完成后返回结果。"""
    from app import oauth

    monkeypatch.setattr(oauth, "exchange_code", _fake_exchange_ok)
    state = "test_state_poll"
    oauth.store_flow(state, "v")

    # 还没回调 → pending
    assert client.get(f"/api/auth/codex/poll?state={state}").json() == {"status": "pending"}

    # 触发回调处理（写入 _OAUTH_RESULTS）
    asyncio.run(app_main._process_oauth_callback("c", state, None))

    # poll 取到结果（取出即删）
    result = client.get(f"/api/auth/codex/poll?state={state}").json()
    assert result["status"] == "ok"
    assert "channel" in result
    assert result["email"] == "oauth@test.com"

    # 再 poll 已经空了（取出即删）
    assert client.get(f"/api/auth/codex/poll?state={state}").json() == {"status": "pending"}


def test_codex_oauth_created_channel_credentials_file_format(client, monkeypatch):
    """OAuth 建的渠道，其凭据文件必须是合法 auth.json（能被 query_codex 链路解析）。"""
    from app import oauth
    from app.config import CONFIG_PATH
    from app.credentials import CRED_OK, parse_codex_credentials

    monkeypatch.setattr(oauth, "exchange_code", _fake_exchange_ok)
    state = "test_state_cred"
    oauth.store_flow(state, "v")
    asyncio.run(app_main._process_oauth_callback("c", state, None))

    channels = client.get("/api/channels").json()
    ch = next(c for c in channels if c["type"] == "codex_subscription")
    cred_path = CONFIG_PATH.parent / ch["extra"]["codex_auth_file"]
    cred_content = cred_path.read_text(encoding="utf-8")
    parsed = parse_codex_credentials(cred_content, str(cred_path))
    assert parsed.status == CRED_OK
    assert parsed.token == "at_fake"
    assert parsed.extra["account_id"] == "acc_oauth_test"
