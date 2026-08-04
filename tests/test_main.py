"""app/main.py 的端点级单测（用 FastAPI TestClient，同步 in-process，不发真实
网络请求）。全部通过 isolated_config fixture 隔离 CONFIG_PATH，绝不碰项目根目录
的真实 config.json。只覆盖不需要真实上游凭据的场景：校验失败、disabled 直通、
ids 过滤、404、config 损坏时的错误暴露。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import config as config_store
from app import main as app_main


@pytest.fixture
def client(isolated_config):
    # main.py 里的缓存字典是模块级全局，测试之间可能残留——每个用到 client 的
    # 测试开始前清空，避免相互干扰。
    app_main._cache.clear()
    app_main._inflight.clear()
    return TestClient(app_main.app)


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


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
    assert (path.stat().st_mode & 0o777) == 0o600
    assert "eyJtest-token-123" in path.read_text(encoding="utf-8")

    # 渠道 extra 已关联相对路径
    channel = config_store.get_channel(ch["id"])
    assert channel.extra["codex_auth_file"] == f"credentials/codex_{ch['id']}.json"


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
